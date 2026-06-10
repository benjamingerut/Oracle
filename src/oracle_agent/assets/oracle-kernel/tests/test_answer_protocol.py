#!/usr/bin/env python3
"""Tests for answer_protocol.py -- the material-answer envelope + refusal gate.

Builds a tmp oracle inline (via the ``minimal_oracle`` fixture) plus a tmp
TRUTH-MAP.md and, where needed, Source/Contradiction notes, then asserts the
exit-code contract:

    exit 4 (refused)  -- empty map / no row, and TBD-source row
    exit 3 (caveated) -- stale source OR open must_resolve contradiction
                         OR a real source but no evidence ingested yet
    exit 0 (grounded) -- row + real source + fresh evidence + no blocker

Self-contained: depends only on this module + truth_map + the floor.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import answer_protocol
import oracle_cli
from answer_protocol import (
    EXIT_CAVEATED,
    EXIT_GROUNDED,
    EXIT_SUPPORTED,
    EXIT_REFUSED,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    preflight,
    research_preflight,
)


# --------------------------------------------------------------------------- #
# helpers to materialize truth-map rows + source/contradiction notes
# --------------------------------------------------------------------------- #
def _write_truth_map(root: Path, rows: list[dict]) -> None:
    """Write a TRUTH-MAP.md with the load-bearing columns and the given rows.

    Each ``rows`` entry is a dict with keys: object, source, budget, status,
    sensitivity (optional).
    """
    header = "| Business object | Primary source | Freshness budget | Status | Sensitivity |"
    sep = "|---|---|---|---|---|"
    lines = ["# Truth Map", "", header, sep]
    for r in rows:
        lines.append(
            "| {object} | {source} | {budget} | {status} | {sensitivity} |".format(
                object=r["object"],
                source=r.get("source", ""),
                budget=r.get("budget", ""),
                status=r.get("status", "draft"),
                sensitivity=r.get("sensitivity", ""),
            )
        )
    (root / "TRUTH-MAP.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _write_source(root: Path, *, object_name: str, as_of: str, sensitivity: str = "internal",
                  confidence: float | None = None, disconfirmer: str | None = None,
                  name: str = "src", authority: str = "accounting/ERP") -> Path:
    folder = root / "Memory.nosync" / "Sources"
    folder.mkdir(parents=True, exist_ok=True)
    fm = [
        "---",
        f"id: {name}",
        "type: source",
        f"title: {name}",
        "created: 2026-01-01",
        "updated: 2026-01-01",
        f"sensitivity: {sensitivity}",
        "status: active",
        f"business_object: {object_name}",
        f"source_system: {authority}",
        f"as_of: {as_of}",
    ]
    if confidence is not None:
        fm.append(f"confidence: {confidence}")
    if disconfirmer is not None:
        fm.append(f"disconfirmer: {disconfirmer}")
    fm += ["---", "", f"# {name}", "", "Source body."]
    p = folder / f"{name}.md"
    p.write_text("\n".join(fm) + "\n", encoding="utf-8")
    return p


def _write_contradiction(root: Path, *, object_name: str, status: str = "open",
                         severity: str = "high", contradiction_class: str | None = None,
                         name: str = "ctr") -> Path:
    folder = root / "Memory.nosync" / "Contradictions"
    folder.mkdir(parents=True, exist_ok=True)
    fm = [
        "---",
        f"id: {name}",
        "type: contradiction",
        f"title: conflict on {object_name}",
        "created: 2026-01-01",
        "updated: 2026-01-01",
        "sensitivity: internal",
        f"status: {status}",
        f"severity: {severity}",
        f"business_object: {object_name}",
    ]
    if contradiction_class is not None:
        fm.append(f"contradiction_class: {contradiction_class}")
    fm += ["---", "", "Two sources disagree."]
    p = folder / f"{name}.md"
    p.write_text("\n".join(fm) + "\n", encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# Envelope field contract (must match ANSWER-PROTOCOL.md checklist exactly)
# --------------------------------------------------------------------------- #
def test_envelope_fields_match_checklist(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "draft"}])
    env = preflight(root, "Revenue")
    d = env.to_dict()
    expected = {
        "business_object",
        "truth_map_row",
        "source_authority",
        "freshness_verdict",
        "sensitivity_ceiling",
        "confidence",
        "disconfirmers",
        "open_contradictions",
        "refusal_reason",
        "authority_state",
        "evidence_count",
        "suggested_fix",
    }
    assert set(d.keys()) == expected


# --------------------------------------------------------------------------- #
# exit 4 -- refusal
# --------------------------------------------------------------------------- #
def test_empty_map_refuses_bootstrap(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    # No TRUTH-MAP.md at all -> bootstrap-empty.
    env = preflight(root, "Revenue")
    assert env.truth_map_row is None
    assert env.refusal_reason == "no-authority-bootstrap"
    assert env.exit_code() == EXIT_REFUSED


def test_map_present_but_object_missing_refuses(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [{"object": "Cash", "source": "bank", "budget": "24h", "status": "draft"}])
    env = preflight(root, "Revenue")
    assert env.refusal_reason == "no-authority-bootstrap"
    assert env.exit_code() == EXIT_REFUSED


def test_tbd_primary_source_refuses_no_authority(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [{"object": "Revenue", "source": "TBD", "budget": "7d", "status": "draft"}])
    env = preflight(root, "Revenue")
    assert env.truth_map_row is not None
    assert env.source_authority is None
    assert env.refusal_reason == "no-authority"
    assert env.exit_code() == EXIT_REFUSED


def test_empty_business_object_refuses(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "draft"}])
    env = preflight(root, "")
    assert env.refusal_reason == "no-business-object"
    assert env.exit_code() == EXIT_REFUSED


def test_public_research_preflight_does_not_require_truth_map(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)

    env = research_preflight(root, "Research battery recycling regulations")

    assert env.exit_code() == EXIT_GROUNDED
    assert env.verdict == "allowed"
    assert env.mode == "exploratory_public_research"
    assert env.context_sensitivity == "public"
    assert env.refusal_reason is None
    assert any("Oracle-authoritative answer" in c for c in env.constraints)


def test_research_preflight_refuses_external_private_context(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)

    env = research_preflight(
        root,
        "Compare our pipeline against competitors",
        context_sensitivity="internal",
        includes_company_context=True,
    )

    assert env.exit_code() == EXIT_REFUSED
    assert env.verdict == "refused"
    assert env.processing_verdict == "deny"
    assert env.refusal_reason == "external-processing-denied"


def test_oracle_cli_answer_short_form_and_research_command(
    tmp_path,
    minimal_oracle,
    capsys,
):
    root = minimal_oracle(tmp_path)

    rc = oracle_cli.main([
        "answer",
        "--root",
        str(root),
        "--object",
        "Revenue",
        "--format",
        "json",
    ])
    out = capsys.readouterr().out
    assert rc == EXIT_REFUSED
    assert '"refusal_reason": "no-authority-bootstrap"' in out

    rc = oracle_cli.main([
        "answer",
        "--root",
        str(root),
        "research",
        "--question",
        "Research battery recycling regulations",
        "--format",
        "json",
    ])
    out = capsys.readouterr().out
    assert rc == EXIT_GROUNDED
    assert '"mode": "exploratory_public_research"' in out


# --------------------------------------------------------------------------- #
# exit 3 -- caveat
# --------------------------------------------------------------------------- #
def test_real_source_but_no_evidence_caveats(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "confirmed"}]
    )
    # No Source notes ingested yet.
    env = preflight(root, "Revenue")
    assert env.source_authority == "accounting/ERP"
    assert env.confidence is None
    assert env.freshness_verdict == FRESHNESS_UNKNOWN
    assert env.refusal_reason is None
    assert env.exit_code() == EXIT_CAVEATED


def test_stale_source_caveats(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "confirmed"}]
    )
    old = datetime.now(timezone.utc) - timedelta(days=30)
    _write_source(root, object_name="Revenue", as_of=_iso(old), confidence=0.8)
    env = preflight(root, "Revenue")
    assert env.freshness_verdict == FRESHNESS_STALE
    assert env.refusal_reason is None
    assert env.exit_code() == EXIT_CAVEATED


def test_same_object_wrong_authority_does_not_ground(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "confirmed"}]
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    _write_source(
        root,
        object_name="Revenue",
        as_of=_iso(recent),
        confidence=0.95,
        authority="crm/export",
    )

    env = preflight(root, "Revenue")

    assert env.source_authority == "accounting/ERP"
    assert env.confidence is None
    assert env.freshness_verdict == FRESHNESS_UNKNOWN
    assert env.refusal_reason is None
    assert env.exit_code() == EXIT_CAVEATED


def test_primary_source_id_can_ground_without_object_tag(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root, [{"object": "Revenue", "source": "src", "budget": "7d", "status": "confirmed"}]
    )
    folder = root / "Memory.nosync" / "Sources"
    folder.mkdir(parents=True, exist_ok=True)
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    (folder / "src.md").write_text(
        "\n".join(
            [
                "---",
                "id: src",
                "type: source",
                "title: Source by id",
                "created: 2026-01-01",
                "updated: 2026-01-01",
                "sensitivity: internal",
                "status: active",
                f"as_of: {_iso(recent)}",
                "confidence: 0.8",
                "---",
                "",
                "Source body.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    env = preflight(root, "Revenue")

    assert env.freshness_verdict == FRESHNESS_FRESH
    assert env.confidence == pytest.approx(0.8)
    assert env.exit_code() == EXIT_GROUNDED


def test_open_must_resolve_contradiction_caveats(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "confirmed"}]
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    _write_source(root, object_name="Revenue", as_of=_iso(recent), confidence=0.9)
    # A fresh source would ground it, but an open must_resolve contradiction blocks.
    _write_contradiction(
        root, object_name="Revenue", status="open", contradiction_class="must_resolve"
    )
    env = preflight(root, "Revenue")
    assert env.freshness_verdict == FRESHNESS_FRESH
    assert env.open_contradictions, "contradiction must be surfaced"
    assert any(c.get("must_resolve") for c in env.open_contradictions)
    assert env.exit_code() == EXIT_CAVEATED


def test_high_severity_contradiction_treated_as_must_resolve(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "confirmed"}]
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    _write_source(root, object_name="Revenue", as_of=_iso(recent), confidence=0.9)
    # No explicit class, but severity high -> blocking.
    _write_contradiction(root, object_name="Revenue", status="open", severity="high")
    env = preflight(root, "Revenue")
    assert env.exit_code() == EXIT_CAVEATED


# --------------------------------------------------------------------------- #
# exit 0 -- grounded
# --------------------------------------------------------------------------- #
def test_fresh_source_no_blocker_grounds(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "confirmed"}]
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_source(
        root,
        object_name="Revenue",
        as_of=_iso(recent),
        confidence=0.9,
        disconfirmer="a bank reconciliation that fails to tie out",
    )
    env = preflight(root, "Revenue")
    assert env.source_authority == "accounting/ERP"
    assert env.freshness_verdict == FRESHNESS_FRESH
    assert env.confidence == pytest.approx(0.9)
    assert env.disconfirmers, "disconfirmers should be surfaced"
    assert env.refusal_reason is None
    assert env.open_contradictions == []
    assert env.exit_code() == EXIT_GROUNDED


def test_resolved_open_contradiction_does_not_block(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "confirmed"}]
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_source(root, object_name="Revenue", as_of=_iso(recent), confidence=0.9)
    # A RESOLVED contradiction is not "open" -> does not block.
    _write_contradiction(
        root, object_name="Revenue", status="resolved", contradiction_class="must_resolve"
    )
    env = preflight(root, "Revenue")
    assert env.open_contradictions == []
    assert env.exit_code() == EXIT_GROUNDED


def test_bounded_residual_contradiction_does_not_block(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "confirmed"}]
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_source(root, object_name="Revenue", as_of=_iso(recent), confidence=0.9)
    # Open but explicitly NOT must_resolve -> surfaced but does not force caveat.
    _write_contradiction(
        root, object_name="Revenue", status="open", severity="low",
        contradiction_class="bounded_residual",
    )
    env = preflight(root, "Revenue")
    assert env.open_contradictions, "still surfaced for transparency"
    assert not any(c.get("must_resolve") for c in env.open_contradictions)
    assert env.exit_code() == EXIT_GROUNDED


def test_draft_row_lowers_confidence(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "draft"}]
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_source(root, object_name="Revenue", as_of=_iso(recent), confidence=1.0)
    env = preflight(root, "Revenue")
    # draft status applies the 0.85 haircut.
    assert env.confidence == pytest.approx(0.85)
    # v2 ladder: fresh evidence on a DRAFT row is supported (exit 2), not grounded.
    assert env.exit_code() == EXIT_SUPPORTED
    assert env.authority_state == "draft"
    assert env.suggested_fix


# --------------------------------------------------------------------------- #
# sensitivity ceiling (stricter-row-wins)
# --------------------------------------------------------------------------- #
def test_sensitivity_ceiling_takes_strictest(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root,
        [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "confirmed",
          "sensitivity": "internal"}],
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_source(
        root, object_name="Revenue", as_of=_iso(recent), sensitivity="restricted", confidence=0.9
    )
    env = preflight(root, "Revenue")
    assert env.sensitivity_ceiling == "restricted"


# --------------------------------------------------------------------------- #
# CLI exit codes
# --------------------------------------------------------------------------- #
def test_cli_refuses_empty_map(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    rc = answer_protocol.main(["--root", str(root), "answer", "--object", "Revenue"])
    assert rc == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "refused" in out.lower()


def test_cli_grounds_json(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "confirmed"}]
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_source(root, object_name="Revenue", as_of=_iso(recent), confidence=0.9)
    rc = answer_protocol.main(
        ["--root", str(root), "answer", "--object", "Revenue", "--format", "json"]
    )
    assert rc == EXIT_GROUNDED
    out = capsys.readouterr().out
    assert '"verdict": "grounded"' in out


def test_cli_caveats_stale(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_truth_map(
        root, [{"object": "Revenue", "source": "accounting/ERP", "budget": "7d", "status": "confirmed"}]
    )
    old = datetime.now(timezone.utc) - timedelta(days=30)
    _write_source(root, object_name="Revenue", as_of=_iso(old), confidence=0.9)
    rc = answer_protocol.main(["--root", str(root), "answer", "--object", "Revenue"])
    assert rc == EXIT_CAVEATED


# --------------------------------------------------------------------------- #
# real shipped TRUTH-MAP.md grounds nothing at bootstrap (all draft/TBD, no sources)
# --------------------------------------------------------------------------- #
def test_real_map_object_with_tbd_source_refuses(tmp_path, minimal_oracle, kernel_dir):
    # Copy the shipped TRUTH-MAP into a tmp oracle and confirm a TBD-source row
    # refuses (no-authority) -- the intended bootstrap posture.
    root = minimal_oracle(tmp_path)
    shipped = (kernel_dir / "TRUTH-MAP.md").read_text(encoding="utf-8")
    (root / "TRUTH-MAP.md").write_text(shipped, encoding="utf-8")
    env = preflight(root, "Revenue / invoices")
    # "Revenue / invoices" seeds with a TBD accounting/ERP source.
    assert env.refusal_reason == "no-authority"
    assert env.exit_code() == EXIT_REFUSED
