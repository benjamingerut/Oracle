"""Tests for gateway/slack.py -- the Slack adapter (Phase 4, P4-T2).

Option A (Socket Mode) with an INJECTED fake transport (P4S-14): every Slack
security guarantee is enforced DEP-FREE here -- no ``skipif(websockets)`` gates
any of these (so ``security_map.verify_enforcers`` accepts the enforcer nodes):

  * a DM (``channel_type=="im"``) from an allowlisted ``U…`` id round-trips
    through GatewayCore over the fake transport;
  * ``mpim`` / group / channel events are DROPPED at the adapter (no
    InboundMessage produced);
  * unknown sender is ignored (deny-by-default; no reply);
  * subtype / bot / from-less messages are dropped;
  * typing affordance is emitted ONLY after authorization (P4S-19) -- a denied
    update produces NO typing event;
  * Socket Mode envelopes are acked; failure arms non-blocking backoff (P4S-18).

The dep-absent clean-import test lives in ``test_stdlib_only.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from oracle_agent.gateway.core import GatewayCore, OutboundReply, _noop_lock
from oracle_agent.gateway.slack import SlackAdapter, transport_available
from oracle_agent.testkit import FakeSlackTransport


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeLoop:
    def __init__(self):
        self.turns = []

    def run_turn(self, text):
        self.turns.append(text)

        class _R:
            def __init__(self, t):
                self.text = t
                self.envelopes = [{"verdict": "grounded"}]
                self.grounding = "enforce"
                self.repairs = 0
                self.redacted_count = 0
                self.withheld = False

        return _R("the slack answer")


def _im_envelope(user="U123", channel="D123", text="hello oracle",
                 envelope_id="env-1", channel_type="im", subtype=None,
                 bot_id=None):
    event = {"type": "message", "user": user, "channel": channel,
             "text": text, "channel_type": channel_type, "ts": "1.2"}
    if subtype:
        event["subtype"] = subtype
    if bot_id:
        event["bot_id"] = bot_id
    return {"type": "events_api", "envelope_id": envelope_id,
            "payload": {"team_id": "T1", "event": event}}


def _core(tmp_path, allowlist, *, max_sensitivity="internal"):
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    surface_cfg = {"allowlist": allowlist, "max_sensitivity": max_sensitivity,
                   "per_user_writes_per_hour": 20}
    loops = {}

    def builder(user_id, instance, r, *, ceiling_override, write_actor, write_gate):
        loop = FakeLoop()
        loops[(user_id, instance)] = loop
        return loop

    core = GatewayCore(surface_cfg, "slack", {"main": root}, builder,
                       clock=lambda: 1000.0, root_lock_factory=_noop_lock)
    core._loops_seen = loops
    return core


def _adapter(tmp_path, allowlist, **kw):
    transport = FakeSlackTransport(**{k: v for k, v in kw.items()
                                      if k in ("envelopes", "fail")})
    core = _core(tmp_path, allowlist)
    typing = kw.get("typing", True)
    adapter = SlackAdapter(transport, core, clock=lambda: 1000.0, typing=typing)
    return adapter, transport, core


# --------------------------------------------------------------------------- #
# Happy path: im DM from allowlisted U… round-trips (P4S-13)
# --------------------------------------------------------------------------- #
def test_im_dm_from_allowlisted_user_round_trips(tmp_path):
    allow = {"U123": {"role": "user", "instance": "main"}}
    adapter, transport, _core_ = _adapter(tmp_path, allow)
    transport.envelopes = [_im_envelope(user="U123")]

    for env in adapter.fetch():
        adapter.handle_envelope(env)

    assert transport.sent == [("D123", "the slack answer")]
    assert transport.acked == ["env-1"]


def test_handle_envelope_returns_one_on_served_turn(tmp_path):
    allow = {"U123": {"role": "user", "instance": "main"}}
    adapter, transport, _ = _adapter(tmp_path, allow)
    env = _im_envelope(user="U123")
    assert adapter.handle_envelope(env) == 1


# --------------------------------------------------------------------------- #
# mpim / group / channel are DROPPED at the adapter (P4S-13 im-only)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("channel_type", ["mpim", "group", "channel", "", None])
def test_non_im_channel_types_dropped_at_adapter(tmp_path, channel_type):
    allow = {"U123": {"role": "user", "instance": "main"}}
    adapter, transport, _ = _adapter(tmp_path, allow)
    env = _im_envelope(user="U123", channel_type=channel_type)
    msg = adapter.parse(env)
    assert msg is None, f"channel_type={channel_type!r} must be dropped (not im)"
    # And a full handle produces no reply / no LLM call.
    assert adapter.handle_envelope(env) == 0
    assert transport.sent == []


# --------------------------------------------------------------------------- #
# Unknown sender ignored (deny-by-default; core decides)
# --------------------------------------------------------------------------- #
def test_unknown_sender_ignored(tmp_path):
    allow = {"U123": {"role": "user", "instance": "main"}}
    adapter, transport, _ = _adapter(tmp_path, allow)
    env = _im_envelope(user="UNOPE")
    assert adapter.handle_envelope(env) == 0
    assert transport.sent == []
    # The envelope is still acked (Socket Mode contract).
    assert transport.acked == ["env-1"]


# --------------------------------------------------------------------------- #
# subtype / bot / from-less messages dropped
# --------------------------------------------------------------------------- #
def test_subtype_message_dropped(tmp_path):
    adapter, _, _ = _adapter(tmp_path, {"U123": {"instance": "main"}})
    assert adapter.parse(_im_envelope(subtype="message_changed")) is None


def test_bot_message_dropped(tmp_path):
    adapter, _, _ = _adapter(tmp_path, {"U123": {"instance": "main"}})
    assert adapter.parse(_im_envelope(bot_id="B999")) is None


def test_from_less_message_dropped(tmp_path):
    adapter, _, _ = _adapter(tmp_path, {"U123": {"instance": "main"}})
    env = _im_envelope()
    env["payload"]["event"].pop("user")
    assert adapter.parse(env) is None


def test_empty_text_dropped(tmp_path):
    adapter, _, _ = _adapter(tmp_path, {"U123": {"instance": "main"}})
    assert adapter.parse(_im_envelope(text="   ")) is None


def test_non_message_event_dropped(tmp_path):
    adapter, _, _ = _adapter(tmp_path, {"U123": {"instance": "main"}})
    env = {"type": "events_api", "envelope_id": "e",
           "payload": {"event": {"type": "reaction_added", "user": "U123"}}}
    assert adapter.parse(env) is None


# --------------------------------------------------------------------------- #
# Typing affordance ONLY after authorization (P4S-19)
# --------------------------------------------------------------------------- #
def test_typing_emitted_only_after_authorization(tmp_path):
    """An authorized im message emits typing THEN the real reply (P4S-19)."""
    allow = {"U123": {"role": "user", "instance": "main"}}
    adapter, transport, _ = _adapter(tmp_path, allow)
    adapter.handle_envelope(_im_envelope(user="U123"))
    assert transport.typing == ["D123"]
    assert transport.sent == [("D123", "the slack answer")]


def test_denied_update_produces_no_typing(tmp_path):
    """A denied (unknown) sender must NOT emit a typing indicator (SH-017)."""
    allow = {"U123": {"role": "user", "instance": "main"}}
    adapter, transport, _ = _adapter(tmp_path, allow)
    adapter.handle_envelope(_im_envelope(user="UNOPE"))
    assert transport.typing == [], "typing on a denied update is a presence oracle"
    assert transport.sent == []


def test_typing_disabled_emits_nothing(tmp_path):
    allow = {"U123": {"role": "user", "instance": "main"}}
    adapter, transport, _ = _adapter(tmp_path, allow, typing=False)
    adapter.handle_envelope(_im_envelope(user="U123"))
    assert transport.typing == []
    assert transport.sent == [("D123", "the slack answer")]


# --------------------------------------------------------------------------- #
# Non-blocking backoff on transport failure (P4S-18)
# --------------------------------------------------------------------------- #
def test_events_failure_arms_nonblocking_backoff(tmp_path):
    adapter, transport, _ = _adapter(tmp_path, {"U123": {"instance": "main"}})
    transport.fail = True
    assert adapter.fetch() == []
    assert adapter.next_poll_not_before > 1000.0  # armed, not slept
    assert adapter._fail_streak == 1


def test_backoff_clears_on_success(tmp_path):
    adapter, transport, _ = _adapter(tmp_path, {"U123": {"instance": "main"}})
    transport.fail = True
    adapter.fetch()
    transport.fail = False
    transport.envelopes = [_im_envelope(user="U123")]
    adapter.fetch()
    assert adapter.next_poll_not_before == 0.0
    assert adapter._fail_streak == 0


# --------------------------------------------------------------------------- #
# Reply chunking (Slack ~40k)
# --------------------------------------------------------------------------- #
def test_send_chunks_long_text(tmp_path):
    adapter, transport, _ = _adapter(tmp_path, {})
    long = "x" * 80000
    adapter.send(OutboundReply("D1", long))
    assert len(transport.sent) >= 2
    assert "".join(t for _, t in transport.sent) == long


# --------------------------------------------------------------------------- #
# transport_available() is a clean boolean probe (no raise)
# --------------------------------------------------------------------------- #
def test_transport_available_is_boolean():
    assert isinstance(transport_available(), bool)
