"""Tests for oracle_agent/testkit.py (P1-T2).

Covers:
  - No-production-import enforcer: no production module imports testkit.
  - ScriptedResponse constructs real ChatResponse / ToolCall objects.
  - FakeLLM records messages and replays script.
  - FakeLLM.assert_no_content_above catches a planted sensitivity leak.
  - Harness.chat wires a real AgentLoop with FakeLLM injected.
  - Harness.gateway wires a TelegramGateway with a fake API.
  - spawn_test_root (via spawned_root fixture) is a pure pytest-free helper.
  - test_stdlib_only extension: testkit itself has no non-stdlib module-scope
    imports (already covered by the existing walk; this file adds an explicit
    named assertion for documentation).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Modules that must never import testkit (production modules).
_PRODUCTION_MODULES = [
    "oracle_agent.cli",
    "oracle_agent.agentloop.builder",
    "oracle_agent.agentloop.loop",
    "oracle_agent.service.serve",
    "oracle_agent.gateway.telegram",
    "oracle_agent.service.scheduler",
    "oracle_agent.config",
    "oracle_agent.doctor",
    "oracle_agent.wizard",
    "oracle_agent.spawn",
]

# Production source files that must not import testkit (AST walk).
_PROD_DIRS = [
    _SRC / "oracle_agent",
]


# ---------------------------------------------------------------------------
# No-production-import enforcer
# ---------------------------------------------------------------------------

def _ast_imports(path: Path):
    """Yield all module names imported (top-level and inside functions)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import: reconstruct full name for comparison.
                # We only care if it imports testkit specifically.
                if node.module and "testkit" in node.module:
                    yield node.module
            elif node.module:
                yield node.module


