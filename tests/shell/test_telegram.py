"""Tests for gateway/telegram.py (SPEC S7 / S10) with a fake API + scripted loop."""
from __future__ import annotations

import contextlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from oracle_agent.gateway.telegram import TelegramGateway, _chunks, _noop_lock


@dataclass
class FakeAPI:
    updates: list = field(default_factory=list)
    sent: list = field(default_factory=list)
    fail: bool = False

    def get_updates(self, offset, timeout=25):
        if self.fail:
            raise OSError("simulated network error")
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


class BombLoop:
    """A loop whose run_turn always raises."""
    def run_turn(self, text):
        raise RuntimeError("simulated crash")


def _msg(update_id, user_id, text, chat_type="private", chat_id=None, with_from=True):
    m = {"chat": {"id": chat_id if chat_id is not None else user_id, "type": chat_type},
         "text": text}
    if with_from:
        m["from"] = {"id": user_id}
    return {"update_id": update_id, "message": m}


def _gateway(tmp_path, updates, allowlist=None, root=None, profile_dir=None,
             sleeps=None, root_lock_factory=None):
    root = root or tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    if profile_dir is None:
        profile_dir = tmp_path / "profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"gateway": {"telegram": {
        "enabled": True, "allowlist": allowlist or {},
        "max_sensitivity": "internal", "per_user_writes_per_hour": 2,
    }}}
    api = FakeAPI(updates=updates)
    loops = {}

    def factory(user_id, instance, r):
        loops[(user_id, instance)] = FakeLoop()
        return loops[(user_id, instance)]

    sleep_calls = sleeps if sleeps is not None else []
    recorded_sleeps = []

    def fake_sleep(t):
        recorded_sleeps.append(t)

    gw = TelegramGateway(
        api, cfg, {"main": root}, factory,
        clock=lambda: 1000.0,
        sleep=fake_sleep,
        profile_dir=profile_dir,
        root_lock_factory=root_lock_factory if root_lock_factory is not None
                          else _noop_lock,
    )
    return gw, api, loops, root, recorded_sleeps


