"""Tests for agentloop/grounding.py (Phase 3, P3-T1 + P3-T2).

Covers the claim extractor (deterministic, recall-tuned, per-adversarial-class
recall >= 0.95 on a labeled corpus, zero flags on purely-conversational text),
the grounding checker (normalize-equality coverage, latest-per-object, exit_code
with verdict fallback, withheld == refused-class, object_guess None == unbacked,
no substring containment), known_objects server-side enumeration, and the
fail-closed GateError contract.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from oracle_agent.agentloop.grounding import (
    Claim,
    ClaimCheck,
    GateError,
    check_grounding,
    extract_claims,
    known_objects,
    repair_prompt,
)
from oracle_agent.agentloop import grounding as g


_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "grounding" / "corpus.json").read_text(
        encoding="utf-8"
    )
)
_CASES = _CORPUS["cases"]
_CLASSES = _CORPUS["_meta"]["classes"]


def _norm(s: str) -> str:
    return g._normalize_object(s)


def _recall_for_case(case: dict) -> tuple[int, int, list[str]]:
    """Return (planted, recalled, extracted_texts) for one corpus case."""
    claims = extract_claims(case["draft"], objects_seen=case["objects_seen"])
    texts = [c.text for c in claims]
    joined = " || ".join(_norm(t) for t in texts)
    recalled = 0
    for needle in case["planted"]:
        nn = _norm(needle)
        if nn and nn in joined:
            recalled += 1
    return len(case["planted"]), recalled, texts


# --------------------------------------------------------------------------- #
# P3-T1 -- extraction & corpus recall
# --------------------------------------------------------------------------- #
def test_corpus_overall_recall_at_least_095():
    total_p = total_r = 0
    for case in _CASES:
        p, r, _ = _recall_for_case(case)
        total_p += p
        total_r += r
    assert total_p > 0
    recall = total_r / total_p
    assert recall >= 0.95, f"overall recall {recall:.3f} below 0.95"


def test_per_adversarial_class_recall_at_least_095():
    by_class_p: dict = defaultdict(int)
    by_class_r: dict = defaultdict(int)
    for case in _CASES:
        p, r, _ = _recall_for_case(case)
        by_class_p[case["class"]] += p
        by_class_r[case["class"]] += r
    report = {}
    for cls in _CLASSES:
        p = by_class_p.get(cls, 0)
        r = by_class_r.get(cls, 0)
        report[cls] = (r, p, (r / p) if p else 1.0)
    # Every named adversarial class with planted claims must hit >= 0.95.
    for cls in _CLASSES:
        if cls == "conversational":
            continue
        r, p, recall = report[cls]
        assert p > 0, f"class {cls!r} has no planted claims in the corpus"
        assert recall >= 0.95, f"class {cls!r} recall {recall:.3f} below 0.95 ({r}/{p})"


def test_purely_conversational_class_zero_flags():
    for case in _CASES:
        if case["class"] != "conversational":
            continue
        claims = extract_claims(case["draft"], objects_seen=case["objects_seen"])
        assert claims == [], (
            f"conversational case {case['id']!r} flagged: {[c.text for c in claims]}"
        )


def test_footer_lookalike_lines_are_stripped():
    for case in _CASES:
        for mnf in case.get("must_not_flag", []):
            claims = extract_claims(case["draft"], objects_seen=case["objects_seen"])
            for c in claims:
                assert _norm(mnf) not in _norm(c.text), (
                    f"footer lookalike leaked into a claim in {case['id']!r}: {c.text!r}"
                )


def test_figure_extracted_even_without_known_object():
    # A figure asserted as fact is material even when no truth-map object is
    # named (object_guess stays None -> checker treats it as unbacked).
    claims = extract_claims("The total came to $12,000,000.", objects_seen=[])
    assert len(claims) == 1
    assert claims[0].object_guess is None


def test_known_object_mention_sets_object_guess():
    claims = extract_claims(
        "Revenue / invoices reconciles to the bank statement.",
        objects_seen=["Revenue / invoices"],
    )
    assert claims, "a known-object mention should be flagged"
    assert any(c.object_guess == "Revenue / invoices" for c in claims)


def test_pure_question_not_flagged():
    claims = extract_claims(
        "What was the revenue last quarter?", objects_seen=["Revenue / invoices"]
    )
    # A bare interrogative with no figure/date/entity carries no assertion.
    assert claims == []


def test_hedge_does_not_exempt_material_unit():
    # P3S-15: hedge words do not exempt a unit carrying a figure.
    claims = extract_claims("I believe revenue was $3.3M.", objects_seen=[])
    assert len(claims) == 1


def test_quoted_text_is_extracted():
    claims = extract_claims(
        'The memo states, "cash is $500,000".', objects_seen=["Cash / bank"]
    )
    assert claims, "quoted figures must not be exempt (smuggling channel)"


def test_code_block_content_is_extracted():
    draft = "```\nrevenue = $7,000,000\n```"
    claims = extract_claims(draft, objects_seen=[])
    assert any("7,000,000" in c.text for c in claims)


def test_table_separator_row_not_flagged():
    draft = "| A | B |\n| --- | --- |\n| Acme Corp | $1,000,000 |"
    claims = extract_claims(draft, objects_seen=[])
    for c in claims:
        assert not set(c.text.replace("|", "").strip()) <= set("-: "), c.text


# --------------------------------------------------------------------------- #
# P3-T2 -- checker coverage semantics
# --------------------------------------------------------------------------- #
def _env(obj, *, exit_code=None, verdict=None, withheld=None, ceiling="public"):
    e = {"business_object": obj, "sensitivity_ceiling": ceiling}
    if exit_code is not None:
        e["exit_code"] = exit_code
    if verdict is not None:
        e["verdict"] = verdict
    if withheld is not None:
        e["withheld"] = withheld
    return e


def test_grounded_envelope_backs_plain_assertion_for_its_object():
    draft = "Revenue / invoices totaled $4,000,000 this year."
    envs = [_env("Revenue / invoices", exit_code=0, verdict="grounded")]
    check = check_grounding(draft, envs, objects_seen=["Revenue / invoices"])
    assert check.claims, "the assertion should be a claim"
    assert check.unbacked == []
    assert check.mismatched == []


def test_grounded_envelope_does_not_cover_a_different_object():
    # Equality, not containment: an envelope for "company" must not cover
    # "company revenue".
    draft = "Company revenue was $9,000,000."
    envs = [_env("company", exit_code=0, verdict="grounded")]
    check = check_grounding(
        draft, envs, objects_seen=["company", "company revenue"]
    )
    # The claim resolves to the more-specific object "company revenue"; there is
    # no envelope for it, so it is unbacked.
    assert any(c.object_guess == "company revenue" for c in check.claims)
    assert check.unbacked, "containment must NOT back the claim"
    assert check.mismatched == []


def test_no_envelope_is_unbacked():
    draft = "Revenue / invoices reached $2,000,000."
    check = check_grounding(draft, [], objects_seen=["Revenue / invoices"])
    assert check.unbacked
    assert check.mismatched == []


def test_object_guess_none_is_always_unbacked():
    # A figure with no named object -> object_guess None -> always unbacked,
    # even if some unrelated grounded envelope exists.
    draft = "The number was $5,000,000."
    envs = [_env("Revenue / invoices", exit_code=0, verdict="grounded")]
    check = check_grounding(draft, envs, objects_seen=["Revenue / invoices"])
    assert any(c.object_guess is None for c in check.claims)
    assert check.unbacked
    assert check.mismatched == []


def test_refused_envelope_is_mismatched():
    draft = "Revenue / invoices was $3,000,000."
    envs = [_env("Revenue / invoices", exit_code=4, verdict="refused")]
    check = check_grounding(draft, envs, objects_seen=["Revenue / invoices"])
    assert check.mismatched, "asserting on a refused envelope is a mismatch"
    assert check.unbacked == []


def test_withheld_envelope_is_mismatched():
    # P3S-1: a withheld:true envelope (even with a grounded exit_code) is
    # refused-class -- the model never saw the grounded payload.
    draft = "Revenue / invoices was $3,000,000."
    envs = [_env("Revenue / invoices", exit_code=0, verdict="grounded", withheld=True)]
    check = check_grounding(draft, envs, objects_seen=["Revenue / invoices"])
    assert check.mismatched, "a withheld grounded envelope must not certify a claim"
    assert check.unbacked == []


def test_supported_envelope_satisfied_by_footer():
    draft = "Revenue / invoices was $3,000,000."
    envs = [_env("Revenue / invoices", exit_code=2, verdict="supported, authority not confirmed")]
    check = check_grounding(draft, envs, objects_seen=["Revenue / invoices"])
    assert check.unbacked == []
    assert check.mismatched == []


def test_caveated_envelope_satisfied_by_footer():
    draft = "Revenue / invoices was $3,000,000."
    envs = [_env("Revenue / invoices", exit_code=3, verdict="caveated")]
    check = check_grounding(draft, envs, objects_seen=["Revenue / invoices"])
    assert check.unbacked == []
    assert check.mismatched == []


def test_latest_envelope_per_object_governs():
    # Two envelopes for one object: the LATEST governs (P3S-13). An earlier
    # refused followed by a later grounded -> backed.
    draft = "Revenue / invoices was $3,000,000."
    envs = [
        _env("Revenue / invoices", exit_code=4, verdict="refused"),
        _env("Revenue / invoices", exit_code=0, verdict="grounded"),
    ]
    check = check_grounding(draft, envs, objects_seen=["Revenue / invoices"])
    assert check.unbacked == []
    assert check.mismatched == []


def test_latest_envelope_governs_downgrade():
    # Later refused overrides earlier grounded -> mismatched.
    draft = "Revenue / invoices was $3,000,000."
    envs = [
        _env("Revenue / invoices", exit_code=0, verdict="grounded"),
        _env("Revenue / invoices", exit_code=4, verdict="refused"),
    ]
    check = check_grounding(draft, envs, objects_seen=["Revenue / invoices"])
    assert check.mismatched
    assert check.unbacked == []


def test_verdict_string_fallback_when_no_exit_code():
    draft = "Revenue / invoices was $3,000,000."
    envs = [_env("Revenue / invoices", verdict="grounded")]  # no exit_code key
    check = check_grounding(draft, envs, objects_seen=["Revenue / invoices"])
    assert check.unbacked == []
    assert check.mismatched == []


def test_verdict_string_fallback_refused():
    draft = "Revenue / invoices was $3,000,000."
    envs = [_env("Revenue / invoices", verdict="refused")]  # no exit_code key
    check = check_grounding(draft, envs, objects_seen=["Revenue / invoices"])
    assert check.mismatched
    assert check.unbacked == []


def test_normalize_equality_matches_slash_and_space_variants():
    # "Customers / accounts" claim, envelope keyed "customers accounts" -> equal
    # under normalize_object, so it covers.
    draft = "Customers / accounts grew to 1,200 this year."
    envs = [_env("customers  accounts", exit_code=0, verdict="grounded")]
    check = check_grounding(
        draft, envs, objects_seen=["Customers / accounts"]
    )
    assert check.unbacked == []
    assert check.mismatched == []


# --------------------------------------------------------------------------- #
# fail-closed GateError contract (P3S-8)
# --------------------------------------------------------------------------- #
def test_extract_claims_raises_gate_error_on_bad_input():
    with pytest.raises(GateError):
        extract_claims(12345, objects_seen=[])  # type: ignore[arg-type]


def test_check_grounding_wraps_failure_as_gate_error(monkeypatch):
    # Force an internal failure inside extraction; check_grounding must surface
    # it as GateError (fail-closed), never as a raw exception.
    def boom(*a, **k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(g, "_split_units", boom)
    with pytest.raises(GateError):
        check_grounding("anything material $1,000,000", [], objects_seen=[])


def test_known_objects_read_failure_is_gate_error(monkeypatch):
    def boom():
        raise RuntimeError("no truth map module")

    monkeypatch.setattr(g, "_truth_map_module", boom)
    with pytest.raises(GateError):
        known_objects(Path("/nonexistent/root"))


# --------------------------------------------------------------------------- #
# known_objects against a real spawned root
# --------------------------------------------------------------------------- #
def test_known_objects_enumerates_truth_map(spawned_root):
    objs = known_objects(spawned_root)
    assert isinstance(objs, list)
    # The seed TRUTH-MAP.md ships eight business objects.
    assert any("revenue" in g._normalize_object(o) for o in objs), objs
    assert any("customers" in g._normalize_object(o) for o in objs), objs


def test_known_objects_never_returns_duplicates(spawned_root):
    objs = known_objects(spawned_root)
    norms = [g._normalize_object(o) for o in objs]
    assert len(norms) == len(set(norms))


# --------------------------------------------------------------------------- #
# repair prompt
# --------------------------------------------------------------------------- #
def test_repair_prompt_names_object_and_oracle_answer():
    check = ClaimCheck(
        claims=[Claim("Revenue was $1M.", "Revenue / invoices")],
        unbacked=[Claim("Revenue was $1M.", "Revenue / invoices")],
        mismatched=[],
    )
    prompt = repair_prompt(check)
    assert "oracle_answer" in prompt
    assert "Revenue / invoices" in prompt


def test_repair_prompt_for_none_object_asks_to_name_it():
    check = ClaimCheck(
        claims=[Claim("The total was $1M.", None)],
        unbacked=[Claim("The total was $1M.", None)],
        mismatched=[],
    )
    prompt = repair_prompt(check)
    assert "name the specific business object" in prompt.lower()


def test_repair_prompt_includes_mismatched_claims():
    check = ClaimCheck(
        claims=[Claim("Revenue was $1M.", "Revenue / invoices")],
        unbacked=[],
        mismatched=[Claim("Revenue was $1M.", "Revenue / invoices")],
    )
    prompt = repair_prompt(check)
    assert "Revenue was $1M" in prompt
