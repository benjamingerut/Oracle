"""Tests for agentloop/loop.py (SPEC S5 / S10) with a scripted fake client."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from oracle_agent.agentloop.loop import AgentLoop, authority_footer
from oracle_agent.llm.client import ChatResponse, ToolCall


@dataclass
class FakeClient:
    """Returns scripted ChatResponses in order; records messages seen."""
    script: list
    seen: list = field(default_factory=list)
    i: int = 0

    def chat(self, messages, tools=None, **kw):
        self.seen.append((list(messages), bool(tools)))
        resp = self.script[self.i]
        self.i += 1
        return resp


@dataclass
class FakeDispatcher:
    surface: str = "local"
    environment: str = "local_agent"
    outcomes: dict = field(default_factory=dict)

    def dispatch(self, name, args):
        from oracle_agent.agentloop.verbtools import ToolOutcome
        return self.outcomes.get(name, ToolOutcome("[ok]", rc=0))


def _loop(script, dispatcher=None, **kw):
    return AgentLoop(FakeClient(script), dispatcher or FakeDispatcher(),
                     "SYS", retry_kwargs={"sleep": lambda *_: None}, **kw)


def test_simple_text_turn_gets_conversational_footer():
    loop = _loop([ChatResponse(content="hello")])
    res = loop.run_turn("hi")
    assert "hello" in res.text
    assert "conversational; no authority protocol invoked" in res.text
    assert res.iterations == 1


def test_multi_step_tool_loop():
    from oracle_agent.agentloop.verbtools import ToolOutcome
    script = [
        ChatResponse(content=None, tool_calls=[ToolCall("c1", "oracle_search", '{"terms":"x"}')]),
        ChatResponse(content="found it"),
    ]
    disp = FakeDispatcher(outcomes={"oracle_search": ToolOutcome("result text", rc=0)})
    loop = _loop(script, disp)
    res = loop.run_turn("look it up")
    assert "found it" in res.text
    assert res.iterations == 2
    # tool result is in the message history as role=tool
    assert any(m.get("role") == "tool" for m in loop.messages)


def test_grounded_footer_from_envelope():
    from oracle_agent.agentloop.verbtools import ToolOutcome
    env = {"business_object": "Revenue", "exit_code": 0, "verdict": "grounded"}
    script = [
        ChatResponse(content=None, tool_calls=[ToolCall("c1", "oracle_answer", '{"business_object":"Revenue"}')]),
        ChatResponse(content="Revenue is $1M."),
    ]
    disp = FakeDispatcher(outcomes={"oracle_answer": ToolOutcome("{}", envelope=env, rc=0)})
    res = _loop(script, disp).run_turn("what is revenue")
    assert "grounded (Revenue)" in res.text


def test_refused_footer_includes_fix():
    env = {"business_object": "Secret", "exit_code": 4, "verdict": "refused",
           "suggested_fix": ["./oracle ingest <file>"]}
    foot = authority_footer([env])
    assert "refused (Secret)" in foot
    assert "./oracle ingest <file>" in foot


def test_iteration_cap_forces_answer():
    from oracle_agent.agentloop.verbtools import ToolOutcome
    # Always returns a tool call -> never terminates until cap.
    tc = [ToolCall("c", "oracle_search", "{}")]
    # exactly max_iterations tool-call turns, then the forced tools-disabled call
    script = [ChatResponse(content=None, tool_calls=tc) for _ in range(3)]
    script.append(ChatResponse(content="forced final"))
    disp = FakeDispatcher(outcomes={"oracle_search": ToolOutcome("x", rc=0)})
    loop = _loop(script, disp, max_iterations=3)
    res = loop.run_turn("loop forever")
    assert "forced final" in res.text


def test_bad_tool_json_is_handled():
    from oracle_agent.agentloop.verbtools import ToolOutcome
    script = [
        ChatResponse(content=None, tool_calls=[ToolCall("c1", "oracle_search", "{not json")]),
        ChatResponse(content="recovered"),
    ]
    res = _loop(script, FakeDispatcher()).run_turn("x")
    assert "recovered" in res.text


def test_system_prompt_byte_stable_across_turns():
    loop = _loop([ChatResponse(content="a"), ChatResponse(content="b")])
    loop.run_turn("one")
    sys1 = loop.messages[0]["content"]
    loop.run_turn("two")
    sys2 = loop.messages[0]["content"]
    assert sys1 == sys2
    assert loop.messages[0]["role"] == "system"


def test_eviction_preserves_toolcall_pairing():
    """Evicting old turns must never split an assistant tool_calls msg from
    its tool replies (a dangling tool_call_id fails the next API call)."""
    from oracle_agent.agentloop.verbtools import ToolOutcome
    disp = FakeDispatcher(outcomes={"oracle_search": ToolOutcome("x" * 500, rc=0)})
    # tiny budget forces eviction after a couple of tool turns
    loop = _loop([ChatResponse(content="done")], disp, history_max_chars=800)
    # seed several completed tool-using turns
    for n in range(4):
        loop.messages.append({"role": "user", "content": f"q{n}"})
        loop.messages.append({"role": "assistant", "content": "",
                              "tool_calls": [{"id": f"t{n}", "type": "function",
                                              "function": {"name": "oracle_search", "arguments": "{}"}}]})
        loop.messages.append({"role": "tool", "tool_call_id": f"t{n}",
                              "name": "oracle_search", "content": "x" * 500})
    loop._evict_if_needed()
    # every assistant tool_calls id must have a matching tool reply after it
    ids_called = []
    ids_replied = set()
    for m in loop.messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls", []):
                ids_called.append(tc["id"])
        if m.get("role") == "tool":
            ids_replied.add(m["tool_call_id"])
    for cid in ids_called:
        assert cid in ids_replied, f"dangling tool_call_id {cid}"
    assert loop.messages[0]["role"] == "system"


def test_force_eviction_drops_a_group():
    loop = _loop([ChatResponse(content="x")])
    loop.messages.append({"role": "user", "content": "old"})
    loop.messages.append({"role": "assistant", "content": "old-answer"})
    loop.messages.append({"role": "user", "content": "current"})
    before = len(loop.messages)
    loop._evict_if_needed(force=True)
    assert len(loop.messages) < before
    assert loop.messages[0]["role"] == "system"
    assert loop.messages[-1]["content"] == "current"
