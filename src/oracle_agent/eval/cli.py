"""oracle_agent/eval/cli.py -- the `oracle eval` command implementation (P6-T6).

Lives INSIDE the sanctioned eval package so it may import the harness directly
(the production CLI reaches this via importlib.import_module with a string, so
the converse-guard AST walk never sees cli.py name testkit or the eval package
-- the lazy-import discipline, P6S-12).

    oracle eval                  run the catalog, print the scorecard. Writes NOTHING.
    oracle eval --dimension leak subset to one dimension
    oracle eval --ci             exit non-zero on ANY safety_floor_breach. Writes NOTHING.
    oracle eval --write          the HUMAN action: also write docs/eval/<date>.md

CI writes nothing under docs/eval/; --write is the human persistence action
(P6S-5). Trend comparison reads the last COMMITTED scorecard and compares only
class-1/2 metrics; a quality regression past a configured delta renders a
warning, never a failure (the trend renderer is shared with P6-T5; this CLI
only invokes it if present).

Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

from .harness import (
    SEVERITY_BY_DIMENSION,
    render_scorecard,
    run_catalog,
)


def _repo_root() -> Path:
    # src/oracle_agent/eval/cli.py -> repo root is four parents up.
    return Path(__file__).resolve().parents[3]


def cmd_eval(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="oracle eval", add_help=True)
    parser.add_argument("--dimension", default=None,
                        help="run only one dimension "
                             f"({', '.join(sorted(SEVERITY_BY_DIMENSION))})")
    parser.add_argument("--ci", action="store_true",
                        help="exit non-zero on ANY safety floor breach; "
                             "writes nothing")
    parser.add_argument("--write", action="store_true",
                        help="the HUMAN action: also write docs/eval/<date>.md")
    parser.add_argument("--date", default=None,
                        help="scorecard date stamp (default: today, UTC)")
    args = parser.parse_args(rest)

    from . import all_scenarios, scenarios_for_dimension

    if args.dimension:
        if args.dimension not in SEVERITY_BY_DIMENSION:
            print(f"oracle eval: unknown dimension {args.dimension!r} "
                  f"(known: {', '.join(sorted(SEVERITY_BY_DIMENSION))})",
                  file=sys.stderr)
            return 2
        scenarios = scenarios_for_dimension(args.dimension)
    else:
        scenarios = all_scenarios()

    try:
        scorecard = run_catalog(scenarios)
    finally:
        # Release the once-spawned template + per-scenario copies.
        from .scenarios import _support
        _support.reset_template_cache()

    date = args.date or datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d")
    rendered = render_scorecard(scorecard, date)
    print(rendered)

    if args.write:
        # The HUMAN persistence action -- never taken by --ci.
        out_dir = _repo_root() / "docs" / "eval"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{date}.md"
        out_path.write_text(rendered, encoding="utf-8")
        print(f"\n[written] {out_path}", file=sys.stderr)

    if args.ci:
        # The gate: exit non-zero on ANY safety floor breach. Writes nothing.
        if scorecard.safety_floor_breaches:
            print(
                "\noracle eval --ci: SAFETY FLOOR BREACH -- "
                + ", ".join(scorecard.safety_floor_breaches),
                file=sys.stderr)
            return 1
    return 0
