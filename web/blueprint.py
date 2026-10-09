"""HTTP surface of the website: pages, auth API, chat API, admin API.

Authorisation is decided on the server for every request (see ``web.auth``):
the session cookie identifies the user, and the role/status/membership are
re-derived from the database and configuration. Every admin route is wrapped in
``_api_user(admin=True)``; the admin page checks the same rule.
"""

from __future__ import annotations

import hmac
import secrets
import time
from datetime import datetime, timezone
from functools import wraps
from threading import Lock
from typing import Any, Callable
import uuid

from flask import Blueprint, Flask, Response, g, jsonify, make_response, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

import database
from services import get_services
from utils.logger import get_logger, redact
from web import admin_store, auth, imports, logbuffer, quota, settings_service, store, telegram_auth
from web import security
from web.chat_service import ChatError, ChatService
from web.telegram_api import TelegramUnavailable

logger = get_logger(__name__)
bp = Blueprint("web", __name__, template_folder="templates")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSP = ("default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
       "img-src 'self' data: https:; connect-src 'self'; frame-src https://oauth.telegram.org "
       "https://telegram.org; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

_chat = ChatService()
_limiter = security.SlidingWindowLimiter()

# Login uses the Telegram Login Widget in redirect mode (data-auth-url). The widget's
# data-onauth callback needs eval, which the strict CSP forbids, so the signed fields
# arrive as query parameters on /api/auth/telegram/callback instead. A short-lived
# nonce cookie, set when the login page is served, must match the ``n`` parameter.
# That stops a crafted link (login CSRF) from logging a victim into someone else's account.
TELEGRAM_FIELDS = frozenset({"id", "first_name", "last_name", "username", "photo_url", "auth_date", "hash"})
LOGIN_NONCE_COOKIE = "ig_login_nonce"
LOGIN_NONCE_SECONDS = 600
LOGIN_ERRORS = {
    "login_expired": "This login link has expired or was opened in another browser. Please try again.",
    "login_failed": "Telegram could not verify this login. Please try again.",
    "login_replayed": "This login was already used. Please log in again.",
    "not_member": "Access is limited to members of the INFO GROUP.",
    "access_suspended": "Your access is suspended. Contact the administrator.",
    "access_blocked": "Your access is blocked. Contact the administrator.",
    "membership_unavailable": "Telegram could not confirm your group membership right now. Please try again later.",
    "rate_limited": "Too many login attempts. Please wait a few minutes.",
}


class _LoginDenied(Exception):
    def __init__(self, status: int, message: str, code: str, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after
_pending_lock = Lock()
_pending_imports: dict[str, dict[str, Any]] = {}
_ready_lock = Lock()
_ready_for: int | None = None
_version = "0"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "error", retry_after: int | None = None,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after
        self.details = details or {}


def _settings():
    """Live settings (admin changes replace the object in the services container)."""
    services = get_services()
    _ensure_ready(services)
    return services.settings


def _ensure_ready(services) -> None:
    global _ready_for
    if _ready_for == id(services):
        return
    with _ready_lock:
        if _ready_for == id(services):
            return
        try:
            settings_service.load_into(services)
            _ready_for = id(services)
        except Exception as exc:  # DB not ready yet: retry on the next request
            logger.warning("settings overrides not loaded yet: %s", redact(str(exc))[:160])


def _err(status: int, message: str, code: str = "error", retry_after: int | None = None):
    payload: dict[str, Any] = {"ok": False, "error": message, "code": code}
    if retry_after:
        payload["retry_after"] = int(retry_after)
    response = jsonify(payload)
    response.status_code = status
    if retry_after:
        response.headers["Retry-After"] = str(int(retry_after))
    return response


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "Send a JSON object.", "invalid_body")
    return data


def _uid_param(value: Any) -> int:
    try:
        uid = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "Invalid user id.", "invalid_user") from None
    if not 0 < uid < 2 ** 63:
        raise ApiError(400, "Invalid user id.", "invalid_user")
    return uid


def _origin_ok(settings) -> bool:
    return security.origin_allowed(request, security.allowed_origin_netlocs(request, settings.public_url))


