"""Admin-editable settings: validated, persisted, applied live or after restart.

Only the keys listed in ``SCHEMA`` can be changed from the dashboard. Each one
is stored in ``bot_settings`` under ``web.<key>``. ``live`` keys are applied to
the running process immediately (settings object swapped, responder updated).
``restart`` keys are stored and reported as "restart required" because they
are read once at start-up. Secrets, IDs, URLs and the database are never here:
the dashboard only shows whether they are configured.
"""

from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass
from typing import Any

import database
from utils.logger import get_logger

logger = get_logger(__name__)

PREFIX = "web."


@dataclass(frozen=True)
class Field:
    kind: str                 # int | bool | choice | text
    live: bool
    group: str
    label: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    max_len: int = 0


SCHEMA: dict[str, Field] = {
    # live -------------------------------------------------------------
    "ai_enabled": Field("bool", True, "AI", "AI answering enabled (Telegram + web)"),
    "web_chat_enabled": Field("bool", True, "AI", "Website AI chat enabled"),
    "answer_style": Field("choice", True, "Answers", "Answer style",
                          choices=("balanced", "concise", "detailed")),
    "answer_language": Field("choice", True, "Answers", "Answer language",
                             choices=("auto", "bangla", "banglish", "english")),
    "system_prompt_extra": Field("text", True, "Answers", "Extra instructions for the assistant",
                                 max_len=1500),
    "quota_default_daily_requests": Field("int", True, "Default limits", "Daily requests per member",
                                          0, 10000),
    "quota_default_monthly_requests": Field("int", True, "Default limits", "Monthly requests per member",
                                            0, 100000),
    "quota_default_daily_tokens": Field("int", True, "Default limits", "Daily tokens per member (0 = unlimited)",
                                        0, 100000000),
    "quota_default_monthly_tokens": Field("int", True, "Default limits", "Monthly tokens per member (0 = unlimited)",
                                          0, 1000000000),
    "quota_default_cooldown_seconds": Field("int", True, "Default limits", "Cooldown between requests (seconds)",
                                            0, 600),
    "quota_admin_daily_requests": Field("int", True, "Admin safety caps", "Admin daily request cap (0 = none)",
                                        0, 100000),
    "quota_admin_monthly_requests": Field("int", True, "Admin safety caps", "Admin monthly request cap (0 = none)",
                                          0, 1000000),
    "quota_admin_daily_tokens": Field("int", True, "Admin safety caps", "Admin daily token cap (0 = none)",
                                      0, 1000000000),
    "quota_admin_monthly_tokens": Field("int", True, "Admin safety caps", "Admin monthly token cap (0 = none)",
                                        0, 10000000000),
    "web_max_message_chars": Field("int", True, "Website", "Max characters per message", 100, 4000),
    "web_history_messages": Field("int", True, "Website", "Conversation turns sent to the model", 0, 20),
    "membership_recheck_seconds": Field("int", True, "Security", "Group membership recheck interval (seconds)",
                                        30, 86400),
    "login_rate_limit_per_window": Field("int", True, "Security", "Login attempts per 5 minutes per IP",
                                         1, 100),
    "telegram_auth_max_age_seconds": Field("int", True, "Security", "Max age of a Telegram login (seconds)",
                                           60, 900),
    "web_session_hours": Field("int", True, "Security", "Session length (hours)", 1, 168),
    # restart required -------------------------------------------------
    "groq_model": Field("text", False, "Model", "Groq model name (restart)", max_len=120),
    "rate_limit_per_minute": Field("int", False, "Limits", "Telegram requests per minute per user (restart)",
                                   1, 60),
    "max_question_chars": Field("int", False, "Limits", "Max characters per question (restart)", 100, 4000),
    "max_answer_chars": Field("int", False, "Limits", "Max characters per answer (restart)", 500, 10000),
}


def _coerce(field: Field, raw: Any) -> Any:
    if field.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in {"true", "false", "1", "0", "on", "off", "yes", "no"}:
            return raw.strip().lower() in {"true", "1", "on", "yes"}
        raise ValueError("must be true or false")
    if field.kind == "int":
        if isinstance(raw, bool):
            raise ValueError("must be a whole number")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError("must be a whole number") from None
        if field.minimum is not None and value < field.minimum:
            raise ValueError(f"must be at least {field.minimum}")
        if field.maximum is not None and value > field.maximum:
            raise ValueError(f"must be at most {field.maximum}")
        return value
    if field.kind == "choice":
        value = str(raw or "").strip().lower()
        if value not in field.choices:
            raise ValueError(f"must be one of: {', '.join(field.choices)}")
        return value
    if field.kind == "text":
        if raw is None:
            return ""
        if not isinstance(raw, str):
            raise ValueError("must be text")
        value = raw.replace("\x00", "").strip()
        if field.max_len and len(value) > field.max_len:
            raise ValueError(f"must be at most {field.max_len} characters")
        return value
    raise ValueError("unsupported field")


