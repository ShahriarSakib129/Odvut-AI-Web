"""Minimal synchronous Telegram Bot API client for the web layer.

Used for the authorisation decisions that must be made on the server:
``getChatMember`` (group membership) and ``getMe`` (bot username for the
login widget). Every call has a timeout, rate-limit responses are retried a
bounded number of times, and any failure raises :class:`TelegramUnavailable`
so callers can fail closed. Tokens and full URLs never appear in messages.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from utils.logger import get_logger, redact

logger = get_logger(__name__)

MEMBER_STATUSES = {"creator", "administrator", "member"}
DEFAULT_TIMEOUT = 8.0
MAX_RETRY_AFTER = 3.0


class TelegramUnavailable(RuntimeError):
    """The Bot API could not answer reliably. Callers must fail closed."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class TelegramClient:
    def __init__(self, token: str, base_url: str = "https://api.telegram.org",
                 session: requests.Session | None = None, timeout: float = DEFAULT_TIMEOUT):
        self._token = token or ""
        self._base = (base_url or "https://api.telegram.org").rstrip("/")
        self._session = session or requests.Session()
        self._timeout = timeout
        self._me_cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _call(self, method: str, params: dict[str, Any] | None = None, attempts: int = 2) -> Any:
        if not self._token:
            raise TelegramUnavailable("not_configured", "Telegram bot token is not configured.")
        url = f"{self._base}/bot{self._token}/{method}"
        last_kind, last_message = "network", "Telegram did not respond."
        for attempt in range(1, attempts + 1):
            try:
                response = self._session.post(url, json=params or {}, timeout=self._timeout)
            except requests.exceptions.Timeout:
                last_kind, last_message = "timeout", "Telegram timed out."
            except requests.exceptions.RequestException as exc:
                last_kind, last_message = "network", f"Telegram network error ({type(exc).__name__})."
            else:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if response.status_code == 200 and payload.get("ok"):
                    return payload.get("result")
                if response.status_code == 429:
                    retry_after = float((payload.get("parameters") or {}).get("retry_after", 1))
                    last_kind, last_message = "rate_limited", "Telegram rate limit reached."
                    if attempt < attempts and retry_after <= MAX_RETRY_AFTER:
                        time.sleep(retry_after)
                        continue
                    break
                if response.status_code >= 500:
                    last_kind, last_message = "server", f"Telegram returned HTTP {response.status_code}."
                else:
                    # 400/401/403 etc: a definitive answer, not transient
                    description = redact(str(payload.get("description") or ""))[:120]
                    last_kind = "rejected"
                    last_message = f"Telegram rejected the request (HTTP {response.status_code})."
                    if description:
                        last_message += f" {description}"
                    break
            if attempt < attempts:
                time.sleep(0.4 * attempt)
        logger.warning("telegram %s failed: %s", method, last_kind)
        raise TelegramUnavailable(last_kind, last_message)

    def get_me(self) -> dict[str, Any]:
        """Cached for 10 minutes (bot username does not change often)."""
        with self._lock:
            if self._me_cache and time.time() - self._me_cache[0] < 600:
                return dict(self._me_cache[1])
        result = self._call("getMe")
        info = {"id": result.get("id"), "username": result.get("username"),
                "first_name": result.get("first_name")}
        with self._lock:
            self._me_cache = (time.time(), info)
        return dict(info)

    def chat_member_status(self, chat_id: int, user_id: int) -> tuple[str, bool]:
        """Return ``(status, is_member)`` for a user in a chat. Raises on failure."""
        result = self._call("getChatMember", {"chat_id": int(chat_id), "user_id": int(user_id)})
        status = str((result or {}).get("status") or "")
        if status == "restricted":
            return status, bool((result or {}).get("is_member"))
        return status, status in MEMBER_STATUSES

    def is_group_member(self, chat_id: int, user_id: int) -> bool:
        _status, is_member = self.chat_member_status(chat_id, user_id)
        return is_member
