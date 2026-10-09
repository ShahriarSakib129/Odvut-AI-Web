"""Usage periods are defined in Asia/Dhaka (the operator's timezone).

A "day" starts at 00:00 Asia/Dhaka and a "month" at the 1st of the month in the
same zone. Period keys are stored as text so they are easy to index and audit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DHAKA_TZ_NAME = "Asia/Dhaka"
DHAKA = ZoneInfo(DHAKA_TZ_NAME)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def period_keys(now: datetime | None = None) -> tuple[str, str]:
    """Return ``(day_key, month_key)`` for the Dhaka calendar, e.g. ('2026-10-10', '2026-10')."""
    local = (now or now_utc()).astimezone(DHAKA)
    return local.strftime("%Y-%m-%d"), local.strftime("%Y-%m")


def next_day_reset(now: datetime | None = None) -> datetime:
    local = (now or now_utc()).astimezone(DHAKA)
    midnight = datetime(local.year, local.month, local.day, tzinfo=DHAKA)
    return midnight + timedelta(days=1)


def next_month_reset(now: datetime | None = None) -> datetime:
    local = (now or now_utc()).astimezone(DHAKA)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return datetime(year, month, 1, tzinfo=DHAKA)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