def validate(updates: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Return (clean_values, errors). Unknown keys are rejected."""
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, raw in (updates or {}).items():
        field = SCHEMA.get(key)
        if field is None:
            errors[key] = "unknown or read-only setting"
            continue
        try:
            clean[key] = _coerce(field, raw)
        except ValueError as exc:
            errors[key] = str(exc)
    return clean, errors


def _encode(value: Any) -> tuple[str, str]:
    if isinstance(value, bool):
        return ("true" if value else "false"), "bool"
    if isinstance(value, int):
        return str(value), "int"
    return str(value), "string"


def _decode(raw: str, field: Field) -> Any:
    try:
        return _coerce(field, raw if field.kind != "bool" else raw)
    except ValueError:
        return None


_lock = threading.Lock()
_loaded_for: int | None = None


def stored_values() -> dict[str, str]:
    rows = database._fetchall(
        "select key, value from public.bot_settings where key like %(p)s",
        {"p": PREFIX + "%"},
    )
    return {row["key"][len(PREFIX):]: row["value"] for row in rows}


def save(values: dict[str, Any], actor_id: int) -> None:
    for key, value in values.items():
        encoded, value_type = _encode(value)
        if not database.set_setting(PREFIX + key, encoded, updated_by=actor_id, value_type=value_type):
            raise database.DatabaseUnavailable(f"could not store setting {key}")


def _apply_live(services: Any, overrides: dict[str, Any]) -> None:
    """Swap the immutable settings object for the running process."""
    if not overrides:
        return
    mapping = {
        "quota_default_daily_requests", "quota_default_monthly_requests", "quota_default_daily_tokens",
        "quota_default_monthly_tokens", "quota_default_cooldown_seconds", "quota_admin_daily_requests",
        "quota_admin_monthly_requests", "quota_admin_daily_tokens", "quota_admin_monthly_tokens",
        "web_max_message_chars", "web_history_messages", "membership_recheck_seconds",
        "login_rate_limit_per_window", "telegram_auth_max_age_seconds", "web_session_hours",
        "answer_style", "answer_language", "system_prompt_extra",
    }
    applicable = {k: v for k, v in overrides.items() if k in mapping and SCHEMA[k].live}
    if not applicable:
        return
    new_settings = dataclasses.replace(services.settings, **applicable)
    services.settings = new_settings
    if services.responder is not None and hasattr(services.responder, "settings"):
        services.responder.settings = new_settings


def load_into(services: Any) -> dict[str, Any]:
    """Apply saved overrides to this process. Live keys always; restart keys are applied
    only at process start (``restart`` keys are read on first load)."""
    global _loaded_for
    with _lock:
        stored = stored_values()
        overrides: dict[str, Any] = {}
        for key, raw in stored.items():
            field = SCHEMA.get(key)
            if field is None:
                continue
            value = _decode(raw, field)
            if value is not None:
                overrides[key] = value
        live = {k: v for k, v in overrides.items() if SCHEMA[k].live}
        _apply_live(services, live)
        if _loaded_for is None:
            restart_applied = {k: v for k, v in overrides.items() if not SCHEMA[k].live}
            if restart_applied:
                new_settings = dataclasses.replace(services.settings, **restart_applied)
                services.settings = new_settings
                if services.responder is not None and hasattr(services.responder, "settings"):
                    services.responder.settings = new_settings
        _loaded_for = id(services)
        return overrides


def pending_restart(services: Any) -> dict[str, Any]:
    """Restart-required keys whose stored value differs from the running value."""
    pending = {}
    for key, raw in stored_values().items():
        field = SCHEMA.get(key)
        if field is None or field.live:
            continue
        stored = _decode(raw, field)
        if stored is not None and stored != getattr(services.settings, key, None):
            pending[key] = stored
    return pending


def describe(services: Any) -> list[dict[str, Any]]:
    """Dashboard view: current value for every editable key (no secrets)."""
    stored = stored_values()
    items: list[dict[str, Any]] = []
    for key, field in SCHEMA.items():
        current = getattr(services.settings, key, None)
        items.append({
            "key": key,
            "label": field.label,
            "group": field.group,
            "type": field.kind,
            "live": field.live,
            "value": current,
            "stored": key in stored,
            "min": field.minimum,
            "max": field.maximum,
            "choices": list(field.choices),
            "max_len": field.max_len,
        })
    return items


def apply_saved(services: Any, clean: dict[str, Any], actor_id: int) -> dict[str, list[str]]:
    """Persist validated values and report which take effect now vs after restart."""
    flags = {"ai_enabled", "web_chat_enabled"}  # stored as runtime flags, not web.* keys
    for flag in flags:
        if flag in clean:
            services.set_runtime_flag(flag, bool(clean[flag]), updated_by=actor_id)
    persisted = {k: v for k, v in clean.items() if k not in flags}
    save(persisted, actor_id=actor_id)
    live = [k for k in clean if SCHEMA[k].live]
    restart = [k for k in clean if not SCHEMA[k].live]
    _apply_live(services, {k: v for k, v in persisted.items() if SCHEMA[k].live})
    return {"applied_now": live, "restart_required": restart}