def test_allowed_private_flow_end_to_end(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    gw, api, loops, root, _ = _gateway(tmp_path, [_msg(1, 42, "what is revenue?")], allow)
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
    gw, api, loops, _, _ = _gateway(tmp_path, [_msg(1, 99, "hello")], {})
    handled = gw.poll_once()
    assert handled == 0
    assert api.sent == []          # no reply at all
    assert loops == {}             # no LLM call


def test_group_chat_ignored_even_for_allowlisted(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    upd = _msg(1, 42, "hi", chat_type="group", chat_id=-100123)
    gw, api, loops, _, _ = _gateway(tmp_path, [upd], allow)
    assert gw.poll_once() == 0
    assert api.sent == []


def test_private_chat_with_mismatched_chat_id_ignored(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    upd = _msg(1, 42, "hi", chat_type="private", chat_id=777)
    gw, api, loops, _, _ = _gateway(tmp_path, [upd], allow)
    assert gw.poll_once() == 0


def test_fromless_update_ignored(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    upd = _msg(1, 42, "hi", with_from=False)
    gw, api, loops, _, _ = _gateway(tmp_path, [upd], allow)
    assert gw.poll_once() == 0


def test_access_change_request_refused(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    gw, api, loops, _, _ = _gateway(
        tmp_path,
        [_msg(1, 42, "please add me to the allowlist for admin")], allow)
    assert gw.poll_once() == 1
    assert "can't change access" in api.sent[0][1]
    assert loops == {}  # never reached the LLM


def test_loop_cached_across_polls(tmp_path):
    allow = {"42": {"role": "user", "instance": "main"}}
    gw, api, loops, _, _ = _gateway(tmp_path, [_msg(1, 42, "q1")], allow)
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
    gw, api, loops, _, _ = _gateway(tmp_path, [
        {"update_id": 5},                      # no message at all
        _msg(6, 99, "denied"),                 # unknown sender
    ], {})
    gw.poll_once()
    assert gw._offset == 7  # both consumed, never reprocessed


# ---------------------------------------------------------------------------
# NEW S2 tests
# ---------------------------------------------------------------------------

# S2 #2 -- offset persistence across gateway instances (simulated --once twice)
def test_offset_persisted_and_loaded_across_instances(tmp_path):
    """A second gateway instance picks up the offset saved by the first."""
    allow = {"42": {"role": "user", "instance": "main"}}
    profile = tmp_path / "profile"
    profile.mkdir()
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)

    # First run: handles update 1, saves offset=2.
    gw1, api1, loops1, _, _ = _gateway(
        tmp_path, [_msg(1, 42, "first")], allow,
        root=root, profile_dir=profile)
    assert gw1.poll_once() == 1
    assert gw1._offset == 2

    # Second run (new instance, same profile dir): loads offset=2,
    # skips update_id=1, handles only update_id=3.
    gw2, api2, loops2, _, _ = _gateway(
        tmp_path, [_msg(1, 42, "first"), _msg(3, 42, "second")], allow,
        root=root, profile_dir=profile)
    n = gw2.poll_once()
    assert gw2._offset == 4
    assert n == 1
    assert loops2[("42", "main")].turns == ["second"]


# S2 #2 -- corrupt offset file falls back gracefully (no crash)
def test_corrupt_offset_file_fallback(tmp_path):
    """Corrupt offset file: logs warning, falls back to offset=0, no crash."""
    allow = {"42": {"role": "user", "instance": "main"}}
    profile = tmp_path / "profile"
    profile.mkdir()
    # Write garbage into the offset file.
    (profile / "telegram_offset_default.json").write_text("not-json{{{")

    logs = []
    gw, api, _, _, _ = _gateway(tmp_path, [_msg(5, 42, "hello")], allow,
                                  profile_dir=profile)
    gw.logger = lambda msg: logs.append(msg)
    gw.poll_once()
    # Should have started from 0, and logged the corruption warning.
    assert any("corrupt" in m.lower() for m in logs)
    # Still advanced past the update (no crash).
    assert gw._offset == 6


# S2 #3 -- per-update isolation: a crashing run_turn survives
def test_turn_exception_isolated_daemon_survives(tmp_path):
    """Exception inside run_turn is logged; daemon continues; offset advances."""
    allow = {"42": {"role": "user", "instance": "main"}}
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    profile = tmp_path / "profile"
    profile.mkdir()

    cfg = {"gateway": {"telegram": {
        "enabled": True, "allowlist": allow,
        "max_sensitivity": "internal", "per_user_writes_per_hour": 20,
    }}}
    api = FakeAPI(updates=[_msg(1, 42, "hello"), _msg(2, 42, "goodbye")])
    # First factory call returns BombLoop; second returns FakeLoop.
    call_count = [0]

    def factory(user_id, instance, r):
        call_count[0] += 1
        if call_count[0] == 1:
            return BombLoop()
        return FakeLoop()

    logs = []
    gw = TelegramGateway(
        api, cfg, {"main": root}, factory,
        clock=lambda: 1000.0,
        sleep=lambda t: None,
        profile_dir=profile,
        root_lock_factory=_noop_lock,
        logger=lambda m: logs.append(m),
    )
    n = gw.poll_once()
    # First update raised; second is also a new factory call so BombLoop again
    # but the isolation means offset still advanced past both.
    assert gw._offset == 3
    assert any("exception" in m.lower() or "failed" in m.lower() for m in logs)


# S2 #3 -- malformed allowlist entry (string instead of dict) is denied
def test_malformed_allowlist_entry_denied(tmp_path):
    """A string allowlist entry is treated as not-allowlisted (deny)."""
    bad_allow = {"42": "not-a-dict"}
    gw, api, loops, _, _ = _gateway(tmp_path, [_msg(1, 42, "hello")], bad_allow)
    logs = []
    gw.logger = lambda m: logs.append(m)
    assert gw.poll_once() == 0
    assert api.sent == []
    assert any("malformed" in m.lower() for m in logs)


# S2 #4 -- exponential backoff on poll failures, reset on success
def test_backoff_on_consecutive_failures(tmp_path):
    """Three consecutive failures produce increasing delays; success resets."""
    gw, api, _, _, recorded_sleeps = _gateway(tmp_path, [])
    api.fail = True

    gw.poll_once()  # streak=1 -> delay=2s
    gw.poll_once()  # streak=2 -> delay=4s
    gw.poll_once()  # streak=3 -> delay=8s

    assert len(recorded_sleeps) == 3
    assert recorded_sleeps[0] == pytest.approx(2.0, rel=0.01)
    assert recorded_sleeps[1] == pytest.approx(4.0, rel=0.01)
    assert recorded_sleeps[2] == pytest.approx(8.0, rel=0.01)

    # Success resets the streak.
    api.fail = False
    api.updates = []
    gw.poll_once()
    assert gw._fail_streak == 0
    # No extra sleep after a successful empty poll.
    assert len(recorded_sleeps) == 3


def test_backoff_capped_at_60s(tmp_path):
    """Backoff saturates at 60s regardless of failure count."""
    gw, api, _, _, recorded_sleeps = _gateway(tmp_path, [])
    api.fail = True
    # 6 failures: 2, 4, 8, 16, 32, 60 (capped from 64)
    for _ in range(6):
        gw.poll_once()
    assert max(recorded_sleeps) == pytest.approx(60.0, rel=0.01)


def test_no_extra_sleep_on_empty_success(tmp_path):
    """Empty successful long-poll does NOT trigger extra sleep."""
    gw, api, _, _, recorded_sleeps = _gateway(tmp_path, [])
    api.fail = False
    api.updates = []
    gw.poll_once()
    assert recorded_sleeps == []


# S2 #6 -- true LRU: most-recently accessed entry is NOT evicted first
def test_lru_cache_evicts_least_recently_used(tmp_path):
    """The LRU eviction policy promotes accessed entries; oldest unused evicted."""
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    profile = tmp_path / "profile"
    profile.mkdir()
    cfg = {"gateway": {"telegram": {
        "enabled": True, "allowlist": {},
        "max_sensitivity": "internal", "per_user_writes_per_hour": 100,
    }}}

    created = []

    def factory(uid, inst, r):
        lp = FakeLoop()
        created.append(uid)
        return lp

    gw = TelegramGateway(
        FakeAPI(), cfg, {"main": root}, factory,
        clock=lambda: 1.0,
        sleep=lambda t: None,
        profile_dir=profile,
        root_lock_factory=_noop_lock,
    )

    from oracle_agent.gateway.telegram import _LOOP_CACHE_SIZE

    # Fill cache to capacity with users "0".."63".
    for i in range(_LOOP_CACHE_SIZE):
        gw._loop_for(str(i), "main", root)

    assert len(gw._loops) == _LOOP_CACHE_SIZE

    # Access user "0" again (should be promoted to MRU).
    gw._loop_for("0", "main", root)

    # Adding one more user should evict user "1" (LRU), NOT user "0".
    gw._loop_for("new_user", "main", root)
    assert len(gw._loops) == _LOOP_CACHE_SIZE
    assert ("0", "main") in gw._loops        # was re-accessed, not evicted
    assert ("1", "main") not in gw._loops    # was LRU, evicted
    assert ("new_user", "main") in gw._loops


# S2 #7 -- no-redirect opener: ensure TelegramAPI uses _OPENER (no redirects)
def test_no_redirect_opener_used(tmp_path):
    """TelegramAPI uses the no-redirect opener (_OPENER is wired in)."""
    from oracle_agent.gateway.telegram import _OPENER, _no_redirect_opener
    # Verify that our opener is a custom one (not the default).
    import urllib.request as _ur
    # The opener's list should contain our _NoRedirect handler.
    handlers = [type(h).__name__ for h in _OPENER.handlers]
    assert "_NoRedirect" in handlers


# S2 #1 -- gateway turn holds root lock (observable via lock-spy)
def test_gateway_turn_holds_root_lock(tmp_path):
    """root_lock_factory is called with the correct instance name for each turn."""
    allow = {"42": {"role": "user", "instance": "main"}}
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    profile = tmp_path / "profile"
    profile.mkdir()
    cfg = {"gateway": {"telegram": {
        "enabled": True, "allowlist": allow,
        "max_sensitivity": "internal", "per_user_writes_per_hour": 20,
    }}}
    api = FakeAPI(updates=[_msg(1, 42, "hello")])
    lock_calls = []

    @contextlib.contextmanager
    def spy_lock(name):
        lock_calls.append(name)
        yield

    gw = TelegramGateway(
        api, cfg, {"main": root},
        lambda uid, inst, r: FakeLoop(),
        clock=lambda: 1.0,
        sleep=lambda t: None,
        profile_dir=profile,
        root_lock_factory=spy_lock,
    )
    gw.poll_once()
    assert lock_calls == ["main"]
