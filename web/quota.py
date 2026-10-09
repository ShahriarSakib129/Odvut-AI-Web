"""Per-user AI quotas, enforced in PostgreSQL before any provider call.

Design
------
* **Reserve first, call second.** ``reserve()`` runs one transaction that
  (1) checks/stamps the cooldown on the user's ``bot_users`` row (row lock),
  (2) records the request in ``ai_request_ledger`` keyed by a client
  ``request_uuid`` (UNIQUE -> a retried request is never charged twice),
  (3) increments the day and month counters with a conditional upsert
  ``ON CONFLICT ... DO UPDATE ... WHERE requests < limit``. Concurrent requests
  serialise on the counter row, so the limit cannot be overshot.
* **Refund on failure.** ``complete(ok=False)`` marks the ledger row failed and
  decrements the counters exactly once (guarded by ``status = 'reserved'``).
* **Token limits** are checked before the call against tokens already used and
  charged after the call with the provider's reported usage. A single response
  can therefore exceed a token limit by at most one response; this is stated in
  the admin documentation.
* **Stale reservations** (process died mid-call) are refunded after 5 minutes.

Limit semantics
---------------
``None`` = unlimited. Request limit ``0`` = no AI access. Token limit ``0`` =
unlimited (the default for tokens). Admin caps use ``0`` = no cap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import database
from web.timeutil import next_day_reset, next_month_reset, period_keys

STALE_AFTER_MINUTES = 5


class QuotaError(Exception):
    code = "quota_error"

    def __init__(self, message: str, *, retry_after: int | None = None, period: str | None = None,
                 kind: str | None = None):
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after
        self.period = period
        self.kind = kind


class QuotaExceeded(QuotaError):
    code = "quota_exceeded"


class CooldownActive(QuotaError):
    code = "cooldown"


class DuplicateRequest(Exception):
    """This request_uuid is already reserved, completed or failed-and-refunded."""

    def __init__(self, request_uuid: str):
        super().__init__("duplicate request")
        self.request_uuid = request_uuid


@dataclass(frozen=True)
class Limits:
    daily_requests: int | None
    monthly_requests: int | None
    daily_tokens: int | None
    monthly_tokens: int | None
    cooldown_seconds: int
    max_response_tokens: int | None
    model_override: str | None
    is_admin: bool


@dataclass(frozen=True)
class Reservation:
    ledger_id: int
    request_uuid: str
    telegram_user_id: int
    day_key: str
    month_key: str


def _token_cap(value: Any) -> int | None:
    value = int(value or 0)
    return value if value > 0 else None


def effective_limits(user: dict[str, Any] | None, *, is_admin: bool, settings: Any) -> Limits:
    """Resolve the limits that apply to this user right now.

    Admins (``ADMIN_ID``) are exempt from ordinary member limits but never from
    the safety caps configured in ``QUOTA_ADMIN_*``.
    """
    user = user or {}
    if is_admin:
        return Limits(
            daily_requests=_token_cap(settings.quota_admin_daily_requests),
            monthly_requests=_token_cap(settings.quota_admin_monthly_requests),
            daily_tokens=_token_cap(settings.quota_admin_daily_tokens),
            monthly_tokens=_token_cap(settings.quota_admin_monthly_tokens),
            cooldown_seconds=0,
            max_response_tokens=None,
            model_override=user.get("model_override") or None,
            is_admin=True,
        )

    def pick(field: str, default: int) -> int:
        value = user.get(field)
        return int(default if value is None else value)

    return Limits(
        daily_requests=pick("daily_request_limit", settings.quota_default_daily_requests),
        monthly_requests=pick("monthly_request_limit", settings.quota_default_monthly_requests),
        daily_tokens=_token_cap(pick("daily_token_limit", settings.quota_default_daily_tokens)),
        monthly_tokens=_token_cap(pick("monthly_token_limit", settings.quota_default_monthly_tokens)),
        cooldown_seconds=max(0, pick("cooldown_seconds", settings.quota_default_cooldown_seconds)),
        max_response_tokens=(int(user["max_response_tokens"])
                             if user.get("max_response_tokens") else None),
        model_override=user.get("model_override") or None,
        is_admin=False,
    )


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _refund_stale(telegram_user_id: int) -> int:
    refunded = 0
    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update public.ai_request_ledger
                   set status = 'failed', error_type = 'stale_reservation', updated_at = now()
                 where telegram_user_id = %s and status = 'reserved'
                   and created_at < now() - make_interval(mins => %s)
                returning day_key, month_key
                """,
                (int(telegram_user_id), STALE_AFTER_MINUTES),
            )
            for day_key, month_key in cur.fetchall():
                _decrement(cur, telegram_user_id, day_key, month_key)
                refunded += 1
    return refunded


