#!/usr/bin/env python3
"""Tests for the contradiction adjudicator (contradiction.py).

Covers: schema-valid note creation + registration, the ranking invariant that a
decision-relevant high-severity conflict is pinned above a trivial-but-easy one
(never averaged away), the four-way classification, open/resolve lifecycle, and
that notes round-trip through the STRICT oracle_yaml loader. Self-contained:
depends only on contradiction.py + the floor + the shared minimal_oracle
fixture, so it stays green in isolation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import contradiction as C  # noqa: E402
import ledger  # noqa: E402


def _ensure_dirs(root: Path) -> None:
    (root / "Memory.nosync" / "Contradictions").mkdir(parents=True, exist_ok=True)
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# creation + registration
# --------------------------------------------------------------------------- #
def test_new_writes_contained_schema_valid_note(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    p = C.new(
        root,
        {
            "title": "ARR conflict SF vs Brex",
            "severity": "high",
            "decision_relevance": True,
            "claims_in_conflict": ["SF=12.1M", "Brex=9.4M"],
        },
    )
    # lives under Memory.nosync/Contradictions (containment)
    assert p.exists()
    assert "Memory.nosync" in p.parts and "Contradictions" in p.parts
    # re-reads + validates
    c = C.read_note(p)
    assert c.frontmatter["type"] == "contradiction"
    assert C.validate_frontmatter(root, c.frontmatter) == []
    # registered in the index ledger with metadata only
    rows, warn = ledger.load(root / "Meta.nosync" / "ledgers" / "contradiction_index.jsonl")
    assert warn == []
    assert len(rows) == 1
    assert rows[0]["content_sha256"]
    assert rows[0]["classification"] == "must_resolve"
    # NO claim payload leaked into the tracked ledger
    assert "claims_in_conflict" not in rows[0]


def test_malicious_title_cannot_escape_folder(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    # A traversal-laden title is slugged to a safe segment; the file still lands
    # inside Contradictions/.
    p = C.new(
        root,
        {
            "title": "../../etc/passwd takeover",
            "severity": "low",
            "decision_relevance": False,
            "claims_in_conflict": ["x", "y"],
        },
    )
    assert (root / "Memory.nosync" / "Contradictions") in p.parents
    assert "etc" not in [seg for seg in p.parts[:-1]]


# --------------------------------------------------------------------------- #
# ranking invariant
# --------------------------------------------------------------------------- #
def test_decision_relevant_high_severity_outranks_trivial_easy(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    C.new(
        root,
        {
            "title": "trivial easy cosmetic",
            "severity": "low",
            "decision_relevance": False,
            "ease": "trivial",
            "freshness": "fresh",
            "claims_in_conflict": ["a", "b"],
        },
    )
    C.new(
        root,
        {
            "title": "hard critical decision-relevant",
            "severity": "critical",
            "decision_relevance": True,
            "ease": "blocked",
            "risk_if_wrong": "critical",
            "claims_in_conflict": ["a", "b"],
        },
    )
    ranked = C.rank(C.load_open(root))
    top = ranked[0][0]
    # The hard, decision-relevant, critical conflict is pinned to the top even
    # though it is the HARDEST to resolve -- ease never demotes it.
    assert top.decision_relevant is True
    assert top.severity == "critical"
    assert C.classify(top) == "must_resolve"


def test_rank_is_deterministic(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    for i in range(3):
        C.new(
            root,
            {
                "title": f"conflict {i}",
                "severity": "medium",
                "decision_relevance": True,
                "claims_in_conflict": ["a", "b"],
            },
        )
    order1 = [c.id for c, _, _ in C.rank(C.load_open(root))]
    order2 = [c.id for c, _, _ in C.rank(C.load_open(root))]
    assert order1 == order2


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def test_classify_must_resolve(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    p = C.new(
        root,
        {
            "title": "must resolve",
            "severity": "high",
            "decision_relevance": True,
            "claims_in_conflict": ["a", "b"],
        },
    )
    assert C.classify(C.read_note(p)) == "must_resolve"


def test_classify_watch_when_not_decision_relevant(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    p = C.new(
        root,
        {
            "title": "watch only",
            "severity": "low",
            "decision_relevance": False,
            "claims_in_conflict": ["a", "b"],
        },
    )
    assert C.classify(C.read_note(p)) == "watch"


def test_classify_schema_or_definition_debt(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    p = C.new(
        root,
        {
            "title": "grain mismatch",
            "severity": "medium",
            "decision_relevance": True,
            "conflict_kind": "grain",
            "possible_causes": ["as-of date grain mismatch"],
            "claims_in_conflict": ["212", "205"],
        },
    )
    assert C.classify(C.read_note(p)) == "schema_or_definition_debt"


def test_classify_bounded_residual(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    p = C.new(
        root,
        {
            "title": "bounded residual",
            "severity": "medium",
            "decision_relevance": True,
            "residual_bounded": True,
            "claims_in_conflict": ["a", "b"],
        },
    )
    assert C.classify(C.read_note(p)) == "bounded_residual"


def test_definition_debt_still_must_resolve_when_high_and_decision_relevant(
    tmp_path, minimal_oracle
):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    # A definition mismatch that is ALSO high-severity and decision-relevant is
    # already reaching decisions -> must_resolve overrides the debt label.
    p = C.new(
        root,
        {
            "title": "definition mismatch reaching board deck",
            "severity": "critical",
            "decision_relevance": True,
            "conflict_kind": "definition",
            "possible_causes": ["definition mismatch"],
            "claims_in_conflict": ["a", "b"],
        },
    )
    assert C.classify(C.read_note(p)) == "must_resolve"


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
def test_open_then_resolve(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    p = C.new(
        root,
        {
            "title": "resolve me",
            "severity": "high",
            "decision_relevance": True,
            "claims_in_conflict": ["a", "b"],
        },
    )
    cid = C.read_note(p).id
    assert len(C.load_open(root)) == 1
    C.resolve(
        root,
        cid,
        resolving_source="restated-source",
        resolution="epoch lag corrected",
        status="resolved",
    )
    assert len(C.load_open(root)) == 0
    assert C.must_resolve_open(root) == []
    # the resolved note still validates and carries the resolution fields
    c = C.read_note(p)
    assert c.status == "resolved"
    assert c.frontmatter["resolving_source"] == "restated-source"
    assert C.validate_frontmatter(root, c.frontmatter) == []


def test_resolve_unknown_id_raises(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    with pytest.raises(ValueError):
        C.resolve(
            root,
            "nope",
            resolving_source="x",
            resolution="y",
        )


# --------------------------------------------------------------------------- #
# strict-YAML round-trip safety
# --------------------------------------------------------------------------- #
def test_special_char_title_round_trips_through_strict_yaml(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    title = 'Q2: "ARR" mismatch #data vs report'
    p = C.new(
        root,
        {
            "title": title,
            "severity": "high",
            "decision_relevance": True,
            "claims_in_conflict": ['A: 10', 'B: "twelve"'],
        },
    )
    # _split_frontmatter uses the STRICT oracle_yaml loader; if our renderer
    # emitted anything outside the safe subset this raises.
    c = C.read_note(p)
    assert c.frontmatter["title"] == title
    assert c.frontmatter["claims_in_conflict"] == ['A: 10', 'B: "twelve"']


def test_cli_open_json(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    _ensure_dirs(root)
    C.new(
        root,
        {
            "title": "cli case",
            "severity": "high",
            "decision_relevance": True,
            "claims_in_conflict": ["a", "b"],
        },
    )
    rc = C.main(["--root", str(root), "open", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "must_resolve" in out
