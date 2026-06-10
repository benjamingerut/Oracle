#!/usr/bin/env python3
"""Tests for the durable JSONL ledger (ledger.py).

Self-contained: depends only on ledger.py (a floor module) plus stdlib. Adds the
kernel ``_tools`` directory to sys.path so the test runs in isolation even
before a shared conftest exists.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import ledger  # noqa: E402


# --------------------------------------------------------------------------- #
# append / load round-trip
# --------------------------------------------------------------------------- #
def test_append_load_roundtrip(tmp_path: Path):
    path = tmp_path / "ledgers" / "events.jsonl"
    rows_in = [
        {"drop_id": "EV-20260608-001", "ts": "2026-06-08T10:00:00", "kind": "a"},
        {"drop_id": "EV-20260608-002", "ts": "2026-06-08T10:01:00", "kind": "b"},
        {"drop_id": "EV-20260608-003", "ts": "2026-06-08T10:02:00", "kind": "c"},
    ]
    for r in rows_in:
        ledger.append(path, r)

    rows, warnings = ledger.load(path)
    assert warnings == []
    assert [r["drop_id"] for r in rows] == [r["drop_id"] for r in rows_in]
    assert [r["kind"] for r in rows] == ["a", "b", "c"]
    # The physical file is one JSON object per line.
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 3
    for ln in lines:
        obj = json.loads(ln)
        assert "drop_id" in obj and "ts" in obj


def test_append_stamps_ts_and_id_when_missing(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    ledger.append(path, {"kind": "no-id-no-ts"})
    rows, warnings = ledger.load(path)
    assert warnings == []
    assert len(rows) == 1
    assert rows[0]["drop_id"]  # auto-minted
    assert rows[0]["ts"]  # auto-stamped


def test_append_rejects_non_dict(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    with pytest.raises(TypeError):
        ledger.append(path, ["not", "a", "dict"])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# corruption tolerance: one bad line does NOT brick load
# --------------------------------------------------------------------------- #
def test_corrupt_line_quarantined_does_not_brick(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    ledger.append(path, {"drop_id": "EV-1", "ts": "t", "v": 1})
    # Inject a non-JSON line in the middle, the hard way.
    with open(path, "a", encoding="utf-8") as f:
        f.write("THIS IS NOT JSON {[}\n")
    ledger.append(path, {"drop_id": "EV-2", "ts": "t", "v": 2})

    rows, warnings = ledger.load(path)
    # Good rows survive; the bad line is reported, not raised.
    assert [r["drop_id"] for r in rows] == ["EV-1", "EV-2"]
    assert any("quarantin" in w.lower() for w in warnings)

    # The bad line landed in the quarantine sidecar.
    qpath = path.with_name(path.name + ".quarantine")
    assert qpath.exists()
    assert "THIS IS NOT JSON" in qpath.read_text(encoding="utf-8")


def test_load_never_raises_on_garbage(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text("\x00\x01garbage\nnot json\n[1,2,3]\n{}\n", encoding="utf-8")
    # [1,2,3] parses as JSON but is not an object -> quarantined; {} is an object.
    rows, warnings = ledger.load(path)
    assert isinstance(rows, list)
    assert isinstance(warnings, list)
    assert rows == [{}]  # only the empty object is a valid row
    assert warnings  # several warnings recorded


def test_load_missing_file_is_empty(tmp_path: Path):
    rows, warnings = ledger.load(tmp_path / "nope.jsonl")
    assert rows == []
    assert warnings == []


# --------------------------------------------------------------------------- #
# atomic rewrite
# --------------------------------------------------------------------------- #
def test_rewrite_atomic_replaces_content(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    for i in range(5):
        ledger.append(path, {"drop_id": f"EV-{i}", "ts": "t", "v": i})

    keep = [{"drop_id": "EV-0", "ts": "t", "v": 0}, {"drop_id": "EV-4", "ts": "t", "v": 4}]
    ledger.rewrite_atomic(path, keep)

    rows, warnings = ledger.load(path)
    assert warnings == []
    # rewrite_atomic appends a REWRITE-MARKER row for auditability.
    data_ids = [r["drop_id"] for r in rows if r.get("drop_id") != "REWRITE-MARKER"]
    assert data_ids == ["EV-0", "EV-4"]
    marker_rows = [r for r in rows if r.get("drop_id") == "REWRITE-MARKER"]
    assert len(marker_rows) == 1
    assert marker_rows[0].get("event") == "ledger_rewrite"
    # No stray temp files left behind.
    leftovers = [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_rewrite_atomic_empty(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    ledger.append(path, {"drop_id": "EV-0", "ts": "t"})
    ledger.rewrite_atomic(path, [])
    rows, warnings = ledger.load(path)
    assert warnings == []
    # Even an empty rewrite leaves an auditable REWRITE-MARKER row.
    assert len(rows) == 1
    assert rows[0]["drop_id"] == "REWRITE-MARKER"
    assert rows[0].get("event") == "ledger_rewrite"


# --------------------------------------------------------------------------- #
# next_id collision safety
# --------------------------------------------------------------------------- #
def test_next_id_format(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    nid = ledger.next_id(path, "IN")
    assert nid.startswith("IN-")
    # IN-YYYYMMDD-NNN
    parts = nid.split("-")
    assert len(parts) == 3
    assert parts[0] == "IN"
    assert len(parts[1]) == 8 and parts[1].isdigit()
    assert len(parts[2]) == 3 and parts[2].isdigit()


def test_next_id_no_collision_sequential(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    ids = []
    for _ in range(5):
        nid = ledger.next_id(path, "IN")
        ledger.append(path, {"drop_id": nid, "ts": "t"})
        ids.append(nid)
    assert len(set(ids)) == 5  # all unique
    # Strictly increasing sequence numbers.
    seqs = [int(i.split("-")[2]) for i in ids]
    assert seqs == sorted(seqs)
    assert seqs[0] == 1


def test_next_id_no_collision_concurrent(tmp_path: Path):
    # The concurrency-safe pattern mints + writes under one lock via id_prefix=.
    path = tmp_path / "events.jsonl"
    minted: list[str] = []
    lock = threading.Lock()

    def worker():
        nid = ledger.append(path, {"ts": "t"}, id_prefix="IN")
        with lock:
            minted.append(nid)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows, warnings = ledger.load(path)
    assert warnings == []
    on_disk = [r["drop_id"] for r in rows]
    # Every minted id is unique and every appended row is present and unique.
    assert len(set(minted)) == len(minted)
    assert len(set(on_disk)) == len(on_disk)
    assert len(on_disk) == 12
    # Sequence numbers form a contiguous 1..12 set with no collision.
    seqs = sorted(int(i.split("-")[2]) for i in on_disk)
    assert seqs == list(range(1, 13))


# --------------------------------------------------------------------------- #
# verify / repair
# --------------------------------------------------------------------------- #
def test_verify_clean(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    ledger.append(path, {"drop_id": "EV-1", "ts": "t"})
    ledger.append(path, {"drop_id": "EV-2", "ts": "t"})
    report = ledger.verify(path)
    assert report["ok"] is True
    assert report["ok_rows"] == 2
    assert report["bad_lines"] == []
    assert report["duplicate_ids"] == []


def test_verify_detects_problems(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    ledger.append(path, {"drop_id": "EV-1", "ts": "t"})
    ledger.append(path, {"drop_id": "EV-1", "ts": "t"})  # duplicate id
    with open(path, "a", encoding="utf-8") as f:
        f.write("broken line\n")
        f.write(json.dumps({"ts": "t"}) + "\n")  # missing drop_id
    report = ledger.verify(path)
    assert report["ok"] is False
    assert "EV-1" in report["duplicate_ids"]
    assert report["bad_lines"]  # the "broken line"
    assert report["missing_keys"]  # the missing drop_id row


def test_repair_cleans_and_dedupes(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    ledger.append(path, {"drop_id": "EV-1", "ts": "t", "v": 1})
    ledger.append(path, {"drop_id": "EV-1", "ts": "t", "v": 2})  # dup -> first kept
    with open(path, "a", encoding="utf-8") as f:
        f.write("garbage!!!\n")
    ledger.append(path, {"drop_id": "EV-2", "ts": "t", "v": 3})

    result = ledger.repair(path)
    assert result["dropped_duplicates"] == 1
    assert result["quarantined"] == 1

    rows, warnings = ledger.load(path)
    assert warnings == []
    # repair() calls rewrite_atomic which appends a REWRITE-MARKER row.
    data_rows = [r for r in rows if r.get("drop_id") != "REWRITE-MARKER"]
    ids = [r["drop_id"] for r in data_rows]
    assert ids == ["EV-1", "EV-2"]
    # First occurrence kept (v == 1).
    assert data_rows[0]["v"] == 1
    # Now clean (REWRITE-MARKER has its own valid hash in the chain).
    assert ledger.verify(path)["ok"] is True


def test_cli_verify_repair(tmp_path: Path, capsys):
    path = tmp_path / "events.jsonl"
    ledger.append(path, {"drop_id": "EV-1", "ts": "t"})
    rc = ledger.main(["verify", str(path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out.lower()


# --------------------------------------------------------------------------- #
# K2 acceptance tests: hash chain
# --------------------------------------------------------------------------- #

def test_hash_chain_append_sets_row_hash(tmp_path: Path):
    """Every appended row must carry a row_hash."""
    path = tmp_path / "events.jsonl"
    for i in range(4):
        ledger.append(path, {"drop_id": f"EV-{i}", "ts": "t", "v": i})
    rows, warnings = ledger.load(path)
    assert warnings == []
    for row in rows:
        assert "row_hash" in row, f"row_hash missing in {row}"
        assert len(row["row_hash"]) == 64  # sha256 hex


def test_hash_chain_verify_clean(tmp_path: Path):
    """Freshly appended rows verify with ok=True and no chain_breaks."""
    path = tmp_path / "events.jsonl"
    for i in range(5):
        ledger.append(path, {"drop_id": f"EV-{i}", "ts": "t", "v": i})
    report = ledger.verify(path)
    assert report["ok"] is True
    assert report["chain_breaks"] == []
    assert report["legacy_rows"] == 0


def test_hash_chain_edit_detected(tmp_path: Path):
    """In-place edit of row k breaks the chain; verify() reports it."""
    path = tmp_path / "events.jsonl"
    for i in range(5):
        ledger.append(path, {"drop_id": f"EV-{i}", "ts": "t", "v": i})

    # Tamper with row 2 (0-indexed): change its value in-place.
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    obj = json.loads(lines[2])
    obj["v"] = 999  # altered
    lines[2] = json.dumps(obj, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = ledger.verify(path)
    assert report["ok"] is False
    # Break reported at the tampered line (line 3, 1-indexed).
    assert 3 in report["chain_breaks"]


def test_hash_chain_delete_row_detected(tmp_path: Path):
    """Deleting a row breaks the chain for the subsequent row."""
    path = tmp_path / "events.jsonl"
    for i in range(5):
        ledger.append(path, {"drop_id": f"EV-{i}", "ts": "t", "v": i})

    # Delete row at index 2 (the 3rd row).
    raw = path.read_text(encoding="utf-8")
    lines = [l for l in raw.splitlines() if l.strip()]
    del lines[2]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = ledger.verify(path)
    assert report["ok"] is False
    assert report["chain_breaks"]  # the row after the deleted one breaks


def test_hash_chain_reorder_detected(tmp_path: Path):
    """Swapping two rows breaks the chain."""
    path = tmp_path / "events.jsonl"
    for i in range(4):
        ledger.append(path, {"drop_id": f"EV-{i}", "ts": "t", "v": i})

    raw = path.read_text(encoding="utf-8")
    lines = [l for l in raw.splitlines() if l.strip()]
    # Swap rows 1 and 2.
    lines[1], lines[2] = lines[2], lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = ledger.verify(path)
    assert report["ok"] is False
    assert report["chain_breaks"]


def test_hash_chain_legacy_prefix_tolerated(tmp_path: Path):
    """Legacy rows (no row_hash) before hashed rows are tolerated as legacy prefix."""
    path = tmp_path / "events.jsonl"

    # Write legacy rows manually (no row_hash field).
    with open(path, "a", encoding="utf-8") as f:
        for i in range(3):
            f.write(json.dumps({"drop_id": f"LEG-{i}", "ts": "t", "v": i}) + "\n")

    # Now append new hashed rows via ledger.append.
    for i in range(3):
        ledger.append(path, {"drop_id": f"NEW-{i}", "ts": "t", "v": i})

    report = ledger.verify(path)
    assert report["ok"] is True
    assert report["legacy_rows"] == 3
    assert report["chain_breaks"] == []


def test_hash_chain_legacy_prefix_edit_in_hashed_suffix_detected(tmp_path: Path):
    """Edit in the hashed suffix of a mixed legacy+hashed ledger is caught."""
    path = tmp_path / "events.jsonl"

    # Write legacy rows manually.
    with open(path, "a", encoding="utf-8") as f:
        for i in range(2):
            f.write(json.dumps({"drop_id": f"LEG-{i}", "ts": "t", "v": i}) + "\n")

    # Append hashed rows.
    for i in range(3):
        ledger.append(path, {"drop_id": f"NEW-{i}", "ts": "t", "v": i})

    # Tamper with the 4th line (first hashed row, line index 2 → lineno 3).
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    obj = json.loads(lines[2])
    obj["v"] = 999
    lines[2] = json.dumps(obj, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = ledger.verify(path)
    assert report["ok"] is False
    assert report["chain_breaks"]
    assert report["legacy_rows"] == 2


# --------------------------------------------------------------------------- #
# K2 acceptance tests: quarantine dedupe
# --------------------------------------------------------------------------- #

def test_quarantine_dedupe_single_bad_line(tmp_path: Path):
    """The same malformed line read 3× produces exactly one quarantine entry."""
    path = tmp_path / "events.jsonl"

    # Write two good rows and one bad line.
    ledger.append(path, {"drop_id": "EV-1", "ts": "t"})
    with open(path, "a", encoding="utf-8") as f:
        f.write("NOT JSON AT ALL\n")
    ledger.append(path, {"drop_id": "EV-2", "ts": "t"})

    # First read: bad line should be quarantined.
    rows1, warnings1 = ledger.load(path)
    assert any("quarantin" in w for w in warnings1)
    qpath = path.with_name(path.name + ".quarantine")
    content1 = qpath.read_text(encoding="utf-8")
    count1 = content1.count("NOT JSON AT ALL")

    # Second read: the bad line is already quarantined — no new entry.
    rows2, warnings2 = ledger.load(path)
    content2 = qpath.read_text(encoding="utf-8")
    count2 = content2.count("NOT JSON AT ALL")

    # Third read: still exactly one quarantine entry.
    rows3, warnings3 = ledger.load(path)
    content3 = qpath.read_text(encoding="utf-8")
    count3 = content3.count("NOT JSON AT ALL")

    assert count1 == 1, "bad line should be quarantined exactly once on first read"
    assert count2 == 1, "quarantine must not grow on second read"
    assert count3 == 1, "quarantine must not grow on third read"

    # Good rows still load correctly every time.
    for rows in (rows1, rows2, rows3):
        good = [r["drop_id"] for r in rows]
        assert "EV-1" in good
        assert "EV-2" in good


def test_quarantine_dedupe_no_duplicate_warning_on_repeat(tmp_path: Path):
    """Repeated reads of a bad line do not emit additional quarantine warnings."""
    path = tmp_path / "events.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write("BAD LINE\n")

    _, warnings1 = ledger.load(path)
    _, warnings2 = ledger.load(path)
    _, warnings3 = ledger.load(path)

    # First read: one quarantine-related warning.
    assert any("quarantin" in w for w in warnings1)
    # Subsequent reads: no quarantine warnings (line already seen).
    quarantine_warnings2 = [w for w in warnings2 if "quarantin" in w]
    quarantine_warnings3 = [w for w in warnings3 if "quarantin" in w]
    assert quarantine_warnings2 == [], f"unexpected warnings on 2nd read: {warnings2}"
    assert quarantine_warnings3 == [], f"unexpected warnings on 3rd read: {warnings3}"


# --------------------------------------------------------------------------- #
# K2 acceptance tests: repair re-chains + rewrite marker
# --------------------------------------------------------------------------- #

def test_repair_rechains_and_appends_marker(tmp_path: Path):
    """repair() re-chains hashes and appends a REWRITE-MARKER row."""
    path = tmp_path / "events.jsonl"
    for i in range(4):
        ledger.append(path, {"drop_id": f"EV-{i}", "ts": "t", "v": i})

    # Inject a bad line so repair has something to do.
    with open(path, "a", encoding="utf-8") as f:
        f.write("not json\n")

    ledger.repair(path)

    rows, warnings = ledger.load(path)
    assert warnings == []

    # A REWRITE-MARKER row must be present.
    marker_rows = [r for r in rows if r.get("drop_id") == "REWRITE-MARKER"]
    assert len(marker_rows) == 1
    marker = marker_rows[0]
    assert marker.get("event") == "ledger_rewrite"
    assert marker.get("actor") == "repair"
    assert "row_hash" in marker

    # Chain must be valid after repair.
    report = ledger.verify(path)
    assert report["ok"] is True
    assert report["chain_breaks"] == []


def test_rewrite_atomic_rechains_and_verify_passes(tmp_path: Path):
    """rewrite_atomic re-chains surviving rows; verify() passes afterward."""
    path = tmp_path / "events.jsonl"
    for i in range(6):
        ledger.append(path, {"drop_id": f"EV-{i}", "ts": "t", "v": i})

    # Keep only even rows (stripping out odd ones).
    rows, _ = ledger.load(path)
    survivors = [r for r in rows if r.get("v", 1) % 2 == 0]
    ledger.rewrite_atomic(path, survivors, actor="test", reason="filter_odd")

    report = ledger.verify(path)
    assert report["ok"] is True
    assert report["chain_breaks"] == []

    # Confirm the marker carries the right actor/reason.
    loaded, _ = ledger.load(path)
    marker = next(r for r in loaded if r.get("drop_id") == "REWRITE-MARKER")
    assert marker["actor"] == "test"
    assert marker["reason"] == "filter_odd"
