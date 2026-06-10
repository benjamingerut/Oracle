"""Tests for service/scheduler.py (SPEC S6 / S10)."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from oracle_agent.service import scheduler


def _ledger_count(root: Path) -> int:
    p = root / "Meta.nosync" / "ledgers" / "action_event.jsonl"
    if not p.exists():
        return 0
    return sum(1 for _ in p.read_text().splitlines())


def test_autonomy_off_by_default(spawned_root):
    assert scheduler.autonomy_enabled(spawned_root) is False


def test_tick_skips_when_autonomy_off(spawned_root, profile):
    before = _ledger_count(spawned_root)
    res = scheduler.tick_instance("t", spawned_root)
    assert res.skipped is True
    assert res.rc == 0
    # No harness spawned => no new action_event rows (A5).
    assert _ledger_count(spawned_root) == before


def test_tick_missing_root(profile, tmp_path):
    res = scheduler.tick_instance("t", tmp_path / "nope")
    assert res.rc == 2
    assert res.skipped is False


def test_root_lock_serializes(profile):
    order = []

    def worker(tag, hold):
        with scheduler.root_lock("inst"):
            order.append(f"{tag}-in")
            time.sleep(hold)
            order.append(f"{tag}-out")

    t1 = threading.Thread(target=worker, args=("a", 0.2))
    t1.start()
    time.sleep(0.05)
    t2 = threading.Thread(target=worker, args=("b", 0.0))
    t2.start()
    t1.join()
    t2.join()
    # b must not enter until a has exited.
    assert order == ["a-in", "a-out", "b-in", "b-out"]


def test_serve_lock_is_exclusive(profile):
    fh = scheduler.acquire_serve_lock()
    assert fh is not None
    second = scheduler.acquire_serve_lock()
    assert second is None  # already held
    fh.close()
    third = scheduler.acquire_serve_lock()
    assert third is not None
    third.close()


def test_tick_all_isolated_failure(profile, spawned_root, tmp_path):
    insts = {"good": spawned_root, "bad": tmp_path / "missing"}
    results = {r.instance: r for r in scheduler.tick_all(insts)}
    assert results["good"].rc == 0
    assert results["bad"].rc == 2
