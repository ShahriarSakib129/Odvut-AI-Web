"""Shared security helpers: hashing, constant-time compare, throttling, origin checks."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import urlsplit

from utils.logger import redact


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def safe_equals(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def client_ip(request_obj: Any) -> str:
    """Client address. Behind Render's proxy, ProxyFix (x_for=1) supplies the real one."""
    return (getattr(request_obj, "remote_addr", None) or "unknown")[:64]


def hash_identifier(value: str | None) -> str | None:
    """Store a salted-by-pepper digest instead of raw IP/User-Agent values."""
    if not value:
        return None
    return sha256_hex("ig-web|" + value)[:32]


class SlidingWindowLimiter:
    """Thread-safe in-memory sliding-window limiter (per process)."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: float) -> tuple[bool, float]:
        """Record one event for ``key``. Returns ``(allowed, retry_after_seconds)``."""
        now = time.monotonic()
        with self._lock:
            bucket = self._events.setdefault(key, deque())
            while bucket and now - bucket[0] > window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                return False, max(0.0, window_seconds - (now - bucket[0]))
            bucket.append(now)
            if len(self._events) > 5000:  # bound memory on long-running instances
                for stale in [k for k, v in self._events.items() if not v][:1000]:
                    self._events.pop(stale, None)
            return True, 0.0

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


def origin_allowed(request_obj: Any, allowed_origins: set[str]) -> bool:
    """Reject cross-site state-changing requests (defence in depth besides CSRF tokens)."""
    origin = request_obj.headers.get("Origin")
    referer = request_obj.headers.get("Referer")
    candidate = origin or referer
    if not candidate:
        # non-browser clients (curl, tests) send neither; CSRF token is still required
        return True
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return False
    netloc = (parts.netloc or "").lower()
    return netloc in allowed_origins


def allowed_origin_netlocs(request_obj: Any, public_url: str) -> set[str]:
    allowed = {(request_obj.host or "").lower()}
    if public_url:
        allowed.add((urlsplit(public_url).netloc or "").lower())
    return {item for item in allowed if item}


def safe_text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\x00", "").strip()
    return text[:limit]


def public_error(exc: BaseException, fallback: str = "Something went wrong.") -> str:
    """User-facing text that never contains secrets or stack details."""
    text = redact(str(exc))[:200]
    return text if text and "Traceback" not in text else fallback
