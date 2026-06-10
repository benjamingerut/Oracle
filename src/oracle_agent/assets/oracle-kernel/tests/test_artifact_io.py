#!/usr/bin/env python3
"""Tests for artifact_io.py -- contained, policy-gated artifact I/O.

These tests build a MINIMAL oracle inline via the ``minimal_oracle`` conftest
helper. They prove:

  * a clean log -> ingest -> emit -> render ROUND-TRIP works and the registries
    are rendered from the durable ledgers;
  * INGEST refuses a path-traversal lane ('../../ESCAPE') and a path-traversal
    slug ('a/b') and leaves the _INPUT source file INTACT;
  * EMIT refuses the same traversal vectors, writes nothing to _OUTPUT, and
    leaves the source --src file INTACT;
  * EMIT of a confidential artifact WITHOUT approval is policy-refused (no
    _OUTPUT bytes), and WITH approval succeeds and logs an export_event;
  * ``log`` REQUIRES --sensitivity.

The tool is exercised through its real CLI entrypoint ``artifact_io.main(argv)``
so the argument parsing + exit-code contract is covered too.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import artifact_io
import ledger
import safe_paths


_WP = "Workproduct.nosync"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _drop_input_file(root: Path, name: str, content: str) -> Path:
    """Place a raw file into the _INPUT queue (as a real drop would arrive)."""
    p = root / _WP / "_INPUT" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _input_ledger_rows(root: Path) -> list[dict]:
    rows, _ = ledger.load(root / _WP / "_INPUT" / ".registry.jsonl")
    return rows


def _output_ledger_rows(root: Path) -> list[dict]:
    rows, _ = ledger.load(root / _WP / "_OUTPUT" / ".registry.jsonl")
    return rows


def _artifact_import_rows(root: Path) -> list[dict]:
    rows, _ = ledger.load(root / "Meta.nosync" / "ledgers" / "artifact_import_event.jsonl")
    return rows


def _output_files(root: Path) -> list[str]:
    out = root / _WP / "_OUTPUT"
    if not out.exists():
        return []
    return sorted(
        p.name
        for p in out.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.name != "REGISTRY.md"
    )


# --------------------------------------------------------------------------- #
# clean round-trip
# --------------------------------------------------------------------------- #
def test_clean_round_trip_log_ingest_emit_render(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _drop_input_file(root, "q3-notes.txt", "quarterly notes body\n")

    # scan should see the unlogged file
    rc = artifact_io.main(["--root", str(root), "scan"])
    assert rc == 0

    # log REQUIRES --sensitivity
    rc = artifact_io.main(
        ["--root", str(root), "log", "--file", "q3-notes.txt",
         "--sensitivity", "internal", "--directive", "file under finance"]
    )
    assert rc == 0
    in_rows = _input_ledger_rows(root)
    assert len(in_rows) == 1
    assert in_rows[0]["original_name"] == "q3-notes.txt"
    assert in_rows[0]["sensitivity"] == "internal"
    assert in_rows[0]["status"] == "pending"
    assert in_rows[0]["drop_id"].startswith("IN-")

    # ingest into a valid lane (non-destructive move out of _INPUT)
    rc = artifact_io.main(
        ["--root", str(root), "ingest", "--file", "q3-notes.txt",
         "--lane", "01_Finance", "--slug", "Q3 Notes"]
    )
    assert rc == 0
    # source consumed out of _INPUT, landed in the lane's received/ dir
    assert not (root / _WP / "_INPUT" / "q3-notes.txt").exists()
    received = list((root / _WP / "01_Finance" / "received").iterdir())
    landed = [p for p in received if p.is_file()]
    assert len(landed) == 1
    assert landed[0].name.endswith("_q3-notes.txt")
    assert landed[0].read_text(encoding="utf-8") == "quarterly notes body\n"
    # ledger row flipped to filed
    in_rows = _input_ledger_rows(root)
    assert in_rows[0]["status"] == "filed"
    assert in_rows[0]["filed_location"]

    # emit an internal artifact (policy-allowed without approval)
    src = tmp_path / "deliverable.md"
    src.write_text("# Q3 deliverable\nbody\n", encoding="utf-8")
    rc = artifact_io.main(
        ["--root", str(root), "emit", "--src", str(src),
         "--lane", "01_Finance", "--slug", "Q3 Deliverable",
         "--sensitivity", "internal", "--agent", "tester"]
    )
    assert rc == 0
    # source PRESERVED (emit is non-destructive to the caller's --src)
    assert src.exists()
    # landed in canonical lane created/ and mirrored in _OUTPUT
    created = [p for p in (root / _WP / "01_Finance" / "created").iterdir() if p.is_file()]
    assert len(created) == 1
    assert _output_files(root) == [created[0].name]
    out_rows = _output_ledger_rows(root)
    assert len(out_rows) == 1
    assert out_rows[0]["drop_id"].startswith("OUT-")
    assert out_rows[0]["sensitivity"] == "internal"

    # render regenerates REGISTRY.md from the ledgers
    rc = artifact_io.main(["--root", str(root), "render"])
    assert rc == 0
    in_reg = (root / _WP / "_INPUT" / "REGISTRY.md").read_text(encoding="utf-8")
    out_reg = (root / _WP / "_OUTPUT" / "REGISTRY.md").read_text(encoding="utf-8")
    assert "q3-notes.txt" in in_reg
    assert out_rows[0]["artifact_name"] in out_reg


def test_emit_external_src_logs_sanitized_import_provenance(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = tmp_path / "external-report.md"
    src.write_text("# External report\n", encoding="utf-8")

    rc = artifact_io.main(
        [
            "--root", str(root), "emit", "--src", str(src),
            "--lane", "01_Finance", "--slug", "External Report",
            "--sensitivity", "internal", "--agent", "tester",
        ]
    )

    assert rc == 0
    output_rows = _output_ledger_rows(root)
    assert output_rows and output_rows[0]["source_external"] is True
    assert output_rows[0]["source_name"] == "external-report.md"

    import_rows = _artifact_import_rows(root)
    assert len(import_rows) == 1
    assert import_rows[0]["source_name"] == "external-report.md"
    assert import_rows[0]["source_sha256_12"] == output_rows[0]["source_sha256_12"]
    ledger_text = (root / "Meta.nosync" / "ledgers" / "artifact_import_event.jsonl").read_text(
        encoding="utf-8"
    )
    assert str(src) not in ledger_text


# --------------------------------------------------------------------------- #
# log requires --sensitivity
# --------------------------------------------------------------------------- #
def test_log_requires_sensitivity(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _drop_input_file(root, "x.txt", "data\n")
    # argparse enforces required=True -> SystemExit(2)
    with pytest.raises(SystemExit):
        artifact_io.main(["--root", str(root), "log", "--file", "x.txt"])


# --------------------------------------------------------------------------- #
# INGEST traversal rejection (lane + slug), source preserved
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "lane,slug",
    [
        ("../../ESCAPE_ZONE", "pwned"),     # traversal lane
        ("/etc", "pwned"),                   # absolute lane
        ("99_NotALane", "ok"),               # lane not in routing_lanes allowlist
        ("01_Finance/../../x", "pwned"),     # separator/traversal inside lane
    ],
)
def test_ingest_rejects_traversal_lane_and_preserves_source(
    tmp_path, minimal_oracle, lane, slug
):
    root = minimal_oracle(tmp_path)
    src = _drop_input_file(root, "secret.txt", "TOP SECRET PAYLOAD\n")
    before = src.read_text(encoding="utf-8")

    rc = artifact_io.main(
        ["--root", str(root), "ingest", "--file", "secret.txt",
         "--lane", lane, "--slug", slug]
    )
    # non-zero refusal
    assert rc != 0
    # the _INPUT source file is NOT destroyed
    assert src.exists(), "ingest refusal must leave the source intact"
    assert src.read_text(encoding="utf-8") == before
    # nothing escaped above the oracle root
    escape = root.parent / "ESCAPE_ZONE"
    assert not escape.exists()


@pytest.mark.parametrize("slug", ["../../escape", "a/b", "..", "....//"])
def test_ingest_slug_with_separators_is_sanitized_not_traversal(
    tmp_path, minimal_oracle, slug
):
    """A '/'-bearing or '..' slug must NEVER produce a nested/escaping path:
    safe_slug flattens it to a single safe filename component. Either the op is
    refused (empty slug) or it lands as a flat file inside the lane -- never an
    escape."""
    root = minimal_oracle(tmp_path)
    src = _drop_input_file(root, "secret.txt", "PAYLOAD\n")

    rc = artifact_io.main(
        ["--root", str(root), "ingest", "--file", "secret.txt",
         "--lane", "01_Finance", "--slug", slug]
    )
    # nothing escaped the oracle root under any sanitization outcome
    assert not (root.parent / "escape").exists()
    assert not (root.parent / "b").exists()
    received = root / _WP / "01_Finance" / "received"
    if rc == 0:
        # landed as a single FLAT file (no nested 'a/b' dir) inside the lane
        landed = [p for p in received.rglob("*") if p.is_file()]
        assert len(landed) == 1
        assert landed[0].parent == received  # flat, not nested
        assert not (received / "a").exists()
    else:
        # refused (e.g. slug sanitized to empty): source preserved
        assert src.exists()


def test_ingest_traversal_writes_nothing_into_lanes(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _drop_input_file(root, "secret.txt", "TOP SECRET\n")
    artifact_io.main(
        ["--root", str(root), "ingest", "--file", "secret.txt",
         "--lane", "../../ESCAPE_ZONE", "--slug", "pwned"]
    )
    # no created/received artifacts anywhere from the refused op
    for lane in ["01_Finance", "00_Ownership-Strategy"]:
        recv = root / _WP / lane / "received"
        if recv.exists():
            assert [p for p in recv.iterdir() if p.is_file()] == []


# --------------------------------------------------------------------------- #
# EMIT traversal rejection (lane + slug), source preserved, no _OUTPUT bytes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "lane,slug",
    [
        ("../../ESCAPE_ZONE", "pwned"),
        ("/etc", "pwned"),
        ("99_NotALane", "ok"),
        ("01_Finance/../../x", "pwned"),
    ],
)
def test_emit_rejects_traversal_lane_and_preserves_source(
    tmp_path, minimal_oracle, lane, slug
):
    root = minimal_oracle(tmp_path)
    src = tmp_path / "report.md"
    src.write_text("CONFIDENTIAL REPORT\n", encoding="utf-8")
    before = src.read_text(encoding="utf-8")

    rc = artifact_io.main(
        ["--root", str(root), "emit", "--src", str(src),
         "--lane", lane, "--slug", slug, "--sensitivity", "internal"]
    )
    assert rc != 0
    # caller's source is intact
    assert src.exists()
    assert src.read_text(encoding="utf-8") == before
    # nothing landed in _OUTPUT
    assert _output_files(root) == []
    assert _output_ledger_rows(root) == []
    # nothing escaped the root
    assert not (root.parent / "ESCAPE_ZONE").exists()


@pytest.mark.parametrize("slug", ["../../escape", "a/b"])
def test_emit_slug_with_separators_is_sanitized_not_traversal(
    tmp_path, minimal_oracle, slug
):
    """An emit slug bearing '/' or '..' is flattened by safe_slug; it can never
    create a nested/escaping path. The source is preserved either way."""
    root = minimal_oracle(tmp_path)
    src = tmp_path / "report.md"
    src.write_text("REPORT\n", encoding="utf-8")

    rc = artifact_io.main(
        ["--root", str(root), "emit", "--src", str(src),
         "--lane", "01_Finance", "--slug", slug, "--sensitivity", "internal"]
    )
    assert src.exists()  # source always preserved
    assert not (root.parent / "escape").exists()
    if rc == 0:
        out_files = _output_files(root)
        assert len(out_files) == 1
        # flat filename, no nested dirs created in _OUTPUT
        assert "/" not in out_files[0]
        assert not (root / _WP / "_OUTPUT" / "a").exists()


# --------------------------------------------------------------------------- #
# EMIT policy gate: confidential without approval refused; with approval allowed
# --------------------------------------------------------------------------- #
def test_emit_confidential_without_approval_refused(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = tmp_path / "secret-deck.md"
    src.write_text("board-only material\n", encoding="utf-8")

    rc = artifact_io.main(
        ["--root", str(root), "emit", "--src", str(src),
         "--lane", "01_Finance", "--slug", "Board Deck",
         "--sensitivity", "confidential"]
    )
    assert rc != 0, "confidential export without approval must be refused"
    # no bytes in _OUTPUT, no canonical artifact, no output ledger row
    assert _output_files(root) == []
    assert _output_ledger_rows(root) == []
    created = root / _WP / "01_Finance" / "created"
    if created.exists():
        assert [p for p in created.iterdir() if p.is_file()] == []
    # source intact
    assert src.exists()


def test_emit_confidential_with_approval_succeeds_and_logs_export_event(
    tmp_path, minimal_oracle
):
    root = minimal_oracle(tmp_path)
    src = tmp_path / "secret-deck.md"
    src.write_text("board-only material\n", encoding="utf-8")

    rc = artifact_io.main(
        ["--root", str(root), "emit", "--src", str(src),
         "--lane", "01_Finance", "--slug", "Board Deck",
         "--sensitivity", "confidential", "--approval", "ADMIN-APPROVAL-2026-01",
         "--actor", "admin", "--role", "admin"]
    )
    assert rc == 0
    # landed in _OUTPUT
    assert len(_output_files(root)) == 1
    assert len(_output_ledger_rows(root)) == 1
    # an export_event was recorded (metadata only -- no payload field)
    ev_path = root / "Meta.nosync" / "ledgers" / "export_event.jsonl"
    rows, _ = ledger.load(ev_path)
    assert rows, "expected an export_event row"
    last = rows[-1]
    assert last.get("classification") == "confidential"
    assert last.get("approval") == "ADMIN-APPROVAL-2026-01"
    # event carries metadata only, never the artifact body
    assert "payload" not in last
    assert "board-only material" not in json.dumps(last)


# --------------------------------------------------------------------------- #
# missing input / src surface as a non-zero error, not a crash
# --------------------------------------------------------------------------- #
def test_ingest_missing_input_file_errors(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with pytest.raises(SystemExit):
        artifact_io.main(
            ["--root", str(root), "ingest", "--file", "nope.txt",
             "--lane", "01_Finance", "--slug", "x"]
        )


def test_emit_missing_src_errors(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with pytest.raises(SystemExit):
        artifact_io.main(
            ["--root", str(root), "emit", "--src", str(tmp_path / "nope.md"),
             "--lane", "01_Finance", "--slug", "x", "--sensitivity", "internal"]
        )
