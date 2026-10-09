"""Web chat orchestration: validation -> quota reservation -> shared AI -> settlement.

The actual answer comes from ``services.responder`` (``AnswerService.answer``),
the same object the Telegram handlers use, so retrieval, memory, prompts, Groq
and the safety post-processing are shared. This module only adds what is
specific to the website: per-user quotas, idempotency, private storage of the
conversation, and the concurrency cap.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from utils.logger import get_logger, redact
from web import quota, store
from web.auth import WebUser

logger = get_logger(__name__)

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
DEFAULT_TITLE = "New chat"
TITLE_CHARS = 60
UNAVAILABLE_TEXT = "দুঃখিত, এখন উত্তর দেওয়া সম্ভব হচ্ছে না। একটু পরে আবার চেষ্টা করুন।"


class ChatError(Exception):
    def __init__(self, status: int, message: str, code: str = "bad_request", *,
                 retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after


def valid_conversation_id(value: str) -> str:
    if not isinstance(value, str) or not _UUID_RE.match(value):
        raise ChatError(404, "Conversation not found.", code="not_found")
    return value.lower()


def valid_request_id(value: Any) -> str:
    if not isinstance(value, str) or not _REQUEST_ID_RE.match(value):
        raise ChatError(400, "Invalid request id.", code="invalid_request_id")
    return value


def clean_message(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise ChatError(400, "Message must be text.", code="invalid_message")
    text = value.replace("\x00", "").strip()
    if not text:
        raise ChatError(400, "Message is empty.", code="empty_message")
    if len(text) > limit:
        raise ChatError(413, f"Message is too long (max {limit} characters).", code="too_long")
    return text


def clean_title(value: Any) -> str:
    if not isinstance(value, str):
        raise ChatError(400, "Title must be text.", code="invalid_title")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    if not text:
        raise ChatError(400, "Title cannot be empty.", code="invalid_title")
    return text[:TITLE_CHARS * 2]


def public_message(row: dict[str, Any]) -> dict[str, Any]:
    created = row.get("created_at")
    return {
        "id": int(row["id"]),
        "role": row["role"],
        "content": row.get("content") or "",
        "status": row.get("status") or "complete",
        "model": row.get("model") or None,
        "used_memory": bool(row.get("used_memory")),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        "request_id": row.get("request_uuid"),
    }


def public_conversation(row: dict[str, Any]) -> dict[str, Any]:
    def iso(key: str) -> str | None:
        value = row.get(key)
        return value.isoformat() if hasattr(value, "isoformat") else value

    return {
        "id": str(row["id"]),
        "title": row.get("title") or DEFAULT_TITLE,
        "message_count": int(row.get("message_count") or 0),
        "created_at": iso("created_at"),
        "updated_at": iso("updated_at"),
        "last_message_at": iso("last_message_at"),
    }


class ChatService:
    def __init__(self) -> None:
        self._slots: threading.BoundedSemaphore | None = None
        self._slot_size = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def _semaphore(self, size: int) -> threading.BoundedSemaphore:
        with self._lock:
            if self._slots is None or self._slot_size != size:
                self._slots = threading.BoundedSemaphore(max(1, size))
                self._slot_size = size
            return self._slots

    def _services(self):
        from services import get_services  # local import: avoids import cycles at startup
        return get_services()

    # ------------------------------------------------------------------ #
    def list_conversations(self, user: WebUser, limit: int = 50, offset: int = 0) -> list[dict]:
        return [public_conversation(r) for r in store.list_conversations(user.telegram_user_id, limit, offset)]

    def create_conversation(self, user: WebUser, title: str | None = None) -> dict:
        row = store.create_conversation(user.telegram_user_id, clean_title(title) if title else DEFAULT_TITLE)
        return public_conversation(row)

    def get_conversation(self, user: WebUser, conversation_id: str) -> dict:
        cid = valid_conversation_id(conversation_id)
        row = store.get_conversation(user.telegram_user_id, cid)
        if row is None:
            raise ChatError(404, "Conversation not found.", code="not_found")
        data = public_conversation(row)
        data["messages"] = [public_message(m) for m in store.list_messages(user.telegram_user_id, cid)]
        return data

    def rename(self, user: WebUser, conversation_id: str, title: Any) -> dict:
        cid = valid_conversation_id(conversation_id)
        if not store.rename_conversation(user.telegram_user_id, cid, clean_title(title)):
            raise ChatError(404, "Conversation not found.", code="not_found")
        return public_conversation(store.get_conversation(user.telegram_user_id, cid))

    def delete(self, user: WebUser, conversation_id: str) -> None:
        cid = valid_conversation_id(conversation_id)
        if not store.delete_conversation(user.telegram_user_id, cid):
            raise ChatError(404, "Conversation not found.", code="not_found")

    def quota_snapshot(self, user: WebUser) -> dict:
        services = self._services()
        db_user = store.get_user(user.telegram_user_id)
        limits = quota.effective_limits(db_user, is_admin=user.is_admin, settings=services.settings)
        return quota.snapshot(user.telegram_user_id, limits)

    # ------------------------------------------------------------------ #
    def send(self, user: WebUser, conversation_id: str, content: Any, request_id: Any) -> dict:
        cid = valid_conversation_id(conversation_id)
        rid = valid_request_id(request_id)
        services = self._services()
        text = clean_message(content, services.settings.web_max_message_chars)
        conv = store.get_conversation(user.telegram_user_id, cid)
        if conv is None:
            raise ChatError(404, "Conversation not found.", code="not_found")
        replay = self._replay(user, rid)
        if replay is not None:
            return replay
        return self._generate(user, cid, text, rid, insert_user=True, conv=conv)

    def regenerate(self, user: WebUser, conversation_id: str, request_id: Any) -> dict:
        cid = valid_conversation_id(conversation_id)
        rid = valid_request_id(request_id)
        conv = store.get_conversation(user.telegram_user_id, cid)
        if conv is None:
            raise ChatError(404, "Conversation not found.", code="not_found")
        replay = self._replay(user, rid)
        if replay is not None:
            return replay
        last = store.last_user_message(cid)
        if last is None:
            raise ChatError(400, "There is no question to answer again.", code="nothing_to_regenerate")
        return self._generate(user, cid, last["content"], rid, insert_user=False, conv=conv,
                              replace_after=int(last["id"]))

    def _replay(self, user: WebUser, request_id: str) -> dict | None:
        """A repeated request id returns the stored answer and is never charged again."""
        prior = store.find_assistant_for_request(user.telegram_user_id, request_id)
        if prior is None:
            return None
        return {"ok": prior.get("status") in ("complete", "fallback"), "duplicate": True,
                "assistant_message": public_message(prior), "quota": None}

    # ------------------------------------------------------------------ #
    def _generate(self, user: WebUser, cid: str, text: str, request_id: str, *,
                  insert_user: bool, conv: dict, replace_after: int | None = None) -> dict:
        services = self._services()
        settings = services.settings
        responder = services.responder
        uid = user.telegram_user_id

        if not settings.web_enabled:
            raise ChatError(503, "The website is currently disabled.", code="web_disabled")
        if not services.runtime_flag("web_chat_enabled", True):
            raise ChatError(503, "AI chat is temporarily disabled by the administrator.",
                            code="chat_disabled")
        if not services.runtime_flag("ai_enabled", True):
            raise ChatError(503, "AI answering is currently switched off.", code="ai_disabled")
        if responder is None or not services.db_ready():
            raise ChatError(503, "The service is starting up. Please try again shortly.",
                            code="not_ready")

        limiter = services.rate_limiter
        if limiter is not None and not limiter.allow(f"web:{uid}"):
            raise ChatError(429, "Too many messages. Please wait a moment.", code="rate_limited",
                            retry_after=max(1, int(limiter.retry_after(f"web:{uid}"))))

        db_user = store.get_user(uid)
        limits = quota.effective_limits(db_user, is_admin=user.is_admin, settings=settings)
        model = limits.model_override

        slots = self._semaphore(settings.web_max_concurrent_ai)
        if not slots.acquire(blocking=False):
            raise ChatError(503, "The assistant is busy with other requests. Please retry in a few seconds.",
                            code="busy", retry_after=3)
        reservation = None
        try:
            try:
                reservation = quota.reserve(telegram_user_id=uid, request_uuid=request_id,
                                            conversation_id=cid, limits=limits, model=model)
            except quota.DuplicateRequest:
                replay = self._replay(user, request_id)
                if replay is not None:
                    return replay
                raise ChatError(409, "This request is already being processed.", code="in_progress")
            except quota.CooldownActive as exc:
                raise ChatError(429, exc.message, code="cooldown", retry_after=exc.retry_after)
            except quota.QuotaError as exc:
                raise ChatError(429, exc.message, code=exc.code, retry_after=exc.retry_after)

            started = time.time()
            try:
                if replace_after is not None:
                    history = store.history_before(cid, replace_after, settings.web_history_messages)
                    user_row = None
                else:
                    history = store.recent_history(cid, settings.web_history_messages)
                    user_row = store.unanswered_user_message(cid, text)
                    if user_row is not None:
                        # retry of a question that never got an answer: keep one copy of it and
                        # drop the failed attempts that followed it
                        store.delete_assistant_after(cid, int(user_row["id"]))
                        store.refresh_conversation_counters(cid)
                    else:
                        user_row = store.insert_message(conversation_id=cid, telegram_user_id=uid,
                                                        role="user", content=text, request_uuid=request_id)
                result = responder.answer(
                    text,
                    chat_id=settings.group_id or 0,
                    user_id=uid,
                    username=user.username or None,
                    use_history=False,
                    # Website answers never touch the shared (Telegram) answer cache: a cache row
                    # stores the question text, and private questions must not leak to the group.
                    use_cache=False,
                    include_disclosure=True,
                    record_usage=True,
                    history=history,
                    persist_session=False,
                    model=model,
                    max_tokens=limits.max_response_tokens,
                    source="web",
                    store_cache=False,
                )
            except Exception as exc:
                quota.complete(reservation, ok=False, latency_ms=int((time.time() - started) * 1000),
                               error_type="exception")
                reservation = None
                logger.error("web chat failed trace=%s: %s", request_id[:8],
                             redact(str(exc))[:200], exc_info=True)
                raise ChatError(500, "Something went wrong while answering. Your quota was not charged.",
                                code="internal") from None

            latency = int((time.time() - started) * 1000)
            kind = getattr(result, "error_kind", "none") or "none"
            if kind == "none":
                status = "complete"
            elif kind == "extractive":
                status = "fallback"
            else:
                status = "error"
            answered_ok = status in ("complete", "fallback")
            # 'fallback' is a memory-only answer produced without the model: not charged
            charged = status == "complete"
            quota.complete(
                reservation, ok=charged, model=getattr(result, "model", None) or None,
                prompt_tokens=getattr(result, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(result, "completion_tokens", 0) or 0,
                total_tokens=getattr(result, "tokens", 0) or 0, latency_ms=latency,
                error_type=None if charged else (kind if status == "error" else "no_model_answer"),
            )
            reservation = None

            if replace_after is not None:
                store.delete_assistant_after(cid, replace_after)
            answer_text = result.text or UNAVAILABLE_TEXT
            assistant = store.insert_message(
                conversation_id=cid, telegram_user_id=uid, role="assistant", content=answer_text,
                status=status, model=getattr(result, "model", None) or None,
                used_memory=bool(getattr(result, "used_memory", False)),
                total_tokens=int(getattr(result, "tokens", 0) or 0), request_uuid=request_id)
            if insert_user and conv.get("title") in (None, "", DEFAULT_TITLE):
                store.rename_conversation(uid, cid, text[:TITLE_CHARS])
            if replace_after is not None:
                store.refresh_conversation_counters(cid)
            return {
                "ok": answered_ok,
                "duplicate": False,
                "user_message": public_message(user_row) if user_row else None,
                "assistant_message": public_message(assistant),
                "error": None if answered_ok else "The assistant could not answer this time.",
                "quota": self._safe_snapshot(user, limits),
            }
        finally:
            if reservation is not None:  # only reached on unexpected control flow
                quota.complete(reservation, ok=False, error_type="aborted")
            slots.release()

    def _safe_snapshot(self, user: WebUser, limits: quota.Limits) -> dict | None:
        try:
            return quota.snapshot(user.telegram_user_id, limits)
        except Exception:  # quota display must never break a completed answer
            return None
