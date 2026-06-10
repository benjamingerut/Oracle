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
    assert [r["drop_id"] for r in rows] == ["EV-0", "EV-4"]
    # No stray temp files left behind.
    leftovers = [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_rewrite_atomic_empty(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    ledger.append(path, {"drop_id": "EV-0", "ts": "t"})
    ledger.rewrite_atomic(path, [])
    rows, warnings = ledger.load(path)
    assert rows == []
    assert warnings == []


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
    ids = [r["drop_id"] for r in rows]
    assert ids == ["EV-1", "EV-2"]
    # First occurrence kept (v == 1).
    assert rows[0]["v"] == 1
    # Now clean.
    assert ledger.verify(path)["ok"] is True


def test_cli_verify_repair(tmp_path: Path, capsys):
    path = tmp_path / "events.jsonl"
    ledger.append(path, {"drop_id": "EV-1", "ts": "t"})
    rc = ledger.main(["verify", str(path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out.lower()
