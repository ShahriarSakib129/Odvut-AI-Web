"""Test doubles for the website tests.

* :class:`FakeTelegramAPI` is a real HTTP server on 127.0.0.1 that answers the two
  Bot API methods the website uses (``getMe`` and ``getChatMember``), so the
  production HTTP client is exercised end to end.
* :class:`FakeGroq` counts provider calls, which lets tests prove that a
  quota-blocked or duplicate request never reaches the AI provider.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ai.groq_client import AIResult


class FakeTelegramAPI:
    def __init__(self, token: str) -> None:
        self.token = token
        self.members: dict[tuple[int, int], str] = {}
        self.fail = False
        self.rate_limit_next = 0
        self.calls: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url = ""

    def start(self) -> None:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # keep test output quiet
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                prefix = f"/bot{stub.token}/"
                if not self.path.startswith(prefix):
                    return self._send(404, {"ok": False, "description": "Not Found"})
                method = self.path[len(prefix):]
                stub.calls.append(method)
                if stub.fail:
                    return self._send(500, {"ok": False, "description": "internal"})
                if stub.rate_limit_next > 0:
                    stub.rate_limit_next -= 1
                    return self._send(429, {"ok": False, "parameters": {"retry_after": 0}})
                if method == "getMe":
                    return self._send(200, {"ok": True, "result": {
                        "id": 123456789, "is_bot": True, "first_name": "Info", "username": "info_test_bot"}})
                if method == "getChatMember":
                    key = (int(body.get("chat_id")), int(body.get("user_id")))
                    status = stub.members.get(key, "left")
                    result = {"status": status, "user": {"id": key[1], "is_bot": False, "first_name": "x"}}
                    if status == "restricted":
                        result["is_member"] = False
                    return self._send(200, {"ok": True, "result": result})
                return self._send(400, {"ok": False, "description": "unsupported in test stub"})

            def _send(self, code: int, payload: dict) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class FakeGroq:
    """Counts calls. ``enabled`` mirrors the real client's property."""

    enabled = True

    def __init__(self, tokens: int = 50) -> None:
        self.calls = 0
        self.tokens = tokens
        self.next_result: AIResult | None = None
        self.last_kwargs: dict[str, Any] = {}

    def chat(self, messages, **kwargs) -> AIResult:  # noqa: ANN001 - duck-typed client
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        if self.next_result is not None:
            result, self.next_result = self.next_result, None
            return result
        return AIResult(ok=True, text="ধন্যবাদ, এটি একটি পরীক্ষার উত্তর।", model="test-model",
                        prompt_tokens=10, completion_tokens=max(0, self.tokens - 10),
                        total_tokens=self.tokens, latency_ms=5)