def _api_user(*, admin: bool = False, csrf: bool = True) -> Callable:
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def inner(*args: Any, **kwargs: Any):
            settings = _settings()
            if request.method not in SAFE_METHODS and not _origin_ok(settings):
                return _err(403, "Request origin not allowed.", "origin")
            try:
                user = auth.resolve_user(request, settings)
            except auth.AuthFailure as exc:
                return _err(exc.status, exc.message, exc.code)
            except Exception as exc:  # noqa: BLE001
                if not _is_db_error(exc):
                    raise
                return _err(503, "Database is temporarily unavailable. Please try again.", "db_unavailable")
            if user is None:
                return _err(401, "Please log in with Telegram.", "login_required")
            if admin and not user.is_admin:
                return _err(403, "Only the administrator can do this.", "forbidden")
            if csrf and request.method not in SAFE_METHODS and not auth.check_csrf(request, user):
                return _err(403, "Security token missing or expired. Reload the page.", "csrf")
            g.user = user
            return fn(*args, **kwargs)
        return inner
    return decorator


def _is_db_error(exc: BaseException) -> bool:
    import psycopg2
    return isinstance(exc, (database.DatabaseUnavailable, psycopg2.Error))


def _page_user():
    """Authenticated user for HTML pages, or None (redirect to /login)."""
    settings = _settings()
    try:
        return auth.resolve_user(request, settings)
    except auth.AuthFailure:
        return None
    except Exception as exc:  # noqa: BLE001
        if _is_db_error(exc):
            return None
        raise


def _clear_cookie(response, settings) -> None:
    response.set_cookie(auth.cookie_name(settings), "", max_age=0, expires=0, httponly=True,
                        secure=settings.web_cookie_secure, samesite="Lax", path="/")


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
@bp.get("/")
def chat_page():
    user = _page_user()
    if user is None:
        return redirect(url_for("web.login_page"))
    return render_template("chat.html", user=user.public(), is_admin=user.is_admin,
                           bot_name=_settings().bot_name)


@bp.get("/login")
def login_page():
    settings = _settings()
    if _page_user() is not None:
        return redirect(url_for("web.chat_page"))
    bot_username = settings.bot_username
    error = None
    if not bot_username and settings.bot_token:
        try:
            bot_username = str(auth.telegram_client(settings).get_me().get("username") or "")
        except TelegramUnavailable:
            bot_username = ""
    if not bot_username:
        error = "Telegram login is not configured yet (BOT_USERNAME / BOT_TOKEN)."
    nonce = secrets.token_urlsafe(24)
    if error is None and request.args.get("error") in LOGIN_ERRORS:
        error = LOGIN_ERRORS[request.args["error"]]
    response = make_response(render_template(
        "login.html", bot_username=bot_username or "", error=error, bot_name=settings.bot_name,
        auth_url=url_for("web.telegram_callback", n=nonce)))
    response.set_cookie(LOGIN_NONCE_COOKIE, nonce, max_age=LOGIN_NONCE_SECONDS, httponly=True,
                        secure=settings.web_cookie_secure, samesite="Lax",
                        path="/api/auth/telegram/callback")
    return response


@bp.get("/admin_panel")
def admin_page():
    user = _page_user()
    if user is None:
        return redirect(url_for("web.login_page", next="/admin_panel"))
    if not user.is_admin:
        return render_template("forbidden.html", bot_name=_settings().bot_name), 403
    return render_template("admin.html", user=user.public(), bot_name=_settings().bot_name)


