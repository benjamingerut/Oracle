"""oracle_agent/eval/harness.py -- the Phase 6 scoring harness (P6-T1).

A *scenario* is a scripted, deterministic interaction: a spawned oracle in a
known state (a fresh COPY of a once-spawned template root), a scripted model
and/or fake gateways, and an assertion about what reached a sink. The harness
runs the whole catalog, derives each scenario's severity from its dimension
(the frozen ``SEVERITY_BY_DIMENSION`` map -- a scenario cannot declare itself
out of the gate, P6S-13), scores each dimension, and renders a dated scorecard.

This package is the ONE sanctioned exception to the no-production-imports-testkit
rule (P6S-12): it ships in the package and imports ``testkit``.
``test_no_production_module_imports_testkit`` allowlists exactly
``oracle_agent/eval/``; a converse guard asserts nothing outside ``eval/`` +
``testkit.py`` imports either ``testkit`` or ``oracle_agent.eval``.

Reproducibility pins (P6S-14): every ranked/listed output uses a pinned total
order (id ascending) and fixed precision (``round(., 4)``). Date is an INPUT to
``render_scorecard``; the body carries no wall-clock, so two consecutive runs
render byte-identical scorecards on every CI cell.

Stdlib only.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Derived severity (frozen). Severity is NOT a Scenario field, so a scenario
# cannot declare dimension="leak", severity="quality" and dodge the gate
# (P6S-13). run_catalog rejects any Scenario carrying a `severity` attribute.
# ---------------------------------------------------------------------------
SEVERITY_BY_DIMENSION: dict[str, str] = {
    "leak": "safety",
    "grounding": "safety",
    "policy": "safety",
    "gateway": "safety",
    "behavior": "quality",
    "usefulness": "quality",
}

_SAFETY_DIMENSIONS = frozenset(
    d for d, sev in SEVERITY_BY_DIMENSION.items() if sev == "safety"
)


# ---------------------------------------------------------------------------
# Data model (frozen interface)
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    """The outcome of a scenario's assert_outcome -- pass/fail + evidence.

    ``evidence`` is the WHY, rendered into the scorecard on failure. A bare
    bool cannot prove the probe reached the sink (P6S-8); the evidence string
    carries the sink-scan result (which marker was/was not present, which
    control fired).
    """
    passed: bool
    evidence: str


@dataclass
class Observation:
    """Everything a scenario's run() produced, for assert_outcome to inspect.

    ``ledger_rows`` are for assertions ONLY -- never rendered raw (rows carry
    wall-clock ts; rendering them would break scorecard reproducibility, P6S-13).
    """
    user_visible: list[str] = field(default_factory=list)
    ledger_rows: list[dict] = field(default_factory=list)
    verdicts: list[dict] = field(default_factory=list)
    # Sink handles captured at run time so assert_outcome can scan the model
    # context and embedding requests without re-driving the fake. Optional --
    # policy/parity scenarios have no fake sink.
    extras: dict = field(default_factory=dict)


@dataclass
class Scenario:
    """One scripted, deterministic scenario.

    ``guarantee`` is the SH-xxx this scenario enforces, or ``None`` ONLY while
    the matching NEW Guarantee lands in ``security_map.GUARANTEES`` in the same
    change-set (frozen interface).

    ``fault_point`` is the dotted path of an in-process SHELL callable that,
    no-op'd, MUST flip this scenario to fail (mandatory for safety scenarios
    with a seam; ``None`` => the scenario lands in ``Scorecard.no_seam``, the
    honest enumeration for kernel-internal logic with no patchable shell seam).
    """
    id: str
    dimension: str
    guarantee: Optional[str]
    setup: Callable          # (Harness) -> context
    run: Callable            # (context) -> Observation
    assert_outcome: Callable  # (Observation) -> Verdict
    fault_point: Optional[str] = None

    @property
    def severity(self) -> str:
        """DERIVED from dimension -- never a stored field (P6S-13)."""
        return SEVERITY_BY_DIMENSION.get(self.dimension, "quality")


@dataclass
class DimensionScore:
    passed: int
    total: int
    rate: float                 # round(passed/total, 4)
    failed_ids: list[str]       # sorted ascending (pinned total order)


@dataclass
class Scorecard:
    by_dimension: dict[str, DimensionScore]
    safety_floor_breaches: list[str]   # scenario ids below the 100% floor
    no_seam: list[str]                 # safety scenarios with no fault_point


# ---------------------------------------------------------------------------
# Per-scenario root isolation: copytree of a once-spawned template (P6-T1)
# ---------------------------------------------------------------------------

def fresh_root(template_root: Path, dest: Path) -> Path:
    """Copy the once-spawned *template_root* to *dest* (cheap, deterministic).

    Each scenario receives a FRESH copy -- never the shared session
    ``spawned_root`` -- so one scenario's ingest/promote can never contaminate
    another (P6S-13). copytree of a spawn template is far cheaper than a fresh
    spawn subprocess per scenario.
    """
    shutil.copytree(template_root, dest)
    return dest


# ---------------------------------------------------------------------------
# run_catalog
# ---------------------------------------------------------------------------

def run_catalog(scenarios: list[Scenario]) -> Scorecard:
    """Run every scenario, derive severity, score each dimension.

    Rejects any scenario that tries to carry an explicit ``severity`` attribute
    (the derived-severity invariant, P6S-13) or an unknown dimension.
    """
    for sc in scenarios:
        if "severity" in vars(sc):
            raise ValueError(
                f"scenario {sc.id!r} carries an explicit 'severity' -- severity "
                f"is DERIVED from dimension via SEVERITY_BY_DIMENSION (P6S-13)"
            )
        if sc.dimension not in SEVERITY_BY_DIMENSION:
            raise ValueError(
                f"scenario {sc.id!r} has unknown dimension {sc.dimension!r}; "
                f"known: {sorted(SEVERITY_BY_DIMENSION)}"
            )

    by_dimension: dict[str, dict] = {}
    breaches: list[str] = []
    no_seam: list[str] = []

    for sc in scenarios:
        bucket = by_dimension.setdefault(
            sc.dimension, {"passed": 0, "total": 0, "failed_ids": []}
        )
        verdict = run_scenario(sc)
        bucket["total"] += 1
        if verdict.passed:
            bucket["passed"] += 1
        else:
            bucket["failed_ids"].append(sc.id)
            if sc.dimension in _SAFETY_DIMENSIONS:
                breaches.append(sc.id)
        # Honest no_seam enumeration: a safety scenario with no patchable shell
        # fault_point is covered by parity comparison instead (P6S-7).
        if sc.dimension in _SAFETY_DIMENSIONS and sc.fault_point is None:
            no_seam.append(sc.id)

    scored: dict[str, DimensionScore] = {}
    for dim in sorted(by_dimension):
        b = by_dimension[dim]
        rate = round(b["passed"] / b["total"], 4) if b["total"] else 0.0
        scored[dim] = DimensionScore(
            passed=b["passed"],
            total=b["total"],
            rate=rate,
            failed_ids=sorted(b["failed_ids"]),
        )

    return Scorecard(
        by_dimension=scored,
        safety_floor_breaches=sorted(breaches),
        no_seam=sorted(no_seam),
    )


def run_scenario(sc: Scenario) -> Verdict:
    """Drive one scenario end-to-end: build a harness, setup, run, assert.

    The harness is constructed lazily inside here so importing this module
    never imports testkit eagerly down a production CLI path (the lazy-import
    discipline still applies at the package boundary).
    """
    from oracle_agent.testkit import Harness  # local: keep import boundary tight

    ctx = sc.setup(Harness)
    obs = sc.run(ctx)
    return sc.assert_outcome(obs)


# ---------------------------------------------------------------------------
# Rendering -- counts/ids/rates ONLY, fixed precision, date is an INPUT
# ---------------------------------------------------------------------------

def render_scorecard(sc: Scorecard, date: str) -> str:
    """Render *sc* as markdown. ``date`` is an INPUT (never wall-clock-derived).

    Consumes only counts/ids/rates -- never raw ledger rows -- so two runs on
    any CI cell render byte-identical output (P6S-14). Dimensions render in id
    order; failed ids and no_seam ids are pre-sorted ascending.
    """
    safe_total = sum(
        d.total for dim, d in sc.by_dimension.items()
        if dim in _SAFETY_DIMENSIONS
    )
    safe_passed = sum(
        d.passed for dim, d in sc.by_dimension.items()
        if dim in _SAFETY_DIMENSIONS
    )
    lines: list[str] = [
        f"# Oracle Eval Scorecard -- {date}",
        "",
        "Deterministic, offline (scripted model + fake gateways + fake "
        "embedder). Safety dimensions are HARD gates at 100%; quality "
        "dimensions are tracked trends. Class-3 (real-model / real-traffic) "
        "metrics never appear here.",
        "",
        f"Safety floor: **{safe_passed}/{safe_total}** "
        f"({'PASS' if not sc.safety_floor_breaches else 'BREACH'}).",
        "",
        "## Dimensions",
        "",
        "| dimension | severity | passed | total | rate |",
        "| --- | --- | --- | --- | --- |",
    ]
    for dim in sorted(sc.by_dimension):
        d = sc.by_dimension[dim]
        sev = SEVERITY_BY_DIMENSION.get(dim, "quality")
        lines.append(
            f"| {dim} | {sev} | {d.passed} | {d.total} | {d.rate:.4f} |"
        )
    lines.append("")

    # Failed ids per dimension (sorted; counts only).
    failed_any = any(d.failed_ids for d in sc.by_dimension.values())
    lines.append("## Failures")
    lines.append("")
    if not failed_any:
        lines.append("None.")
    else:
        for dim in sorted(sc.by_dimension):
            d = sc.by_dimension[dim]
            if d.failed_ids:
                lines.append(f"- **{dim}**: " + ", ".join(d.failed_ids))
    lines.append("")

    # Safety floor breaches (explicit, even if redundant with Failures).
    lines.append("## Safety floor breaches")
    lines.append("")
    if not sc.safety_floor_breaches:
        lines.append("None.")
    else:
        for sid in sc.safety_floor_breaches:
            lines.append(f"- {sid}")
    lines.append("")

    # No-seam enumeration -- ALWAYS rendered, never hidden (P6S-7).
    lines.append("## No-seam safety scenarios")
    lines.append("")
    lines.append(
        "Safety scenarios with no patchable in-process shell fault seam "
        "(kernel-internal logic); covered by parity comparison instead of a "
        "planted fault. Enumerated honestly, never hidden."
    )
    lines.append("")
    if not sc.no_seam:
        lines.append("None.")
    else:
        for sid in sc.no_seam:
            lines.append(f"- {sid}")
    lines.append("")

    return "\n".join(lines)
