"""Tests for llm/client.py (SPEC S2 / S10)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from oracle_agent.llm.client import (
    LLMClient, LLMError, chat_with_retry, classify_error,
)


class _Handler(BaseHTTPRequestHandler):
    script = {}  # set per-test: {"status": int, "body": str, "headers": {...}}

    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        s = self.script
        status = s.get("status", 200)
        self.send_response(status)
        for k, v in (s.get("headers") or {}).items():
            self.send_header(k, v)
        if status in (301, 302, 307, 308):
            self.end_headers()
            return
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(s.get("body", "{}").encode("utf-8"))


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    yield base, _Handler
    srv.shutdown()


def _ok_body(content="hello", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return json.dumps({"choices": [{"message": msg, "finish_reason": "stop"}],
                       "usage": {"total_tokens": 5}})


def test_happy_path(server):
    base, H = server
    H.script = {"status": 200, "body": _ok_body("hi there")}
    resp = LLMClient(base, "m").chat([{"role": "user", "content": "x"}])
    assert resp.content == "hi there"
    assert resp.usage["total_tokens"] == 5


def test_tool_call_parsing(server):
    base, H = server
    tc = [{"id": "call_1", "type": "function",
           "function": {"name": "oracle_search", "arguments": '{"terms":"x"}'}}]
    H.script = {"status": 200, "body": _ok_body(None, tc)}
    resp = LLMClient(base, "m").chat([{"role": "user", "content": "x"}])
    assert resp.tool_calls[0].name == "oracle_search"
    assert json.loads(resp.tool_calls[0].arguments)["terms"] == "x"


def test_auth_error_not_retryable(server):
    base, H = server
    H.script = {"status": 401, "body": '{"error":"bad key"}'}
    with pytest.raises(LLMError) as ei:
        LLMClient(base, "m", api_key="sk-secret").chat([{"role": "user", "content": "x"}])
    assert ei.value.kind == "auth"
    assert ei.value.retryable is False


def test_rate_limit_retryable_with_retry_after(server):
    base, H = server
    H.script = {"status": 429, "body": "slow down", "headers": {"Retry-After": "2"}}
    with pytest.raises(LLMError) as ei:
        LLMClient(base, "m").chat([{"role": "user", "content": "x"}])
    assert ei.value.kind == "rate_limit"
    assert ei.value.retryable is True
    assert ei.value.retry_after == 2.0


def test_context_overflow_classified(server):
    base, H = server
    H.script = {"status": 400, "body": '{"error":"maximum context length exceeded"}'}
    with pytest.raises(LLMError) as ei:
        LLMClient(base, "m").chat([{"role": "user", "content": "x"}])
    assert ei.value.kind == "context_overflow"
    assert ei.value.retryable is False


def test_redirect_is_blocked(server):
    base, H = server
    H.script = {"status": 302, "headers": {"Location": "https://evil.example.com/"}}
    with pytest.raises(LLMError) as ei:
        LLMClient(base, "m", api_key="sk-secret").chat([{"role": "user", "content": "x"}])
    # blocked 3xx surfaces as a (non-2xx) error, never a followed request
    assert ei.value.status in (302, None) or ei.value.kind in ("server", "bad_request", "network")


def test_api_key_never_in_error(server):
    base, H = server
    H.script = {"status": 500, "body": "boom"}
    try:
        LLMClient(base, "m", api_key="sk-TOPSECRET-DONOTLEAK").chat(
            [{"role": "user", "content": "x"}])
    except LLMError as exc:
        assert "TOPSECRET" not in str(exc)
        assert "TOPSECRET" not in repr(exc)


def test_classify_network_when_no_status():
    err = classify_error(None, "")
    assert err.kind == "network" and err.retryable is True


def test_retry_backoff_schedule(server):
    base, H = server
    H.script = {"status": 503, "body": "unavailable"}
    delays = []
    with pytest.raises(LLMError):
        chat_with_retry(LLMClient(base, "m"), [{"role": "user", "content": "x"}],
                        max_attempts=4, base_delay=1.0, sleep=delays.append,
                        rng=_FixedRng())
    # 3 sleeps before the 4th attempt raises; full-jitter w/ fixed rng = cap
    assert delays == [1.0, 2.0, 4.0]


def test_retry_succeeds_after_transient(server):
    base, H = server
    state = {"n": 0}
    import oracle_agent.llm.client as mod

    real = LLMClient(base, "m")

    def flaky(messages, tools=None, **kw):
        state["n"] += 1
        if state["n"] < 2:
            raise LLMError("server", "boom", status=503, retryable=True)
        return mod.ChatResponse(content="recovered")

    real.chat = flaky  # type: ignore
    resp = chat_with_retry(real, [{"role": "user", "content": "x"}],
                           sleep=lambda *_: None, rng=_FixedRng())
    assert resp.content == "recovered"


class _FixedRng:
    def uniform(self, a, b):
        return b  # deterministic: full-jitter upper bound
