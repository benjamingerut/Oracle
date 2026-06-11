#!/usr/bin/env python3
"""Tests for session_memory.py.

The important behavior is architectural: sessions are captured as Meta memory,
then decomposed into the existing Oracle Memory/Meta behavioral stores instead
of creating a parallel "session facts" store. Derived MemPalace/Graphify files
are access layers only and carry no answer authority.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import ledger
import loops
import session_memory


def _write_loop(root: Path, loop_id: str, *, runner: str, cadence: str = "every-session") -> Path:
    d = root / "Meta.nosync" / "Loops"
    d.mkdir(parents=True, exist_ok=True)
    text = "\n".join([
        "---",
        f"id: {loop_id}",
        "type: loop",
        f"title: {loop_id}",
        'created: "2026-06-08"',
        'updated: "2026-06-08"',
        "sensitivity: internal",
        "status: active",
        "tags:",
        "  - loop",
        f"cadence: {cadence}",
        f"runner: {runner}",
        'last_run: "2026-06-08"',
        'next_review: "2026-06-08"',
        "trigger_conditions:",
        "  - material session captured",
        "---",
        "",
        "Body.",
        "",
    ])
    p = d / f"loop-{loop_id}.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_capture_session_writes_meta_note_and_ledger(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)

    res = session_memory.capture_session(
        root,
        user_request="Why is churn rising?",
        answer_summary="Reviewed CRM and support signals.",
        business_objects=["Churn", "Customers"],
        source_ids=["SRC-20260608-001"],
        skills=["oracle-analysis"],
        tools=["knowledge_index"],
        learned_claims=["Support escalations increased for enterprise accounts."],
        open_questions=["Is the increase concentrated in one product line?"],
        sensitivity="internal",
        actor="tester",
        now=now,
    )

    assert res["session_id"].startswith("SES-")
    note = root / res["note_path"]
    assert note.exists()
    text = note.read_text(encoding="utf-8")
    assert "type: session" in text
    assert "Why is churn rising?" in text

    rows, warnings = ledger.load(root / "Meta.nosync" / "ledgers" / "session_memory.jsonl")
    assert warnings == []
    assert rows[0]["action"] == "capture"
    assert rows[0]["kind"] == "session"
    assert rows[0]["business_objects"] == ["Churn", "Customers"]


def test_dream_decomposes_session_into_canonical_records_and_derived_exports(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    cap = session_memory.capture_session(
        root,
        user_request="What did we learn about enterprise churn?",
        answer_summary="Enterprise churn appears linked to support escalations.",
        business_objects=["Enterprise churn"],
        source_ids=["SRC-20260608-001"],
        skills=["oracle-churn-review"],
        tools=["knowledge_index", "graphify"],
        queries=["enterprise churn support escalations"],
        learned_claims=["Enterprise churn risk is higher where support escalation volume rose."],
        open_questions=["Which accounts have unresolved escalations older than 14 days?"],
        contradictions=["CS says churn is support-driven while Sales says pricing is the main driver."],
        remote_datasets=["CRM accounts export"],
        latency_ms=42000,
        sensitivity="internal",
        now=now,
    )

    out = session_memory.dream(root, now=now)

    assert out["status"] == "ok"
    assert out["processed"] == 1
    assert session_memory.list_sessions(root, pending=True) == []

    assert (root / "Memory.nosync" / "Findings").exists()
    assert (root / "Memory.nosync" / "Questions").exists()
    assert (root / "Memory.nosync" / "Contradictions").exists()
    assert (root / "Memory.nosync" / "Queries").exists()
    assert (root / "Meta.nosync" / "Improvements").exists()

    finding_text = next((root / "Memory.nosync" / "Findings").glob("*.md")).read_text(encoding="utf-8")
    assert "type: finding" in finding_text
    assert "status: needs_review" in finding_text
    assert "session-derived" in finding_text
    assert cap["session_id"] in finding_text

    contradiction_text = next((root / "Memory.nosync" / "Contradictions").glob("*.md")).read_text(encoding="utf-8")
    assert "type: contradiction" in contradiction_text
    assert "classification: watch" in contradiction_text

    mempalace = root / "_data.nosync" / "derived" / "mempalace" / "raw" / "oracle-session-memory.jsonl"
    graph_nodes = root / "_data.nosync" / "derived" / "graphify" / "raw" / "oracle-session-graph-nodes.jsonl"
    graph_edges = root / "_data.nosync" / "derived" / "graphify" / "raw" / "oracle-session-graph-edges.jsonl"
    assert mempalace.exists()
    assert graph_nodes.exists()
    assert graph_edges.exists()
    assert "answer_authority" in mempalace.read_text(encoding="utf-8")
    assert "Enterprise churn" in graph_nodes.read_text(encoding="utf-8")
    assert "used_source" in graph_edges.read_text(encoding="utf-8")


def test_dream_skips_derived_export_when_nothing_changed(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    session_memory.capture_session(
        root,
        user_request="What changed in vendor pricing?",
        learned_claims=["Vendor pricing rose 4% on renewal."],
        now=now,
    )

    first = session_memory.dream(root, now=now)
    assert first["processed"] == 1
    assert not first["derived"].get("skipped")

    ledger_path = root / "Meta.nosync" / "ledgers" / "session_memory.jsonl"
    exports_before = sum(
        1 for r in ledger.load(ledger_path)[0] if r.get("action") == "export-derived"
    )

    # A checkpoint with no new captures must not re-render the derived files.
    second = session_memory.dream(root, now=datetime(2026, 6, 9, 12, 0, 0))
    assert second["status"] == "ok"
    assert second["processed"] == 0
    assert second["derived"].get("skipped") is True
    exports_after = sum(
        1 for r in ledger.load(ledger_path)[0] if r.get("action") == "export-derived"
    )
    assert exports_after == exports_before

    # A new capture makes the manifest stale again: the next dream re-exports.
    session_memory.capture_session(
        root,
        user_request="Did the vendor concede on payment terms?",
        learned_claims=["Net-60 terms were granted for FY27."],
        now=datetime(2026, 6, 10, 12, 0, 0),
    )
    third = session_memory.dream(root, now=datetime(2026, 6, 10, 13, 0, 0))
    assert third["processed"] == 1
    assert not third["derived"].get("skipped")
    assert third["derived"]["session_count"] == 2


def test_memory_matriculation_runner_owns_dreaming_without_extra_core_loop(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(root, "memory-matriculation", runner="builtin:memory-matriculation")
    session_memory.capture_session(
        root,
        user_request="Capture a reusable finance retrieval pattern.",
        queries=["monthly revenue actuals by customer segment"],
        learned_claims=["Finance analysis needs customer-segment revenue actuals."],
        now=now,
    )

    result = loops.run(root, "memory-matriculation", now=now)

    assert result["status"] == "ok"
    assert result["kind"] == "builtin:memory-matriculation"
    assert result["outcome"]["processed"] == 1
    assert not (root / "Meta.nosync" / "Loops" / "loop-memory-dreaming.md").exists()


# --------------------------------------------------------------------------- #
# P5-T2a: --role threading (attribution only; role-invariant).
# --------------------------------------------------------------------------- #
def test_capture_session_threads_role_into_ledger_and_note(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    res = session_memory.capture_session(
        root,
        user_request="Why is churn rising?",
        answer_summary="Reviewed signals.",
        actor="gateway_user:telegram:123",
        role="user",
        now=now,
    )
    rows, warnings = ledger.load(root / "Meta.nosync" / "ledgers" / "session_memory.jsonl")
    assert warnings == []
    assert rows[0]["role"] == "user"
    assert rows[0]["actor"] == "gateway_user:telegram:123"
    text = (root / res["note_path"]).read_text(encoding="utf-8")
    assert "role: user" in text


def test_capture_session_role_defaults_to_unknown(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    res = session_memory.capture_session(
        root, user_request="Bare kernel-CLI write.", now=now,
    )
    rows, _ = ledger.load(root / "Meta.nosync" / "ledgers" / "session_memory.jsonl")
    assert rows[0]["role"] == "unknown"
    text = (root / res["note_path"]).read_text(encoding="utf-8")
    assert "role: unknown" in text


def test_capture_session_is_role_invariant(tmp_path, minimal_oracle):
    """Same inputs under different roles produce identical records except for the
    recorded role attribution (role is attribution, never capability -- P5S-13)."""
    root_a = minimal_oracle(tmp_path / "a")
    root_b = minimal_oracle(tmp_path / "b")
    now = datetime(2026, 6, 8, 12, 0, 0)
    kwargs = dict(
        user_request="Identical request",
        answer_summary="Identical answer",
        business_objects=["Churn"],
        learned_claims=["A claim."],
        actor="someone",
        now=now,
    )
    res_user = session_memory.capture_session(root_a, role="user", **kwargs)
    res_admin = session_memory.capture_session(root_b, role="admin", **kwargs)

    rows_a, _ = ledger.load(root_a / "Meta.nosync" / "ledgers" / "session_memory.jsonl")
    rows_b, _ = ledger.load(root_b / "Meta.nosync" / "ledgers" / "session_memory.jsonl")
    # Strip the attribution-only fields and the per-write id; everything else
    # (the captured business memory) must be identical regardless of role.
    def _strip(row):
        skip = ("role", "drop_id", "row_hash", "prev_hash")
        return {k: v for k, v in row.items() if k not in skip}
    assert _strip(rows_a[0]) == _strip(rows_b[0])
    assert rows_a[0]["role"] == "user"
    assert rows_b[0]["role"] == "admin"
    # The session note bodies (the decomposable content) are identical.
    body_a = (root_a / res_user["note_path"]).read_text(encoding="utf-8")
    body_b = (root_b / res_admin["note_path"]).read_text(encoding="utf-8")
    assert body_a.replace("role: user", "ROLE") == body_b.replace("role: admin", "ROLE")


def test_capture_session_cli_accepts_role(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    rc = session_memory.main(
        ["--root", str(root), "capture",
         "--user-request", "CLI role test",
         "--actor", "local_user:operator", "--role", "admin"]
    )
    assert rc == 0
    rows, _ = ledger.load(root / "Meta.nosync" / "ledgers" / "session_memory.jsonl")
    assert rows[0]["role"] == "admin"
    assert rows[0]["actor"] == "local_user:operator"