# --------------------------------------------------------------------------- #
# auth API
# --------------------------------------------------------------------------- #
def _authenticate_telegram(payload: dict[str, Any], settings) -> tuple[int, bool, str, str, str, datetime]:
    """Verify a Telegram login payload and decide access. Raises :class:`_LoginDenied`.

    Returns ``(telegram_user_id, is_admin, display_name, session_token, csrf_token, session_expires_at)``.
    """
    ip = security.client_ip(request)
    allowed, retry = _limiter.check(f"login-ip:{ip}", settings.login_rate_limit_per_window, 300)
    if not allowed:
        raise _LoginDenied(429, LOGIN_ERRORS["rate_limited"], "rate_limited", int(retry) + 1)
    try:
        identity = telegram_auth.verify(payload, settings.bot_token, now_ts=time.time(),
                                        max_age_seconds=settings.telegram_auth_max_age_seconds)
    except telegram_auth.TelegramAuthError as exc:
        logger.warning("telegram login rejected: %s ip=%s", exc.code, security.hash_identifier(ip))
        raise _LoginDenied(401, LOGIN_ERRORS["login_failed"], "login_failed") from None

    uid = identity.telegram_user_id
    allowed, retry = _limiter.check(f"login-user:{uid}", max(3, settings.login_rate_limit_per_window), 300)
    if not allowed:
        raise _LoginDenied(429, LOGIN_ERRORS["rate_limited"], "rate_limited", int(retry) + 1)

    # single use: a captured payload cannot be replayed within its validity window
    expires = datetime.fromtimestamp(identity.auth_date + settings.telegram_auth_max_age_seconds + 120,
                                     tz=timezone.utc)
    if not store.consume_login_payload(telegram_auth.payload_digest(identity), uid,
                                       identity.auth_date, expires):
        raise _LoginDenied(401, LOGIN_ERRORS["login_replayed"], "login_replayed")

    admin = auth.is_admin_id(settings, uid)
    if not admin:
        existing = store.get_user(uid)
        status = (existing or {}).get("access_status") or "active"
        if status != "active":
            logger.info("login denied for user %s: status=%s", security.hash_identifier(str(uid)), status)
            code = f"access_{status}" if f"access_{status}" in LOGIN_ERRORS else "access_blocked"
            raise _LoginDenied(403, LOGIN_ERRORS[code], code)
        try:
            member = auth.verify_group_membership(settings, uid)
        except auth.AuthFailure as exc:
            raise _LoginDenied(exc.status, exc.message, exc.code) from None
        if not member:
            logger.info("login denied (not a member): %s", security.hash_identifier(str(uid)))
            raise _LoginDenied(403, LOGIN_ERRORS["not_member"], "not_member")

    row = store.upsert_web_user(telegram_user_id=uid, username=identity.username or "",
                                first_name=identity.first_name or "", last_name=identity.last_name or "",
                                photo_url=identity.photo_url or "")
    token, csrf, expires_at = auth.issue_session(request, settings, telegram_user_id=uid,
                                                 role="admin" if admin else "member")
    logger.info("web login ok: role=%s user=%s", "admin" if admin else "member",
                security.hash_identifier(str(uid)))
    display = (row or {}).get("first_name") or identity.username or str(uid)
    return uid, admin, display, token, csrf, expires_at


@bp.post("/api/auth/telegram")
def api_login():
    """JSON login (kept for programmatic clients). Same verification as the redirect flow."""
    settings = _settings()
    if not settings.web_enabled:
        return _err(503, "The website is disabled.", "web_disabled")
    if not _origin_ok(settings):
        return _err(403, "Request origin not allowed.", "origin")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _err(400, "Invalid login data.", "invalid_login")
    try:
        uid, admin, display, token, csrf, expires_at = _authenticate_telegram(payload, settings)
    except _LoginDenied as denied:
        return _err(denied.status, denied.message, denied.code, denied.retry_after)
    target = "/admin_panel" if (admin and str(request.args.get("next", "")) == "/admin_panel") else "/"
    response = jsonify({
        "ok": True,
        "redirect": target,
        "user": {"telegram_user_id": str(uid), "display_name": display,
                 "role": "admin" if admin else "member"},
        "csrf_token": csrf,
        "expires_at": expires_at.isoformat(),
    })
    _set_session_cookie(response, settings, token)
    return response


def _set_session_cookie(response: Response, settings, token: str) -> None:
    response.set_cookie(auth.cookie_name(settings), token, max_age=settings.web_session_hours * 3600,
                        httponly=True, secure=settings.web_cookie_secure, samesite="Lax", path="/")


def _login_redirect(code: str) -> Response:
    response = redirect(url_for("web.login_page", error=code), code=303)
    response.delete_cookie(LOGIN_NONCE_COOKIE, path="/api/auth/telegram/callback")
    return response


