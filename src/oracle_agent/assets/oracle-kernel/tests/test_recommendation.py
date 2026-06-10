#!/usr/bin/env python3
"""Tests for the recommendation adjudicator (recommendation.py).

Covers the load-bearing guarantees: the original block (action/rationale/
evidence/baseline/expected_signal/risk_if_wrong) is IMMUTABLE and hash-locked
(tampering is refused, forcing supersession); adjudication scores against
OBSERVED Decisions + value_events, NEVER human approval; the verdict ladder
(pending/conformed/contradicted/partial); and the scorecard. Self-contained:
depends only on recommendation.py + the floor + the shared minimal_oracle
fixture.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import recommendation as R  # noqa: E402
import ledger  # noqa: E402


def _ensure_dirs(root: Path) -> None:
    (root / "Memory.nosync" / "Recommendations").mkdir(parents=True, exist_ok=True)
    (root / "Memory.nosync" / "Decisions").mkdir(parents=True, exist_ok=True)
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)


def _make_rec(root: Path, title: str = "Raise SMB price") -> str:
    p = R.new(
        root,
        {
            "title": title,
            "action": "Increase SMB plan $39 -> $49",
            "rationale": "Elasticity test: <5% churn at +25%",
            "evidence": ["pricing-test-2026Q1"],
            "baseline": "SMB MRR $410k at $39",
            "expected_signal": ["SMB MRR up >=15%", "churn delta < 5%"],
            "risk_if_wrong": "churn spike",
        },
    )
    return R.read_note(p).id


def _write_decision(root: Path, name: str, rid: str, *, link: str) -> None:
    """link in {'conforms_to','conflicts_with'}."""
    text = (
        "---\n"
        f"id: dec-{name}\n"
        "type: decision\n"
        f"title: decision {name}\n"
        "created: 2026-06-08\n"
        "updated: 2026-06-08\n"
        "sensitivity: internal\n"
        "status: observed\n"
        "tags:\n"
        "  - decision\n"
        f"{link}:\n"
        f"  - {rid}\n"
        "---\n\nObserved organizational action.\n"
    )
    # constant literal-prefixed path under the test tmp tree (not user-influenced)
    (root / "Memory.nosync" / "Decisions" / f"{name}.md").write_text(
        text, encoding="utf-8"
    )


def _add_value_event(root: Path, rid: str, polarity: int, strength: float) -> None:
    ledger.append(
        root / "Meta.nosync" / "ledgers" / "value_event.jsonl",
        {"target": rid, "polarity": polarity, "strength": strength},
        id_prefix="VAL",
    )


# --------------------------------------------------------------------------- #
# creation + immutability
# --------------------------------------------------------------------------- #
def test_new_writes_contained_note_with_frozen_original(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    rid = _make_rec(root)
    rec = R.load_all(root)[0]
    assert rec.id == rid
    assert "Recommendations" in rec.path.parts
    assert R.validate_frontmatter(root, rec.frontmatter) == []
    # the index ledger recorded the ORIGINAL fingerprint
    rows, warn = ledger.load(root / "Meta.nosync" / "ledgers" / "recommendation_index.jsonl")
    assert warn == []
    assert rows and rows[0]["original_sha256"]
    # adjudication block exists and is the mutable surface (starts pending)
    assert rec.frontmatter["adjudication"]["verdict"] == "pending"


def test_immutable_original_change_is_refused(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    rid = _make_rec(root)
    rec = R.load_all(root)[0]
    fm = dict(rec.frontmatter)
    fm["action"] = "TAMPERED"
    with pytest.raises(ValueError):
        R._assert_original_immutable(root, fm)


def test_adjudicate_refuses_physically_tampered_note(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    rid = _make_rec(root)
    p = R.load_all(root)[0].path
    raw = p.read_text(encoding="utf-8")
    raw2 = raw.replace("$49", "$99")
    p.write_text(raw2, encoding="utf-8")
    with pytest.raises(ValueError):
        R.adjudicate(root, rid)


# --------------------------------------------------------------------------- #
# adjudication: OBSERVED reality, never approval
# --------------------------------------------------------------------------- #
def test_no_observed_data_is_pending_not_approved(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    rid = _make_rec(root)
    adj = R.adjudicate(root, rid)
    # We refuse to manufacture a verdict from nothing; no human "approve" path.
    assert adj["verdict"] == "pending"
    assert adj["evidence_basis"] == "observed_decisions_and_value_events"


def test_conforming_decision_and_positive_value_is_conformed(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    rid = _make_rec(root)
    _write_decision(root, "shipped", rid, link="conforms_to")
    _add_value_event(root, rid, polarity=1, strength=2.0)
    adj = R.adjudicate(root, rid)
    assert adj["verdict"] == "conformed"
    assert adj["decisions_conform"] == 1
    assert adj["value_events"] == 1
    assert adj["net_observed_value"] == 2.0


def test_conflicting_decision_is_contradicted(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    rid = _make_rec(root)
    _write_decision(root, "reverted", rid, link="conflicts_with")
    adj = R.adjudicate(root, rid)
    assert adj["verdict"] == "contradicted"
    assert adj["decisions_conflict"] == 1


def test_negative_value_signal_alone_is_contradicted(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    rid = _make_rec(root)
    _add_value_event(root, rid, polarity=-1, strength=3.0)
    adj = R.adjudicate(root, rid)
    assert adj["verdict"] == "contradicted"
    assert adj["net_observed_value"] == -3.0


def test_mixed_decisions_is_partial(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    rid = _make_rec(root)
    _write_decision(root, "a", rid, link="conforms_to")
    _write_decision(root, "b", rid, link="conforms_to")
    _write_decision(root, "c", rid, link="conflicts_with")
    adj = R.adjudicate(root, rid)
    # conform > conflict (2 vs 1) but a conflict exists -> not clean -> partial
    assert adj["verdict"] == "partial"


def test_adjudicate_preserves_immutable_original(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    rid = _make_rec(root)
    before = R.load_all(root)[0].original()
    _write_decision(root, "shipped", rid, link="conforms_to")
    R.adjudicate(root, rid)
    after = R.load_all(root)[0].original()
    assert before == after  # the frozen fields are byte-identical post-adjudication


# --------------------------------------------------------------------------- #
# scorecard
# --------------------------------------------------------------------------- #
def test_scorecard_portfolio_view(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    r1 = _make_rec(root, "rec one")
    r2 = _make_rec(root, "rec two")
    _write_decision(root, "ship1", r1, link="conforms_to")
    _add_value_event(root, r1, polarity=1, strength=1.0)
    _write_decision(root, "revert2", r2, link="conflicts_with")
    sc = R.scorecard(root)
    assert sc["total"] == 2
    assert sc["by_verdict"]["conformed"] == 1
    assert sc["by_verdict"]["contradicted"] == 1


def test_cli_new_adjudicate_scorecard(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    import json

    payload = root / "pl.json"
    payload.write_text(
        json.dumps(
            {
                "title": "cli rec",
                "action": "do X",
                "rationale": "because Y",
                "evidence": ["e1"],
                "baseline": "b",
                "expected_signal": ["s up"],
            }
        ),
        encoding="utf-8",
    )
    assert R.main(["--root", str(root), "new", "--payload", str(payload)]) == 0
    rid = R.load_all(root)[0].id
    assert R.main(["--root", str(root), "adjudicate", "--id", rid]) == 0
    assert R.main(["--root", str(root), "scorecard"]) == 0
    out = capsys.readouterr().out
    assert "pending" in out
