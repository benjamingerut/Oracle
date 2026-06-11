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

import re
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

    # Hold-out lifecycle stamp (P6-T7 / P6S-6): rendered whenever the usefulness
    # dimension is scored (the gold eval consumes the hold-out on a scoring run).
    # The consumed ids are the frozen every-5th hold-out -- a deterministic
    # constant, so this stays byte-identical across runs. Convention-only: the
    # ids are in-repo, world-readable, excluded from tuning by P8S-12 discipline
    # -- NOT a secret.
    if "usefulness" in sc.by_dimension:
        lines.append("## Hold-out lifecycle (usefulness, convention-only)")
        lines.append("")
        lines.append(
            f"This scoring run CONSUMED the frozen retrieval hold-out (query "
            f"ids {', '.join(str(i) for i in _HOLDOUT_IDS)} -- every 5th id). "
            f"The hold-out is CONVENTION-ONLY: in-repo, world-readable, excluded "
            f"from tuning by the P8S-12 discipline -- NOT a secret. On "
            f"consumption the ids are recorded here (dated by the scorecard "
            f"stamp), folded into the tracked set, and a fresh hold-out is "
            f"minted via eval/fixtures/regen_retrieval_gold.py (new generation, "
            f"same every-5th rule) for the next eval generation. Review rule: no "
            f"change-set may touch ranker constants and gold fixtures together."
        )
        lines.append("")

    return "\n".join(lines)


#: The frozen every-5th retrieval hold-out, stamped on each scoring run (P6S-6).
#: Kept here (not only in the usefulness scenario) so render_scorecard is the
#: single place the consumed-ids stamp is produced -- deterministic + dated.
_HOLDOUT_IDS = (5, 10, 15, 20, 25)


# ---------------------------------------------------------------------------
# Trend renderer (P6-T5): compare the current scorecard against the LAST
# COMMITTED docs/eval/*.md. Class-1/2 metrics only (every dimension's rate is a
# class-1/2 number -- safety counts and quality rates; no class-3 number is ever
# rendered, so there is nothing here CI cannot honestly compute). A missing
# baseline => no trend, stated. A quality regression past TREND_DELTA renders a
# WARNING, never a failure (quality is tracked, not gated -- the CLI's --ci gate
# fires only on a safety_floor_breach, not on a trend).
# ---------------------------------------------------------------------------

#: A quality rate must drop by MORE than this to be flagged a regression. Equal
#: rates (and tiny float wobble) read as "flat", never a spurious regression
#: (the P6S-14 reproducibility discipline carried into trends).
TREND_DELTA = 0.0001

# Matches a dimension row of a rendered scorecard table:
#   | leak | safety | 3 | 3 | 1.0000 |
_DIM_ROW = re.compile(
    r"^\|\s*([A-Za-z_]+)\s*\|\s*(safety|quality)\s*\|\s*(\d+)\s*\|\s*"
    r"(\d+)\s*\|\s*([0-9.]+)\s*\|\s*$"
)


def last_committed_scorecard(docs_eval_dir: Path) -> Optional[Path]:
    """The most recent committed ``docs/eval/<date>.md`` (lexical date order).

    Files are named ``YYYY-MM-DD.md``; ISO dates sort lexically, so the last
    name is the most recent. Returns ``None`` when the directory is absent or
    empty (=> no baseline, no trend).
    """
    docs_eval_dir = Path(docs_eval_dir)
    if not docs_eval_dir.is_dir():
        return None
    cards = sorted(
        p for p in docs_eval_dir.glob("*.md")
        if not p.name.startswith("_")
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem)
    )
    return cards[-1] if cards else None


def parse_scorecard_rates(markdown: str) -> dict[str, float]:
    """Extract ``{dimension: rate}`` from a rendered scorecard's dimension table.

    Reads only the rendered rates -- the counts/ids/rates the renderer already
    consumes -- so the trend is computed from committed numbers, never raw rows.
    """
    rates: dict[str, float] = {}
    for line in markdown.splitlines():
        m = _DIM_ROW.match(line.strip())
        if m:
            rates[m.group(1)] = float(m.group(5))
    return rates


def render_trend(sc: Scorecard, baseline_path: Optional[Path]) -> str:
    """Render the trend block: current dimension rates vs the last committed card.

    Class-1/2 only; missing baseline => an explicit "no trend" line (stated,
    never silent). A quality regression past :data:`TREND_DELTA` is a WARNING
    (tracked, not gated). Output is deterministic: dimensions in id order, fixed
    precision -- two runs against the same baseline render byte-identical.
    """
    lines: list[str] = ["## Trend (class-1/2 only, tracked not gated)", ""]
    if baseline_path is None:
        lines.append(
            "No committed baseline scorecard under docs/eval/ -- no trend. "
            "(A missing baseline is stated, never silently treated as 'flat'.)")
        lines.append("")
        return "\n".join(lines)

    try:
        baseline = parse_scorecard_rates(
            Path(baseline_path).read_text(encoding="utf-8"))
    except OSError:
        lines.append(
            f"Baseline {Path(baseline_path).name} could not be read -- no "
            f"trend (stated, not silently 'flat').")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"Baseline: `{Path(baseline_path).name}`.")
    lines.append("")
    lines.append("| dimension | severity | baseline | current | direction |")
    lines.append("| --- | --- | --- | --- | --- |")
    regressions: list[str] = []
    for dim in sorted(sc.by_dimension):
        cur = sc.by_dimension[dim].rate
        sev = SEVERITY_BY_DIMENSION.get(dim, "quality")
        if dim not in baseline:
            direction = "new"
            base_str = "--"
        else:
            base = baseline[dim]
            base_str = f"{base:.4f}"
            if cur > base + TREND_DELTA:
                direction = "improving"
            elif cur < base - TREND_DELTA:
                direction = "regressing"
                if sev == "quality":
                    regressions.append(dim)
            else:
                direction = "flat"
        lines.append(
            f"| {dim} | {sev} | {base_str} | {cur:.4f} | {direction} |")
    lines.append("")
    if regressions:
        lines.append(
            "**WARNING (tracked, not gated):** quality regression in "
            + ", ".join(sorted(regressions))
            + ". This is a trend signal, NOT a CI failure -- the --ci gate "
            "fires only on a safety floor breach.")
    else:
        lines.append(
            "No quality regression past the trend delta. (Trends never gate "
            "CI; only safety floor breaches do.)")
    lines.append("")
    return "\n".join(lines)