@bp.get("/api/auth/telegram/callback")
def telegram_callback():
    """Telegram Login Widget redirect target. Signed fields arrive as query parameters.

    This is a top-level navigation from oauth.telegram.org, so the Origin check does not
    apply. The nonce cookie, the HMAC, the auth_date window and the single-use check do.
    """
    settings = _settings()
    if not settings.web_enabled:
        return _login_redirect("login_failed")
    args = request.args
    if set(args.keys()) - (TELEGRAM_FIELDS | {"n"}):
        return _login_redirect("login_failed")
    nonce_cookie = request.cookies.get(LOGIN_NONCE_COOKIE, "")
    nonce_arg = args.get("n", "")
    if not nonce_cookie or not nonce_arg or not hmac.compare_digest(nonce_cookie, nonce_arg):
        return _login_redirect("login_expired")
    payload: dict[str, Any] = {key: args.get(key) for key in TELEGRAM_FIELDS if key in args}
    for key in ("id", "auth_date"):
        if key in payload:
            try:
                payload[key] = int(payload[key])
            except (TypeError, ValueError):
                return _login_redirect("login_failed")
    try:
        _uid, _admin, _display, token, _csrf, _expires = _authenticate_telegram(payload, settings)
    except _LoginDenied as denied:
        return _login_redirect(denied.code)
    response = redirect("/", code=303)
    response.delete_cookie(LOGIN_NONCE_COOKIE, path="/api/auth/telegram/callback")
    _set_session_cookie(response, settings, token)
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/api/auth/me")
@_api_user(csrf=False)
def api_me():
    settings = _settings()
    user = g.user
    services = get_services()
    db_user = store.get_user(user.telegram_user_id)
    limits = quota.effective_limits(db_user, is_admin=user.is_admin, settings=settings)
    try:
        usage = quota.snapshot(user.telegram_user_id, limits)
    except Exception as exc:  # noqa: BLE001
        if not _is_db_error(exc):
            raise
        usage = None
    return jsonify({
        "user": user.public(),
        "csrf_token": user.csrf_token,
        "quota": usage,
        "features": {
            "chat_enabled": bool(services.runtime_flag("web_chat_enabled", True)
                                 and services.runtime_flag("ai_enabled", True)),
            "max_message_chars": settings.web_max_message_chars,
        },
    })


@bp.post("/api/auth/logout")
@_api_user()
def api_logout():
    settings = _settings()
    auth.revoke(request, settings, "logout")
    response = jsonify({"ok": True})
    _clear_cookie(response, settings)
    return response


# --------------------------------------------------------------------------- #
# public info (kept from the original "/" response)
# --------------------------------------------------------------------------- #
@bp.get("/api/info")
def api_info():
    settings = _settings()
    return jsonify({
        "status": "ok",
        "service": settings.bot_name,
        "version": _version,
        "model": settings.groq_model,
        "webhook_path": settings.webhook_path,
        "expected_webhook_url": settings.webhook_url or None,
        "diagnose": "GET /diagnose?token=<WEBHOOK_SECRET>",
        "web": {"chat": "/", "login": "/login", "admin": "/admin_panel"},
        "docs": "See README.md or DEPLOY_A_TO_Z.md for setup instructions.",
    })


# --------------------------------------------------------------------------- #
# chat API
# --------------------------------------------------------------------------- #
@bp.get("/api/conversations")
@_api_user(csrf=False)
def api_conversations_list():
    page = max(0, int(request.args.get("page", 0) or 0))
    return jsonify({"items": _chat.list_conversations(g.user, limit=50, offset=page * 50)})


@bp.post("/api/conversations")
@_api_user()
def api_conversations_create():
    data = request.get_json(silent=True) or {}
    title = data.get("title") if isinstance(data, dict) else None
    return jsonify(_chat.create_conversation(g.user, title)), 201


@bp.get("/api/conversations/<cid>")
@_api_user(csrf=False)
def api_conversation_get(cid: str):
    return jsonify(_chat.get_conversation(g.user, cid))


@bp.patch("/api/conversations/<cid>")
@_api_user()
def api_conversation_rename(cid: str):
    return jsonify(_chat.rename(g.user, cid, _body().get("title")))


@bp.delete("/api/conversations/<cid>")
@_api_user()
def api_conversation_delete(cid: str):
    _chat.delete(g.user, cid)
    return jsonify({"ok": True})


@bp.post("/api/conversations/<cid>/messages")
@_api_user()
def api_send(cid: str):
    data = _body()
    return jsonify(_chat.send(g.user, cid, data.get("content"), data.get("request_id")))


@bp.post("/api/conversations/<cid>/regenerate")
@_api_user()
def api_regenerate(cid: str):
    data = _body()
    return jsonify(_chat.regenerate(g.user, cid, data.get("request_id")))


@bp.get("/api/quota")
@_api_user(csrf=False)
def api_quota():
    return jsonify(_chat.quota_snapshot(g.user))


# --------------------------------------------------------------------------- #
# admin API (every route independently requires ADMIN_ID)
# --------------------------------------------------------------------------- #
def _audit(action: str, target_type: str | None, target_id: Any, summary: str,
           details: dict[str, Any] | None = None) -> None:
    store.write_audit(actor_telegram_id=g.user.telegram_user_id, action=action, target_type=target_type,
                      target_id=None if target_id is None else str(target_id), summary=summary,
                      details=details)