def _decrement(cur: Any, telegram_user_id: int, day_key: str, month_key: str) -> None:
    for period_type, key in (("day", day_key), ("month", month_key)):
        cur.execute(
            "update public.user_usage_periods set requests = greatest(requests - 1, 0), "
            "updated_at = now() where telegram_user_id = %s and period_type = %s and period_key = %s",
            (int(telegram_user_id), period_type, key),
        )


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def reserve(*, telegram_user_id: int, request_uuid: str, conversation_id: str | None,
            limits: Limits, model: str | None, now: datetime | None = None) -> Reservation:
    """Atomically check and charge one request. Raises QuotaError / DuplicateRequest."""
    uid = int(telegram_user_id)
    day_key, month_key = period_keys(now)
    _refund_stale(uid)

    with database.get_connection() as conn:
        with conn.cursor() as cur:
            # 1. cooldown: the UPDATE takes a row lock on the user, so concurrent
            #    requests from the same user are serialised here.
            if limits.cooldown_seconds > 0:
                cur.execute(
                    """
                    update public.bot_users set last_ai_request_at = now()
                     where telegram_user_id = %s
                       and (last_ai_request_at is null
                            or last_ai_request_at <= now() - make_interval(secs => %s))
                    returning 1
                    """,
                    (uid, int(limits.cooldown_seconds)),
                )
                if cur.fetchone() is None:
                    cur.execute(
                        "select extract(epoch from (last_ai_request_at + make_interval(secs => %s)"
                        " - now())) from public.bot_users where telegram_user_id = %s",
                        (int(limits.cooldown_seconds), uid),
                    )
                    remaining = cur.fetchone()
                    raise CooldownActive(
                        "Please wait a moment before sending the next message.",
                        retry_after=max(1, math.ceil(float(remaining[0] or 1))),
                        kind="cooldown",
                    )
            else:
                cur.execute("update public.bot_users set last_ai_request_at = now() "
                            "where telegram_user_id = %s", (uid,))

            # 2. idempotent ledger row (a failed-and-refunded uuid may be retried once)
            cur.execute(
                """
                insert into public.ai_request_ledger
                    (request_uuid, telegram_user_id, source, conversation_id, status, model,
                     day_key, month_key)
                values (%s, %s, 'web', %s::uuid, 'reserved', %s, %s, %s)
                on conflict (request_uuid) do update set
                    status = 'reserved', error_type = null, updated_at = now(),
                    conversation_id = excluded.conversation_id, model = excluded.model,
                    day_key = excluded.day_key, month_key = excluded.month_key,
                    created_at = now()
                 where public.ai_request_ledger.status = 'failed'
                   and public.ai_request_ledger.telegram_user_id = excluded.telegram_user_id
                returning id
                """,
                (request_uuid, uid, conversation_id, model, day_key, month_key),
            )
            row = cur.fetchone()
            if row is None:
                raise DuplicateRequest(request_uuid)
            ledger_id = int(row[0])

            # 3. counters: conditional upsert; no row returned = limit reached
            for period_type, key, req_limit, tok_limit, label in (
                ("day", day_key, limits.daily_requests, limits.daily_tokens, "daily"),
                ("month", month_key, limits.monthly_requests, limits.monthly_tokens, "monthly"),
            ):
                if req_limit == 0:
                    raise QuotaExceeded(
                        "AI access is currently disabled for your account.", period=period_type,
                        kind="blocked")
                cur.execute(
                    """
                    insert into public.user_usage_periods
                        (telegram_user_id, period_type, period_key, requests, tokens)
                    values (%(uid)s, %(ptype)s, %(pkey)s, 1, 0)
                    on conflict (telegram_user_id, period_type, period_key) do update set
                        requests = public.user_usage_periods.requests + 1,
                        updated_at = now()
                     where (%(rlim)s::int is null or public.user_usage_periods.requests < %(rlim)s::int)
                       and (%(tlim)s::bigint is null or public.user_usage_periods.tokens < %(tlim)s::bigint)
                    returning requests
                    """,
                    {"uid": uid, "ptype": period_type, "pkey": key,
                     "rlim": req_limit, "tlim": tok_limit},
                )
                if cur.fetchone() is None:
                    cur.execute(
                        "select requests, tokens from public.user_usage_periods "
                        "where telegram_user_id = %s and period_type = %s and period_key = %s",
                        (uid, period_type, key),
                    )
                    used = cur.fetchone() or (0, 0)
                    token_hit = tok_limit is not None and int(used[1] or 0) >= tok_limit
                    reset = next_day_reset(now) if period_type == "day" else next_month_reset(now)
                    kind = "tokens" if token_hit else "requests"
                    message = (
                        f"Your {label} {kind} limit has been reached. "
                        f"It resets at {reset.strftime('%d %b %Y %H:%M')} (Asia/Dhaka)."
                    )
                    raise QuotaExceeded(message, period=period_type, kind=kind,
                                        retry_after=max(1, int((reset - (now or datetime.now(reset.tzinfo))).total_seconds())))
    return Reservation(ledger_id=ledger_id, request_uuid=request_uuid, telegram_user_id=uid,
                       day_key=day_key, month_key=month_key)


