#!/usr/bin/env python3
"""briefing.py -- the leadership brief: the oracle's proactive voice.

A v1 oracle answered what it was asked and volunteered nothing. The brief is
the thought-partner deliverable that fixes that: on cadence (the
``leadership-briefing`` loop, active at spawn) or on demand
(``./oracle brief``), the oracle composes what leadership should know NOW:

  1. State of the oracle      -- evidence/authority coverage, maturity counts
  2. What changed             -- sources + findings in the period
  3. Decisions waiting        -- the top of the Review Inbox
  4. Contradictions           -- open conflicts, must_resolve first
  5. Authority coverage gaps  -- truth-map rows that cannot yet ground answers
  6. Questions going stale    -- open questions past their budget
  7. Needs authority appendix -- objects whose claims were withheld (exit 4),
                                 each with the exact fix commands

Discipline: every object-level claim in sections 1-6 is routed through
``answer_protocol.preflight``; refused objects move to the appendix with their
``suggested_fix`` instead of shipping as silent omissions. The deterministic
skeleton is complete by itself; the operating agent may append narrative
interpretation in the marked enrichment section, subject to the same answer
protocol for any new material claim.

Publication reuses ``standing_deliverables.emit`` so briefs obey the same
policy gate + verified copy + registry ledger as every standing deliverable.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:  # bare import (conftest puts _tools on sys.path); package fallback
    import answer_protocol as _ap
    import review_queue as _rq
    import truth_map as _truth_map
except Exception:  # pragma: no cover - package import path
    from . import answer_protocol as _ap  # type: ignore
    from . import review_queue as _rq  # type: ignore
    from . import truth_map as _truth_map  # type: ignore

__all__ = ["build_brief", "emit_brief", "run_leadership_briefing"]

DEFAULT_PERIOD_DAYS = 7
_DEFAULT_SENSITIVITY = "internal"


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iter_notes(folder: Path):
    if not folder.is_dir():
        return
    for p in sorted(folder.glob("*.md")):
        if p.name.startswith("_"):
            continue
        yield p


def _count_notes(root: Path, kind: str) -> int:
    return sum(1 for _ in _iter_notes(root / "Memory.nosync" / kind))


def _recent_notes(root: Path, kind: str, since: datetime) -> list[dict]:
    out = []
    for p in _iter_notes(root / "Memory.nosync" / kind):
        fm = _ap.read_frontmatter(p)
        dt = _ap._parse_as_of(str(fm.get("created", fm.get("as_of", ""))))
        if dt is not None and dt >= since:
            out.append({"title": str(fm.get("title", p.stem)), "path": p.name, "dt": dt})
    out.sort(key=lambda r: r["dt"], reverse=True)
    return out


_LADDER_LABEL = {0: "grounded", 2: "supported", 3: "caveated", 4: "withheld"}


def build_brief(
    root,
    *,
    period_days: int = DEFAULT_PERIOD_DAYS,
    now: Optional[datetime] = None,
) -> dict:
    """Build the leadership brief document (standing-deliverable doc shape)."""
    root = Path(root)
    t = _now(now)
    since = t - timedelta(days=period_days)

    diags = _truth_map.validate_rows(root)
    inbox = _rq.build_queue(root, t)
    new_sources = _recent_notes(root, "Sources", since)
    new_findings = _recent_notes(root, "Findings", since)

    shipped = 0
    dropped: list[dict] = []
    caveated = 0

    # Per-object verdicts (the brief's claims about authority coverage).
    coverage: list[dict] = []
    for d in diags:
        env = _ap.preflight(root, d["business_object"])
        code = env.exit_code()
        if code == _ap.EXIT_REFUSED:
            dropped.append(
                {
                    "object": d["business_object"],
                    "reason": env.refusal_reason,
                    "fix": list(env.suggested_fix),
                }
            )
        else:
            shipped += 1
            if code == _ap.EXIT_CAVEATED:
                caveated += 1
        coverage.append(
            {
                "object": d["business_object"],
                "verdict": _LADDER_LABEL.get(code, str(code)),
                "status": d["status"],
                "evidence": d["evidence_count"],
                "freshness": d["freshness"],
                "needs": d["needs"],
            }
        )

    contradictions = [i for i in inbox if i["kind"] == "contradiction"]
    stale_questions = [i for i in inbox if i["kind"] == "stale-question"]
    decisions_waiting = [
        i for i in inbox if i["kind"] not in ("contradiction", "stale-question")
    ][:7]

    lines: list[str] = [
        "# Leadership Brief",
        "",
        f"_Generated {t.strftime('%Y-%m-%d %H:%M UTC')} -- covering the last "
        f"{period_days} day(s). Every object-level claim routed through the "
        "answer protocol; withheld objects appear in the appendix with their fix._",
        "",
        "## 1. State of the oracle",
        "",
        f"- Evidence: {_count_notes(root, 'Sources')} source(s), "
        f"{_count_notes(root, 'Findings')} finding(s), "
        f"{_count_notes(root, 'Models')} model(s)",
        f"- Authority: {sum(1 for d in diags if d['status'] == 'confirmed')} confirmed / "
        f"{len(diags)} truth-map row(s); "
        f"{sum(1 for d in diags if d['promotable'])} ready to promote",
        f"- Review inbox: {len(inbox)} item(s) waiting on a decision",
        "",
        "## 2. What changed",
        "",
    ]
    if new_sources or new_findings:
        for s in new_sources[:10]:
            lines.append(f"- new source: {s['title']}")
        for f in new_findings[:10]:
            lines.append(f"- new finding: {f['title']}")
    else:
        lines.append("_No new sources or findings in the period._")

    lines += ["", "## 3. Decisions waiting", ""]
    if decisions_waiting:
        for i in decisions_waiting:
            lines.append(f"- [{i['kind']}] {i['title']}")
            lines.append(f"  - do: {i['action']}")
    else:
        lines.append("_Nothing is waiting on a decision._")

    lines += ["", "## 4. Contradictions", ""]
    if contradictions:
        for c in contradictions:
            urgent = " **(must_resolve)**" if c["rank"] == 0 else ""
            lines.append(f"- {c['title']}{urgent}")
    else:
        lines.append("_No open contradictions._")

    lines += ["", "## 5. Authority coverage", ""]
    if coverage:
        lines.append("| Business object | Verdict | Status | Evidence | Freshness |")
        lines.append("|---|---|---|---|---|")
        for c in coverage:
            lines.append(
                f"| {c['object']} | {c['verdict']} | {c['status']} | "
                f"{c['evidence']} | {c['freshness']} |"
            )
    else:
        lines.append("_No truth-map rows yet -- the oracle cannot ground any answer._")

    lines += ["", "## 6. Questions going stale", ""]
    if stale_questions:
        for q in stale_questions:
            lines.append(f"- {q['title']} ({q['age_days']}d)")
    else:
        lines.append("_No stale open questions._")

    lines += ["", "## Appendix: needs authority setup", ""]
    if dropped:
        lines.append(
            "_Claims about these objects were withheld (no authority). "
            "Run the fix to unlock them:_"
        )
        for d in dropped:
            lines.append(f"- **{d['object']}** ({d['reason']})")
            for fx in d["fix"]:
                lines.append(f"  - `{fx}`")
    else:
        lines.append("_Every tracked object can ground or support an answer._")

    lines += [
        "",
        "## Agent enrichment",
        "",
        "_The operating agent may append narrative interpretation below this "
        "line. Any new material claim must pass `./oracle answer` first._",
    ]

    return {
        "kind": "leadership-brief",
        "title": "Leadership Brief",
        "body": "\n".join(lines) + "\n",
        "sensitivity_ceiling": _DEFAULT_SENSITIVITY,
        "shipped": shipped,
        "dropped": len(dropped),
        "caveated": caveated,
        "inbox_total": len(inbox),
        "needs_authority": dropped,
    }


def emit_brief(
    root,
    *,
    period_days: int = DEFAULT_PERIOD_DAYS,
    sensitivity: Optional[str] = None,
    approval: Optional[str] = None,
    actor: Optional[str] = None,
    role: str = "user",
) -> dict:
    """Publish the brief into ``_STANDING`` via the standing-deliverable gate."""
    try:
        import standing_deliverables as _sd  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        from . import standing_deliverables as _sd  # type: ignore
    doc = build_brief(root, period_days=period_days)
    report = _sd.emit(
        Path(root),
        "leadership-brief",
        sensitivity=sensitivity or doc["sensitivity_ceiling"],
        approval=approval,
        actor=actor,
        role=role,
        doc=doc,
    )
    report["needs_authority"] = doc["needs_authority"]
    return report


def run_leadership_briefing(root, loop=None, *, now: Optional[datetime] = None) -> dict:
    """Builtin runner for the ``leadership-briefing`` loop.

    Builds and publishes the deterministic brief. The agent's enrichment (and
    delivery to the leader) is the remaining worklist when material exists.
    """
    try:
        report = emit_brief(root, actor="leadership-briefing-loop", role="system")
        return {
            "status": "ok",
            "performed": True,
            "kind": "builtin:leadership-briefing",
            "artifact": report.get("artifact_name") or report.get("path"),
            "summary": (
                f"brief published; shipped={report.get('shipped')} "
                f"dropped={report.get('dropped')} caveated={report.get('caveated')}"
            ),
            "worklist": {
                "instructions": (
                    "Review the published brief, append narrative enrichment "
                    "(answer-protocol-gated), and deliver it to the leader."
                ),
            },
        }
    except Exception as exc:
        return {
            "status": "fail",
            "performed": False,
            "kind": "builtin:leadership-briefing",
            "error": f"{type(exc).__name__}: {exc}",
        }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="briefing",
        description="Compose (and optionally publish) the leadership brief.",
    )
    ap.add_argument("--root", default=".", help="oracle root")
    sub = ap.add_subparsers(dest="cmd", required=False)

    gen = sub.add_parser("gen", help="print the brief to stdout (default)")
    gen.add_argument("--days", type=int, default=DEFAULT_PERIOD_DAYS)
    gen.add_argument("--json", action="store_true")

    pub = sub.add_parser("publish", help="publish into _STANDING via the policy gate")
    pub.add_argument("--days", type=int, default=DEFAULT_PERIOD_DAYS)
    pub.add_argument("--sensitivity")
    pub.add_argument("--approval")
    pub.add_argument("--actor")
    pub.add_argument("--role", default="user")
    pub.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    root = Path(args.root)
    cmd = args.cmd or "gen"

    if cmd == "gen":
        doc = build_brief(root, period_days=getattr(args, "days", DEFAULT_PERIOD_DAYS))
        if getattr(args, "json", False):
            print(json.dumps(doc, indent=2, default=str))
        else:
            print(doc["body"])
        return 0

    if cmd == "publish":
        report = emit_brief(
            root,
            period_days=args.days,
            sensitivity=args.sensitivity,
            approval=args.approval,
            actor=args.actor,
            role=args.role,
        )
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(
                f"published {report.get('artifact_name')}: shipped={report.get('shipped')} "
                f"dropped={report.get('dropped')} caveated={report.get('caveated')}"
            )
        return 0

    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