@bp.get("/api/admin/overview")
@_api_user(admin=True, csrf=False)
def admin_overview():
    settings = _settings()
    return jsonify(admin_store.overview(settings.group_id))


@bp.get("/api/admin/users")
@_api_user(admin=True, csrf=False)
def admin_users():
    return jsonify(admin_store.list_users(query=request.args.get("q", ""),
                                          status=request.args.get("status", ""),
                                          page=request.args.get("page", 1)))


@bp.get("/api/admin/users/<uid>")
@_api_user(admin=True, csrf=False)
def admin_user_detail(uid: str):
    detail = admin_store.get_user_detail(_uid_param(uid))
    if detail is None:
        raise ApiError(404, "User not found.", "not_found")
    return jsonify(detail)


@bp.patch("/api/admin/users/<uid>")
@_api_user(admin=True)
def admin_user_update(uid: str):
    target = _uid_param(uid)
    data = _body()
    if auth.is_admin_id(_settings(), target) and data.get("access_status") in ("suspended", "blocked"):
        raise ApiError(400, "The administrator account cannot be suspended or blocked.", "protected")
    clean, errors = admin_store.clean_user_update(data)
    if errors:
        return jsonify({"ok": False, "error": "Some values are invalid.", "code": "validation",
                        "fields": errors}), 400
    if not clean:
        raise ApiError(400, "Nothing to change.", "empty")
    before = admin_store.get_user_detail(target)
    if before is None:
        raise ApiError(404, "User not found.", "not_found")
    updated = admin_store.update_user(target, clean, g.user.telegram_user_id)
    revoked = 0
    if clean.get("access_status") in ("suspended", "blocked"):
        revoked = store.revoke_user_sessions(target, f"access_{clean['access_status']}")
    _audit("user.update", "user", target, f"updated user {target}",
           {"changes": clean, "sessions_revoked": revoked})
    return jsonify({"ok": True, "user": updated, "sessions_revoked": revoked})


@bp.post("/api/admin/users/<uid>/reset-quota")
@_api_user(admin=True)
def admin_user_reset_quota(uid: str):
    target = _uid_param(uid)
    scope = str(_body().get("scope") or "")
    if scope not in ("today", "month", "all"):
        raise ApiError(400, "scope must be today, month or all.", "validation")
    removed = quota.reset_counters(target, scope)
    _audit("user.reset_quota", "user", target, f"reset {scope} counters", {"scope": scope, "rows": removed})
    return jsonify({"ok": True, "rows_removed": removed})


@bp.post("/api/admin/users/<uid>/revoke-sessions")
@_api_user(admin=True)
def admin_user_revoke(uid: str):
    target = _uid_param(uid)
    count = store.revoke_user_sessions(target, "admin_revoke")
    _audit("user.revoke_sessions", "user", target, f"revoked {count} session(s)", {"count": count})
    return jsonify({"ok": True, "revoked": count})


@bp.get("/api/admin/settings")
@_api_user(admin=True, csrf=False)
def admin_settings_get():
    services = get_services()
    _ensure_ready(services)
    settings = services.settings
    secrets = {
        "BOT_TOKEN": bool(settings.bot_token),
        "GROQ_API_KEY": bool(settings.groq_api_key),
        "DATABASE_URL": bool(settings.database_url),
        "WEBHOOK_SECRET": bool(settings.webhook_secret),
        "WEB_SECRET_KEY": bool(settings.web_secret_key),
    }
    return jsonify({
        "items": settings_service.describe(services),
        "pending_restart": settings_service.pending_restart(services),
        "secrets_configured": secrets,
        "environment_fields": {
            "GROUP_ID": bool(settings.group_id),
            "ADMIN_ID": bool(settings.web_admin_ids),
            "PUBLIC_URL": settings.public_url or None,
            "WEB_COOKIE_SECURE": settings.web_cookie_secure,
        },
    })


@bp.put("/api/admin/settings")
@_api_user(admin=True)
def admin_settings_put():
    services = get_services()
    _ensure_ready(services)
    data = _body()
    values = data.get("values", data)
    if not isinstance(values, dict) or not values:
        raise ApiError(400, "No settings to change.", "empty")
    clean, errors = settings_service.validate(values)
    if errors:
        return jsonify({"ok": False, "error": "Some values are invalid.", "code": "validation",
                        "fields": errors}), 400
    try:
        result = settings_service.apply_saved(services, clean, g.user.telegram_user_id)
    except database.DatabaseUnavailable:
        return _err(503, "Could not save settings. Please try again.", "db_unavailable")
    _audit("settings.update", "settings", None, f"changed {len(clean)} setting(s)",
           {"keys": sorted(clean.keys()), "restart_required": result["restart_required"]})
    return jsonify({"ok": True, **result, "items": settings_service.describe(services)})


