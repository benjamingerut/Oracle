#!/usr/bin/env python3
"""v2 experience + intelligence layer tests.

Covers the graduated authority ladder end-to-end (refused -> ingest ->
supported -> promote -> grounded), truth-map propose/promote/validate, the
Review Inbox, batch ingest with external staging, synthesis worklists, the
leadership brief, the session protocol (status/checkpoint), the CLI verb
surface, and the doc-budget lint gate.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import answer_protocol
import briefing
import ingest_pipeline
import oracle_cli
import oracle_status
import review_queue
import synthesis
import truth_map
from answer_protocol import (
    EXIT_CAVEATED,
    EXIT_GROUNDED,
    EXIT_REFUSED,
    EXIT_SUPPORTED,
    preflight,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _write_truth_map(root: Path, rows: list[dict]) -> None:
    header = "| Business object | Primary source | Freshness budget | Status |"
    sep = "|---|---|---|---|"
    lines = ["# Truth Map", "", header, sep]
    for r in rows:
        lines.append(
            f"| {r['object']} | {r.get('source', 'TBD')} | "
            f"{r.get('budget', '7d')} | {r.get('status', 'draft')} |"
        )
    (root / "TRUTH-MAP.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_source(
    root: Path,
    *,
    object_name: str,
    authority: str = "erp",
    name: str = "src-1",
    as_of: str | None = None,
    confidence: float | None = 0.9,
    extra: list[str] | None = None,
) -> Path:
    folder = root / "Memory.nosync" / "Sources"
    folder.mkdir(parents=True, exist_ok=True)
    as_of = as_of or _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    fm = [
        "---",
        f"id: {name}",
        "type: source",
        f"title: {name}",
        "created: 2026-06-01",
        "sensitivity: internal",
        "status: active",
        f"business_object: {object_name}",
        f"source_system: {authority}",
        f"authority_id: {authority}",
        f"as_of: {as_of}",
    ]
    if confidence is not None:
        fm.append(f"confidence: {confidence}")
    fm.extend(extra or [])
    fm += ["---", "", f"# {name}", ""]
    p = folder / f"{name}.md"
    p.write_text("\n".join(fm) + "\n", encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# the graduated ladder
# --------------------------------------------------------------------------- #
def test_ladder_refused_with_suggested_fix_when_nothing(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [])
    env = preflight(root, "Fleet vehicles")
    assert env.exit_code() == EXIT_REFUSED
    assert env.authority_state == "none"
    assert env.suggested_fix, "refusal must carry actionable fix commands"
    assert any("ingest" in f for f in env.suggested_fix)


def test_ladder_candidate_evidence_supports_without_any_row(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [])
    _write_source(root, object_name="Fleet vehicles", authority="fleetio")
    env = preflight(root, "Fleet vehicles")
    assert env.exit_code() == EXIT_SUPPORTED
    assert env.authority_state == "candidate"
    assert env.evidence_count == 1
    assert any("truth propose" in f for f in env.suggested_fix)


def test_ladder_tbd_row_with_evidence_supports(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [{"object": "Revenue", "source": "TBD"}])
    _write_source(root, object_name="Revenue", authority="erp")
    env = preflight(root, "Revenue")
    assert env.exit_code() == EXIT_SUPPORTED
    assert env.authority_state == "candidate"


def test_ladder_confirmed_row_with_fresh_evidence_grounds(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [{"object": "Revenue", "source": "erp", "status": "confirmed"}])
    _write_source(root, object_name="Revenue", authority="erp")
    env = preflight(root, "Revenue")
    assert env.exit_code() == EXIT_GROUNDED
    assert env.authority_state == "confirmed"


def test_ladder_authority_without_evidence_caveats(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [{"object": "Revenue", "source": "erp", "status": "confirmed"}])
    env = preflight(root, "Revenue")
    assert env.exit_code() == EXIT_CAVEATED
    assert env.suggested_fix, "caveat-for-no-evidence should point at ingest"


# --------------------------------------------------------------------------- #
# truth map: propose / promote / validate
# --------------------------------------------------------------------------- #
def test_propose_creates_then_is_idempotent(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [])
    r1 = truth_map.propose_row(root, "Fleet vehicles", primary_source="fleetio")
    assert r1["action"] == "created"
    r2 = truth_map.propose_row(root, "Fleet vehicles", primary_source="other")
    assert r2["action"] == "exists"
    row = truth_map.resolve("Fleet vehicles", root)
    assert row["primary source"] == "fleetio"
    assert row["status"] == "draft"


def test_propose_upgrades_tbd_source(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [{"object": "Revenue", "source": "TBD"}])
    r = truth_map.propose_row(root, "Revenue", primary_source="erp")
    assert r["action"] == "source-set"
    assert truth_map.resolve("Revenue", root)["primary source"] == "erp"


def test_promote_requires_evidence_and_flips_status(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [{"object": "Revenue", "source": "erp"}])
    with pytest.raises(truth_map.TruthMapError):
        truth_map.promote_row(root, "Revenue", actor="boss", role="admin")
    _write_source(root, object_name="Revenue", authority="erp")
    r = truth_map.promote_row(root, "Revenue", actor="boss", role="admin")
    assert r["action"] == "promoted"
    assert truth_map.resolve("Revenue", root)["status"] == "confirmed"
    # ledger row recorded
    ledger_file = root / "Meta.nosync" / "ledgers" / "truth_map.jsonl"
    assert ledger_file.exists() and "truth_row_promoted" in ledger_file.read_text()


def test_promote_denied_to_user_role(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [{"object": "Revenue", "source": "erp"}])
    _write_source(root, object_name="Revenue", authority="erp")
    with pytest.raises(PermissionError):
        truth_map.promote_row(root, "Revenue", actor="mallory", role="user")


def test_validate_reports_promotable_and_needs(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root,
        [
            {"object": "Revenue", "source": "erp"},
            {"object": "People", "source": "TBD"},
        ],
    )
    _write_source(root, object_name="Revenue", authority="erp")
    diags = {d["business_object"]: d for d in truth_map.validate_rows(root)}
    assert diags["Revenue"]["promotable"] is True
    assert diags["People"]["authority"] is False
    assert diags["People"]["needs"]


# --------------------------------------------------------------------------- #
# review inbox
# --------------------------------------------------------------------------- #
def test_inbox_empty_on_clean_oracle(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [])
    assert review_queue.build_queue(root) == []


def test_inbox_surfaces_pending_states(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [{"object": "Revenue", "source": "erp"}])
    _write_source(root, object_name="Revenue", authority="erp")  # -> promotable
    _write_source(
        root,
        object_name="Churn",
        authority="crm",
        name="src-cand",
        extra=["tags:", "  - source", "  - authority-candidate"],
    )
    _write_source(
        root,
        object_name="Contracts",
        authority="dms",
        name="src-ocr",
        extra=["needs_ocr: true"],
    )
    findings = root / "Memory.nosync" / "Findings"
    findings.mkdir(parents=True, exist_ok=True)
    (findings / "f1.md").write_text(
        "---\nid: f1\ntype: finding\ntitle: margin claim\ncreated: 2026-01-01\n"
        "status: needs_review\nbusiness_object: Revenue\n---\nbody\n",
        encoding="utf-8",
    )
    queries = root / "Memory.nosync" / "Queries"
    queries.mkdir(parents=True, exist_ok=True)
    (queries / "q1.md").write_text(
        "---\nid: q1\ntype: query\ntitle: churn retrieval strategy\ncreated: 2026-01-01\n"
        "status: needs_review\ntags:\n  - query\n  - session-derived\n---\nbody\n",
        encoding="utf-8",
    )
    kinds = {i["kind"] for i in review_queue.build_queue(root)}
    assert "promotable-row" in kinds
    assert "authority-candidate" in kinds
    assert "needs-ocr" in kinds
    assert "needs-review-finding" in kinds
    assert "needs-review-query" in kinds
    s = review_queue.summary(root)
    assert s["total"] >= 4 and s["most_urgent"]


def test_inbox_flags_competing_authority(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [])
    _write_source(root, object_name="Revenue", authority="erp", name="src-a")
    _write_source(root, object_name="Revenue", authority="legacy-books", name="src-b")
    items = [i for i in review_queue.build_queue(root) if i["kind"] == "contradiction"]
    assert any("competing authority" in i["title"] for i in items)


# --------------------------------------------------------------------------- #
# batch ingest + staging
# --------------------------------------------------------------------------- #
def test_batch_ingest_stages_external_dir_and_proposes_rows(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("ARR is 4.1M per ERP.", encoding="utf-8")
    (outside / "b.md").write_text("# notes\nchurn 4% per CRM", encoding="utf-8")
    (outside / ".DS_Store").write_text("junk", encoding="utf-8")

    result = ingest_pipeline.run_batch(
        root, [outside], business_object="Revenue", source_system="erp",
        actor="admin", role="admin",
    )
    assert result["ok"] and result["ingested"] == 2  # housekeeping skipped
    # originals untouched
    assert (outside / "a.txt").exists()
    # staged into _INPUT
    staged = list((root / "Workproduct.nosync" / "_INPUT").iterdir())
    assert len(staged) == 2
    # draft truth row proposed -> same-session supported answer
    assert truth_map.resolve("Revenue", root) is not None
    assert preflight(root, "Revenue").exit_code() == EXIT_SUPPORTED


def test_stage_external_dedups_identical_content(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    f = tmp_path / "same.txt"
    f.write_text("identical bytes", encoding="utf-8")
    p1 = ingest_pipeline.stage_external(root, f)
    p2 = ingest_pipeline.stage_external(root, f)
    assert p1 == p2


# --------------------------------------------------------------------------- #
# synthesis
# --------------------------------------------------------------------------- #
def _write_finding(root: Path, name: str, obj: str, created: str = "2026-06-01") -> None:
    folder = root / "Memory.nosync" / "Findings"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(
        f"---\nid: {name}\ntype: finding\ntitle: {name}\ncreated: {created}\n"
        f"status: confirmed\nbusiness_object: {obj}\n---\nbody\n",
        encoding="utf-8",
    )


def test_synthesis_proposes_model_for_unmodeled_cluster(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    for i in range(3):
        _write_finding(root, f"f{i}", "Revenue")
    wl = synthesis.build_worklist(root)
    actions = {i["action"] for i in wl["items"]}
    assert "propose-model" in actions


def test_synthesis_quiet_when_model_is_current(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    for i in range(3):
        _write_finding(root, f"f{i}", "Revenue", created="2026-01-01")
    models = root / "Memory.nosync" / "Models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "m1.md").write_text(
        "---\nid: m1\ntype: model\ntitle: revenue model\nbusiness_object: Revenue\n"
        "last_validated: 2099-01-01\n---\nbody\n",
        encoding="utf-8",
    )
    wl = synthesis.build_worklist(root)
    assert wl["items"] == []


def test_synthesis_flags_model_older_than_findings(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_finding(root, "fnew", "Revenue", created="2026-06-01")
    models = root / "Memory.nosync" / "Models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "m1.md").write_text(
        "---\nid: m1\ntype: model\ntitle: revenue model\nbusiness_object: Revenue\n"
        "last_validated: 2026-01-01\n---\nbody\n",
        encoding="utf-8",
    )
    wl = synthesis.build_worklist(root)
    assert any(i["action"] == "update-model" for i in wl["items"])


# --------------------------------------------------------------------------- #
# briefing
# --------------------------------------------------------------------------- #
def test_brief_lists_withheld_objects_with_fixes(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root,
        [
            {"object": "Revenue", "source": "erp", "status": "confirmed"},
            {"object": "Cash", "source": "TBD"},
        ],
    )
    _write_source(root, object_name="Revenue", authority="erp")
    doc = briefing.build_brief(root)
    assert doc["dropped"] == 1
    assert doc["needs_authority"][0]["object"] == "Cash"
    assert doc["needs_authority"][0]["fix"]
    assert "## Appendix: needs authority setup" in doc["body"]
    assert "grounded" in doc["body"]


# --------------------------------------------------------------------------- #
# session protocol
# --------------------------------------------------------------------------- #
def test_status_rungs_progress(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [{"object": "Revenue", "source": "erp"}])
    assert oracle_status.status(root)["maturity"]["rung"] == 0
    _write_source(root, object_name="Revenue", authority="erp")
    assert oracle_status.status(root)["maturity"]["rung"] == 2
    truth_map.promote_row(root, "Revenue", actor="admin", role="admin")
    s = oracle_status.status(root)
    assert s["maturity"]["rung"] == 3
    assert s["authority"]["confirmed"] == 1
    assert s["suggested_next"]


def test_checkpoint_runs_without_loops_installed(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [])
    c = oracle_status.checkpoint(root)
    assert "review_inbox" in c and "reminder" in c


# --------------------------------------------------------------------------- #
# CLI verb surface
# --------------------------------------------------------------------------- #
def test_cli_daily_verbs_smoke(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [])
    assert oracle_cli.main(["status", "--root", str(root)]) == 0
    assert oracle_cli.main(["review", "--root", str(root)]) == 0
    assert oracle_cli.main(["brief", "--root", str(root)]) == 0
    assert oracle_cli.main(["dashboard", "--root", str(root)]) == 0
    assert oracle_cli.main(["synthesis", "--root", str(root), "worklist"]) == 0
    assert oracle_cli.main(["admin", "truth", "rows", "--root", str(root)]) == 0
    # answer convenience form: flags imply the 'answer' subcommand; refusal = 4
    assert (
        oracle_cli.main(["answer", "--root", str(root), "--object", "Nothing here"])
        == EXIT_REFUSED
    )
    capsys.readouterr()


def test_cli_unknown_verb_lists_surface(capsys):
    assert oracle_cli.main(["frobnicate"]) == 2
    err = capsys.readouterr().err
    assert "Daily verbs" in err


# --------------------------------------------------------------------------- #
# doc budgets (lint)
# --------------------------------------------------------------------------- #
def test_doc_budget_violation_fails_lint(tmp_path, minimal_oracle):
    import oracle_lint

    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [])
    (root / "AGENTS.md").write_text("x\n" * 200, encoding="utf-8")
    out: list = []
    oracle_lint.check_doc_budgets(root, out)
    assert any(v.code == "doc-over-budget" for v in out)
    (root / "AGENTS.md").write_text("x\n" * 100, encoding="utf-8")
    out2: list = []
    oracle_lint.check_doc_budgets(root, out2)
    assert not out2
