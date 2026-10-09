"""Sessions, CSRF, and the server-side authorisation decision for every request.

Every protected request re-derives the user's rights from the database and
the configuration -- the browser's claims (cookies aside) are never trusted:

* role     -> ``ADMIN_ID`` membership is read from settings on each request
* access   -> ``bot_users.access_status`` (active / suspended / blocked)
* group    -> live ``getChatMember`` at login and every ``MEMBERSHIP_RECHECK_SECONDS``
* session  -> revoked / expired rows are rejected immediately (logout works)
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Request

from utils.logger import get_logger
from web import store
from web.security import hash_identifier, new_token, safe_equals, sha256_hex
from web.telegram_api import TelegramClient, TelegramUnavailable

logger = get_logger(__name__)

SESSION_TOUCH_SECONDS = 60


class AuthFailure(Exception):
    """Authentication/authorisation problem. ``status`` is the HTTP status to return."""

    def __init__(self, status: int, message: str, code: str = "unauthorized"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


@dataclass(frozen=True)
class WebUser:
    telegram_user_id: int
    role: str                     # "admin" or "member"
    username: str
    first_name: str
    last_name: str
    photo_url: str
    access_status: str
    session_id: int
    session_hash: str
    csrf_token: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def display_name(self) -> str:
        name = " ".join(part for part in (self.first_name, self.last_name) if part).strip()
        return name or (f"@{self.username}" if self.username else str(self.telegram_user_id))

    def public(self) -> dict[str, Any]:
        return {
            "telegram_user_id": str(self.telegram_user_id),
            "username": self.username or None,
            "display_name": self.display_name,
            "photo_url": self.photo_url or None,
            "role": self.role,
            "access_status": self.access_status,
        }


# --------------------------------------------------------------------------- #
# cookies & CSRF
# --------------------------------------------------------------------------- #
def cookie_name(settings: Any) -> str:
    # __Host- prefix: browser refuses the cookie unless Secure, Path=/ and no Domain
    return "__Host-ig_session" if settings.web_cookie_secure else "ig_session"


def _pepper(settings: Any) -> bytes:
    if settings.web_secret_key:
        return settings.web_secret_key.encode("utf-8")
    # derived from the bot token when no dedicated key is set (documented in .env.example)
    return hashlib.sha256(("web-csrf|" + settings.bot_token).encode("utf-8")).digest()


def derive_csrf(session_token: str, settings: Any) -> str:
    return hmac.new(_pepper(settings), session_token.encode("utf-8"), hashlib.sha256).hexdigest()


def session_cookie_value(request_obj: Request, settings: Any) -> str:
    return request_obj.cookies.get(cookie_name(settings), "") or ""


# --------------------------------------------------------------------------- #
# telegram client (process-wide, rebuilt if the token changes)
# --------------------------------------------------------------------------- #
_client: TelegramClient | None = None
_client_key: tuple[str, str] | None = None


def telegram_client(settings: Any) -> TelegramClient:
    global _client, _client_key
    key = (settings.bot_token, settings.telegram_api_base)
    if _client is None or _client_key != key:
        _client = TelegramClient(settings.bot_token, settings.telegram_api_base)
        _client_key = key
    return _client


def reset_telegram_client() -> None:
    global _client, _client_key
    _client, _client_key = None, None


def is_admin_id(settings: Any, telegram_user_id: int) -> bool:
    """Explicit administrator rule for the web admin area: ``ADMIN_ID`` only."""
    return int(telegram_user_id) in settings.web_admin_ids


def verify_group_membership(settings: Any, telegram_user_id: int) -> bool:
    """Fail closed: any inability to answer raises AuthFailure(503)."""
    if not settings.group_id:
        raise AuthFailure(403, "Member access is not configured yet. Ask the administrator.",
                          code="group_not_configured")
    try:
        return telegram_client(settings).is_group_member(settings.group_id, telegram_user_id)
    except TelegramUnavailable as exc:
        logger.warning("membership check unavailable (%s) user=%s", exc.kind,
                       hash_identifier(str(telegram_user_id)))
        raise AuthFailure(503, "Could not verify your group membership right now. "
                               "Please try again in a minute.", code="membership_unavailable") from exc


# --------------------------------------------------------------------------- #
# session lifecycle
# --------------------------------------------------------------------------- #
def issue_session(request_obj: Request, settings: Any, *, telegram_user_id: int,
                  role: str) -> tuple[str, str, datetime]:
    token = new_token(32)
    csrf = derive_csrf(token, settings)
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.web_session_hours)
    store.create_session(
        session_hash=sha256_hex(token),
        csrf_hash=sha256_hex(csrf),
        telegram_user_id=telegram_user_id,
        role=role,
        ip_hash=hash_identifier(request_obj.remote_addr),
        user_agent_hash=hash_identifier((request_obj.headers.get("User-Agent") or "")[:200]),
        expires_at=expires,
    )
    return token, csrf, expires


def revoke(request_obj: Request, settings: Any, reason: str) -> None:
    token = session_cookie_value(request_obj, settings)
    if token:
        store.revoke_session(sha256_hex(token), reason)


def resolve_user(request_obj: Request, settings: Any) -> WebUser | None:
    """Return the authenticated user, ``None`` when there is no valid session.

    Raises :class:`AuthFailure` when a session exists but must not be honoured
    (blocked/suspended, no longer a member, membership cannot be verified).
    """
    token = session_cookie_value(request_obj, settings)
    if not token:
        return None
    row = store.load_session(sha256_hex(token))
    if row is None:
        return None
    uid = int(row["telegram_user_id"])
    admin = is_admin_id(settings, uid)

    if not admin:
        status = row.get("access_status") or "active"
        if status != "active":
            store.revoke_session(sha256_hex(token), f"access_{status}")
            raise AuthFailure(403, f"Your access is {status}. Contact the administrator.",
                              code=f"access_{status}")
        checked = row.get("membership_checked_at")
        stale = (checked is None or
                 (datetime.now(timezone.utc) - checked).total_seconds() > settings.membership_recheck_seconds)
        if stale:
            member = verify_group_membership(settings, uid)
            if not member:
                store.revoke_session(sha256_hex(token), "not_member")
                raise AuthFailure(403, "You are no longer a member of the INFO GROUP.",
                                  code="not_member")
            store.touch_session(int(row["id"]), membership_checked=True)
    last_seen = row.get("last_seen_at")
    if last_seen is None or (datetime.now(timezone.utc) - last_seen).total_seconds() > SESSION_TOUCH_SECONDS:
        store.touch_session(int(row["id"]))

    return WebUser(
        telegram_user_id=uid,
        role="admin" if admin else "member",
        username=row.get("username") or "",
        first_name=row.get("first_name") or "",
        last_name=row.get("last_name") or "",
        photo_url=row.get("photo_url") or "",
        access_status=row.get("access_status") or "active",
        session_id=int(row["id"]),
        session_hash=sha256_hex(token),
        csrf_token=derive_csrf(token, settings),
    )


def check_csrf(request_obj: Request, user: WebUser) -> bool:
    supplied = request_obj.headers.get("X-CSRF-Token", "")
    return bool(supplied) and safe_equals(supplied, user.csrf_token)

