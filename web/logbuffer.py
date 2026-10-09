"""In-memory ring buffer of recent application log lines for the admin Logs tab.

Lines are passed through ``utils.logger.redact`` before being stored, and the
buffer is process-local (it resets on restart). Nothing here is written to the
database, so there is no extra data retention.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone

from utils.logger import redact

_MAX_LINES = 400


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = _MAX_LINES) -> None:
        super().__init__(level=logging.INFO)
        self._items: deque[dict] = deque(maxlen=capacity)
        self._guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact(record.getMessage())[:400]
            entry = {
                "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name[:80],
                "message": message,
            }
            with self._guard:
                self._items.append(entry)
        except Exception:  # logging must never raise
            self.handleError(record)

    def recent(self, limit: int = 200, min_level: str = "INFO") -> list[dict]:
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
        floor = order.get(min_level.upper(), 20)
        with self._guard:
            items = [item for item in self._items if order.get(item["level"], 0) >= floor]
        return list(reversed(items[-max(1, min(limit, _MAX_LINES)):]))


_handler: RingBufferHandler | None = None


def install() -> RingBufferHandler:
    """Attach the buffer to the root logger once per process."""
    global _handler
    if _handler is None:
        _handler = RingBufferHandler()
        logging.getLogger().addHandler(_handler)
    return _handler


def get() -> RingBufferHandler:
    return install()
