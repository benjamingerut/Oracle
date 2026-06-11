"""oracle_agent/eval -- the Phase 6 trust-and-evaluation scoring harness.

Ships in the package (the sanctioned testkit-importing exception, P6S-12) so
``oracle eval`` can score the safety/quality catalog offline. The CLI handler
imports this lazily (the grounding-report pattern) so the production CLI's
import path stays testkit-free.

Public surface::

    from oracle_agent.eval import (
        Scenario, Verdict, Observation, DimensionScore, Scorecard,
        SEVERITY_BY_DIMENSION, run_catalog, render_scorecard,
        all_scenarios, scenarios_for_dimension,
    )
"""
from __future__ import annotations

from .harness import (
    DimensionScore,
    Observation,
    Scenario,
    Scorecard,
    SEVERITY_BY_DIMENSION,
    Verdict,
    fresh_root,
    last_committed_scorecard,
    parse_scorecard_rates,
    render_scorecard,
    render_trend,
    run_catalog,
    run_scenario,
)

__all__ = [
    "DimensionScore",
    "Observation",
    "Scenario",
    "Scorecard",
    "SEVERITY_BY_DIMENSION",
    "Verdict",
    "fresh_root",
    "last_committed_scorecard",
    "parse_scorecard_rates",
    "render_scorecard",
    "render_trend",
    "run_catalog",
    "run_scenario",
    "all_scenarios",
    "scenarios_for_dimension",
]


def all_scenarios() -> list["Scenario"]:
    """The full catalog: every dimension's scenarios, in a pinned id order.

    Imported lazily so a CLI ``--dimension`` subset never pays to import the
    other catalogs, and so importing the package does not eagerly construct
    every scenario closure.
    """
    from .scenarios import leak, grounding, policy, gateway, behavior, usefulness

    out: list[Scenario] = []
    out.extend(leak.scenarios())
    out.extend(grounding.scenarios())
    out.extend(policy.scenarios())
    out.extend(gateway.scenarios())
    out.extend(behavior.scenarios())
    out.extend(usefulness.scenarios())
    out.sort(key=lambda s: s.id)
    return out


def scenarios_for_dimension(dimension: str) -> list["Scenario"]:
    """The subset of the catalog for one dimension (pinned id order)."""
    return [s for s in all_scenarios() if s.dimension == dimension]
