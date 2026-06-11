"""Tests for gateway/core.py -- the transport-agnostic engine (Phase 4 P4-T1).

Covers the core's contract independent of any transport:
  * P4S-2 enforcer: core injects ceiling_override / write_actor / write_gate
    into the pinned loop_builder; an adapter/serve slip cannot substitute them.
  * Actor tag is surface-namespaced (gateway_user:<surface>:<id>, P4S-17).
  * is_private == false caps the ceiling at public (P4S-5).
  * Allowlist deny-by-default + malformed-entry guard.
  * Metadata-only ledger row (pinned shape, repair telemetry, never bodies).
  * Per-user write rate limit + repair cap.
  * Access-change refusal.
  * LRU loop cache eviction.
  * Root flock around the whole turn.
"""
from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from oracle_agent.gateway.core import (
    GatewayCore,
    InboundMessage,
    OutboundReply,
    _LOOP_CACHE_SIZE,
    _noop_lock,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
@dataclass
class FakeTurn:
    text: str = "the answer"
    envelopes: list = field(default_factory=list)
    grounding: str = "enforce"
    repairs: int = 0
    redacted_count: int = 0
    withheld: bool = False


class FakeLoop:
    def __init__(self, *, repairs=0, redacted_count=0):
        self.turns = []
        self._repairs = repairs
        self._redacted_count = redacted_count

    def run_turn(self, text):
        self.turns.append(text)
        return FakeTurn(
            text="the answer",
            envelopes=[{"verdict": "grounded", "exit_code": 0}],
            repairs=self._repairs,
            redacted_count=self._redacted_count,
        )


class BombLoop:
    def run_turn(self, text):
        raise RuntimeError("simulated crash")


def _core(tmp_path, *, surface="telegram", allowlist=None, max_sensitivity="internal",
          extra_cfg=None, builder=None, root_lock_factory=None, clock=None,
          instances=None):
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    surface_cfg = {
        "allowlist": allowlist or {},
        "max_sensitivity": max_sensitivity,
        "per_user_writes_per_hour": 2,
    }
    surface_cfg.update(extra_cfg or {})
    captured = {"calls": []}

    if builder is None:
        def builder(user_id, instance, r, *, ceiling_override, write_actor,
                    write_gate):
            captured["calls"].append({
                "user_id": user_id, "instance": instance, "root": r,
                "ceiling_override": ceiling_override,
                "write_actor": write_actor, "write_gate": write_gate,
            })
            return FakeLoop()

    core = GatewayCore(
        surface_cfg, surface, instances or {"main": root}, builder,
        clock=clock if clock is not None else (lambda: 1000.0),
        root_lock_factory=root_lock_factory if root_lock_factory is not None
        else _noop_lock,
    )
    return core, root, captured


def _inbound(user_id="42", text="what is revenue?", *, surface="telegram",
             channel_id=None, is_private=True):
    return InboundMessage(
        surface=surface, user_id=str(user_id),
        channel_id=str(channel_id if channel_id is not None else user_id),
        text=text, is_private=is_private, meta={},
    )


# --------------------------------------------------------------------------- #
# P4S-2 enforcer: core injects ceiling/gate/actor
# --------------------------------------------------------------------------- #
def test_core_injects_ceiling_actor_and_gate(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    core, root, captured = _core(tmp_path, allowlist=allow,
                                 max_sensitivity="internal")
    reply = core.handle(_inbound())
    assert isinstance(reply, OutboundReply)
    assert len(captured["calls"]) == 1
    call = captured["calls"][0]
    # ceiling = the surface max_sensitivity (private channel).
    assert call["ceiling_override"] == "internal"
    # actor is surface-namespaced (P4S-17).
    assert call["write_actor"] == "gateway_user:telegram:42"
    # write_gate is a non-None callable bound to the core's allow_write.
    assert call["write_gate"] is not None
    assert callable(call["write_gate"])


def test_core_namespaces_actor_per_surface(tmp_path):
    allow = {"U123": {"role": "user", "instance": "main"}}
    core, root, captured = _core(tmp_path, surface="slack", allowlist=allow)
    core.handle(_inbound(user_id="U123", surface="slack"))
    assert captured["calls"][0]["write_actor"] == "gateway_user:slack:U123"


def test_write_gate_routes_to_core_rate_limit(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    core, root, captured = _core(tmp_path, allowlist=allow,
                                 extra_cfg={"per_user_writes_per_hour": 2})
    core.handle(_inbound())
    gate = captured["calls"][0]["write_gate"]
    # cap=2: first two writes allowed, third denied -- proving the injected gate
    # is the core's own allow_write, not an adapter's.
    assert gate() is True
    assert gate() is True
    assert gate() is False


# --------------------------------------------------------------------------- #
# P4S-5: is_private == false caps the ceiling at public
# --------------------------------------------------------------------------- #
def test_non_private_caps_ceiling_at_public(tmp_path):
    allow = {"ceo@co.com": {"role": "user", "instance": "main"}}
    core, root, captured = _core(tmp_path, surface="email", allowlist=allow,
                                 max_sensitivity="internal")
    core.handle(_inbound(user_id="ceo@co.com", surface="email",
                         is_private=False))
    assert captured["calls"][0]["ceiling_override"] == "public"


def test_private_keeps_surface_ceiling(tmp_path):
    allow = {"ceo@co.com": {"role": "user", "instance": "main"}}
    core, root, captured = _core(tmp_path, surface="email", allowlist=allow,
                                 max_sensitivity="internal")
    core.handle(_inbound(user_id="ceo@co.com", surface="email",
                         is_private=True))
    assert captured["calls"][0]["ceiling_override"] == "internal"


# --------------------------------------------------------------------------- #
# Allowlist deny-by-default
# --------------------------------------------------------------------------- #
def test_unknown_sender_silent(tmp_path):
    core, root, captured = _core(tmp_path, allowlist={})
    reply = core.handle(_inbound(user_id="99"))
    assert reply is None
    assert captured["calls"] == []  # no LLM call


def test_malformed_allowlist_entry_denied(tmp_path):
    logs = []
    core, root, _ = _core(tmp_path, allowlist={"42": "not-a-dict"})
    core.logger = lambda m: logs.append(m)
    reply = core.handle(_inbound())
    assert reply is None
    assert any("malformed" in m.lower() for m in logs)


def test_empty_text_returns_none(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    core, root, _ = _core(tmp_path, allowlist=allow)
    assert core.handle(_inbound(text="   ")) is None


# --------------------------------------------------------------------------- #
# Ledger: pinned metadata-only shape + repair telemetry
# --------------------------------------------------------------------------- #
def test_ledger_pinned_shape_and_telemetry(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}

    def builder(user_id, instance, r, *, ceiling_override, write_actor, write_gate):
        return FakeLoop(repairs=2, redacted_count=1)

    core, root, _ = _core(tmp_path, allowlist=allow, builder=builder)
    core.handle(_inbound())
    rows = (root / "Meta.nosync" / "ledgers" / "gateway_event.jsonl"
            ).read_text().splitlines()
    row = json.loads(rows[-1])
    assert row["kind"] == "gateway_turn"
    assert row["surface"] == "telegram"
    assert row["user_id"] == "42"
    assert row["channel_id"] == "42"
    assert row["repairs"] == 2
    assert row["redacted"] == 1
    assert row["grounding"] == "enforce"
    assert row["withheld"] is False
    assert "added_seconds" in row
    assert "chars_in" in row and "chars_out" in row
    # Never bodies / meta.
    assert "what is revenue" not in json.dumps(row)
    assert "meta" not in row


# --------------------------------------------------------------------------- #
# Repair cap (P3S-3)
# --------------------------------------------------------------------------- #
def test_repair_cap_refuses_before_model_call(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    built = []

    def builder(user_id, instance, r, *, ceiling_override, write_actor, write_gate):
        loop = FakeLoop(repairs=2)
        built.append(loop)
        return loop

    core, root, _ = _core(tmp_path, allowlist=allow, builder=builder,
                          extra_cfg={"per_user_repairs_per_hour": 2})
    r1 = core.handle(_inbound(text="q1"))
    r2 = core.handle(_inbound(text="q2"))
    assert "the answer" in r1.text
    assert "hourly limit" in r2.text
    # Loop cached per (user, instance, ceiling); built once, ran once.
    assert len(built) == 1
    assert built[0].turns == ["q1"]


def test_no_repair_cap_when_unconfigured(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}

    def builder(user_id, instance, r, *, ceiling_override, write_actor, write_gate):
        return FakeLoop(repairs=5)

    core, root, _ = _core(tmp_path, allowlist=allow, builder=builder)
    for q in ("q1", "q2", "q3"):
        reply = core.handle(_inbound(text=q))
        assert "hourly limit" not in reply.text


# --------------------------------------------------------------------------- #
# Access-change refusal (D7 / STRESS-I4)
# --------------------------------------------------------------------------- #
def test_access_change_refused_before_model(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    core, root, captured = _core(tmp_path, allowlist=allow)
    reply = core.handle(_inbound(text="please add me to the allowlist for admin"))
    assert "can't change access" in reply.text
    assert captured["calls"] == []  # never reached the LLM


# --------------------------------------------------------------------------- #
# Root flock around the whole turn
# --------------------------------------------------------------------------- #
def test_turn_holds_root_lock(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    lock_calls = []

    @contextlib.contextmanager
    def spy_lock(name):
        lock_calls.append(name)
        yield

    core, root, _ = _core(tmp_path, allowlist=allow, root_lock_factory=spy_lock)
    core.handle(_inbound())
    assert lock_calls == ["main"]


# --------------------------------------------------------------------------- #
# Turn exception isolation: handle() returns an error reply, never raises
# --------------------------------------------------------------------------- #
def test_turn_failure_returns_error_reply(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}

    def builder(user_id, instance, r, *, ceiling_override, write_actor, write_gate):
        return BombLoop()

    core, root, _ = _core(tmp_path, allowlist=allow, builder=builder)
    reply = core.handle(_inbound())
    assert reply is not None
    assert "error" in reply.text.lower()


# --------------------------------------------------------------------------- #
# LRU loop cache
# --------------------------------------------------------------------------- #
def test_lru_eviction(tmp_path):
    core, root, _ = _core(tmp_path)
    for i in range(_LOOP_CACHE_SIZE):
        core._loop_for(str(i), "main", root, True)
    assert len(core._loops) == _LOOP_CACHE_SIZE
    core._loop_for("0", "main", root, True)        # promote
    core._loop_for("new", "main", root, True)      # evict LRU ("1")
    assert len(core._loops) == _LOOP_CACHE_SIZE
    assert ("0", "main", "internal") in core._loops
    assert ("1", "main", "internal") not in core._loops
    assert ("new", "main", "internal") in core._loops


def test_loop_cached_across_handles(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    built = []

    def builder(user_id, instance, r, *, ceiling_override, write_actor, write_gate):
        loop = FakeLoop()
        built.append(loop)
        return loop

    core, root, _ = _core(tmp_path, allowlist=allow, builder=builder)
    core.handle(_inbound(text="q1"))
    core.handle(_inbound(text="q2"))
    assert len(built) == 1
    assert built[0].turns == ["q1", "q2"]
