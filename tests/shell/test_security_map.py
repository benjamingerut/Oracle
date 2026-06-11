"""Tests for oracle_agent/security_map.py (PHASE-1-foundation-hardening P1-T1).

Asserts:
- verify_enforcers() returns an empty list (all enforcers valid).
- docs/SECURITY.md matches the rendered output of render_security_md()
  (drift test).
- Advisory guarantees are all in ADVISORY_ALLOWED.
- No C*/H*-derived guarantee is advisory (P1S-10 hard rule).
- Non-advisory guarantees are all collected pytest nodes (via verify_enforcers).
- Lint kind enforcers reference real Makefile targets.
- A non-advisory guarantee pointing at a skipped/nonexistent node is reported.
- An advisory guarantee not in ADVISORY_ALLOWED is reported.
"""
from __future__ import annotations

from pathlib import Path

from oracle_agent.security_map import (
    ADVISORY_ALLOWED,
    GUARANTEES,
    Guarantee,
    render_security_md,
    verify_enforcers,
)

# Repo root is three levels up from this file: tests/shell/test_security_map.py
REPO_ROOT = Path(__file__).parent.parent.parent


# ---------------------------------------------------------------------------
# Core contract: verify_enforcers must be empty
# ---------------------------------------------------------------------------

def test_verify_enforcers_is_empty():
    """All enforcer nodes must be collected, non-skipped, and lint-valid."""
    violations = verify_enforcers(REPO_ROOT)
    assert violations == [], (
        "security_map.verify_enforcers() returned violations:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Drift test: checked-in SECURITY.md must match rendered output
# ---------------------------------------------------------------------------

def test_security_md_not_drifted():
    """docs/SECURITY.md must exactly match the rendered output of render_security_md()."""
    security_md_path = REPO_ROOT / "docs" / "SECURITY.md"
    assert security_md_path.exists(), (
        f"docs/SECURITY.md does not exist. Regenerate it by running:\n"
        f"  python3 -c \"import sys; sys.path.insert(0,'src'); "
        f"from oracle_agent.security_map import render_security_md; "
        f"open('docs/SECURITY.md','w').write(render_security_md())\""
    )
    on_disk = security_md_path.read_text()
    rendered = render_security_md()
    assert on_disk == rendered, (
        "docs/SECURITY.md is out of date. Regenerate it:\n"
        "  python3 -c \"import sys; sys.path.insert(0,'src'); "
        "from oracle_agent.security_map import render_security_md; "
        "open('docs/SECURITY.md','w').write(render_security_md())\""
    )


# ---------------------------------------------------------------------------
# Advisory invariants
# ---------------------------------------------------------------------------

def test_all_advisory_ids_are_in_allowlist():
    """Every guarantee with kind='advisory' must be in ADVISORY_ALLOWED."""
    unlisted = [g for g in GUARANTEES if g.kind == "advisory" and g.id not in ADVISORY_ALLOWED]
    assert unlisted == [], (
        "Advisory guarantees not in ADVISORY_ALLOWED: "
        + ", ".join(g.id for g in unlisted)
    )


def test_no_critical_or_high_guarantee_is_advisory():
    """Guarantees sourced from C* or H* STRESS findings must not be advisory (P1S-10)."""
    violations = [
        g for g in GUARANTEES
        if g.kind == "advisory" and g.source.startswith(("C", "H"))
    ]
    assert violations == [], (
        "C*/H*-derived guarantees must not be advisory (P1S-10): "
        + ", ".join(f"{g.id} (source={g.source})" for g in violations)
    )


def test_advisory_allowed_all_exist():
    """Every id in ADVISORY_ALLOWED must correspond to an actual guarantee."""
    ids = {g.id for g in GUARANTEES}
    orphaned = ADVISORY_ALLOWED - ids
    assert not orphaned, f"ADVISORY_ALLOWED contains ids with no guarantee: {orphaned}"


# ---------------------------------------------------------------------------
# Violation detection unit tests
# ---------------------------------------------------------------------------

def test_verify_reports_nonexistent_node(tmp_path):
    """A non-advisory guarantee with a non-existent enforcer node is reported."""
    bad_guarantee = Guarantee(
        id="SH-TEST-NONEXISTENT",
        statement="Test guarantee pointing at a nonexistent node.",
        enforcer="tests/shell/test_security_map.py::test_this_does_not_exist_xyz",
        kind="test",
        source="SPEC-S10",
    )
    original = list(GUARANTEES)
    GUARANTEES.append(bad_guarantee)
    try:
        violations = verify_enforcers(REPO_ROOT)
    finally:
        GUARANTEES[:] = original

    matching = [v for v in violations if "SH-TEST-NONEXISTENT" in v]
    assert matching, (
        f"Expected a violation for SH-TEST-NONEXISTENT but got: {violations}"
    )


def test_verify_reports_advisory_not_in_allowlist(tmp_path):
    """An advisory guarantee whose id is not in ADVISORY_ALLOWED is reported."""
    bad_guarantee = Guarantee(
        id="SH-TEST-UNADVISORY",
        statement="Advisory guarantee not in the allowlist.",
        enforcer="tests/shell/test_security_map.py::test_verify_enforcers_is_empty",
        kind="advisory",
        source="SPEC-S10",
    )
    original = list(GUARANTEES)
    GUARANTEES.append(bad_guarantee)
    try:
        violations = verify_enforcers(REPO_ROOT)
    finally:
        GUARANTEES[:] = original

    matching = [v for v in violations if "SH-TEST-UNADVISORY" in v]
    assert matching, (
        f"Expected a violation for SH-TEST-UNADVISORY but got: {violations}"
    )


def test_verify_reports_lint_target_not_in_makefile(tmp_path):
    """A lint guarantee with a target not in the Makefile is reported."""
    bad_guarantee = Guarantee(
        id="SH-TEST-LINT",
        statement="Lint guarantee with a non-existent Makefile target.",
        enforcer="lint:this_target_does_not_exist_xyz",
        kind="lint",
        source="SPEC-S10",
    )
    original = list(GUARANTEES)
    GUARANTEES.append(bad_guarantee)
    try:
        violations = verify_enforcers(REPO_ROOT)
    finally:
        GUARANTEES[:] = original

    matching = [v for v in violations if "SH-TEST-LINT" in v]
    assert matching, (
        f"Expected a violation for SH-TEST-LINT but got: {violations}"
    )


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------

def test_guarantee_ids_are_unique():
    """All guarantee ids must be unique."""
    ids = [g.id for g in GUARANTEES]
    seen: set[str] = set()
    duplicates = [i for i in ids if i in seen or seen.add(i)]  # type: ignore[func-returns-value]
    assert not duplicates, f"Duplicate guarantee ids: {duplicates}"


def test_guarantee_kind_values_are_valid():
    """All guarantee kinds must be 'test', 'lint', or 'advisory'."""
    invalid = [g for g in GUARANTEES if g.kind not in ("test", "lint", "advisory")]
    assert not invalid, (
        "Invalid kind values: " + ", ".join(f"{g.id}:{g.kind}" for g in invalid)
    )


def test_all_ids_start_with_sh():
    """All guarantee ids must start with 'SH-'."""
    bad = [g for g in GUARANTEES if not g.id.startswith("SH-")]
    assert not bad, "Guarantee ids not starting with 'SH-': " + ", ".join(g.id for g in bad)


def test_guarantees_list_is_nonempty():
    """GUARANTEES must be non-empty."""
    assert len(GUARANTEES) > 0


def test_advisory_allowed_is_frozenset():
    """ADVISORY_ALLOWED must be a frozenset."""
    assert isinstance(ADVISORY_ALLOWED, frozenset)