def complete(reservation: Reservation, *, ok: bool, model: str | None = None,
             prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0,
             latency_ms: int = 0, error_type: str | None = None) -> bool:
    """Settle a reservation: charge tokens on success, refund the request on failure.

    Returns False if the reservation was already settled (idempotent).
    """
    uid = reservation.telegram_user_id
    with database.get_connection() as conn:
        with conn.cursor() as cur:
            if ok:
                cur.execute(
                    """
                    update public.ai_request_ledger set status = 'succeeded', model = %s,
                           prompt_tokens = %s, completion_tokens = %s, total_tokens = %s,
                           latency_ms = %s, updated_at = now()
                     where id = %s and status = 'reserved'
                    returning 1
                    """,
                    (model, int(prompt_tokens), int(completion_tokens), int(total_tokens),
                     int(latency_ms), reservation.ledger_id),
                )
                if cur.fetchone() is None:
                    return False
                for period_type, key in (("day", reservation.day_key),
                                         ("month", reservation.month_key)):
                    cur.execute(
                        """
                        insert into public.user_usage_periods
                            (telegram_user_id, period_type, period_key, requests, tokens)
                        values (%s, %s, %s, 0, %s)
                        on conflict (telegram_user_id, period_type, period_key) do update set
                            tokens = public.user_usage_periods.tokens + excluded.tokens,
                            updated_at = now()
                        """,
                        (uid, period_type, key, int(total_tokens)),
                    )
                return True

            cur.execute(
                """
                update public.ai_request_ledger set status = 'failed', error_type = %s,
                       latency_ms = %s, updated_at = now()
                 where id = %s and status = 'reserved'
                returning day_key, month_key
                """,
                ((error_type or "error")[:64], int(latency_ms), reservation.ledger_id),
            )
            row = cur.fetchone()
            if row is None:
                return False
            _decrement(cur, uid, row[0], row[1])
            return True


def snapshot(telegram_user_id: int, limits: Limits, *, now: datetime | None = None) -> dict[str, Any]:
    """Usage and remaining quota for the UI (read-only)."""
    day_key, month_key = period_keys(now)
    rows = database._fetchall(
        "select period_type, period_key, requests, tokens from public.user_usage_periods "
        "where telegram_user_id = %(uid)s and ((period_type = 'day' and period_key = %(day)s) "
        "or (period_type = 'month' and period_key = %(month)s))",
        {"uid": int(telegram_user_id), "day": day_key, "month": month_key},
    )
    used = {(r["period_type"]): (int(r["requests"]), int(r["tokens"])) for r in rows}
    cooldown = database._fetchone(
        "select greatest(0, extract(epoch from (last_ai_request_at + make_interval(secs => %(s)s) "
        "- now())))::int as remaining from public.bot_users where telegram_user_id = %(uid)s",
        {"uid": int(telegram_user_id), "s": int(limits.cooldown_seconds)},
    ) if limits.cooldown_seconds else None

    def block(period: str, req_limit: int | None, tok_limit: int | None, resets: datetime) -> dict[str, Any]:
        req_used, tok_used = used.get(period, (0, 0))
        return {
            "requests_used": req_used,
            "requests_limit": req_limit,
            "requests_remaining": None if req_limit is None else max(0, req_limit - req_used),
            "tokens_used": tok_used,
            "tokens_limit": tok_limit,
            "tokens_remaining": None if tok_limit is None else max(0, tok_limit - tok_used),
            "resets_at": resets.isoformat(),
        }

    return {
        "is_admin": limits.is_admin,
        "timezone": "Asia/Dhaka",
        "daily": block("day", limits.daily_requests, limits.daily_tokens,
                       next_day_reset(now)),
        "monthly": block("month", limits.monthly_requests, limits.monthly_tokens,
                         next_month_reset(now)),
        "cooldown_seconds": int(limits.cooldown_seconds),
        "cooldown_remaining_seconds": int((cooldown or {}).get("remaining") or 0),
        "max_response_tokens": limits.max_response_tokens,
        "model_override": limits.model_override,
    }


def reset_counters(telegram_user_id: int, scope: str) -> int:
    """Admin action: zero the counters for ``today``, ``month`` or ``all``. Ledger is kept."""
    day_key, month_key = period_keys()
    if scope == "today":
        return database._execute(
            "delete from public.user_usage_periods where telegram_user_id = %(uid)s "
            "and period_type = 'day' and period_key = %(day)s",
            {"uid": int(telegram_user_id), "day": day_key})
    if scope == "month":
        return database._execute(
            "delete from public.user_usage_periods where telegram_user_id = %(uid)s "
            "and period_type = 'month' and period_key = %(month)s",
            {"uid": int(telegram_user_id), "month": month_key})
    if scope == "all":
        return database._execute(
            "delete from public.user_usage_periods where telegram_user_id = %(uid)s",
            {"uid": int(telegram_user_id)})
    raise ValueError("scope must be today, month or all")