# ---- memory --------------------------------------------------------------- #
def _memory_kind(value: Any) -> str:
    if value not in ("memory", "qa"):
        raise ApiError(400, "kind must be memory or qa.", "validation")
    return value


def _group_chat() -> int:
    settings = _settings()
    if not settings.group_id:
        raise ApiError(409, "GROUP_ID is not configured; memory cannot be scoped.", "not_configured")
    return int(settings.group_id)


@bp.get("/api/admin/memory")
@_api_user(admin=True, csrf=False)
def admin_memory_list():
    kind = _memory_kind(request.args.get("kind", "memory"))
    return jsonify(admin_store.list_memory(kind, _group_chat(), query=request.args.get("q", ""),
                                           source=request.args.get("source", ""),
                                           page=request.args.get("page", 1)))


@bp.post("/api/admin/memory")
@_api_user(admin=True)
def admin_memory_create():
    data = _body()
    kind = _memory_kind(data.get("kind"))
    fields, errors = admin_store.clean_memory(kind, data)
    if errors:
        return jsonify({"ok": False, "error": "Some values are invalid.", "code": "validation",
                        "fields": errors}), 400
    result = admin_store.create_memory(kind, fields, chat_id=_group_chat(), actor_id=g.user.telegram_user_id)
    _audit("memory.create", kind, result.get("batch_id"), f"created {kind} entry",
           {"imported": result["imported"], "duplicates": result["duplicates"]})
    if result["imported"] == 0:
        return jsonify({"ok": False, "error": "This entry already exists.", "code": "duplicate"}), 409
    database.bump_memory_generation()
    return jsonify({"ok": True, **result}), 201


@bp.patch("/api/admin/memory/<kind>/<int:mid>")
@_api_user(admin=True)
def admin_memory_update(kind: str, mid: int):
    kind = _memory_kind(kind)
    fields, errors = admin_store.clean_memory(kind, _body())
    if errors:
        return jsonify({"ok": False, "error": "Some values are invalid.", "code": "validation",
                        "fields": errors}), 400
    if not admin_store.update_memory(kind, mid, fields, chat_id=_group_chat()):
        raise ApiError(404, "Entry not found.", "not_found")
    database.bump_memory_generation()
    _audit("memory.update", kind, mid, f"updated {kind} entry")
    return jsonify({"ok": True})


@bp.delete("/api/admin/memory/<kind>/<int:mid>")
@_api_user(admin=True)
def admin_memory_delete(kind: str, mid: int):
    kind = _memory_kind(kind)
    if not admin_store.delete_memory(kind, mid, chat_id=_group_chat()):
        raise ApiError(404, "Entry not found.", "not_found")
    _audit("memory.delete", kind, mid, f"deleted {kind} entry")
    return jsonify({"ok": True})


@bp.post("/api/admin/memory/import/preview")
@_api_user(admin=True)
def admin_import_preview():
    kind = _memory_kind(request.form.get("kind", "memory"))
    upload = request.files.get("file")
    if upload is None:
        raise ApiError(400, "Choose a file to upload.", "validation")
    data = upload.read(imports.MAX_BYTES + 1)
    try:
        records, digest = imports.parse_upload(upload.filename or "", data, kind)
    except imports.ImportProblem as exc:
        raise ApiError(400, str(exc), "import_invalid") from None
    chat = _group_chat()
    summary = imports.preview(kind, records, chat_id=chat, admin_id=g.user.telegram_user_id)
    token = uuid.uuid4().hex
    now = time.time()
    with _pending_lock:
        for key in [k for k, v in _pending_imports.items() if v["expires"] < now]:
            _pending_imports.pop(key, None)
        if len(_pending_imports) >= 20:
            oldest = min(_pending_imports, key=lambda k: _pending_imports[k]["expires"])
            _pending_imports.pop(oldest, None)
        _pending_imports[token] = {"records": records, "sha": digest, "kind": kind,
                                   "file": (upload.filename or "")[:200], "user": g.user.telegram_user_id,
                                   "chat": chat, "expires": now + 900}
    return jsonify({"ok": True, "token": token, "kind": kind, "file_name": upload.filename,
                    "preview": summary})


