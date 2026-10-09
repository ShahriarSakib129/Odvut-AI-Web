"""Telegram Login Widget verification (server side).

Reference: https://core.telegram.org/widgets/login#checking-authorization

* data-check-string = every received field except ``hash``, sorted by key,
  formatted ``key=value`` and joined with ``\\n``
* secret key       = SHA-256(bot token)
* expected hash    = hex(HMAC-SHA-256(data_check_string, secret_key))

On top of the signature we enforce freshness (``auth_date``) and single use
(replay table in PostgreSQL, see ``web.store.consume_login_payload``).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

ALLOWED_FIELDS = ("id", "first_name", "last_name", "username", "photo_url", "auth_date", "hash")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_LIMIT = 128
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{0,32}$")


class TelegramAuthError(Exception):
    """Verification failed. ``public_message`` is safe to show to the browser."""

    def __init__(self, code: str, public_message: str):
        super().__init__(code)
        self.code = code
        self.public_message = public_message


@dataclass(frozen=True)
class TelegramIdentity:
    telegram_user_id: int
    first_name: str
    last_name: str
    username: str
    photo_url: str
    auth_date: int
    hash: str


def _text(value: Any, limit: int = _NAME_LIMIT) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int)):
        raise TelegramAuthError("bad_field", "Malformed Telegram login data.")
    return str(value).replace("\x00", "").strip()[:limit]


def parse_payload(payload: Mapping[str, Any]) -> TelegramIdentity:
    """Validate the shape of the payload (not the signature yet)."""
    if not isinstance(payload, Mapping):
        raise TelegramAuthError("not_mapping", "Malformed Telegram login data.")
    unknown = set(payload) - set(ALLOWED_FIELDS)
    if unknown:
        raise TelegramAuthError("unknown_field", "Malformed Telegram login data.")
    try:
        user_id = int(payload.get("id"))
        auth_date = int(payload.get("auth_date"))
    except (TypeError, ValueError):
        raise TelegramAuthError("bad_id", "Malformed Telegram login data.") from None
    if user_id <= 0 or auth_date <= 0:
        raise TelegramAuthError("bad_id", "Malformed Telegram login data.")
    digest = str(payload.get("hash") or "").lower()
    if not _HASH_RE.match(digest):
        raise TelegramAuthError("bad_hash_format", "Telegram login could not be verified.")
    username = _text(payload.get("username"), 32)
    if not _USERNAME_RE.match(username):
        raise TelegramAuthError("bad_username", "Malformed Telegram login data.")
    photo = _text(payload.get("photo_url"), 512)
    if photo:
        parts = urlsplit(photo)
        if parts.scheme != "https" or not parts.netloc:
            photo = ""  # only https avatars are ever stored or rendered
    return TelegramIdentity(
        telegram_user_id=user_id,
        first_name=_text(payload.get("first_name")),
        last_name=_text(payload.get("last_name")),
        username=username,
        photo_url=photo,
        auth_date=auth_date,
        hash=digest,
    )


def data_check_string(payload: Mapping[str, Any]) -> str:
    pairs = []
    for key in sorted(payload):
        if key == "hash" or payload[key] is None:
            continue
        pairs.append(f"{key}={payload[key]}")
    return "\n".join(pairs)


def expected_hash(payload: Mapping[str, Any], bot_token: str) -> str:
    secret = hashlib.sha256(bot_token.encode("utf-8")).digest()
    return hmac.new(secret, data_check_string(payload).encode("utf-8"), hashlib.sha256).hexdigest()


def verify(payload: Mapping[str, Any], bot_token: str, *, now_ts: float,
           max_age_seconds: int, future_skew_seconds: int = 60) -> TelegramIdentity:
    """Full verification: shape, signature, freshness. Raises :class:`TelegramAuthError`."""
    if not bot_token:
        raise TelegramAuthError("no_token", "Telegram login is not configured.")
    identity = parse_payload(payload)
    # use the parsed, string-normalised values so the check string is canonical
    canonical = {key: str(payload[key]) for key in payload if payload[key] is not None}
    if not hmac.compare_digest(expected_hash(canonical, bot_token), identity.hash):
        raise TelegramAuthError("bad_signature", "Telegram login could not be verified.")
    age = now_ts - identity.auth_date
    if age > max_age_seconds:
        raise TelegramAuthError("expired", "Telegram login expired. Please try again.")
    if age < -future_skew_seconds:
        raise TelegramAuthError("future", "Telegram login timestamp is invalid.")
    return identity


def payload_digest(identity: TelegramIdentity) -> str:
    """Stable digest of one accepted login payload (replay key)."""
    return hashlib.sha256(f"{identity.hash}|{identity.telegram_user_id}|{identity.auth_date}"
                          .encode("utf-8")).hexdigest()
