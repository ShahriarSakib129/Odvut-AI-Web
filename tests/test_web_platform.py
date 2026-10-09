"""End-to-end tests for the website: real PostgreSQL, real Flask test client,
the stub Telegram Bot API over HTTP, and a counting fake AI provider.

These tests are skipped when no PostgreSQL is reachable (set TEST_DATABASE_URL).
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai.groq_client import AIResult
from tests.web_fakes import FakeGroq

WEB_TOKEN = "123456789:AA-dummy-token-for-tests-only-000000"
WEB_CHAT = -1009900000001
ADMIN = 880000001
MEMBER_A = 880000101
MEMBER_B = 880000102
OUTSIDER = 880000103
ALL_IDS = [ADMIN, MEMBER_A, MEMBER_B, OUTSIDER]


def _sign(payload: dict) -> dict:
    from web import telegram_auth
    data = dict(payload)
    data["hash"] = telegram_auth.expected_hash(data, WEB_TOKEN)
    return data


_LOGIN_COUNTER = iter(range(10**9))


def _login_payload(uid: int, *, auth_date: int | None = None, name: str = "Member") -> dict:
    # Each fresh login gets a distinct auth_date (Telegram's is per-login). Identical payloads are replays.
    fresh = int(time.time()) - (next(_LOGIN_COUNTER) % 200)
    return _sign({"id": uid, "first_name": name, "username": f"user{uid}",
                  "auth_date": auth_date or fresh, "photo_url": "https://t.me/i/userpic/a.jpg"})


@pytest.fixture()
def web(db, telegram_stub):
    """A fully wired website on the test database."""
    import database
    import services
    from app import create_app
    from utils.helpers import RateLimiter
    from config import load_settings
    from web import auth

    env = {
        "BOT_TOKEN": WEB_TOKEN, "GROQ_API_KEY": "gsk_test_dummy_key_0000000000000000",
        "DATABASE_URL": os.environ["DATABASE_URL"], "ADMIN_ID": str(ADMIN), "GROUP_ID": str(WEB_CHAT),
        "PUBLIC_URL": "https://info-group-ai-bot.example.com", "ENVIRONMENT": "test",
        "AUTOSTART_BOT": "false", "TELEGRAM_API_BASE": telegram_stub.base_url,
        "BOT_USERNAME": "info_test_bot", "WEB_SECRET_KEY": "w" * 48, "LOG_LEVEL": "WARNING",
        "WEBHOOK_SECRET": "webtest_webtest_webtest_webtest_1234",
    }
    settings = load_settings(env)
    services.reset_services()
    svc = services.build_services(settings, force=True)
    svc.rate_limiter = RateLimiter(1000, 60.0)
    fake = FakeGroq()
    svc.responder.groq = fake
    svc.groq = fake
    auth.reset_telegram_client()
    from web import blueprint, security
    blueprint._limiter = security.SlidingWindowLimiter()  # login throttles are per process; isolate tests
    flask_app = create_app(settings, bootstrap=False)
    flask_app.config["TESTING"] = True

    from flask.testing import FlaskClient

    class HttpsClient(FlaskClient):
        """Every request is made over https so Secure cookies are sent back."""

        def open(self, *args, **kwargs):
            kwargs.setdefault("base_url", "https://info-group-ai-bot.example.com/")
            return super().open(*args, **kwargs)

    flask_app.test_client_class = HttpsClient

    def new_client():
        return flask_app.test_client()

    ctx = SimpleNamespace(app=flask_app, settings=settings, svc=svc, groq=fake, tg=telegram_stub,
                          new_client=new_client, admin_client=None)
    ctx.tg.members.update({(WEB_CHAT, MEMBER_A): "member", (WEB_CHAT, MEMBER_B): "member",
                           (WEB_CHAT, ADMIN): "administrator"})
    started = time.time()
    yield ctx
    _cleanup(database, started)
    services.reset_services()
    auth.reset_telegram_client()


def _cleanup(database, started: float) -> None:
    ids = tuple(ALL_IDS)
    chat = (WEB_CHAT,)
    statements = [
        ("delete from public.web_conversations where telegram_user_id in %s", (ids,)),
        ("delete from public.web_sessions where telegram_user_id in %s", (ids,)),
        ("delete from public.web_login_replays where telegram_user_id in %s", (ids,)),
        ("delete from public.user_usage_periods where telegram_user_id in %s", (ids,)),
        ("delete from public.ai_request_ledger where telegram_user_id in %s", (ids,)),
        ("delete from public.admin_messages where chat_id in %s", (chat,)),
        ("delete from public.qa_memory where chat_id in %s", (chat,)),
        ("delete from public.ai_usage_log where chat_id in %s", (chat,)),
        ("delete from public.response_cache where chat_id in %s", (chat,)),
        ("delete from public.memory_import_batches where created_by in %s", (ids,)),
        ("delete from public.admin_audit_log where actor_telegram_id in %s", (ids,)),
        ("delete from public.bot_users where telegram_user_id in %s", (ids,)),
        ("delete from public.bot_settings where key like 'web.%%'", ()),
        ("update public.bot_settings set value = 'true' where key in ('ai_enabled', 'web_chat_enabled')", ()),
    ]
    for sql, params in statements:
        database._execute(sql, params)
    database.bump_memory_generation()


def _login(ctx, uid: int, *, name: str = "Member", client=None):
    client = client or ctx.new_client()
    response = client.post("/api/auth/telegram", json=_login_payload(uid, name=name))
    return client, response


def _member(ctx, uid: int, *, name: str = "Member"):
    """Login a member (group membership already set in the stub) and return (client, csrf)."""
    client, response = _login(ctx, uid, name=name)
    assert response.status_code == 200, response.get_json()
    return client, response.get_json()["csrf_token"]


def _csrf_headers(token: str) -> dict:
    return {"X-CSRF-Token": token}


def _db_user(uid: int):
    import database
    return database._fetchone("select * from public.bot_users where telegram_user_id = %s", (uid,))


def _set_status(ctx, uid: int, status: str):
    import database
    database._execute("update public.bot_users set access_status = %s where telegram_user_id = %s", (status, uid))


def _admin_patch_user(ctx, uid: int, payload: dict):
    if _db_user(uid) is None:  # the user row is created on first successful login
        _member(ctx, uid)
    client, token = _member(ctx, ADMIN, name="Admin")
    return client.patch(f"/api/admin/users/{uid}", json=payload, headers=_csrf_headers(token))


def _new_conversation(client, token, title=None):
    response = client.post("/api/conversations", json={"title": title} if title else {}, headers=_csrf_headers(token))
    assert response.status_code == 201, response.get_json()
    return response.get_json()["id"]


def _send(client, token, cid, text, request_id=None):
    return client.post(f"/api/conversations/{cid}/messages",
                       json={"content": text, "request_id": request_id or uuid.uuid4().hex},
                       headers=_csrf_headers(token))


def _memory_counts():
    import database
    row = database._fetchone(
        "select (select count(*) from public.admin_messages where chat_id = %s) as a, "
        "(select count(*) from public.qa_memory where chat_id = %s) as q", (WEB_CHAT, WEB_CHAT))
    return int(row["a"]), int(row["q"])


# --------------------------------------------------------------------------- #
class TestLoginSecurity:
    def test_tampered_payload_is_rejected(self, web):
        payload = _login_payload(MEMBER_A)
        payload["id"] = MEMBER_B
        response = web.new_client().post("/api/auth/telegram", json=payload)
        assert response.status_code == 401
        assert response.get_json()["code"] == "login_failed"

    def test_expired_login_is_rejected(self, web):
        response = web.new_client().post("/api/auth/telegram",
                                         json=_login_payload(MEMBER_A, auth_date=int(time.time()) - 3600))
        assert response.status_code == 401

    def test_replayed_payload_is_rejected(self, web):
        web.tg.members[(WEB_CHAT, MEMBER_A)] = "member"
        payload = _login_payload(MEMBER_A)
        first = web.new_client().post("/api/auth/telegram", json=payload)
        assert first.status_code == 200
        second = web.new_client().post("/api/auth/telegram", json=payload)
        assert second.status_code == 401
        assert second.get_json()["code"] == "login_replayed"

    def test_non_member_is_denied_and_not_stored(self, web):
        response = web.new_client().post("/api/auth/telegram", json=_login_payload(OUTSIDER))
        assert response.status_code == 403
        assert response.get_json()["code"] == "not_member"
        assert _db_user(OUTSIDER) is None

    def test_membership_check_fails_closed_when_telegram_is_down(self, web):
        web.tg.fail = True
        response = web.new_client().post("/api/auth/telegram", json=_login_payload(MEMBER_A))
        assert response.status_code == 503
        assert response.get_json()["code"] == "membership_unavailable"
        assert "Set-Cookie" not in response.headers
        assert WEB_TOKEN not in response.get_data(as_text=True)

    def test_member_login_sets_hardened_cookie(self, web):
        client, response = _login(web, MEMBER_A, name="Rahim")
        assert response.status_code == 200
        cookie = response.headers.get("Set-Cookie", "")
        assert cookie.startswith("__Host-ig_session=")
        for flag in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
            assert flag in cookie
        me = client.get("/api/auth/me").get_json()
        assert me["user"]["role"] == "member"
        assert me["user"]["display_name"] == "Rahim"
        assert len(me["csrf_token"]) >= 40

    def test_admin_logs_in_without_group_membership_check(self, web):
        web.tg.members.pop((WEB_CHAT, ADMIN), None)
        _client, response = _login(web, ADMIN, name="Admin")
        assert response.status_code == 200
        assert response.get_json()["user"]["role"] == "admin"

    def test_login_rate_limit_per_ip(self, web):
        web.svc.settings = replace(web.svc.settings, login_rate_limit_per_window=2)
        client = web.new_client()
        codes = []
        for _ in range(3):
            bad = _login_payload(MEMBER_A)
            bad["hash"] = "0" * 64
            codes.append(client.post("/api/auth/telegram", json=bad).status_code)
        assert codes == [401, 401, 429]

    def test_pages_require_login_and_send_security_headers(self, web):
        client = web.new_client()
        assert client.get("/").status_code == 302
        assert "/login" in client.get("/").headers["Location"]
        assert client.get("/admin_panel").status_code == 302
        page = client.get("/login")
        assert page.status_code == 200
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
        assert page.headers["X-Frame-Options"] == "DENY"
        assert page.headers["X-Content-Type-Options"] == "nosniff"
        assert page.headers["Referrer-Policy"] == "no-referrer"
        assert "info_test_bot" in page.get_data(as_text=True)

    def test_info_endpoint_moved_off_root(self, web):
        payload = web.new_client().get("/api/info").get_json()
        assert payload["service"] == "INFO GROUP AI BOT"
        assert payload["web"]["chat"] == "/"


# --------------------------------------------------------------------------- #
class TestSessions:
    def test_logout_revokes_the_session(self, web):
        client, token = _member(web, MEMBER_A)
        assert client.post("/api/auth/logout", json={}, headers=_csrf_headers(token)).status_code == 200
        assert client.get("/api/auth/me").status_code == 401

    def test_csrf_is_required_for_state_changing_requests(self, web):
        client, token = _member(web, MEMBER_A)
        assert client.post("/api/conversations", json={}).status_code == 403
        assert client.post("/api/conversations", json={}, headers=_csrf_headers("forged")).status_code == 403
        assert client.post("/api/conversations", json={}, headers=_csrf_headers(token)).status_code == 201

    def test_suspended_user_is_signed_out_on_next_request(self, web):
        client, token = _member(web, MEMBER_A)
        _set_status(web, MEMBER_A, "suspended")
        response = client.get("/api/conversations")
        assert response.status_code == 403
        assert response.get_json()["code"] == "access_suspended"
        assert client.get("/api/auth/me").status_code == 401  # the session row is revoked now

    def test_blocked_user_cannot_log_in(self, web):
        _member(web, MEMBER_A)
        _set_status(web, MEMBER_A, "blocked")
        _client, response = _login(web, MEMBER_A)
        assert response.status_code == 403
        assert response.get_json()["code"] == "access_blocked"

    def test_leaving_the_group_ends_the_session_after_recheck(self, web):
        import database
        client, _token = _member(web, MEMBER_A)
        web.tg.members[(WEB_CHAT, MEMBER_A)] = "left"
        database._execute("update public.web_sessions set membership_checked_at = now() - interval '2 hours' "
                          "where telegram_user_id = %s", (MEMBER_A,))
        response = client.get("/api/conversations")
        assert response.status_code == 403
        assert response.get_json()["code"] == "not_member"

    def test_telegram_outage_during_recheck_fails_closed(self, web):
        import database
        client, _token = _member(web, MEMBER_A)
        database._execute("update public.web_sessions set membership_checked_at = now() - interval '2 hours' "
                          "where telegram_user_id = %s", (MEMBER_A,))
        web.tg.fail = True
        response = client.get("/api/conversations")
        assert response.status_code == 503
        assert response.get_json()["code"] == "membership_unavailable"

    def test_every_admin_route_rejects_anonymous_and_members(self, web):
        from app import create_app  # noqa: F401 - ensures routes are registered
        rules = [r for r in web.app.url_map.iter_rules() if r.rule.startswith("/api/admin/")]
        assert len(rules) >= 20
        member, token = _member(web, MEMBER_A)
        anonymous = web.new_client()
        for rule in rules:
            url = rule.rule.replace("<uid>", "1").replace("<cid>", str(uuid.uuid4())).replace(
                "<kind>", "memory").replace("<int:mid>", "1")
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                body = {}
                a = anonymous.open(url, method=method, json=body)
                assert a.status_code == 401, (method, url, a.status_code)
                m = member.open(url, method=method, json=body, headers=_csrf_headers(token))
                assert m.status_code == 403, (method, url, m.status_code)


# --------------------------------------------------------------------------- #
class TestChat:
    def test_send_returns_answer_and_stores_private_history(self, web):
        client, token = _member(web, MEMBER_A)
        cid = _new_conversation(client, token)
        before = _memory_counts()
        response = _send(client, token, cid, "ফর্মে কী কী তথ্য লাগবে?")
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["assistant_message"]["status"] == "complete"
        assert data["assistant_message"]["content"]
        assert data["user_message"]["content"] == "ফর্মে কী কী তথ্য লাগবে?"
        assert data["quota"]["daily"]["requests_used"] == 1
        assert web.groq.calls == 1
        # private chat must never become memory
        assert _memory_counts() == before
        conversation = client.get(f"/api/conversations/{cid}").get_json()
        assert len(conversation["messages"]) == 2
        assert conversation["title"].startswith("ফর্মে")  # first message names the chat

    def test_web_answers_stay_out_of_the_shared_answer_cache(self, web):
        import database
        client, token = _member(web, MEMBER_A)
        cid = _new_conversation(client, token)
        question = "private website question " + uuid.uuid4().hex[:8]
        assert _send(client, token, cid, question).status_code == 200
        assert web.groq.calls == 1
        row = database._fetchone("select count(*) as c from public.response_cache where chat_id = %s "
                                 "and question_text like %s", (WEB_CHAT, "%" + question[-8:] + "%"))
        assert row["c"] == 0
        # the same question asked again still goes to the provider (no cache served to the web)
        assert _send(client, token, cid, question).status_code == 200
        assert web.groq.calls == 2

    def test_same_request_id_is_never_charged_twice(self, web):
        client, token = _member(web, MEMBER_A)
        cid = _new_conversation(client, token)
        rid = "fixedrequest0001"
        first = _send(client, token, cid, "question one", request_id=rid).get_json()
        second = _send(client, token, cid, "question one", request_id=rid).get_json()
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert second["assistant_message"]["content"] == first["assistant_message"]["content"]
        assert web.groq.calls == 1
        quota = client.get("/api/quota").get_json()
        assert quota["daily"]["requests_used"] == 1

    def test_request_limit_is_enforced_before_the_provider(self, web):
        assert _admin_patch_user(web, MEMBER_A, {"daily_request_limit": 1}).status_code == 200
        client, token = _member(web, MEMBER_A)
        cid = _new_conversation(client, token)
        assert _send(client, token, cid, "first").status_code == 200
        blocked = _send(client, token, cid, "second")
        assert blocked.status_code == 429
        assert blocked.get_json()["code"] == "quota_exceeded"
        assert "limit" in blocked.get_json()["error"].lower()
        assert web.groq.calls == 1  # the provider was not called for the blocked request

    def test_zero_request_limit_blocks_ai_access(self, web):
        _admin_patch_user(web, MEMBER_A, {"daily_request_limit": 0})
        client, token = _member(web, MEMBER_A)
        cid = _new_conversation(client, token)
        response = _send(client, token, cid, "hello")
        assert response.status_code == 429
        assert web.groq.calls == 0

    def test_token_budget_is_enforced(self, web):
        _admin_patch_user(web, MEMBER_A, {"daily_token_limit": 100})
        web.groq.tokens = 60
        client, token = _member(web, MEMBER_A)
        cid = _new_conversation(client, token)
        assert _send(client, token, cid, "one").status_code == 200
        assert _send(client, token, cid, "two").status_code == 200  # 60 < 100 at reservation time
        blocked = _send(client, token, cid, "three")
        assert blocked.status_code == 429
        assert web.groq.calls == 2

    def test_cooldown_between_requests(self, web):
        _admin_patch_user(web, MEMBER_A, {"cooldown_seconds": 300})
        client, token = _member(web, MEMBER_A)
        cid = _new_conversation(client, token)
        assert _send(client, token, cid, "one").status_code == 200
        second = _send(client, token, cid, "two")
        assert second.status_code == 429
        assert second.get_json()["code"] == "cooldown"
        assert second.get_json()["retry_after"] > 0
        assert web.groq.calls == 1

    def test_failed_provider_call_is_refunded(self, web):
        web.groq.next_result = AIResult(ok=False, error="upstream 500", error_kind="api_error", status_code=500)
        client, token = _member(web, MEMBER_A)
        cid = _new_conversation(client, token)
        response = _send(client, token, cid, "will fail")
        data = response.get_json()
        assert data["ok"] is False
        assert data["assistant_message"]["status"] == "error"
        assert data["quota"]["daily"]["requests_used"] == 0
        import database
        row = database._fetchone("select status from public.ai_request_ledger where telegram_user_id = %s "
                                 "order by id desc limit 1", (MEMBER_A,))
        assert row["status"] == "failed"

    def test_retry_of_a_failed_question_stores_it_once(self, web):
        client, token = _member(web, MEMBER_A)
        cid = _new_conversation(client, token)
        web.groq.next_result = AIResult(ok=False, error="upstream 500", error_kind="api_error", status_code=500)
        assert _send(client, token, cid, "retry me").get_json()["assistant_message"]["status"] == "error"
        retry = _send(client, token, cid, "retry me").get_json()
        assert retry["ok"] is True
        messages = client.get(f"/api/conversations/{cid}").get_json()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "retry me"
        assert messages[1]["status"] == "complete"
        assert web.groq.calls == 2

    def test_regenerate_replaces_the_last_answer(self, web):
        client, token = _member(web, MEMBER_A)
        cid = _new_conversation(client, token)
        first = _send(client, token, cid, "explain").get_json()["assistant_message"]
        web.groq.tokens = 77
        response = client.post(f"/api/conversations/{cid}/regenerate", json={"request_id": uuid.uuid4().hex},
                               headers=_csrf_headers(token))
        assert response.status_code == 200
        new = response.get_json()["assistant_message"]
        assert new["id"] != first["id"]
        messages = client.get(f"/api/conversations/{cid}").get_json()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[1]["id"] == new["id"]
        assert client.get("/api/quota").get_json()["daily"]["requests_used"] == 2

    def test_other_users_cannot_read_or_change_a_conversation(self, web):
        owner, owner_token = _member(web, MEMBER_A)
        cid = _new_conversation(owner, owner_token, title="private")
        _send(owner, owner_token, cid, "secret question")
        other, other_token = _member(web, MEMBER_B)
        assert other.get(f"/api/conversations/{cid}").status_code == 404
        assert other.patch(f"/api/conversations/{cid}", json={"title": "hacked"},
                           headers=_csrf_headers(other_token)).status_code == 404
        assert other.delete(f"/api/conversations/{cid}", headers=_csrf_headers(other_token)).status_code == 404
        assert _send(other, other_token, cid, "intrude").status_code == 404
        assert other.post(f"/api/conversations/{cid}/regenerate", json={"request_id": uuid.uuid4().hex},
                          headers=_csrf_headers(other_token)).status_code == 404
        assert [c["id"] for c in other.get("/api/conversations").get_json()["items"]] == []
        assert web.groq.calls == 1  # only the owner's question reached the provider

    def test_conversation_crud_and_input_validation(self, web):
        client, token = _member(web, MEMBER_A)
        cid = _new_conversation(client, token)
        renamed = client.patch(f"/api/conversations/{cid}", json={"title": "Exam dates"},
                               headers=_csrf_headers(token)).get_json()
        assert renamed["title"] == "Exam dates"
        assert client.post(f"/api/conversations/{cid}/messages", json={"content": "   ", "request_id": "abcdefgh1234"},
                           headers=_csrf_headers(token)).status_code == 400
        assert client.post(f"/api/conversations/{cid}/messages",
                           json={"content": "x" * 2001, "request_id": "abcdefgh1234"},
                           headers=_csrf_headers(token)).status_code == 413
        assert client.post(f"/api/conversations/{cid}/messages", json={"content": "hi", "request_id": "x"},
                           headers=_csrf_headers(token)).status_code == 400
        assert client.delete(f"/api/conversations/{cid}", headers=_csrf_headers(token)).status_code == 200
        assert client.get(f"/api/conversations/{cid}").status_code == 404
        assert client.get("/api/conversations/not-a-uuid").status_code == 404

    def test_admin_switch_disables_chat(self, web):
        admin, token = _member(web, ADMIN, name="Admin")
        response = admin.put("/api/admin/settings", json={"values": {"web_chat_enabled": False}},
                             headers=_csrf_headers(token))
        assert response.status_code == 200
        client, mtoken = _member(web, MEMBER_A)
        cid = _new_conversation(client, mtoken)
        blocked = _send(client, mtoken, cid, "hello")
        assert blocked.status_code == 503
        assert blocked.get_json()["code"] == "chat_disabled"
        assert web.groq.calls == 0


# --------------------------------------------------------------------------- #
class TestAdminPanel:
    def test_admin_page_is_only_for_admin(self, web):
        member, _ = _member(web, MEMBER_A)
        assert member.get("/admin_panel").status_code == 403
        admin, _ = _member(web, ADMIN, name="Admin")
        page = admin.get("/admin_panel")
        assert page.status_code == 200
        assert b"admin-shell" in page.data

    def test_overview_and_lists_for_admin(self, web):
        admin, _ = _member(web, ADMIN, name="Admin")
        overview = admin.get("/api/admin/overview").get_json()
        assert {"users", "today", "month", "memory", "conversations"} <= set(overview)
        users = admin.get("/api/admin/users?q=user88").get_json()
        assert users["total"] >= 1
        assert admin.get("/api/admin/usage?days=7").status_code == 200
        assert admin.get("/api/admin/audit").status_code == 200
        logs = admin.get("/api/admin/logs?level=INFO").get_json()
        assert "items" in logs

    def test_user_update_is_validated_audited_and_applied(self, web):
        bad = _admin_patch_user(web, MEMBER_A, {"daily_request_limit": 10**6, "model_override": "rm -rf /"})
        assert bad.status_code == 400
        assert set(bad.get_json()["fields"]) == {"daily_request_limit", "model_override"}
        good = _admin_patch_user(web, MEMBER_A, {"daily_request_limit": 7, "admin_notes": "trusted"})
        assert good.status_code == 200
        assert good.get_json()["user"]["daily_request_limit"] == 7
        import database
        row = database._fetchone("select actor_telegram_id, action from public.admin_audit_log "
                                 "where target_id = %s and action = 'user.update' order by id desc limit 1",
                                 (str(MEMBER_A),))
        assert row["actor_telegram_id"] == ADMIN

    def test_admin_cannot_suspend_self(self, web):
        response = _admin_patch_user(web, ADMIN, {"access_status": "suspended"})
        assert response.status_code == 400

    def test_suspension_signs_out_existing_sessions(self, web):
        member, _ = _member(web, MEMBER_A)
        response = _admin_patch_user(web, MEMBER_A, {"access_status": "blocked"})
        assert response.status_code == 200
        assert response.get_json()["sessions_revoked"] >= 1
        assert member.get("/api/auth/me").status_code == 401

    def test_settings_validation_and_restart_reporting(self, web):
        admin, token = _member(web, ADMIN, name="Admin")
        bad = admin.put("/api/admin/settings", json={"values": {"quota_default_daily_requests": 99999,
                                                               "groq_api_key": "leak-me"}},
                        headers=_csrf_headers(token))
        assert bad.status_code == 400
        assert set(bad.get_json()["fields"]) == {"quota_default_daily_requests", "groq_api_key"}
        ok = admin.put("/api/admin/settings", json={"values": {"answer_style": "concise",
                                                              "groq_model": "openai/gpt-oss-20b"}},
                       headers=_csrf_headers(token))
        assert ok.status_code == 200
        body = ok.get_json()
        assert body["applied_now"] == ["answer_style"]
        assert body["restart_required"] == ["groq_model"]
        assert web.svc.settings.answer_style == "concise"
        described = admin.get("/api/admin/settings").get_json()
        assert "groq_model" in described["pending_restart"]

    def test_secrets_never_leave_the_server(self, web):
        admin, token = _member(web, ADMIN, name="Admin")
        texts = [admin.get("/api/admin/settings").get_data(as_text=True),
                 admin.get("/api/admin/diagnostics").get_data(as_text=True),
                 admin.get("/api/admin/logs").get_data(as_text=True)]
        for text in texts:
            assert WEB_TOKEN not in text
            assert "gsk_test_dummy_key" not in text
            assert "w" * 48 not in text
        diagnostics = admin.get("/api/admin/diagnostics").get_json()
        assert diagnostics["secrets_configured"]["BOT_TOKEN"] is True
        assert diagnostics["telegram"]["ok"] is True
        assert diagnostics["telegram"]["bot_username"] == "info_test_bot"

    def test_memory_crud_and_duplicate_protection(self, web):
        admin, token = _member(web, ADMIN, name="Admin")
        text = "Office closes 6pm on Friday " + uuid.uuid4().hex[:6]
        created = admin.post("/api/admin/memory", json={"kind": "memory", "text": text},
                             headers=_csrf_headers(token))
        assert created.status_code == 201
        duplicate = admin.post("/api/admin/memory", json={"kind": "memory", "text": text},
                               headers=_csrf_headers(token))
        assert duplicate.status_code == 409
        listing = admin.get("/api/admin/memory?kind=memory&q=Office").get_json()
        item = next(i for i in listing["items"] if i["text"] == text)
        updated = admin.patch(f"/api/admin/memory/memory/{item['id']}", json={"text": text + " updated"},
                              headers=_csrf_headers(token))
        assert updated.status_code == 200
        assert admin.delete(f"/api/admin/memory/memory/{item['id']}", headers=_csrf_headers(token)).status_code == 200
        assert admin.delete(f"/api/admin/memory/memory/{item['id']}", headers=_csrf_headers(token)).status_code == 404

    def test_import_preview_then_commit_is_idempotent(self, web):
        admin, token = _member(web, ADMIN, name="Admin")
        marker = uuid.uuid4().hex[:8]
        payload = json.dumps([{"text": f"Rule one {marker}"}, {"text": f"Rule two {marker}"},
                              {"text": f"Rule three {marker}"}]).encode("utf-8")

        def upload():
            return admin.post("/api/admin/memory/import/preview", headers=_csrf_headers(token),
                              data={"kind": "memory", "file": (io.BytesIO(payload), "rules.json")},
                              content_type="multipart/form-data")

        first = upload()
        assert first.status_code == 200
        preview = first.get_json()["preview"]
        assert preview["total"] == 3 and preview["new"] == 3 and preview["duplicates"] == 0
        commit = admin.post("/api/admin/memory/import/commit", json={"token": first.get_json()["token"]},
                            headers=_csrf_headers(token))
        assert commit.status_code == 200
        assert commit.get_json()["imported"] == 3
        second = upload().get_json()["preview"]
        assert second["new"] == 0 and second["duplicates"] == 3
        replay = admin.post("/api/admin/memory/import/commit", json={"token": first.get_json()["token"]},
                            headers=_csrf_headers(token))
        assert replay.status_code == 410  # tokens are single use

    def test_import_rejects_bad_files(self, web):
        admin, token = _member(web, ADMIN, name="Admin")
        response = admin.post("/api/admin/memory/import/preview", headers=_csrf_headers(token),
                              data={"kind": "memory", "file": (io.BytesIO(b"<html>"), "evil.html")},
                              content_type="multipart/form-data")
        assert response.status_code == 400
        assert response.get_json()["code"] == "import_invalid"

    def test_dedupe_requires_matching_preview(self, web):
        admin, token = _member(web, ADMIN, name="Admin")
        preview = admin.get("/api/admin/memory/dedupe?kind=memory").get_json()
        stale = admin.post("/api/admin/memory/dedupe", json={"kind": "memory", "expected": preview["removable"] + 5},
                           headers=_csrf_headers(token))
        assert stale.status_code == 409
        ok = admin.post("/api/admin/memory/dedupe", json={"kind": "memory", "expected": preview["removable"]},
                        headers=_csrf_headers(token))
        assert ok.status_code == 200

    def test_viewing_a_transcript_is_audited(self, web):
        client, token = _member(web, MEMBER_A)
        cid = _new_conversation(client, token)
        _send(client, token, cid, "what is the fee?")
        admin, _ = _member(web, ADMIN, name="Admin")
        response = admin.get(f"/api/admin/conversations/{cid}")
        assert response.status_code == 200
        assert response.get_json()["messages"][0]["content"] == "what is the fee?"
        import database
        row = database._fetchone("select count(*) as c from public.admin_audit_log where action = "
                                 "'conversation.view' and target_id = %s", (cid,))
        assert row["c"] == 1

    def test_admin_api_rejects_arbitrary_sql_shaped_input(self, web):
        admin, token = _member(web, ADMIN, name="Admin")
        response = admin.get("/api/admin/users?q=' OR 1=1 --&status=active; drop table bot_users")
        assert response.status_code == 200
        assert response.get_json()["total"] >= 0
        assert admin.get("/api/admin/memory?kind=bot_users").status_code == 400
        assert admin.get("/api/admin/users/abc").status_code == 400


# --------------------------------------------------------------------------- #
class TestMigrations:
    def test_migration_0002_is_applied_and_rerun_is_a_no_op(self, web):
        import database
        import database_migrations
        with database.get_connection() as conn:
            with conn.cursor() as cur:
                state = database_migrations.status(cur)
        assert "0002" in state["applied"]
        assert state["pending"] == []
        assert state["checksum_mismatch"] == []

    def test_new_tables_have_rls_enabled(self, web):
        import database
        tables = ["memory_import_batches", "ai_request_ledger", "user_usage_periods", "web_sessions",
                  "web_login_replays", "web_conversations", "web_messages", "admin_audit_log"]
        rows = database._fetchall(
            "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relname = any(%s)", (tables,))
        assert {r["relname"] for r in rows} == set(tables)
        assert all(r["relrowsecurity"] for r in rows)

    def test_rollback_drops_only_new_web_tables(self, project_root):
        import re
        down = (project_root / "database/migrations/0002_web_platform.down.sql").read_text(encoding="utf-8")
        dropped = set(re.findall(r"drop\s+table\s+(?:if\s+exists\s+)?(?:public\.)?(\w+)", down, re.I))
        protected = {"bot_users", "admin_messages", "qa_memory", "ai_usage_log", "message_logs", "bot_settings",
                     "schema_migrations"}
        assert dropped and not (dropped & protected)
        assert "alter table" not in down.lower() or "drop column" not in down.lower()


# --------------------------------------------------------------------------- #
class TestTelegramRedirectLogin:
    """The Login Widget redirects to /api/auth/telegram/callback (no eval, so the CSP holds)."""

    def _login_page(self, client):
        import html as html_lib
        from urllib.parse import parse_qs, urlsplit
        page = client.get("/login")
        assert page.status_code == 200
        text = page.get_data(as_text=True)
        match = re.search(r'data-auth-url="([^"]+)"', text)
        assert match, "login page must carry the widget redirect URL"
        auth_url = html_lib.unescape(match.group(1))
        nonce = parse_qs(urlsplit(auth_url).query)["n"][0]
        return page, nonce

    def _fields(self, uid, *, auth_date=None, name="Member"):
        data = _login_payload(uid, auth_date=auth_date, name=name)
        return {key: str(value) for key, value in data.items()}

    def test_login_page_binds_a_short_lived_nonce_cookie(self, web):
        page, nonce = self._login_page(web.new_client())
        cookie = page.headers.get("Set-Cookie", "")
        assert f"ig_login_nonce={nonce}" in cookie
        assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" in cookie
        assert "Path=/api/auth/telegram/callback" in cookie
        assert "eval" not in page.headers.get("Content-Security-Policy", "")
        assert "unsafe-eval" not in page.headers.get("Content-Security-Policy", "")

    def test_valid_callback_logs_the_member_in(self, web):
        from urllib.parse import urlencode
        client = web.new_client()
        _page, nonce = self._login_page(client)
        response = client.get(f"/api/auth/telegram/callback?n={nonce}&{urlencode(self._fields(MEMBER_A))}")
        assert response.status_code == 303
        assert response.headers["Location"] == "/"
        session_cookies = [c for c in response.headers.getlist("Set-Cookie") if c.startswith("__Host-ig_session=")]
        assert session_cookies and "HttpOnly" in session_cookies[0] and "Secure" in session_cookies[0]
        me = client.get("/api/auth/me")
        assert me.status_code == 200 and me.get_json()["user"]["role"] == "member"

    def test_callback_without_the_login_page_cookie_is_refused(self, web):
        from urllib.parse import urlencode
        client = web.new_client()
        response = client.get(f"/api/auth/telegram/callback?n=whatever&{urlencode(self._fields(MEMBER_A))}")
        assert response.status_code == 303
        assert "/login?error=login_expired" in response.headers["Location"]
        assert client.get("/api/auth/me").status_code == 401

    def test_crafted_link_from_another_browser_cannot_log_the_victim_in(self, web):
        """Login CSRF: an attacker logs in as themselves and sends the victim their link."""
        from urllib.parse import urlencode
        attacker = web.new_client()
        _page, attacker_nonce = self._login_page(attacker)
        link_query = f"n={attacker_nonce}&{urlencode(self._fields(MEMBER_B, name='Attacker'))}"
        victim = web.new_client()
        self._login_page(victim)  # the victim's browser has its own nonce cookie
        response = victim.get(f"/api/auth/telegram/callback?{link_query}")
        assert "/login?error=login_expired" in response.headers["Location"]
        assert victim.get("/api/auth/me").status_code == 401

    def test_tampered_callback_is_refused(self, web):
        from urllib.parse import urlencode
        client = web.new_client()
        _page, nonce = self._login_page(client)
        fields = self._fields(MEMBER_A)
        fields["id"] = str(MEMBER_B)
        response = client.get(f"/api/auth/telegram/callback?n={nonce}&{urlencode(fields)}")
        assert "/login?error=login_failed" in response.headers["Location"]
        assert client.get("/api/auth/me").status_code == 401

    def test_unknown_parameters_are_refused(self, web):
        from urllib.parse import urlencode
        client = web.new_client()
        _page, nonce = self._login_page(client)
        response = client.get(f"/api/auth/telegram/callback?n={nonce}&role=admin&{urlencode(self._fields(MEMBER_A))}")
        assert "/login?error=login_failed" in response.headers["Location"]
        assert client.get("/api/auth/me").status_code == 401

    def test_replayed_callback_is_refused(self, web):
        from urllib.parse import urlencode
        client = web.new_client()
        _page, nonce = self._login_page(client)
        query = f"n={nonce}&{urlencode(self._fields(MEMBER_A))}"
        assert client.get(f"/api/auth/telegram/callback?{query}").status_code == 303
        # the nonce cookie was cleared after success; put it back to test the replay check alone
        client.set_cookie("ig_login_nonce", nonce, domain="info-group-ai-bot.example.com",
                          path="/api/auth/telegram/callback")
        second = client.get(f"/api/auth/telegram/callback?{query}")
        assert "/login?error=login_replayed" in second.headers["Location"]

    def test_non_member_is_redirected_with_a_fixed_message(self, web):
        from urllib.parse import urlencode
        client = web.new_client()
        _page, nonce = self._login_page(client)
        response = client.get(f"/api/auth/telegram/callback?n={nonce}&{urlencode(self._fields(OUTSIDER))}")
        assert "/login?error=not_member" in response.headers["Location"]
        assert _db_user(OUTSIDER) is None
        page = client.get(response.headers["Location"])
        assert "members of the INFO GROUP" in page.get_data(as_text=True)

    def test_error_parameter_is_never_echoed(self, web):
        page = web.new_client().get("/login?error=%3Cscript%3Ealert(1)%3C%2Fscript%3E")
        text = page.get_data(as_text=True)
        assert "<script>alert(1)" not in text
        assert "alert(1)" not in text


class TestWebRetention:
    """WEB_CHAT_RETENTION_DAYS and expired auth rows are purged by the shared maintenance job."""

    def test_old_conversations_are_purged_and_recent_ones_kept(self, web):
        import database
        import services
        client, token = _member(web, MEMBER_A)
        keep = _new_conversation(client, token, title="recent")
        old = _new_conversation(client, token, title="ancient")
        database._execute(
            "update public.web_conversations set created_at = now() - interval '400 days', "
            "last_message_at = now() - interval '400 days' where id = %(cid)s::uuid", {"cid": old})
        result = services.run_maintenance(services.get_services(), force=True)
        assert result is not None and "web_error" not in result
        assert result["web_purged_conversations"] >= 1
        assert client.get(f"/api/conversations/{old}").status_code == 404
        assert client.get(f"/api/conversations/{keep}").status_code == 200