@bp.post("/api/admin/memory/import/commit")
@_api_user(admin=True)
def admin_import_commit():
    token = str(_body().get("token") or "")
    with _pending_lock:
        pending = _pending_imports.pop(token, None)
    if pending is None or pending["expires"] < time.time() or pending["user"] != g.user.telegram_user_id:
        raise ApiError(410, "This preview expired. Upload the file again.", "expired")
    result = imports.commit(pending["kind"], pending["records"], chat_id=pending["chat"],
                            admin_id=g.user.telegram_user_id, file_name=pending["file"],
                            file_sha256=pending["sha"])
    database.bump_memory_generation()
    _audit("memory.import", pending["kind"], result["batch_id"],
           f"imported {result['imported']} {pending['kind']} record(s)",
           {"file": pending["file"], "imported": result["imported"], "duplicates": result["duplicates"],
            "failed": result["failed"]})
    return jsonify({"ok": True, **result})


@bp.get("/api/admin/memory/batches")
@_api_user(admin=True, csrf=False)
def admin_import_batches():
    return jsonify({"items": admin_store.import_batches()})


@bp.get("/api/admin/memory/dedupe")
@_api_user(admin=True, csrf=False)
def admin_dedupe_preview():
    kind = _memory_kind(request.args.get("kind", "memory"))
    return jsonify({"kind": kind, **admin_store.dedupe_preview(kind, _group_chat())})


@bp.post("/api/admin/memory/dedupe")
@_api_user(admin=True)
def admin_dedupe_commit():
    data = _body()
    kind = _memory_kind(data.get("kind"))
    chat = _group_chat()
    preview = admin_store.dedupe_preview(kind, chat)
    try:
        expected = int(data.get("expected"))
    except (TypeError, ValueError):
        raise ApiError(400, "Send the number shown in the preview as 'expected'.", "validation") from None
    if expected != preview["removable"]:
        return jsonify({"ok": False, "error": "The data changed since the preview. Review it again.",
                        "code": "stale_preview", "removable": preview["removable"]}), 409
    removed = admin_store.dedupe_commit(kind, chat)
    _audit("memory.dedupe", kind, None, f"removed {removed} duplicate {kind} row(s)", {"removed": removed})
    return jsonify({"ok": True, "removed": removed})


# ---- conversations -------------------------------------------------------- #
@bp.get("/api/admin/conversations")
@_api_user(admin=True, csrf=False)
def admin_conversations():
    uid = request.args.get("uid")
    target = _uid_param(uid) if uid else None
    return jsonify(admin_store.list_conversations(telegram_user_id=target, page=request.args.get("page", 1)))


@bp.get("/api/admin/conversations/<cid>")
@_api_user(admin=True, csrf=False)
def admin_conversation_transcript(cid: str):
    cid = _chat_uuid(cid)
    transcript = admin_store.conversation_transcript(cid)
    if transcript is None:
        raise ApiError(404, "Conversation not found.", "not_found")
    # viewing private chats is itself recorded in the audit trail
    _audit("conversation.view", "conversation", cid, "viewed a website conversation transcript",
           {"user": str(transcript["telegram_user_id"])})
    return jsonify(transcript)


@bp.delete("/api/admin/conversations/<cid>")
@_api_user(admin=True)
def admin_conversation_delete(cid: str):
    cid = _chat_uuid(cid)
    if not admin_store.delete_conversation_admin(cid):
        raise ApiError(404, "Conversation not found.", "not_found")
    _audit("conversation.delete", "conversation", cid, "deleted a website conversation")
    return jsonify({"ok": True})


def _chat_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ApiError(404, "Conversation not found.", "not_found") from None


# ---- usage, audit, logs, diagnostics ------------------------------------- #
@bp.get("/api/admin/usage")
@_api_user(admin=True, csrf=False)
def admin_usage():
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        days = 7
    return jsonify(admin_store.usage_summary(days))


@bp.get("/api/admin/audit")
@_api_user(admin=True, csrf=False)
def admin_audit():
    return jsonify(admin_store.audit_list(page=request.args.get("page", 1)))


@bp.get("/api/admin/logs")
@_api_user(admin=True, csrf=False)
def admin_logs():
    level = request.args.get("level", "INFO").upper()
    if level not in ("INFO", "WARNING", "ERROR"):
        level = "INFO"
    return jsonify({"items": logbuffer.get().recent(limit=200, min_level=level),
                    "note": "Recent application log lines (in memory, redacted). Resets on restart."})


