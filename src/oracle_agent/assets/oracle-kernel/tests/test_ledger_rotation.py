#!/usr/bin/env python3
"""Tests for cross-segment ledger rotation (P5-T8, P5S-8/9).

Covers the frozen ledger-rotation interface: ``rotate`` / ``verify_chain`` /
``load_window``, the tamper-evident hash-chained segment manifest + chained
HEAD pointer, rotation markers re-anchoring the row_hash chain, rotation under
the append lock with the no-row-after-marker invariant, and windowed reads.

Self-contained: depends only on ledger.py (a floor module) plus stdlib.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
import threading
import time
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import ledger  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _seed(path: Path, n: int, *, prefix: str = "EV") -> None:
    for i in range(n):
        ledger.append(path, {"drop_id": f"{prefix}-{i:04d}", "ts": f"2026-06-08T10:{i:02d}:00", "v": i})


def _force_rotate(path: Path) -> dict:
    """Force rotation of any non-empty open segment (max_bytes=0)."""
    return ledger.rotate(path, max_bytes=0, max_age_days=None)


# --------------------------------------------------------------------------- #
# rotation basics: closed segment + re-anchored new segment
# --------------------------------------------------------------------------- #
def test_rotate_seals_segment_and_reanchors(tmp_path: Path):
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    _seed(path, 5)

    rep = _force_rotate(path)
    assert rep["rotated"] is True
    assert rep["segment"] == "action_event.seg-0001.jsonl"
    assert rep["seq"] == 1
    assert rep["terminal_row_hash"]

    # The sealed segment exists and ends in a ROTATION-MARKER.
    seg = d / "action_event.seg-0001.jsonl"
    assert seg.exists()
    seg_rows, _ = ledger.load(seg)
    assert seg_rows[-1]["drop_id"] == ledger.ROTATION_MARKER
    # No data row follows the marker.
    assert all(
        seg_rows[i]["drop_id"] != ledger.ROTATION_MARKER for i in range(len(seg_rows) - 1)
    )

    # The open file now begins with a ROTATION-ANCHOR that names the predecessor.
    open_rows, _ = ledger.load(path)
    assert open_rows[0]["drop_id"] == ledger.ROTATION_ANCHOR
    assert open_rows[0]["predecessor_segment"] == "action_event.seg-0001.jsonl"
    assert open_rows[0]["predecessor_row_hash"] == rep["terminal_row_hash"]

    # The sealed segment itself verifies internally.
    assert ledger.verify(seg)["ok"] is True


def test_new_segment_chain_anchors_to_predecessor_terminal(tmp_path: Path):
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    _seed(path, 3)
    rep = _force_rotate(path)

    # After rotation, appended rows chain off the anchor; the anchor's
    # predecessor hash equals the sealed terminal. Note: the re-anchored open
    # file does NOT validate under plain ``verify`` (which assumes genesis-prev);
    # it is verifiable only via ``verify_chain`` which knows the anchor hash.
    _seed(path, 3, prefix="NEW")
    open_rows, _ = ledger.load(path)
    assert open_rows[0]["drop_id"] == ledger.ROTATION_ANCHOR
    assert open_rows[0]["predecessor_row_hash"] == rep["terminal_row_hash"]
    assert ledger.verify_chain(d, "action_event")["ok"] is True


# --------------------------------------------------------------------------- #
# cross-segment verify_chain: intact set passes
# --------------------------------------------------------------------------- #
def test_verify_chain_intact_multi_segment(tmp_path: Path):
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    # Build 3 sealed segments + an open one.
    for _ in range(3):
        _seed(path, 4)
        _force_rotate(path)
    _seed(path, 2)  # open segment rows

    rep = ledger.verify_chain(d, "action_event")
    assert rep["ok"] is True, rep
    assert rep["manifest_breaks"] == []
    assert rep["segment_breaks"] == []
    assert rep["anchor_breaks"] == []
    assert rep["missing_segments"] == []
    assert rep["head_ok"] is True
    assert len(rep["segments"]) == 3


def test_verify_chain_no_manifest_is_legacy_single_file(tmp_path: Path):
    """A ledger never rotated verifies as a single open file (backward compat)."""
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    _seed(path, 5)
    rep = ledger.verify_chain(d, "action_event")
    assert rep["ok"] is True
    assert rep.get("legacy_single_file") is True


# --------------------------------------------------------------------------- #
# verify_chain detects tampering: edit, removed middle, removed HEAD
# --------------------------------------------------------------------------- #
def test_verify_chain_detects_edited_sealed_segment(tmp_path: Path):
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    for _ in range(2):
        _seed(path, 4)
        _force_rotate(path)
    _seed(path, 2)

    # Tamper with a row inside sealed segment 1.
    seg = d / "action_event.seg-0001.jsonl"
    lines = seg.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[1])
    obj["v"] = 9999
    lines[1] = json.dumps(obj, ensure_ascii=False)
    seg.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rep = ledger.verify_chain(d, "action_event")
    assert rep["ok"] is False
    assert 1 in rep["segment_breaks"]


def test_verify_chain_detects_removed_middle_segment(tmp_path: Path):
    """P5S-8: a removed MIDDLE segment is detected."""
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    for _ in range(3):
        _seed(path, 4)
        _force_rotate(path)
    _seed(path, 2)

    # Remove the middle sealed segment's FILE (leave the manifest entry).
    mid = d / "action_event.seg-0002.jsonl"
    assert mid.exists()
    mid.unlink()

    rep = ledger.verify_chain(d, "action_event")
    assert rep["ok"] is False
    assert "action_event.seg-0002.jsonl" in rep["missing_segments"]


def test_verify_chain_detects_removed_middle_segment_and_manifest_entry(tmp_path: Path):
    """Deleting a middle segment file AND scrubbing its manifest entry is caught.

    Even if an attacker also removes the manifest row to hide the gap, the
    manifest's own hash chain (and the cross-segment anchor walk) break.
    """
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    for _ in range(3):
        _seed(path, 4)
        _force_rotate(path)
    _seed(path, 2)

    mid = d / "action_event.seg-0002.jsonl"
    mid.unlink()
    # Scrub the seg-0002 manifest row, leaving seg-0001 + seg-0003 + head.
    mpath = d / "action_event.manifest.jsonl"
    entries = [json.loads(l) for l in mpath.read_text(encoding="utf-8").splitlines() if l.strip()]
    kept = [e for e in entries if not (e.get("kind") == "segment" and e.get("seq") == 2)]
    mpath.write_text("\n".join(json.dumps(e) for e in kept) + "\n", encoding="utf-8")

    rep = ledger.verify_chain(d, "action_event")
    assert rep["ok"] is False
    # The manifest chain breaks at seg-0003 (its prev manifest_hash no longer
    # matches) and/or the cross-segment anchor walk breaks.
    assert rep["manifest_breaks"] or rep["anchor_breaks"]


def test_verify_chain_detects_removed_head_segment(tmp_path: Path):
    """P5S-8: a removed HEAD/latest (open) segment is detected by the pointer."""
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    for _ in range(2):
        _seed(path, 4)
        _force_rotate(path)
    _seed(path, 3)  # open HEAD segment has rows

    # Remove the open HEAD segment file entirely.
    path.unlink()

    rep = ledger.verify_chain(d, "action_event")
    assert rep["ok"] is False
    assert rep["head_ok"] is False
    assert "action_event.jsonl" in rep["missing_segments"]


def test_verify_chain_detects_removed_newest_sealed_segment(tmp_path: Path):
    """Removing the newest SEALED segment (just before HEAD) is detected."""
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    for _ in range(3):
        _seed(path, 4)
        _force_rotate(path)
    _seed(path, 2)

    newest = d / "action_event.seg-0003.jsonl"
    newest.unlink()
    rep = ledger.verify_chain(d, "action_event")
    assert rep["ok"] is False
    assert "action_event.seg-0003.jsonl" in rep["missing_segments"]


def test_verify_chain_detects_reordered_segments(tmp_path: Path):
    """Swapping two segment files (renumber) breaks the anchor walk."""
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    for _ in range(3):
        _seed(path, 4)
        _force_rotate(path)
    _seed(path, 2)

    s1 = d / "action_event.seg-0001.jsonl"
    s2 = d / "action_event.seg-0002.jsonl"
    a = s1.read_text(encoding="utf-8")
    b = s2.read_text(encoding="utf-8")
    s1.write_text(b, encoding="utf-8")
    s2.write_text(a, encoding="utf-8")

    rep = ledger.verify_chain(d, "action_event")
    assert rep["ok"] is False


def test_verify_chain_detects_manifest_head_edit(tmp_path: Path):
    """Editing the HEAD manifest entry breaks the manifest hash chain."""
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    _seed(path, 4)
    _force_rotate(path)
    _seed(path, 2)

    mpath = d / "action_event.manifest.jsonl"
    entries = [json.loads(l) for l in mpath.read_text(encoding="utf-8").splitlines() if l.strip()]
    for e in entries:
        if e.get("kind") == "head":
            e["last_sealed_terminal_hash"] = "deadbeef" * 8
    mpath.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    rep = ledger.verify_chain(d, "action_event")
    assert rep["ok"] is False
    assert "head" in rep["manifest_breaks"] or rep["head_ok"] is False


# --------------------------------------------------------------------------- #
# no-row-after-marker invariant under concurrent appenders (P5S-9)
# --------------------------------------------------------------------------- #
def _assert_no_row_after_marker(d: Path, name: str) -> None:
    for seg in sorted(d.glob(f"{name}.seg-*.jsonl")):
        rows, _ = ledger.load(seg)
        assert rows, f"empty sealed segment {seg}"
        marker_idx = [i for i, r in enumerate(rows) if r.get("drop_id") == ledger.ROTATION_MARKER]
        assert marker_idx, f"sealed segment {seg} has no rotation marker"
        # Exactly one marker and it is the LAST physical row.
        assert len(marker_idx) == 1, f"multiple markers in {seg}"
        assert marker_idx[0] == len(rows) - 1, f"row follows marker in {seg}"


def test_rotation_vs_concurrent_appenders_threads(tmp_path: Path):
    """Race N appender threads (auto_rotate) against frequent rotation triggers.

    Every appender uses auto_rotate=True with a tiny byte threshold so rotation
    fires repeatedly mid-flight. The invariant: no row ever follows a rotation
    marker in any sealed segment, and the full reconstructed chain verifies.
    """
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    # Force a very small rotation threshold so rotation fires constantly.
    orig = ledger.ROTATE_MAX_BYTES
    ledger.ROTATE_MAX_BYTES = 200
    try:
        n_threads = 8
        per_thread = 25

        def worker(tid: int):
            for i in range(per_thread):
                ledger.append(
                    path,
                    {"drop_id": f"T{tid}-{i:03d}", "ts": "2026-06-08T10:00:00", "tid": tid, "i": i},
                    auto_rotate=True,
                )

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        ledger.ROTATE_MAX_BYTES = orig

    # Invariant: no row after any marker in any sealed segment.
    _assert_no_row_after_marker(d, "action_event")

    # Every data row written is present exactly once across all segments.
    all_ids: list[str] = []
    for seg in sorted(d.glob("action_event.seg-*.jsonl")):
        rows, _ = ledger.load(seg)
        all_ids += [r["drop_id"] for r in rows if r["drop_id"] not in (ledger.ROTATION_MARKER, ledger.ROTATION_ANCHOR)]
    open_rows, _ = ledger.load(path)
    all_ids += [r["drop_id"] for r in open_rows if r["drop_id"] not in (ledger.ROTATION_MARKER, ledger.ROTATION_ANCHOR)]
    expected = {f"T{t}-{i:03d}" for t in range(8) for i in range(25)}
    assert set(all_ids) == expected
    assert len(all_ids) == len(expected), "rows lost or duplicated under rotation race"

    # The whole cross-segment chain verifies after the race.
    rep = ledger.verify_chain(d, "action_event")
    assert rep["ok"] is True, rep


def _proc_worker(path_str: str, tid: int, per: int, thresh: int):
    import sys as _sys
    _t = str(Path(__file__).resolve().parents[1] / "_tools")
    if _t not in _sys.path:
        _sys.path.insert(0, _t)
    import ledger as _ledger
    _ledger.ROTATE_MAX_BYTES = thresh
    for i in range(per):
        _ledger.append(
            Path(path_str),
            {"drop_id": f"P{tid}-{i:03d}", "ts": "2026-06-08T10:00:00", "tid": tid},
            auto_rotate=True,
        )


def test_rotation_vs_concurrent_appenders_processes(tmp_path: Path):
    """Race N appender PROCESSES (real fcntl LOCK_EX across processes)."""
    d = tmp_path / "ledgers"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "action_event.jsonl"
    n_proc = 5
    per = 20
    thresh = 250
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_proc_worker, args=(str(path), t, per, thresh))
        for t in range(n_proc)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"appender process failed (exit {p.exitcode})"

    _assert_no_row_after_marker(d, "action_event")

    all_ids: list[str] = []
    for seg in sorted(d.glob("action_event.seg-*.jsonl")):
        rows, _ = ledger.load(seg)
        all_ids += [r["drop_id"] for r in rows if r["drop_id"] not in (ledger.ROTATION_MARKER, ledger.ROTATION_ANCHOR)]
    open_rows, _ = ledger.load(path)
    all_ids += [r["drop_id"] for r in open_rows if r["drop_id"] not in (ledger.ROTATION_MARKER, ledger.ROTATION_ANCHOR)]
    expected = {f"P{t}-{i:03d}" for t in range(n_proc) for i in range(per)}
    assert set(all_ids) == expected
    assert len(all_ids) == len(expected)


# --------------------------------------------------------------------------- #
# auto-rotation inside append at threshold
# --------------------------------------------------------------------------- #
def test_append_auto_rotates_at_byte_threshold(tmp_path: Path):
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    orig = ledger.ROTATE_MAX_BYTES
    ledger.ROTATE_MAX_BYTES = 300
    try:
        for i in range(40):
            ledger.append(path, {"drop_id": f"A-{i:03d}", "ts": "2026-06-08T10:00:00", "pad": "x" * 20}, auto_rotate=True)
    finally:
        ledger.ROTATE_MAX_BYTES = orig

    # At least one sealed segment was produced inline by the appender.
    sealed = sorted(d.glob("action_event.seg-*.jsonl"))
    assert sealed, "auto-rotation did not seal any segment"
    rep = ledger.verify_chain(d, "action_event")
    assert rep["ok"] is True, rep


def test_append_without_auto_rotate_never_rotates(tmp_path: Path):
    """Default append (auto_rotate=False) leaves a single growing file."""
    d = tmp_path / "ledgers"
    path = d / "events.jsonl"
    orig = ledger.ROTATE_MAX_BYTES
    ledger.ROTATE_MAX_BYTES = 50
    try:
        for i in range(20):
            ledger.append(path, {"drop_id": f"E-{i}", "ts": "t"})  # no auto_rotate
    finally:
        ledger.ROTATE_MAX_BYTES = orig
    assert not list(d.glob("events.seg-*.jsonl"))


# --------------------------------------------------------------------------- #
# load_window: bounded tail read equals full load filtered to window
# --------------------------------------------------------------------------- #
def test_load_window_equals_full_load_filtered(tmp_path: Path):
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"

    # Build segments with increasing timestamps across the day.
    all_written: list[dict] = []
    ts_counter = 0

    def write_batch(n: int):
        nonlocal ts_counter
        for _ in range(n):
            ts = f"2026-06-08T{ts_counter // 60:02d}:{ts_counter % 60:02d}:00"
            row = {"drop_id": f"W-{ts_counter:04d}", "ts": ts, "v": ts_counter}
            ledger.append(path, row)
            all_written.append(row)
            ts_counter += 1

    write_batch(10)
    _force_rotate(path)
    write_batch(10)
    _force_rotate(path)
    write_batch(10)  # open segment

    since = "2026-06-08T00:15:00"
    win_rows, win_warnings = ledger.load_window(d, "action_event", since=since)
    assert win_warnings == []

    # Reference: full load of EVERY segment, filtered to ts >= since, excluding
    # bookkeeping rows.
    ref: list[dict] = []
    for seg in sorted(d.glob("action_event.seg-*.jsonl")) + [path]:
        rows, _ = ledger.load(seg)
        ref += [
            r for r in rows
            if r.get("drop_id") not in (ledger.ROTATION_MARKER, ledger.ROTATION_ANCHOR)
            and str(r.get("ts", "")) >= since
        ]
    ref.sort(key=lambda r: str(r["ts"]))

    assert [r["drop_id"] for r in win_rows] == [r["drop_id"] for r in ref]
    # And it equals the in-window subset of everything we wrote.
    expected_ids = sorted(r["drop_id"] for r in all_written if r["ts"] >= since)
    assert sorted(r["drop_id"] for r in win_rows) == expected_ids


def test_load_window_stops_before_old_segments(tmp_path: Path):
    """A window covering only recent rows does not include old-segment data."""
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"

    for i in range(5):
        ledger.append(path, {"drop_id": f"OLD-{i}", "ts": f"2026-01-0{i+1}T10:00:00"})
    _force_rotate(path)
    for i in range(5):
        ledger.append(path, {"drop_id": f"NEW-{i}", "ts": f"2026-06-0{i+1}T10:00:00"})

    win, _ = ledger.load_window(d, "action_event", since="2026-06-01T00:00:00")
    ids = {r["drop_id"] for r in win}
    assert ids == {f"NEW-{i}" for i in range(5)}
    assert not any(i.startswith("OLD-") for i in ids)


def test_load_window_no_manifest_reads_open_only(tmp_path: Path):
    """With no rotation, load_window reads the single open file."""
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    for i in range(5):
        ledger.append(path, {"drop_id": f"E-{i}", "ts": f"2026-06-08T10:0{i}:00"})
    win, _ = ledger.load_window(d, "action_event", since="2026-06-08T10:02:00")
    assert {r["drop_id"] for r in win} == {"E-2", "E-3", "E-4"}


# --------------------------------------------------------------------------- #
# rotate is a no-op below threshold / on empty
# --------------------------------------------------------------------------- #
def test_rotate_noop_below_threshold(tmp_path: Path):
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    _seed(path, 3)
    rep = ledger.rotate(path, max_bytes=10_000_000, max_age_days=None)
    assert rep["rotated"] is False
    assert not list(d.glob("action_event.seg-*.jsonl"))


def test_rotate_noop_on_empty(tmp_path: Path):
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    rep = ledger.rotate(path, max_bytes=0)
    assert rep["rotated"] is False


def test_rotate_age_threshold(tmp_path: Path):
    """An old first row triggers age-based rotation even under the size limit."""
    d = tmp_path / "ledgers"
    path = d / "action_event.jsonl"
    # First row is well in the past.
    ledger.append(path, {"drop_id": "OLD-0", "ts": "2020-01-01T00:00:00", "v": 0})
    ledger.append(path, {"drop_id": "OLD-1", "ts": "2020-01-01T00:01:00", "v": 1})
    rep = ledger.rotate(path, max_bytes=10_000_000, max_age_days=30)
    assert rep["rotated"] is True
    assert (d / "action_event.seg-0001.jsonl").exists()
