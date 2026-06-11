#!/usr/bin/env python3
"""Tests for capture.py --role threading (P5-T2a).

``role`` is attribution only: it is recorded on the feedback/value/failure
ledger rows and note frontmatter so audit names *who* under *what* role, but it
NEVER changes what the verb does -- capture is role-invariant (P5S-13). The
``"unknown"`` default is reserved for bare kernel-CLI writes; the shell surfaces
always pass an explicitly resolved role (P5S-14).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import capture
import ledger

NOW = datetime(2026, 6, 8, 12, 0, 0)


def _rows(root: Path, name: str) -> list[dict]:
    rows, _w = ledger.load(root / "Meta.nosync" / "ledgers" / f"{name}.jsonl")
    return rows


def test_feedback_event_threads_role(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    res = capture.feedback_event(
        root, target="brief", polarity="+",
        actor="gateway_user:slack:U42", role="user", now=NOW,
    )
    rows = _rows(root, "feedback_event")
    assert rows[0]["role"] == "user"
    assert rows[0]["actor"] == "gateway_user:slack:U42"
    text = Path(res["note_path"]).read_text(encoding="utf-8")
    assert "role: user" in text


def test_value_event_threads_role(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    capture.value_event(
        root, target="brief", polarity="+", value_kind="decide",
        actor="local_user:operator", role="admin", now=NOW,
    )
    rows = _rows(root, "value_event")
    assert rows[0]["role"] == "admin"


def test_failure_event_threads_role(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    capture.failure_event(
        root, target="answer", severity="medium",
        actor="gateway_user:email:ceo", role="user", now=NOW,
    )
    rows = _rows(root, "failure_event")
    assert rows[0]["role"] == "user"


def test_role_defaults_to_unknown(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    res = capture.feedback_event(root, target="brief", polarity="+", now=NOW)
    rows = _rows(root, "feedback_event")
    assert rows[0]["role"] == "unknown"
    text = Path(res["note_path"]).read_text(encoding="utf-8")
    assert "role: unknown" in text


def test_capture_is_role_invariant(tmp_path, minimal_oracle):
    """The same value_event under different roles produces identical rows except
    for the recorded role (attribution only -- never widens behavior)."""
    root_a = minimal_oracle(tmp_path / "a")
    root_b = minimal_oracle(tmp_path / "b")
    kwargs = dict(target="brief", polarity="+", strength=2.0,
                  value_kind="decide", actor="someone", now=NOW)
    capture.value_event(root_a, role="user", **kwargs)
    capture.value_event(root_b, role="admin", **kwargs)
    row_a = _rows(root_a, "value_event")[0]
    row_b = _rows(root_b, "value_event")[0]

    def _strip(row):
        # role is the attribution that legitimately differs; drop_id/row_hash/
        # prev_hash are per-write chain bookkeeping. Everything else is the
        # captured payload, which must be identical regardless of role.
        skip = ("role", "drop_id", "row_hash", "prev_hash")
        return {k: v for k, v in row.items() if k not in skip}

    assert _strip(row_a) == _strip(row_b)
    assert row_a["role"] == "user"
    assert row_b["role"] == "admin"


def test_cli_accepts_role_on_every_subcommand(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    for cmd, name in (("feedback", "feedback_event"),
                      ("value", "value_event"),
                      ("failure", "failure_event")):
        argv = ["--root", str(root), cmd, "--target", "obj",
                "--actor", "local_user:op", "--role", "admin"]
        if cmd in ("feedback", "value"):
            argv += ["--polarity", "positive"]
        rc = capture.main(argv)
        assert rc == 0, f"{cmd} CLI rejected --role"
        rows = _rows(root, name)
        assert rows[-1]["role"] == "admin"