@bp.get("/api/admin/diagnostics")
@_api_user(admin=True, csrf=False)
def admin_diagnostics():
    services = get_services()
    _ensure_ready(services)
    settings = services.settings
    db_health = database.health()
    db_safe = {k: db_health.get(k) for k in ("configured", "available", "latency_ms", "server_version",
                                             "missing_tables")}
    db_safe["error"] = redact(str(db_health.get("error") or ""))[:160]
    migrations: dict[str, Any] = {}
    try:
        import database_migrations
        with database.get_connection() as conn:
            with conn.cursor() as cur:
                state = database_migrations.status(cur)
        migrations = {"applied": state.get("applied", []), "pending": state.get("pending", []),
                      "tampered": state.get("tampered", [])}
    except Exception as exc:  # noqa: BLE001
        migrations = {"error": redact(str(exc))[:160]}
    telegram_info: dict[str, Any]
    try:
        me = auth.telegram_client(settings).get_me()
        telegram_info = {"ok": True, "bot_username": me.get("username")}
    except TelegramUnavailable as exc:
        telegram_info = {"ok": False, "reason": exc.kind}
    return jsonify({
        "version": _version,
        "environment": settings.environment,
        "render_commit": (settings.render_commit or "")[:12] or None,
        "secrets_configured": {
            "BOT_TOKEN": bool(settings.bot_token), "GROQ_API_KEY": bool(settings.groq_api_key),
            "DATABASE_URL": bool(settings.database_url), "WEBHOOK_SECRET": bool(settings.webhook_secret),
            "WEB_SECRET_KEY": bool(settings.web_secret_key),
        },
        "config": {
            "GROUP_ID": bool(settings.group_id), "ADMIN_ID": bool(settings.web_admin_ids),
            "PUBLIC_URL": bool(settings.public_url), "BOT_USERNAME": bool(settings.bot_username),
            "WEB_COOKIE_SECURE": settings.web_cookie_secure, "WEB_ENABLED": settings.web_enabled,
            "DB_AUTO_MIGRATE": settings.db_auto_migrate,
        },
        "database": db_safe,
        "migrations": migrations,
        "telegram": telegram_info,
        "ai": {"enabled": bool(settings.groq_enabled and settings.groq_api_key), "model": settings.groq_model},
        "problems": list(settings.problems),
        "warnings": list(settings.warnings)[:20],
    })


# --------------------------------------------------------------------------- #
# error handling for the web surface (never leak stack traces)
# --------------------------------------------------------------------------- #
@bp.errorhandler(ApiError)
def _handle_api_error(exc: ApiError):
    payload: dict[str, Any] = {"ok": False, "error": exc.message, "code": exc.code}
    if exc.details:
        payload.update(exc.details)
    response = jsonify(payload)
    response.status_code = exc.status
    if exc.retry_after:
        response.headers["Retry-After"] = str(int(exc.retry_after))
    return response


@bp.errorhandler(ChatError)
def _handle_chat_error(exc: ChatError):
    return _err(exc.status, exc.message, exc.code, exc.retry_after)


@bp.errorhandler(HTTPException)
def _handle_http(exc: HTTPException):
    if request.path.startswith("/api/"):
        return _err(exc.code or 500, exc.description or "Request failed.", (exc.name or "error").lower().replace(" ", "_"))
    return exc


@bp.errorhandler(Exception)
def _handle_unexpected(exc: Exception):
    if _is_db_error(exc):
        logger.error("database error in web request: %s", redact(str(exc))[:200])
        if request.path.startswith("/api/"):
            return _err(503, "Database is temporarily unavailable. Please try again.", "db_unavailable")
        return render_template("error.html", message="Temporarily unavailable. Please try again."), 503
    logger.error("web error on %s: %s", request.path, redact(str(exc))[:200], exc_info=True)
    if request.path.startswith("/api/"):
        return _err(500, "Something went wrong. Please try again.", "internal")
    return render_template("error.html", message="Something went wrong."), 500


@bp.after_request
def _web_headers(response: Response) -> Response:
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.path.startswith("/api/") or response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store"
    return response


def register(app: Flask, *, version: str) -> None:
    """Attach the website to the Flask app (called once from ``app.create_app``)."""
    global _version
    _version = version
    logbuffer.install()
    app.register_blueprint(bp)
