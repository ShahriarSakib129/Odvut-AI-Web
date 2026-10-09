"""Contract tests: the response fields that web/static/js/*.js read must exist.

The UI is plain JavaScript, so a renamed field would only show up as a blank panel.
These tests pin the shapes the scripts depend on.
"""

from __future__ import annotations

from tests.test_web_platform import (ADMIN, MEMBER_A, _member, _new_conversation,  # noqa: F401 - fixture
                                     _send, web)


def _has(obj, *keys):
    missing = [k for k in keys if not (isinstance(obj, dict) and k in obj)]
    assert not missing, f"missing keys {missing} in {sorted(obj) if isinstance(obj, dict) else obj!r}"


def test_member_contract(web):
    client, token = _member(web, MEMBER_A)
    me = client.get("/api/auth/me").get_json()
    _has(me, "csrf_token", "user", "quota", "features")
    _has(me["user"], "role", "display_name", "photo_url", "username", "access_status")
    _has(me["features"], "chat_enabled", "max_message_chars")
    _has(me["quota"], "daily", "monthly", "cooldown_remaining_seconds", "timezone")
    for period in ("daily", "monthly"):
        _has(me["quota"][period], "requests_used", "requests_limit", "tokens_used", "tokens_limit", "resets_at")

    cid = _new_conversation(client, token)
    sent = _send(client, token, cid, "contract question").get_json()
    _has(sent, "ok", "duplicate", "error", "user_message", "assistant_message", "quota")
    _has(sent["assistant_message"], "id", "role", "content", "status", "used_memory", "model", "created_at")

    listing = client.get("/api/conversations").get_json()
    _has(listing["items"][0], "id", "title", "message_count", "last_message_at")
    conversation = client.get(f"/api/conversations/{cid}").get_json()
    _has(conversation, "id", "title", "messages")
    _has(conversation["messages"][0], "id", "role", "content", "status", "used_memory", "created_at")
    _has(client.get("/api/quota").get_json(), "daily", "monthly", "cooldown_remaining_seconds")


def test_admin_contract(web):
    member, mtoken = _member(web, MEMBER_A)
    cid = _new_conversation(member, mtoken)
    _send(member, mtoken, cid, "contract question")

    admin, _ = _member(web, ADMIN, name="Admin")
    overview = admin.get("/api/admin/overview").get_json()
    _has(overview, "users", "today", "month", "memory", "conversations")
    _has(overview["users"], "total", "active", "suspended", "blocked")

    users = admin.get("/api/admin/users").get_json()
    _has(users, "items", "total", "page")
    _has(users["items"][0], "telegram_user_id", "username", "first_name", "access_status",
         "active_sessions", "last_activity_at")

    detail = admin.get(f"/api/admin/users/{MEMBER_A}").get_json()
    _has(detail, "telegram_user_id", "access_status", "daily_request_limit", "daily_token_limit",
         "monthly_request_limit", "monthly_token_limit", "max_response_tokens", "cooldown_seconds",
         "model_override", "admin_notes", "usage", "last_activity_at", "active_sessions", "conversations")
    _has(detail["usage"][0], "period_type", "period_key", "requests", "tokens")

    settings = admin.get("/api/admin/settings").get_json()
    _has(settings, "items", "environment_fields", "secrets_configured", "pending_restart")
    _has(settings["items"][0], "key", "label", "type", "group", "live", "min", "max", "choices", "value", "stored")

    memory = admin.get("/api/admin/memory").get_json()
    _has(memory, "items", "total", "page")
    _has(admin.get("/api/admin/memory/batches").get_json(), "items")
    dedupe = admin.get("/api/admin/memory/dedupe?kind=memory").get_json()
    _has(dedupe, "groups", "removable", "rule", "kind")

    convs = admin.get("/api/admin/conversations").get_json()
    _has(convs["items"][0], "id", "telegram_user_id", "username", "first_name", "title", "message_count",
         "last_message_at")
    transcript = admin.get(f"/api/admin/conversations/{cid}").get_json()
    _has(transcript, "id", "title", "messages")
    _has(transcript["messages"][0], "role", "content", "status", "model", "used_memory", "created_at")

    usage = admin.get("/api/admin/usage?days=7").get_json()
    _has(usage, "daily", "top_users", "ledger", "days")
    _has(usage["daily"][0], "day", "source", "requests", "tokens", "errors")
    _has(usage["top_users"][0], "telegram_user_id", "first_name", "requests", "tokens")

    audit = admin.get("/api/admin/audit").get_json()
    _has(audit, "items", "total", "page")
    _has(audit["items"][0], "created_at", "actor_telegram_id", "action", "target_type", "target_id", "summary")

    logs = admin.get("/api/admin/logs").get_json()
    _has(logs, "items")
    if logs["items"]:
        _has(logs["items"][0], "time", "level", "logger", "message")

    diag = admin.get("/api/admin/diagnostics").get_json()
    _has(diag, "ai", "database", "migrations", "telegram", "secrets_configured", "config", "problems", "warnings")
    _has(diag["ai"], "enabled", "model")
    _has(diag["database"], "available", "latency_ms", "server_version", "missing_tables", "error")
    _has(diag["migrations"], "applied", "pending", "tampered")
    _has(diag["telegram"], "ok", "bot_username")
