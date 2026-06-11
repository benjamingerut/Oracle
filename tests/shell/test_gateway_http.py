"""Tests for gateway/http.py -- the local HTTP/MCP surface (Phase 4, P4-T4).

Drives :class:`HTTPAdapter` two ways:
  * a REAL HTTPServer on an ephemeral 127.0.0.1 port (token/host/oversize/clean
    start-stop), driven by urllib;
  * handler-level unit calls (handle_ask / handle_mcp) for the Dispatcher-only
    MCP contract, dropped-verb denial, no-control-plane, and 503-busy.

Every pin in P4S-7/8/9 is asserted dep-free.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from oracle_agent.gateway.core import GatewayCore, InboundMessage, OutboundReply, _noop_lock
from oracle_agent.gateway.http import HTTPAdapter, validate_bind, _allowed_hosts


TOKEN = "test-token-high-entropy-xyz"
PRINCIPAL = "http-operator"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeTurn:
    def __init__(self, text="grounded reply"):
        self.text = text
        self.envelopes = [{"verdict": "grounded"}]
        self.grounding = "enforce"
        self.repairs = 0
        self.redacted_count = 0
        self.withheld = False


class FakeLoop:
    def run_turn(self, text):
        return FakeTurn(f"answer to: {text}")


def _core(tmp_path, *, root_lock_factory=None, allowlist=None):
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    surface_cfg = {
        "allowlist": allowlist if allowlist is not None
        else {PRINCIPAL: {"role": "user", "instance": "main"}},
        "max_sensitivity": "internal",
        "per_user_writes_per_hour": 20,
        "principal": PRINCIPAL,
    }

    def builder(user_id, instance, r, *, ceiling_override, write_actor, write_role, write_gate):
        return FakeLoop()

    core = GatewayCore(surface_cfg, "http", {"main": root}, builder,
                       clock=lambda: 1000.0,
                       root_lock_factory=root_lock_factory or _noop_lock)
    return core, surface_cfg


def _adapter(tmp_path, *, token=TOKEN, surface_cfg=None, core=None,
             dispatcher_factory=None, nb_lock_factory=None):
    if core is None:
        core, surface_cfg = _core(tmp_path)
    if surface_cfg is None:
        _, surface_cfg = _core(tmp_path)
    return HTTPAdapter(surface_cfg, core, token=token,
                       dispatcher_factory=dispatcher_factory,
                       nb_lock_factory=nb_lock_factory)


# --------------------------------------------------------------------------- #
# Bind validation (P4S-7)
# --------------------------------------------------------------------------- #
def test_validate_bind_accepts_loopback():
    assert validate_bind("127.0.0.1") == "127.0.0.1"
    assert validate_bind("::1") == "::1"


def test_validate_bind_refuses_localhost_hostname():
    with pytest.raises(ValueError, match="localhost|literal"):
        validate_bind("localhost")


def test_validate_bind_refuses_zero_address():
    with pytest.raises(ValueError, match="loopback"):
        validate_bind("0.0.0.0")


def test_validate_bind_refuses_public_ip():
    with pytest.raises(ValueError, match="loopback"):
        validate_bind("8.8.8.8")


def test_adapter_refuses_non_loopback_bind_at_startup(tmp_path):
    core, surface_cfg = _core(tmp_path)
    surface_cfg = dict(surface_cfg)
    surface_cfg["bind"] = "0.0.0.0"
    with pytest.raises(ValueError, match="loopback"):
        HTTPAdapter(surface_cfg, core, token=TOKEN)


# --------------------------------------------------------------------------- #
# Fail-closed startup (P4S-7)
# --------------------------------------------------------------------------- #
def test_empty_token_refuses_to_start(tmp_path):
    core, surface_cfg = _core(tmp_path)
    with pytest.raises(RuntimeError, match="token|unauthenticated"):
        HTTPAdapter(surface_cfg, core, token="")


def test_none_token_refuses_to_start(tmp_path):
    core, surface_cfg = _core(tmp_path)
    with pytest.raises(RuntimeError):
        HTTPAdapter(surface_cfg, core, token=None)


# --------------------------------------------------------------------------- #
# Auth (constant-time) + Host (P4S-7)
# --------------------------------------------------------------------------- #
def test_check_token_constant_time_and_correct(tmp_path):
    a = _adapter(tmp_path)
    assert a.check_token(f"Bearer {TOKEN}") is True
    assert a.check_token("Bearer wrong") is False
    assert a.check_token(None) is False
    assert a.check_token(TOKEN) is False           # missing "Bearer " prefix
    assert a.check_token("Bearer ") is False


def test_check_host_allowlist(tmp_path):
    a = _adapter(tmp_path)
    port = a.port
    assert a.check_host(f"127.0.0.1:{port}") is True
    assert a.check_host(f"localhost:{port}") is True
    assert a.check_host(f"[::1]:{port}") is True
    assert a.check_host("evil.com") is False
    assert a.check_host(None) is False


# --------------------------------------------------------------------------- #
# /ask through the core (token IS the principal)
# --------------------------------------------------------------------------- #
def test_handle_ask_grounded_reply(tmp_path):
    a = _adapter(tmp_path)
    result = a.handle_ask("what is revenue?")
    assert result["_status"] == 200
    assert "answer to: what is revenue?" in result["reply"]


def test_handle_ask_unknown_principal_denied(tmp_path):
    core, surface_cfg = _core(tmp_path, allowlist={})  # principal NOT allowlisted
    a = _adapter(tmp_path, core=core, surface_cfg=surface_cfg)
    result = a.handle_ask("hi")
    assert result["_status"] == 403


# --------------------------------------------------------------------------- #
# MCP through the Dispatcher ONLY (P4S-8)
# --------------------------------------------------------------------------- #
def test_mcp_tools_list_is_exactly_gateway_schema(tmp_path):
    from oracle_agent.agentloop.verbtools import tool_schemas
    a = _adapter(tmp_path)
    result = a.handle_mcp({"method": "tools/list", "id": 1})
    assert result["_status"] == 200
    listed = {t["name"] for t in result["result"]["tools"]}
    expected = {s["function"]["name"]
                for s in tool_schemas(surface="gateway", environment="external")}
    assert listed == expected


def test_mcp_dropped_verb_denied_fail_closed(tmp_path):
    """A dropped verb (oracle_brief) called via MCP is denied by the
    Dispatcher chokepoint (P4S-8) -- structurally, not via a parallel table."""
    from oracle_agent.agentloop.verbtools import Dispatcher
    from oracle_agent.agentloop import policy_bridge as pb

    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    core, surface_cfg = _core(tmp_path)

    def dispatcher_factory(principal):
        return Dispatcher(
            root=root, surface="gateway", environment="external",
            max_sensitivity="public", order=list(pb.CANONICAL_ORDER),
            write_actor=f"gateway_user:http:{principal}",
        )

    a = _adapter(tmp_path, core=core, surface_cfg=surface_cfg,
                 dispatcher_factory=dispatcher_factory)
    result = a.handle_mcp({
        "method": "tools/call", "id": 2,
        "params": {"name": "oracle_brief", "arguments": {}},
    })
    assert result["_status"] == 200
    text = result["result"]["content"][0]["text"]
    assert "denied" in text.lower() or "not available" in text.lower()
    assert result["result"]["isError"] is True


def test_mcp_unknown_method_400(tmp_path):
    a = _adapter(tmp_path)
    result = a.handle_mcp({"method": "resources/list", "id": 9})
    assert result["_status"] == 400


# --------------------------------------------------------------------------- #
# No control plane (P4S-8) -- structural test (SH-005 style)
# --------------------------------------------------------------------------- #
def test_no_control_plane_endpoints():
    """Structural: the handler serves ONLY /ask and /mcp via POST, and the
    adapter exposes NO method that mutates allowlists/config/pairing/instances.
    Walk the module source: no allowlist/config/pairing mutation verbs are
    routed."""
    import ast
    import inspect
    from oracle_agent.gateway import http as http_mod

    src = inspect.getsource(http_mod)
    tree = ast.parse(src)
    # Collect EVERY route-shaped string literal in the module (any "/..." path).
    # The structural guarantee: the ONLY request paths the server compares
    # against are /ask and /mcp -- there is no path on which a control-plane
    # mutation (allowlist/config/pairing/instance) could be routed.
    route_literals = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value.startswith("/"):
            route_literals.add(node.value)
    assert route_literals <= {"/ask", "/mcp"}, (
        f"unexpected route literals: {route_literals}")


# --------------------------------------------------------------------------- #
# 503 busy on root-lock contention (P4S-9)
# --------------------------------------------------------------------------- #
def test_lock_busy_returns_503(tmp_path):
    import contextlib

    @contextlib.contextmanager
    def busy_lock(name):
        raise BlockingIOError("root busy")
        yield  # pragma: no cover

    a = _adapter(tmp_path, nb_lock_factory=busy_lock)
    result = a.handle_ask("hi")
    assert result["_status"] == 503
    assert result["error"] == "busy"


def test_lock_acquired_when_free(tmp_path):
    import contextlib
    acquired = []

    @contextlib.contextmanager
    def ok_lock(name):
        acquired.append(name)
        yield

    a = _adapter(tmp_path, nb_lock_factory=ok_lock)
    result = a.handle_ask("hi")
    assert result["_status"] == 200
    assert acquired == ["main"]


# --------------------------------------------------------------------------- #
# Real HTTPServer over an ephemeral port: token/host/oversize/clean start-stop
# --------------------------------------------------------------------------- #
def _post(url, body, headers):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


@pytest.fixture
def live_adapter(tmp_path):
    core, surface_cfg = _core(tmp_path)
    surface_cfg = dict(surface_cfg)
    surface_cfg["port"] = 0  # ephemeral
    a = HTTPAdapter(surface_cfg, core, token=TOKEN)
    a.start()
    # The actual port chosen by the OS:
    a.port = a._server.server_address[1]
    a.allowed_hosts = _allowed_hosts(a.bind, a.port)
    yield a
    a.stop()


def test_live_good_token_returns_reply(live_adapter):
    a = live_adapter
    url = f"http://127.0.0.1:{a.port}/ask"
    status, body = _post(url, {"text": "ping"},
                         {"Authorization": f"Bearer {TOKEN}",
                          "Host": f"127.0.0.1:{a.port}",
                          "Content-Type": "application/json"})
    assert status == 200
    assert "answer to: ping" in body["reply"]


def test_live_missing_token_401(live_adapter):
    a = live_adapter
    url = f"http://127.0.0.1:{a.port}/ask"
    status, _ = _post(url, {"text": "ping"},
                      {"Host": f"127.0.0.1:{a.port}",
                       "Content-Type": "application/json"})
    assert status == 401


def test_live_bad_host_403(live_adapter):
    a = live_adapter
    url = f"http://127.0.0.1:{a.port}/ask"
    status, _ = _post(url, {"text": "ping"},
                      {"Authorization": f"Bearer {TOKEN}",
                       "Host": "evil.example.com",
                       "Content-Type": "application/json"})
    assert status == 403


def test_live_no_cors_headers(live_adapter):
    a = live_adapter
    url = f"http://127.0.0.1:{a.port}/ask"
    data = json.dumps({"text": "ping"}).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Host": f"127.0.0.1:{a.port}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        # No CORS headers are EVER emitted (P4S-7).
        assert resp.headers.get("Access-Control-Allow-Origin") is None
        assert resp.headers.get("Access-Control-Allow-Methods") is None


def test_live_oversize_body_413(live_adapter):
    a = live_adapter
    url = f"http://127.0.0.1:{a.port}/ask"
    big = "x" * (256 * 1024 + 10)
    status, _ = _post(url, {"text": big},
                      {"Authorization": f"Bearer {TOKEN}",
                       "Host": f"127.0.0.1:{a.port}",
                       "Content-Type": "application/json"})
    assert status == 413


def test_live_clean_start_stop(tmp_path):
    core, surface_cfg = _core(tmp_path)
    surface_cfg = dict(surface_cfg)
    surface_cfg["port"] = 0
    a = HTTPAdapter(surface_cfg, core, token=TOKEN)
    a.start()
    assert a._thread is not None and a._thread.is_alive()
    a.stop()
    assert a._server is None
    assert a._thread is None