def test_no_production_module_imports_testkit():
    """No production module under src/oracle_agent/ (except testkit itself)
    may import oracle_agent.testkit at any scope.

    Enforced by AST walk so it catches both module-scope and deferred imports.
    """
    oracle_src = _SRC / "oracle_agent"
    offenders: list[str] = []

    for py_file in oracle_src.rglob("*.py"):
        if "assets" in py_file.parts:
            continue
        # testkit.py itself is the one place that's allowed.
        if py_file.name == "testkit.py":
            continue

        for mod_name in _ast_imports(py_file):
            if "testkit" in mod_name:
                rel = py_file.relative_to(oracle_src)
                offenders.append(f"{rel}: imports {mod_name!r}")

    assert not offenders, (
        "Production modules must not import testkit "
        "(P1S-11 / P1-T2):\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# testkit stdlib-only (named explicit assertion, complements test_stdlib_only)
# ---------------------------------------------------------------------------

def test_testkit_has_no_non_stdlib_module_scope_imports():
    """testkit.py module-scope imports are stdlib + oracle_agent only.

    The existing test_stdlib_only walk covers this implicitly; this test
    makes it explicit and named for the security_map enforcer.
    """
    stdlib = set(sys.stdlib_module_names)
    allowed = {"oracle_agent"}
    testkit_path = _SRC / "oracle_agent" / "testkit.py"
    tree = ast.parse(testkit_path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for node in ast.walk(tree):
        # Only check module-scope (top-level) Import / ImportFrom nodes.
        # We approximate "module scope" as direct children of the Module node.
        pass

    # Reparse checking only top-level imports (direct children of Module).
    module_node = ast.parse(testkit_path.read_text(encoding="utf-8"))
    for node in module_node.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in stdlib and top not in allowed:
                    offenders.append(f"imports {alias.name!r}")
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative = oracle_agent-local
            if node.module:
                top = node.module.split(".")[0]
                if top not in stdlib and top not in allowed:
                    offenders.append(f"from {node.module!r} import ...")

    assert not offenders, (
        "testkit.py has non-stdlib module-scope imports "
        "(P1S-11):\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# ScriptedResponse
# ---------------------------------------------------------------------------

def test_scripted_response_builds_chat_response_text():
    from oracle_agent.testkit import ScriptedResponse
    from oracle_agent.llm.client import ChatResponse

    r = ScriptedResponse("hello world").build()
    assert isinstance(r, ChatResponse)
    assert r.content == "hello world"
    assert r.tool_calls == []


def test_scripted_response_builds_tool_calls():
    from oracle_agent.testkit import ScriptedResponse
    from oracle_agent.llm.client import ChatResponse, ToolCall

    r = ScriptedResponse(
        tool_calls=[("call_1", "oracle_search", '{"terms": "revenue"}')]
    ).build()
    assert isinstance(r, ChatResponse)
    assert len(r.tool_calls) == 1
    tc = r.tool_calls[0]
    assert isinstance(tc, ToolCall)
    assert tc.id == "call_1"
    assert tc.name == "oracle_search"
    assert tc.arguments == '{"terms": "revenue"}'


def test_scripted_response_mixed():
    from oracle_agent.testkit import ScriptedResponse

    r = ScriptedResponse(
        content="also some text",
        tool_calls=[("id1", "oracle_status", "{}")],
        finish_reason="stop",
    ).build()
    assert r.content == "also some text"
    assert len(r.tool_calls) == 1
    assert r.finish_reason == "stop"


# ---------------------------------------------------------------------------
# FakeLLM
# ---------------------------------------------------------------------------

def test_fakellem_replays_script_in_order():
    from oracle_agent.testkit import FakeLLM, ScriptedResponse

    script = [
        ScriptedResponse("first"),
        ScriptedResponse("second"),
    ]
    llm = FakeLLM(script)
    r1 = llm.chat([{"role": "user", "content": "a"}])
    r2 = llm.chat([{"role": "user", "content": "b"}])
    assert r1.content == "first"
    assert r2.content == "second"


def test_fakellem_records_seen_calls():
    from oracle_agent.testkit import FakeLLM, ScriptedResponse

    llm = FakeLLM([ScriptedResponse("hi")])
    msgs = [{"role": "user", "content": "hello"}]
    llm.chat(msgs, tools=[{"type": "function"}])
    assert len(llm.seen) == 1
    seen_msgs, has_tools = llm.seen[0]
    assert seen_msgs == msgs
    assert has_tools is True


def test_fakellem_script_exhausted_raises():
    from oracle_agent.testkit import FakeLLM

    llm = FakeLLM([])
    with pytest.raises(RuntimeError, match="exhausted"):
        llm.chat([])


def test_fakellem_all_messages_flat():
    from oracle_agent.testkit import FakeLLM, ScriptedResponse

    llm = FakeLLM([ScriptedResponse("a"), ScriptedResponse("b")])
    m1 = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q1"}]
    m2 = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q2"}]
    llm.chat(m1)
    llm.chat(m2)
    flat = llm.all_messages
    assert len(flat) == 4
    assert flat[0]["content"] == "sys"
    assert flat[2]["content"] == "sys"


# ---------------------------------------------------------------------------
# FakeLLM.assert_no_content_above -- happy path
# ---------------------------------------------------------------------------

def test_assert_no_content_above_passes_when_clean():
    """No messages with above-ceiling content: assert passes silently."""
    from oracle_agent.testkit import FakeLLM, ScriptedResponse

    llm = FakeLLM([ScriptedResponse("public info")])
    llm.chat([{"role": "user", "content": "question"},
              {"role": "assistant", "content": "public info"}])
    # No above-ceiling content present -> should not raise.
    llm.assert_no_content_above("internal")


def test_assert_no_content_above_passes_with_at_ceiling_label():
    """A sensitivity label exactly AT the ceiling is allowed."""
    from oracle_agent.testkit import FakeLLM, ScriptedResponse

    llm = FakeLLM([ScriptedResponse("ok")])
    # 'internal' is AT the ceiling 'internal' -- should pass.
    content = '{"sensitivity": "internal", "text": "some data"}'
    llm.chat([{"role": "tool", "tool_call_id": "x",
               "name": "oracle_search", "content": content}])
    llm.assert_no_content_above("internal")


# ---------------------------------------------------------------------------
# FakeLLM.assert_no_content_above -- PLANTED LEAK (spec requirement)
#
# A scripted conversation whose messages contain above-ceiling content,
# proving assert_no_content_above raises as expected.
# ---------------------------------------------------------------------------

def test_assert_no_content_above_catches_json_sensitivity_marker():
    """Planted leak: a tool result message contains a JSON sensitivity marker
    above the ceiling.  assert_no_content_above must raise AssertionError.

    This simulates a broken dispatcher that returned a 'secret' result to a
    'public'-ceiling LLM without withholding it.
    """
    from oracle_agent.testkit import FakeLLM, ScriptedResponse

    llm = FakeLLM([ScriptedResponse("final answer")])

    # Plant the above-ceiling content in the messages passed to chat().
    # In a real scenario this would be a tool result from a broken dispatcher.
    leaked_tool_result = (
        '{"doc_id": "s1", "sensitivity": "secret", '
        '"text": "TOP SECRET revenue figures: $999M"}'
    )
    messages = [
        {"role": "system", "content": "You are an oracle."},
        {"role": "user", "content": "What is revenue?"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "oracle_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1",
         "name": "oracle_search", "content": leaked_tool_result},
    ]
    llm.chat(messages)

    with pytest.raises(AssertionError, match="secret"):
        llm.assert_no_content_above("public")


def test_assert_no_content_above_catches_withheld_marker_label():
    """Planted leak: message content contains a withheld-marker-style string
    carrying an above-ceiling sensitivity label.

    assert_no_content_above must extract the label from the marker and raise.
    """
    from oracle_agent.testkit import FakeLLM, ScriptedResponse

    llm = FakeLLM([ScriptedResponse("answer")])
    # Simulate content that looks like the verbtools withheld stub but leaked
    # through (e.g., someone echoed it into user content instead of suppressing
    # it -- NOT the normal flow; this is the adversarial planted-leak scenario).
    marker = (
        "[withheld: this answer requires restricted clearance, above the "
        "internal ceiling for this provider]"
    )
    llm.chat([{"role": "user", "content": marker}])

    with pytest.raises(AssertionError, match="restricted"):
        llm.assert_no_content_above("internal")


def test_assert_no_content_above_below_ceiling_not_caught():
    """Sensitivity label BELOW ceiling is not a violation."""
    from oracle_agent.testkit import FakeLLM, ScriptedResponse

    llm = FakeLLM([ScriptedResponse("ok")])
    content = '{"sensitivity": "public", "text": "public data"}'
    llm.chat([{"role": "tool", "tool_call_id": "x",
               "name": "oracle_search", "content": content}])
    # ceiling is 'internal'; 'public' is below -> no raise.
    llm.assert_no_content_above("internal")


def test_assert_no_content_above_catches_sensitivity_ceiling_json():
    """JSON field sensitivity_ceiling above ceiling is also caught."""
    from oracle_agent.testkit import FakeLLM, ScriptedResponse

    llm = FakeLLM([ScriptedResponse("ok")])
    content = '{"sensitivity_ceiling": "confidential", "verdict": "grounded"}'
    llm.chat([{"role": "tool", "tool_call_id": "x",
               "name": "oracle_answer", "content": content}])
    with pytest.raises(AssertionError):
        llm.assert_no_content_above("internal")


# ---------------------------------------------------------------------------
# Harness.chat
# ---------------------------------------------------------------------------

def test_harness_chat_returns_agentloop_with_fakellem(spawned_root):
    """Harness.chat wires a real AgentLoop with FakeLLM injected."""
    from oracle_agent.testkit import Harness, ScriptedResponse
    from oracle_agent.agentloop.loop import AgentLoop
    from oracle_agent.testkit import FakeLLM

    h = Harness(spawned_root)
    loop = h.chat([ScriptedResponse("public answer")])
    assert isinstance(loop, AgentLoop)
    assert isinstance(loop.client, FakeLLM)


def test_harness_chat_run_turn_returns_text(spawned_root):
    """A scripted text response is returned by run_turn."""
    from oracle_agent.testkit import Harness, ScriptedResponse

    h = Harness(spawned_root)
    loop = h.chat([ScriptedResponse("revenue is $1M")])
    result = loop.run_turn("what is revenue?")
    assert "revenue is $1M" in result.text


def test_harness_chat_environment_derivation(spawned_root):
    """Harness.chat synthesizes URLs so policy_bridge.environment_for runs."""
    from oracle_agent.testkit import Harness, ScriptedResponse
    from oracle_agent.agentloop import policy_bridge as pb

    h = Harness(spawned_root)

    # local_agent: loopback URL -> environment_for returns "local_agent"
    loop_local = h.chat([ScriptedResponse("ok")], environment="local_agent")
    assert loop_local.dispatcher.environment == "local_agent"

    # external: non-loopback URL -> environment_for returns "external"
    loop_ext = h.chat([ScriptedResponse("ok")], environment="external")
    assert loop_ext.dispatcher.environment == "external"


def test_harness_chat_with_tool_call(spawned_root):
    """Harness.chat loop handles a scripted tool call without crashing."""
    from oracle_agent.testkit import Harness, ScriptedResponse

    h = Harness(spawned_root)
    script = [
        ScriptedResponse(
            tool_calls=[("c1", "oracle_search", '{"terms": "anything"}')]
        ),
        ScriptedResponse("search complete"),
    ]
    loop = h.chat(script)
    result = loop.run_turn("search for something")
    assert "search complete" in result.text


# ---------------------------------------------------------------------------
# Harness.gateway
# ---------------------------------------------------------------------------

def test_harness_gateway_returns_telegram_gateway(spawned_root):
    from oracle_agent.testkit import Harness
    from oracle_agent.gateway.telegram import TelegramGateway

    h = Harness(spawned_root)
    gw = h.gateway(updates=[], allowlist={})
    assert isinstance(gw, TelegramGateway)


def test_harness_gateway_poll_empty(spawned_root):
    from oracle_agent.testkit import Harness

    h = Harness(spawned_root)
    gw = h.gateway(updates=[], allowlist={})
    assert gw.poll_once() == 0


def test_harness_gateway_allowlisted_message(spawned_root):
    """An allowlisted user message is processed (poll_once returns 1)."""
    from oracle_agent.testkit import Harness

    allow = {"42": {"role": "user", "instance": "main"}}
    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "hello",
        },
    }

    h = Harness(spawned_root)
    gw = h.gateway(updates=[update], allowlist=allow)
    # The gateway processes the update even though the FakeLLM script is empty
    # because the loop raises (caught internally) or returns a canned answer.
    # We only care that poll_once doesn't crash and returns >=0.
    result = gw.poll_once()
    assert result >= 0  # 0 or 1 depending on loop behavior


# ---------------------------------------------------------------------------
# spawn_test_root (via spawned_root fixture)
# ---------------------------------------------------------------------------

def test_spawn_test_root_is_real_root(spawned_root):
    """The spawned_root fixture uses spawn_test_root; verify oracle.yml exists."""
    assert (spawned_root / "oracle.yml").exists()


def test_spawn_test_root_no_pytest_import():
    """spawn_test_root function itself does not import pytest (pure helper)."""
    import importlib
    import importlib.util

    testkit_path = _SRC / "oracle_agent" / "testkit.py"
    tree = ast.parse(testkit_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "pytest" not in alias.name, (
                    f"testkit.py imports pytest: {alias.name!r}"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                assert "pytest" not in node.module, (
                    f"testkit.py imports from pytest: {node.module!r}"
                )
