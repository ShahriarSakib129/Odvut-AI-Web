"""PostgreSQL access for the website: users, sessions, replay table, conversations, audit.

Every statement is parameterised. Conversation and message queries always carry
``telegram_user_id`` so ownership is enforced in SQL (IDOR-safe), not only in code.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Mapping

import database
from utils.logger import get_logger

logger = get_logger(__name__)


def _one(sql: str, params: Mapping[str, Any] | Iterable[Any] = ()) -> dict[str, Any] | None:
    return database._fetchone(sql, params)


def _all(sql: str, params: Mapping[str, Any] | Iterable[Any] = ()) -> list[dict[str, Any]]:
    return database._fetchall(sql, params)


# --------------------------------------------------------------------------- #
# users (existing bot_users table)
# --------------------------------------------------------------------------- #
def upsert_web_user(*, telegram_user_id: int, username: str, first_name: str,
                    last_name: str, photo_url: str) -> dict[str, Any]:
    row = _one(
        """
        insert into public.bot_users (
            telegram_user_id, username, first_name, last_name, photo_url,
            first_seen, last_seen, last_activity_at
        ) values (
            %(uid)s, nullif(%(username)s, ''), nullif(%(first)s, ''), nullif(%(last)s, ''),
            nullif(%(photo)s, ''), now(), now(), now()
        )
        on conflict (telegram_user_id) do update set
            username         = coalesce(nullif(excluded.username, ''), public.bot_users.username),
            first_name       = coalesce(nullif(excluded.first_name, ''), public.bot_users.first_name),
            last_name        = coalesce(nullif(excluded.last_name, ''), public.bot_users.last_name),
            photo_url        = coalesce(nullif(excluded.photo_url, ''), public.bot_users.photo_url),
            last_seen        = now(),
            last_activity_at = now(),
            updated_at       = now()
        returning *
        """,
        {"uid": int(telegram_user_id), "username": username or "", "first": first_name or "",
         "last": last_name or "", "photo": photo_url or ""},
    )
    if row is None:  # pragma: no cover - RETURNING always yields a row on upsert
        raise database.DatabaseUnavailable("user upsert returned no row")
    return row


def get_user(telegram_user_id: int) -> dict[str, Any] | None:
    return _one("select * from public.bot_users where telegram_user_id = %(uid)s",
                {"uid": int(telegram_user_id)})


def touch_activity(telegram_user_id: int) -> None:
    database._execute(
        "update public.bot_users set last_activity_at = now(), last_seen = now() "
        "where telegram_user_id = %(uid)s",
        {"uid": int(telegram_user_id)},
    )


# --------------------------------------------------------------------------- #
# login replay protection
# --------------------------------------------------------------------------- #
def consume_login_payload(payload_hash: str, telegram_user_id: int, auth_date: int,
                          expires_at: datetime) -> bool:
    """Return True the first time a payload is seen, False on any replay."""
    row = _one(
        """
        insert into public.web_login_replays (payload_hash, telegram_user_id, auth_date, expires_at)
        values (%(hash)s, %(uid)s, %(auth_date)s, %(expires)s)
        on conflict (payload_hash) do nothing
        returning payload_hash
        """,
        {"hash": payload_hash, "uid": int(telegram_user_id), "auth_date": int(auth_date),
         "expires": expires_at},
    )
    return row is not None


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def create_session(*, session_hash: str, csrf_hash: str, telegram_user_id: int, role: str,
                   ip_hash: str | None, user_agent_hash: str | None,
                   expires_at: datetime) -> int:
    row = _one(
        """
        insert into public.web_sessions (
            session_hash, csrf_hash, telegram_user_id, role, ip_hash, user_agent_hash,
            expires_at, membership_checked_at
        ) values (%(sh)s, %(ch)s, %(uid)s, %(role)s, %(ip)s, %(ua)s, %(exp)s, now())
        returning id
        """,
        {"sh": session_hash, "ch": csrf_hash, "uid": int(telegram_user_id), "role": role,
         "ip": ip_hash, "ua": user_agent_hash, "exp": expires_at},
    )
    return int(row["id"])


def load_session(session_hash: str) -> dict[str, Any] | None:
    return _one(
        """
        select s.id, s.session_hash, s.csrf_hash, s.telegram_user_id, s.role,
               s.expires_at, s.last_seen_at, s.membership_checked_at,
               b.access_status, b.username, b.first_name, b.last_name, b.photo_url
          from public.web_sessions s
          left join public.bot_users b on b.telegram_user_id = s.telegram_user_id
         where s.session_hash = %(sh)s
           and s.revoked_at is null
           and s.expires_at > now()
        """,
        {"sh": session_hash},
    )


def touch_session(session_id: int, *, membership_checked: bool = False) -> None:
    database._execute(
        """
        update public.web_sessions
           set last_seen_at = now(),
               membership_checked_at = case when %(checked)s then now() else membership_checked_at end
         where id = %(id)s
        """,
        {"id": int(session_id), "checked": bool(membership_checked)},
    )


def revoke_session(session_hash: str, reason: str) -> int:
    return database._execute(
        "update public.web_sessions set revoked_at = now(), revoked_reason = %(reason)s "
        "where session_hash = %(sh)s and revoked_at is null",
        {"sh": session_hash, "reason": reason[:64]},
    )


def revoke_user_sessions(telegram_user_id: int, reason: str) -> int:
    return database._execute(
        "update public.web_sessions set revoked_at = now(), revoked_reason = %(reason)s "
        "where telegram_user_id = %(uid)s and revoked_at is null",
        {"uid": int(telegram_user_id), "reason": reason[:64]},
    )


def count_active_sessions(telegram_user_id: int | None = None) -> int:
    row = _one(
        "select count(*) as c from public.web_sessions where revoked_at is null "
        "and expires_at > now() and (%(uid)s::bigint is null or telegram_user_id = %(uid)s)",
        {"uid": int(telegram_user_id) if telegram_user_id else None},
    )
    return int((row or {}).get("c") or 0)


def purge_expired_auth_rows() -> dict[str, int]:
    sessions = database._execute(
        "delete from public.web_sessions where expires_at < now() - interval '1 day' "
        "or (revoked_at is not null and revoked_at < now() - interval '7 days')")
    replays = database._execute(
        "delete from public.web_login_replays where expires_at < now()")
    return {"sessions": sessions, "login_replays": replays}


# --------------------------------------------------------------------------- #
# conversations & messages (private; never copied into memory tables)
# --------------------------------------------------------------------------- #
def create_conversation(telegram_user_id: int, title: str = "New chat") -> dict[str, Any]:
    row = _one(
        "insert into public.web_conversations (telegram_user_id, title) "
        "values (%(uid)s, %(title)s) returning id, title, message_count, created_at, updated_at, "
        "last_message_at",
        {"uid": int(telegram_user_id), "title": (title or "New chat")[:120]},
    )
    return dict(row or {})


def list_conversations(telegram_user_id: int, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    return _all(
        """
        select id, title, message_count, created_at, updated_at, last_message_at
          from public.web_conversations
         where telegram_user_id = %(uid)s
         order by coalesce(last_message_at, created_at) desc, id desc
         limit %(limit)s offset %(offset)s
        """,
        {"uid": int(telegram_user_id), "limit": int(limit), "offset": int(offset)},
    )


def get_conversation(telegram_user_id: int, conversation_id: str) -> dict[str, Any] | None:
    return _one(
        "select id, title, message_count, created_at, updated_at, last_message_at "
        "from public.web_conversations where id = %(cid)s::uuid and telegram_user_id = %(uid)s",
        {"cid": str(conversation_id), "uid": int(telegram_user_id)},
    )


def rename_conversation(telegram_user_id: int, conversation_id: str, title: str) -> bool:
    return database._execute(
        "update public.web_conversations set title = %(title)s, updated_at = now() "
        "where id = %(cid)s::uuid and telegram_user_id = %(uid)s",
        {"title": title[:120], "cid": str(conversation_id), "uid": int(telegram_user_id)},
    ) > 0


def delete_conversation(telegram_user_id: int, conversation_id: str) -> bool:
    # ON DELETE CASCADE removes the messages; no copy is kept anywhere
    return database._execute(
        "delete from public.web_conversations where id = %(cid)s::uuid and telegram_user_id = %(uid)s",
        {"cid": str(conversation_id), "uid": int(telegram_user_id)},
    ) > 0


def list_messages(telegram_user_id: int, conversation_id: str, limit: int = 300) -> list[dict[str, Any]]:
    return _all(
        """
        select m.id, m.role, m.content, m.status, m.model, m.used_memory, m.created_at, m.request_uuid
          from public.web_messages m
          join public.web_conversations c on c.id = m.conversation_id
         where m.conversation_id = %(cid)s::uuid and c.telegram_user_id = %(uid)s
         order by m.id asc
         limit %(limit)s
        """,
        {"cid": str(conversation_id), "uid": int(telegram_user_id), "limit": int(limit)},
    )


def recent_history(conversation_id: str, limit: int) -> list[dict[str, str]]:
    """Last completed turns (user + assistant) for the model. Errors are excluded."""
    if limit <= 0:
        return []
    rows = _all(
        """
        select role, content from (
            select id, role, content from public.web_messages
             where conversation_id = %(cid)s::uuid and status in ('complete', 'fallback')
             order by id desc limit %(limit)s
        ) recent order by id asc
        """,
        {"cid": str(conversation_id), "limit": int(limit)},
    )
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def insert_message(*, conversation_id: str, telegram_user_id: int, role: str, content: str,
                   status: str = "complete", model: str | None = None, used_memory: bool = False,
                   total_tokens: int = 0, request_uuid: str | None = None) -> dict[str, Any]:
    row = _one(
        """
        insert into public.web_messages (
            conversation_id, telegram_user_id, role, content, status, model,
            used_memory, total_tokens, request_uuid
        ) values (%(cid)s::uuid, %(uid)s, %(role)s, %(content)s, %(status)s, %(model)s,
                  %(used)s, %(tokens)s, %(req)s)
        returning id, role, content, status, model, used_memory, created_at, request_uuid
        """,
        {"cid": str(conversation_id), "uid": int(telegram_user_id), "role": role,
         "content": content[:20000], "status": status, "model": model,
         "used": bool(used_memory), "tokens": int(total_tokens or 0), "req": request_uuid},
    )
    database._execute(
        "update public.web_conversations set message_count = message_count + 1, "
        "last_message_at = now(), updated_at = now() where id = %(cid)s::uuid",
        {"cid": str(conversation_id)},
    )
    return dict(row or {})


def find_assistant_for_request(telegram_user_id: int, request_uuid: str) -> dict[str, Any] | None:
    return _one(
        "select m.id, m.role, m.content, m.status, m.model, m.used_memory, m.created_at, "
        "m.request_uuid, m.conversation_id from public.web_messages m "
        "where m.request_uuid = %(req)s and m.telegram_user_id = %(uid)s and m.role = 'assistant' "
        "order by m.id desc limit 1",
        {"req": request_uuid, "uid": int(telegram_user_id)},
    )


def last_user_message(conversation_id: str) -> dict[str, Any] | None:
    return _one(
        "select id, content from public.web_messages where conversation_id = %(cid)s::uuid "
        "and role = 'user' order by id desc limit 1",
        {"cid": str(conversation_id)},
    )


def unanswered_user_message(conversation_id: str, content: str) -> dict[str, Any] | None:
    """The newest user message, if it has this exact text and no complete answer follows it.

    A retry of a failed question reuses this row instead of storing the same question twice.
    """
    return _one(
        "select m.id, m.role, m.content, m.created_at, m.request_uuid from public.web_messages m "
        "where m.conversation_id = %(cid)s::uuid and m.role = 'user' and m.content = %(content)s "
        "and not exists (select 1 from public.web_messages n where n.conversation_id = m.conversation_id "
        "and n.role = 'user' and n.id > m.id) "
        "and not exists (select 1 from public.web_messages a where a.conversation_id = m.conversation_id "
        "and a.role = 'assistant' and a.id > m.id and a.status in ('complete', 'fallback')) "
        "order by m.id desc limit 1",
        {"cid": str(conversation_id), "content": content},
    )


def delete_assistant_after(conversation_id: str, after_message_id: int) -> int:
    """Remove the assistant reply(ies) that follow a user message (used by regenerate)."""
    return database._execute(
        "delete from public.web_messages where conversation_id = %(cid)s::uuid "
        "and id > %(after)s and role = 'assistant'",
        {"cid": str(conversation_id), "after": int(after_message_id)},
    )


def refresh_conversation_counters(conversation_id: str) -> None:
    database._execute(
        "update public.web_conversations c set message_count = (select count(*) from public.web_messages m "
        "where m.conversation_id = c.id), updated_at = now() where c.id = %(cid)s::uuid",
        {"cid": str(conversation_id)},
    )


def purge_old_conversations(retention_days: int) -> int:
    return database._execute(
        "delete from public.web_conversations where coalesce(last_message_at, created_at) "
        "< now() - make_interval(days => %(days)s)",
        {"days": int(retention_days)},
    )


# --------------------------------------------------------------------------- #
# audit trail
# --------------------------------------------------------------------------- #
def write_audit(*, actor_telegram_id: int, action: str, target_type: str | None = None,
                target_id: str | None = None, summary: str = "",
                details: Mapping[str, Any] | None = None) -> None:
    """Append-only record of an administrative change. Never raises."""
    try:
        database._execute(
            "insert into public.admin_audit_log (actor_telegram_id, action, target_type, target_id, "
            "summary, details) values (%(actor)s, %(action)s, %(ttype)s, %(tid)s, %(summary)s, %(details)s)",
            {"actor": int(actor_telegram_id), "action": action[:64], "ttype": (target_type or None),
             "tid": (str(target_id)[:128] if target_id is not None else None),
             "summary": summary[:500],
             "details": json.dumps(_scrub(details or {}), ensure_ascii=False, default=str)},
        )
    except Exception as exc:  # audit failures must not hide the (already committed) change
        logger.error("audit write failed for %s: %s", action, str(exc)[:160])


_SECRET_KEYS = ("token", "secret", "key", "password", "dsn", "url")


def _scrub(value: Any) -> Any:
    """Never store anything that looks like a credential in the audit trail."""
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _SECRET_KEYS):
                out[key] = "[redacted]"
            else:
                out[key] = _scrub(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value][:50]
    if isinstance(value, str):
        return value[:300]
    return value


def history_before(conversation_id: str, message_id: int, limit: int) -> list[dict[str, str]]:
    """Completed turns that precede ``message_id`` (used when regenerating an answer)."""
    if limit <= 0:
        return []
    rows = _all(
        """
        select role, content from (
            select id, role, content from public.web_messages
             where conversation_id = %(cid)s::uuid and id < %(mid)s
               and status in ('complete', 'fallback')
             order by id desc limit %(limit)s
        ) recent order by id asc
        """,
        {"cid": str(conversation_id), "mid": int(message_id), "limit": int(limit)},
    )
    return [{"role": r["role"], "content": r["content"]} for r in rows]
