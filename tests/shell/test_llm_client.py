"""Tests for llm/client.py (SPEC S2 / S10, S1 remediation)."""
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


# ---------------------------------------------------------------------------
# S1 remediation: new security tests
# ---------------------------------------------------------------------------

def test_retry_after_capped_at_30s():
    """A Retry-After value larger than 30 s is capped to 30 s.

    Tests the cap via injectable chat function that raises LLMError(retry_after=9999)
    on the first two calls then returns success.
    """
    import oracle_agent.llm.client as mod

    call_count = {"n": 0}

    class _FakeClient:
        environment = "external"

        def chat(self, messages, tools=None, **kw):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise LLMError("rate_limit", "slow down", status=429,
                               retryable=True, retry_after=9999.0)
            return mod.ChatResponse(content="ok")

    delays = []
    fake = _FakeClient()
    resp = chat_with_retry(fake, [{"role": "user", "content": "x"}],  # type: ignore
                           max_attempts=5, sleep=delays.append, rng=_FixedRng())
    assert resp.content == "ok"
    # Every Retry-After delay must be at most 30 s (the cap)
    assert all(d <= 30.0 for d in delays), f"Delays exceeded cap: {delays}"
    assert len(delays) == 2  # two rate-limit retries before success


def test_retry_total_budget_enforced(server):
    """Total sleep across all retries must not exceed 120 s."""
    base, H = server
    H.script = {"status": 503, "body": "unavailable"}
    # Set up enough retries that naive scheduling would exceed 120 s.
    # Use a large base_delay with FixedRng (always returns upper bound).
    delays = []
    with pytest.raises(LLMError):
        chat_with_retry(
            LLMClient(base, "m"), [{"role": "user", "content": "x"}],
            max_attempts=20, base_delay=50.0, sleep=delays.append,
            rng=_FixedRng(),
        )
    total = sum(delays)
    assert total <= 120.0, f"Total sleep {total} s exceeded 120 s budget"


def test_http_plaintext_with_api_key_to_nonloopback_refused():
    """LLMClient must refuse construction when api_key + http:// + non-loopback."""
    with pytest.raises(LLMError) as ei:
        LLMClient("http://api.example.com/v1", "m", api_key="sk-secret")
    assert ei.value.kind == "bad_request"
    assert ei.value.retryable is False
    # Message should be actionable
    assert "http" in str(ei.value).lower() or "plaintext" in str(ei.value).lower()


def test_http_plaintext_loopback_with_api_key_allowed(server):
    """LLMClient must ALLOW http:// to loopback even with an api_key."""
    base, H = server   # base is already http://127.0.0.1:PORT/v1
    H.script = {"status": 200, "body": _ok_body("loopback ok")}
    # Should not raise
    client = LLMClient(base, "m", api_key="sk-localtoken")
    resp = client.chat([{"role": "user", "content": "x"}])
    assert resp.content == "loopback ok"


def test_http_plaintext_without_api_key_to_nonloopback_allowed():
    """No api_key means no credential risk — http:// to non-loopback is allowed."""
    # Just construction; we can't connect but it should not raise at construction.
    client = LLMClient("http://api.example.com/v1", "m", api_key=None)
    assert client.base_url == "http://api.example.com/v1"


def test_per_request_guard_local_agent_blocks_swapped_url(server):
    """A local_agent client must refuse to send if the endpoint resolves outside loopback.

    Simulates a DNS-rebind / config-swap by building a local_agent client
    and then patching base_url to an external address before the call.
    """
    base, H = server
    H.script = {"status": 200, "body": _ok_body("should not reach")}
    client = LLMClient(base, "m", environment="local_agent")
    # Swap to a non-loopback URL post-construction (simulates rebinding)
    client.base_url = "http://api.example.com/v1"
    with pytest.raises(LLMError) as ei:
        client.chat([{"role": "user", "content": "x"}])
    assert ei.value.kind == "bad_request"
    assert "local_agent" in str(ei.value) or "non-loopback" in str(ei.value)


def test_per_request_guard_external_client_has_no_constraint(server):
    """An external client is not constrained — it may talk to any host."""
    base, H = server
    H.script = {"status": 200, "body": _ok_body("fine")}
    client = LLMClient(base, "m", environment="external")
    # No per-request guard for external clients — must succeed.
    resp = client.chat([{"role": "user", "content": "x"}])
    assert resp.content == "fine"
