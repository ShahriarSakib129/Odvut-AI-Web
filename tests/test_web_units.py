"""Unit tests for the website layer that need no database or network."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from web import imports, settings_service, telegram_auth
from web.security import SlidingWindowLimiter, origin_allowed, safe_text
from web.timeutil import next_day_reset, next_month_reset, period_keys
from web.quota import Limits, effective_limits
from web.chat_service import ChatError, clean_message, valid_conversation_id, valid_request_id
from web import admin_store
from web.telegram_api import TelegramClient

TOKEN = "123456789:AA-dummy-token-for-tests-only-000000"


def signed(payload: dict, token: str = TOKEN) -> dict:
    data = dict(payload)
    data["hash"] = telegram_auth.expected_hash(data, token)
    return data


# --------------------------------------------------------------------------- #
# Telegram Login Widget verification
# --------------------------------------------------------------------------- #
class TestTelegramLogin:
    NOW = 1_800_000_000.0

    def base(self, **extra):
        payload = {"id": 555000222, "first_name": "Rahim", "username": "rahim_01",
                   "auth_date": int(self.NOW) - 10, "photo_url": "https://t.me/i/userpic/x.jpg"}
        payload.update(extra)
        return payload

    def test_valid_signature_is_accepted(self):
        identity = telegram_auth.verify(signed(self.base()), TOKEN, now_ts=self.NOW, max_age_seconds=300)
        assert identity.telegram_user_id == 555000222
        assert identity.username == "rahim_01"

    def test_wrong_token_is_rejected(self):
        with pytest.raises(telegram_auth.TelegramAuthError) as exc:
            telegram_auth.verify(signed(self.base(), token="999:other"), TOKEN, now_ts=self.NOW,
                                 max_age_seconds=300)
        assert exc.value.code == "bad_signature"

    def test_tampered_field_is_rejected(self):
        payload = signed(self.base())
        payload["id"] = 555000999  # someone changes the user id after signing
        with pytest.raises(telegram_auth.TelegramAuthError) as exc:
            telegram_auth.verify(payload, TOKEN, now_ts=self.NOW, max_age_seconds=300)
        assert exc.value.code == "bad_signature"

    def test_expired_login_is_rejected(self):
        payload = signed(self.base(auth_date=int(self.NOW) - 3600))
        with pytest.raises(telegram_auth.TelegramAuthError) as exc:
            telegram_auth.verify(payload, TOKEN, now_ts=self.NOW, max_age_seconds=300)
        assert exc.value.code == "expired"

    def test_future_timestamp_is_rejected(self):
        payload = signed(self.base(auth_date=int(self.NOW) + 600))
        with pytest.raises(telegram_auth.TelegramAuthError) as exc:
            telegram_auth.verify(payload, TOKEN, now_ts=self.NOW, max_age_seconds=300)
        assert exc.value.code == "future"

    def test_missing_hash_or_bad_types_are_rejected(self):
        with pytest.raises(telegram_auth.TelegramAuthError):
            telegram_auth.verify(self.base(), TOKEN, now_ts=self.NOW, max_age_seconds=300)
        with pytest.raises(telegram_auth.TelegramAuthError):
            telegram_auth.verify(signed(self.base(id="abc")), TOKEN, now_ts=self.NOW, max_age_seconds=300)

    def test_data_check_string_is_sorted_and_excludes_hash(self):
        check = telegram_auth.data_check_string(signed(self.base()))
        lines = check.split("\n")
        keys = [line.split("=", 1)[0] for line in lines]
        assert keys == sorted(keys)
        assert "hash" not in keys

    def test_payload_digest_is_stable_per_login(self):
        a = telegram_auth.verify(signed(self.base()), TOKEN, now_ts=self.NOW, max_age_seconds=300)
        b = telegram_auth.verify(signed(self.base()), TOKEN, now_ts=self.NOW, max_age_seconds=300)
        assert telegram_auth.payload_digest(a) == telegram_auth.payload_digest(b)


# --------------------------------------------------------------------------- #
# Asia/Dhaka periods
# --------------------------------------------------------------------------- #
class TestDhakaPeriods:
    def test_day_rolls_over_at_dhaka_midnight_not_utc(self):
        # 18:30 UTC on 9 Oct is 00:30 on 10 Oct in Dhaka (UTC+6)
        now = datetime(2026, 10, 9, 18, 30, tzinfo=timezone.utc)
        assert period_keys(now) == ("2026-10-10", "2026-10")
        before = datetime(2026, 10, 9, 17, 59, tzinfo=timezone.utc)  # 23:59 Dhaka
        assert period_keys(before)[0] == "2026-10-09"

    def test_month_rolls_over_in_dhaka(self):
        now = datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc)  # 1 Nov 00:30 Dhaka
        assert period_keys(now) == ("2026-11-01", "2026-11")

    def test_reset_points_are_dhaka_midnight(self):
        now = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)  # 15:00 Dhaka
        reset = next_day_reset(now)
        assert reset.hour == 0 and reset.day == 11 and reset.utcoffset().total_seconds() == 6 * 3600
        month_reset = next_month_reset(now)
        assert (month_reset.year, month_reset.month, month_reset.day) == (2026, 11, 1)


# --------------------------------------------------------------------------- #
# quota limit semantics
# --------------------------------------------------------------------------- #
class _S:
    quota_default_daily_requests = 30
    quota_default_monthly_requests = 600
    quota_default_daily_tokens = 0
    quota_default_monthly_tokens = 0
    quota_default_cooldown_seconds = 0
    quota_admin_daily_requests = 300
    quota_admin_monthly_requests = 6000
    quota_admin_daily_tokens = 0
    quota_admin_monthly_tokens = 0


class TestLimits:
    def test_member_defaults_come_from_settings(self):
        limits = effective_limits({}, is_admin=False, settings=_S)
        assert (limits.daily_requests, limits.monthly_requests) == (30, 600)
        assert limits.daily_tokens is None  # 0 means unlimited tokens

    def test_per_user_override_wins_and_zero_blocks_requests(self):
        limits = effective_limits({"daily_request_limit": 0, "cooldown_seconds": 15}, is_admin=False, settings=_S)
        assert limits.daily_requests == 0
        assert limits.cooldown_seconds == 15

    def test_token_limit_zero_is_unlimited(self):
        limits = effective_limits({"daily_token_limit": 0}, is_admin=False, settings=_S)
        assert limits.daily_tokens is None

    def test_admin_gets_safety_caps_not_member_limits(self):
        limits = effective_limits({"daily_request_limit": 0}, is_admin=True, settings=_S)
        assert limits.is_admin and limits.daily_requests == 300 and limits.cooldown_seconds == 0

    def test_admin_cap_of_zero_means_no_cap(self):
        class Z(_S):
            quota_admin_daily_requests = 0
        assert effective_limits({}, is_admin=True, settings=Z).daily_requests is None


# --------------------------------------------------------------------------- #
# security helpers
# --------------------------------------------------------------------------- #
class TestSecurityHelpers:
    def test_sliding_window_limits_and_reports_retry(self):
        limiter = SlidingWindowLimiter()
        results = [limiter.check("k", 2, 60)[0] for _ in range(3)]
        assert results == [True, True, False]
        allowed, retry = limiter.check("k", 2, 60)
        assert not allowed and retry > 0
        assert limiter.check("other", 2, 60)[0] is True

    def test_origin_check_rejects_foreign_origin(self):
        class Req:
            def __init__(self, origin):
                self.headers = {"Origin": origin} if origin else {}
        allowed = {"info-group-ai-bot.example.com"}
        assert origin_allowed(Req(None), allowed)  # non-browser clients rely on CSRF token
        assert origin_allowed(Req("https://info-group-ai-bot.example.com"), allowed)
        assert not origin_allowed(Req("https://evil.example.net"), allowed)

    def test_safe_text_strips_nul_and_limits_length(self):
        assert safe_text("a\x00b  ", 10) == "ab"
        assert len(safe_text("x" * 50, 5)) == 5


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
class TestValidation:
    def test_settings_bounds_and_choices(self):
        clean, errors = settings_service.validate({
            "quota_default_daily_requests": 40, "answer_style": "concise", "ai_enabled": "false",
        })
        assert clean == {"quota_default_daily_requests": 40, "answer_style": "concise", "ai_enabled": False}
        assert errors == {}
        _clean, errors = settings_service.validate({
            "quota_default_daily_requests": 99999, "answer_style": "shouty", "unknown_key": 1,
            "web_max_message_chars": True,
        })
        assert set(errors) == {"quota_default_daily_requests", "answer_style", "unknown_key", "web_max_message_chars"}

    def test_secrets_are_not_editable(self):
        _clean, errors = settings_service.validate({"BOT_TOKEN": "x", "bot_token": "x", "groq_api_key": "y"})
        assert set(errors) == {"BOT_TOKEN", "bot_token", "groq_api_key"}

    def test_system_prompt_length_limit(self):
        _clean, errors = settings_service.validate({"system_prompt_extra": "x" * 1501})
        assert "system_prompt_extra" in errors

    def test_user_update_validation(self):
        clean, errors = admin_store.clean_user_update({
            "access_status": "suspended", "daily_request_limit": 5, "daily_token_limit": None,
            "model_override": "openai/gpt-oss-120b",
        })
        assert clean == {"access_status": "suspended", "daily_request_limit": 5, "daily_token_limit": None,
                         "model_override": "openai/gpt-oss-120b"}
        assert errors == {}
        _clean, errors = admin_store.clean_user_update({
            "access_status": "root", "daily_request_limit": 10**6, "model_override": "rm -rf /",
            "max_response_tokens": 3, "cooldown_seconds": True,
        })
        assert set(errors) == {"access_status", "daily_request_limit", "model_override", "max_response_tokens",
                               "cooldown_seconds"}

    def test_chat_inputs(self):
        assert clean_message("  hello  ", 10) == "hello"
        with pytest.raises(ChatError):
            clean_message("   ", 10)
        with pytest.raises(ChatError) as exc:
            clean_message("x" * 11, 10)
        assert exc.value.status == 413
        assert valid_conversation_id("4F1F4B3C-1111-4222-8333-944455556666")
        with pytest.raises(ChatError):
            valid_conversation_id("1 OR 1=1")
        assert valid_request_id("abcdef12-3456")
        with pytest.raises(ChatError):
            valid_request_id("short")
        with pytest.raises(ChatError):
            valid_request_id("bad id with spaces")


# --------------------------------------------------------------------------- #
# memory import parsing
# --------------------------------------------------------------------------- #
class TestImportParsing:
    def test_json_memory_records(self):
        records, digest = imports.parse_upload(
            "m.json", b'[{"text": "Notice: meeting at 9pm", "date": "2026-10-09"}, {"message": "Second"}]', "memory")
        assert [r["text"] for r in records] == ["Notice: meeting at 9pm", "Second"]
        assert "date" in records[0] and "date" not in records[1]
        assert len(digest) == 64

    def test_txt_memory_one_per_line(self):
        records, _ = imports.parse_upload("notes.txt", "প্রথম লাইন\n\nদ্বিতীয় লাইন\n".encode("utf-8"), "memory")
        assert [r["text"] for r in records] == ["প্রথম লাইন", "দ্বিতীয় লাইন"]

    def test_qa_requires_question_and_answer(self):
        with pytest.raises(imports.ImportProblem):
            imports.parse_upload("qa.json", b'[{"question": "Q only"}]', "qa")
        with pytest.raises(imports.ImportProblem):
            imports.parse_upload("qa.txt", b"hello", "qa")

    @pytest.mark.parametrize("name,data,kind,fragment", [
        ("evil.html", b"<b>x</b>", "memory", "Only .json or .txt"),
        ("big.json", b"[" + b'{"text":"a"},' * 100000 + b'{"text":"a"}]', "memory", "larger than 1 MB"),
        ("bin.json", b'[{"text":"a\x00"}]', "memory", "Binary"),
        ("bad.json", b"[{not json", "memory", "JSON error"),
        ("empty.json", b"", "memory", "empty"),
        ("list.json", b'["just a string"]', "memory", "JSON object"),
        ("latin.txt", "café".encode("latin-1") + b"\xff\xfe", "memory", "UTF-8"),
    ])
    def test_rejections(self, name, data, kind, fragment):
        with pytest.raises(imports.ImportProblem) as exc:
            imports.parse_upload(name, data, kind)
        assert fragment.lower() in str(exc.value).lower()

    def test_record_count_limit(self):
        body = ("[" + ",".join('{"text":"n%d"}' % i for i in range(imports.MAX_RECORDS + 1)) + "]").encode()
        with pytest.raises(imports.ImportProblem):
            imports.parse_upload("many.json", body, "memory")


# --------------------------------------------------------------------------- #
# Telegram Bot API client (real HTTP against a local stub)
# --------------------------------------------------------------------------- #
class TestTelegramClient:
    def test_membership_and_fail_closed(self, telegram_stub):
        client = TelegramClient(TOKEN, telegram_stub.base_url, timeout=3)
        telegram_stub.members[(-1001, 42)] = "member"
        telegram_stub.members[(-1001, 43)] = "left"
        telegram_stub.members[(-1001, 44)] = "restricted"
        assert client.is_group_member(-1001, 42) is True
        assert client.is_group_member(-1001, 43) is False
        assert client.is_group_member(-1001, 44) is False  # restricted without is_member
        assert client.get_me()["username"] == "info_test_bot"

    def test_server_error_raises_unavailable_without_token(self, telegram_stub):
        from web.telegram_api import TelegramUnavailable
        client = TelegramClient(TOKEN, telegram_stub.base_url, timeout=3)
        telegram_stub.fail = True
        with pytest.raises(TelegramUnavailable) as exc:
            client.is_group_member(-1001, 42)
        assert exc.value.kind == "server"
        assert TOKEN not in str(exc.value)

    def test_rate_limit_is_retried_once(self, telegram_stub):
        client = TelegramClient(TOKEN, telegram_stub.base_url, timeout=3)
        telegram_stub.members[(-1001, 45)] = "member"
        telegram_stub.rate_limit_next = 1
        assert client.is_group_member(-1001, 45) is True

    def test_unreachable_host_fails_closed(self):
        from web.telegram_api import TelegramUnavailable
        client = TelegramClient(TOKEN, "http://127.0.0.1:9", timeout=1)
        with pytest.raises(TelegramUnavailable):
            client.is_group_member(-1001, 42)

    def test_unconfigured_client_refuses(self):
        from web.telegram_api import TelegramUnavailable
        with pytest.raises(TelegramUnavailable) as exc:
            TelegramClient("", "https://api.telegram.org").get_me()
        assert exc.value.kind == "not_configured"


# --------------------------------------------------------------------------- #
# static-asset rules: CSP-friendly templates and no HTML-string rendering
# --------------------------------------------------------------------------- #
class TestFrontendRules:
    def _files(self, root, pattern):
        return list((root / "web").rglob(pattern))

    def test_templates_have_no_inline_scripts_or_styles(self, project_root):
        import re
        for path in self._files(project_root, "*.html"):
            text = path.read_text(encoding="utf-8")
            inline_script = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", text)
            assert not inline_script, f"inline script in {path.name}"
            assert "style=" not in text, f"inline style attribute in {path.name} (blocked by CSP)"

    def test_javascript_never_uses_html_sinks(self, project_root):
        forbidden = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function(",
                     "setAttribute(\"style\"", "style: \"")
        for path in self._files(project_root, "*.js"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in text, f"{token} found in {path.name}"

    def test_no_external_resources_except_telegram_widget(self, project_root):
        import re
        for path in self._files(project_root, "*.html"):
            for url in re.findall(r"(?:src|href)=\"(https?://[^\"]+)\"", path.read_text(encoding="utf-8")):
                pytest.fail(f"external resource {url} in {path.name}")
