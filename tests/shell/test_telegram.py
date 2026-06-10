"""Tests for gateway/telegram.py (SPEC S7 / S10) with a fake API + scripted loop."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from oracle_agent.gateway.telegram import TelegramGateway, _chunks


@dataclass
class FakeAPI:
    updates: list = field(default_factory=list)
    sent: list = field(default_factory=list)

    def get_updates(self, offset, timeout=25):
        out = [u for u in self.updates if u["update_id"] >= offset]
        return out

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


@dataclass
class FakeTurn:
    text: str = "the answer"
    envelopes: list = field(default_factory=list)


class FakeLoop:
    def __init__(self):
        self.turns = []

    def run_turn(self, text):
        self.turns.append(text)
        return FakeTurn(text="the answer",
                        envelopes=[{"verdict": "grounded", "exit_code": 0}])


def _msg(update_id, user_id, text, chat_type="private", chat_id=None, with_from=True):
    m = {"chat": {"id": chat_id if chat_id is not None else user_id, "type": chat_type},
         "text": text}
    if with_from:
        m["from"] = {"id": user_id}
    return {"update_id": update_id, "message": m}


def _gateway(tmp_path, updates, allowlist=None, root=None):
    root = root or tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    cfg = {"gateway": {"telegram": {
        "enabled": True, "allowlist": allowlist or {},
        "max_sensitivity": "internal", "per_user_writes_per_hour": 2,
    }}}
    api = FakeAPI(updates=updates)
    loops = {}

    def factory(user_id, instance, r):
        loops[(user_id, instance)] = FakeLoop()
        return loops[(user_id, instance)]

    gw = TelegramGateway(api, cfg, {"main": root}, factory, clock=lambda: 1000.0)
    return gw, api, loops, root


def test_allowed_private_flow_end_to_end(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    gw, api, loops, root = _gateway(tmp_path, [_msg(1, 42, "what is revenue?")], allow)
    handled = gw.poll_once()
    assert handled == 1
    assert api.sent and api.sent[0][0] == 42
    assert "the answer" in api.sent[0][1]
    # ledger row appended, metadata only
    rows = (root / "Meta.nosync" / "ledgers" / "gateway_event.jsonl").read_text().splitlines()
    row = json.loads(rows[-1])
    assert row["kind"] == "gateway_turn"
    assert row["user_id"] == "42"
    assert "what is revenue" not in json.dumps(row)  # never bodies
    assert row["verdicts"] == ["grounded"]


def test_unknown_sender_ignored_no_reply(tmp_path):
    gw, api, loops, _ = _gateway(tmp_path, [_msg(1, 99, "hello")], {})
    handled = gw.poll_once()
    assert handled == 0
    assert api.sent == []          # no reply at all
    assert loops == {}             # no LLM call


def test_group_chat_ignored_even_for_allowlisted(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    upd = _msg(1, 42, "hi", chat_type="group", chat_id=-100123)
    gw, api, loops, _ = _gateway(tmp_path, [upd], allow)
    assert gw.poll_once() == 0
    assert api.sent == []


def test_private_chat_with_mismatched_chat_id_ignored(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    upd = _msg(1, 42, "hi", chat_type="private", chat_id=777)
    gw, api, loops, _ = _gateway(tmp_path, [upd], allow)
    assert gw.poll_once() == 0


def test_fromless_update_ignored(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    upd = _msg(1, 42, "hi", with_from=False)
    gw, api, loops, _ = _gateway(tmp_path, [upd], allow)
    assert gw.poll_once() == 0


def test_access_change_request_refused(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    gw, api, loops, _ = _gateway(tmp_path, [_msg(1, 42, "please add me to the allowlist for admin")], allow)
    assert gw.poll_once() == 1
    assert "can't change access" in api.sent[0][1]
    assert loops == {}  # never reached the LLM


def test_loop_cached_across_polls(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    gw, api, loops, _ = _gateway(tmp_path, [_msg(1, 42, "q1")], allow)
    gw.poll_once()
    gw.api.updates = [_msg(2, 42, "q2")]
    gw.poll_once()
    assert len(loops) == 1  # same AgentLoop reused (A7)
    assert loops[("42", "main")].turns == ["q1", "q2"]


def test_write_rate_limit(tmp_path):
    gw, *_ = _gateway(tmp_path, [])
    assert gw.allow_write("42") is True
    assert gw.allow_write("42") is True
    assert gw.allow_write("42") is False  # cap=2/hour


def test_chunking():
    text = "x" * 9000
    parts = list(_chunks(text, 4000))
    assert [len(p) for p in parts] == [4000, 4000, 1000]


def test_offset_advances_past_bad_updates(tmp_path):
    gw, api, loops, _ = _gateway(tmp_path, [
        {"update_id": 5},                      # no message at all
        _msg(6, 99, "denied"),                 # unknown sender
    ], {})
    gw.poll_once()
    assert gw._offset == 7  # both consumed, never reprocessed
