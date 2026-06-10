#!/usr/bin/env python3
"""synthesis.py -- the insight-consolidation engine (deterministic worklists).

v1 oracles accumulated Findings forever without ever rolling them up: models
fossilized, patterns never emerged, the oracle grew in volume but not in
coherence. This module is the deterministic half of the fix. It clusters
findings by business object, compares each cluster against the Models folder,
and emits a precise agent worklist:

  * a cluster with >= ``min_cluster`` findings and NO model -> "propose a model"
  * a model older than the newest finding in its cluster   -> "update the model"
  * a model past its staleness budget                      -> "re-validate"

The kernel never writes Models itself -- explanatory compression is judgment,
which is the operating agent's job. Agent-written model updates land with
``status: needs_review`` and flow through the Review Inbox like every other
derived claim. The ``insight-synthesis`` loop (active at spawn) runs this
builtin every cadence.

Model lifecycle: a Model note SHOULD carry ``last_validated:`` in frontmatter;
``review_queue`` and this module treat ``updated``/``created`` as fallbacks.

Read-only. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:  # bare import (conftest puts _tools on sys.path); package fallback
    import answer_protocol as _ap
    import truth_map as _truth_map
except Exception:  # pragma: no cover - package import path
    from . import answer_protocol as _ap  # type: ignore
    from . import truth_map as _truth_map  # type: ignore

__all__ = [
    "cluster_findings",
    "build_worklist",
    "run_insight_synthesis",
    "build_staleness_worklist",
    "run_staleness_sweep",
]

DEFAULT_MIN_CLUSTER = 3
DEFAULT_MODEL_STALE_DAYS = 60
DEFAULT_FINDING_STALE_DAYS = 90


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iter_notes(folder: Path):
    if not folder.is_dir():
        return
    for p in sorted(folder.glob("*.md")):
        if p.name.startswith("_"):
            continue
        yield p


def _note_dt(fm: dict, *keys: str) -> Optional[datetime]:
    best = None
    for key in keys:
        dt = _ap._parse_as_of(str(fm.get(key, "")))
        if dt is not None and (best is None or dt > best):
            best = dt
    return best


def _objects_of(fm: dict) -> list[str]:
    out = []
    for key in ("business_object", "object", "decision_relevance"):
        v = fm.get(key)
        vals = v if isinstance(v, list) else ([v] if v else [])
        out.extend(str(x) for x in vals if x)
    if not out and fm.get("subtype"):
        out = [str(fm["subtype"])]
    return out


def cluster_findings(root) -> dict[str, dict]:
    """Group Findings by normalized business object.

    Returns ``{norm_object: {label, findings: [{path, title, status, dt}]}}``.
    Findings with no object claim cluster under their ``subtype`` as a fallback;
    notes with neither are skipped (nothing to synthesize against).
    """
    root = Path(root)
    clusters: dict[str, dict] = {}
    for p in _iter_notes(root / "Memory.nosync" / "Findings"):
        fm = _ap.read_frontmatter(p)
        if not fm:
            continue
        objects = _objects_of(fm)
        if not objects:
            continue
        entry = {
            "path": str(p.relative_to(root)),
            "title": str(fm.get("title", p.stem)),
            "status": str(fm.get("status", "")).strip().lower(),
            "dt": _note_dt(fm, "updated", "created", "as_of"),
        }
        for obj in objects:
            norm = _truth_map.normalize_object(obj)
            if not norm:
                continue
            c = clusters.setdefault(norm, {"label": str(obj), "findings": []})
            c["findings"].append(entry)
    return clusters


def _models_by_object(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in _iter_notes(root / "Memory.nosync" / "Models"):
        fm = _ap.read_frontmatter(p)
        if not fm:
            continue
        for obj in _objects_of(fm) or [str(fm.get("title", p.stem))]:
            norm = _truth_map.normalize_object(obj)
            if norm and norm not in out:
                out[norm] = {
                    "path": str(p.relative_to(root)),
                    "title": str(fm.get("title", p.stem)),
                    "validated": _note_dt(fm, "last_validated", "updated", "created"),
                }
    return out


def build_worklist(
    root,
    *,
    min_cluster: int = DEFAULT_MIN_CLUSTER,
    model_stale_days: int = DEFAULT_MODEL_STALE_DAYS,
    now: Optional[datetime] = None,
) -> dict:
    """The deterministic synthesis worklist for the operating agent."""
    root = Path(root)
    t = _now(now)
    clusters = cluster_findings(root)
    models = _models_by_object(root)
    items: list[dict] = []

    for norm, cluster in sorted(clusters.items()):
        n = len(cluster["findings"])
        newest = max((f["dt"] for f in cluster["findings"] if f["dt"]), default=None)
        model = models.get(norm)
        if model is None:
            if n >= min_cluster:
                items.append(
                    {
                        "action": "propose-model",
                        "business_object": cluster["label"],
                        "finding_count": n,
                        "findings": [f["path"] for f in cluster["findings"]],
                        "instruction": (
                            f"{n} findings accumulated on {cluster['label']!r} with no "
                            "explanatory Model. Read the findings, write a Model note "
                            "(status: needs_review) that compresses how this part of the "
                            "company works, cite the findings as evidence, and stamp "
                            "last_validated."
                        ),
                    }
                )
            continue
        if newest and model["validated"] and newest > model["validated"]:
            items.append(
                {
                    "action": "update-model",
                    "business_object": cluster["label"],
                    "model": model["path"],
                    "finding_count": n,
                    "instruction": (
                        f"Findings on {cluster['label']!r} are newer than the model "
                        f"({model['title']!r}). Reconcile the model with the new "
                        "evidence; if the model's explanation no longer holds, say so "
                        "and supersede it. Stamp last_validated."
                    ),
                }
            )
            continue
        age_days = (
            (t - model["validated"]).total_seconds() / 86400.0
            if model["validated"]
            else None
        )
        if age_days is None or age_days > model_stale_days:
            items.append(
                {
                    "action": "revalidate-model",
                    "business_object": cluster["label"],
                    "model": model["path"],
                    "age_days": round(age_days, 1) if age_days is not None else None,
                    "instruction": (
                        f"Model {model['title']!r} is past its staleness budget. "
                        "Re-check it against current findings and stamp "
                        "last_validated (or supersede it)."
                    ),
                }
            )

    return {
        "kind": "insight-synthesis-worklist",
        "generated": t.isoformat(),
        "cluster_count": len(clusters),
        "model_count": len(models),
        "items": items,
    }


def run_insight_synthesis(root, loop=None, *, now: Optional[datetime] = None) -> dict:
    """Builtin runner for the ``insight-synthesis`` loop.

    Deterministic half only: computes the worklist and hands it to the agent.
    ``performed`` is False when items exist (agent work remains) and True when
    the pass found nothing to synthesize (the loop run itself is the work).
    """
    wl = build_worklist(root, now=now)
    return {
        "status": "worklist" if wl["items"] else "ok",
        "performed": not wl["items"],
        "kind": "builtin:insight-synthesis",
        "worklist": wl,
        "summary": (
            f"{len(wl['items'])} synthesis item(s) across {wl['cluster_count']} "
            f"finding cluster(s) / {wl['model_count']} model(s)"
        ),
    }


def _finding_stale_budget(root: Path) -> int:
    """``review.finding_stale_days`` from oracle.yml, else the default."""
    try:
        import oracle_yaml  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import oracle_yaml  # type: ignore
        except Exception:
            return DEFAULT_FINDING_STALE_DAYS
    try:
        data = oracle_yaml.safe_load((Path(root) / "oracle.yml").read_text(encoding="utf-8")) or {}
        review = data.get("review") or {}
        if isinstance(review, dict) and "finding_stale_days" in review:
            return int(review["finding_stale_days"])
    except Exception:
        pass
    return DEFAULT_FINDING_STALE_DAYS


def build_staleness_worklist(root, *, now: Optional[datetime] = None) -> dict:
    """Confirmed findings past their staleness budget (the memory-fossilization
    sweep). Models/questions are covered by the Review Inbox budgets; this
    sweep owns the one decay class nothing else watched: a CONFIRMED finding
    that has not been re-validated since the budget elapsed."""
    root = Path(root)
    t = _now(now)
    if t.tzinfo is None:
        # note timestamps parse timezone-aware (UTC); normalize a naive caller.
        t = t.replace(tzinfo=timezone.utc)
    budget = _finding_stale_budget(root)
    items: list[dict] = []
    for p in _iter_notes(root / "Memory.nosync" / "Findings"):
        fm = _ap.read_frontmatter(p)
        if not fm:
            continue
        if str(fm.get("status", "")).strip().lower() != "confirmed":
            continue
        dt = _note_dt(fm, "last_validated", "updated", "created", "as_of")
        age_days = (t - dt).total_seconds() / 86400.0 if dt else None
        if age_days is None or age_days > budget:
            items.append(
                {
                    "action": "refresh-finding",
                    "finding": str(p.relative_to(root)),
                    "title": str(fm.get("title", p.stem)),
                    "age_days": round(age_days, 1) if age_days is not None else None,
                    "instruction": (
                        f"Confirmed finding {fm.get('title', p.stem)!r} is past its "
                        f"{budget}-day staleness budget. Re-check it against current "
                        "sources: re-validate (stamp last_validated), supersede it, or "
                        "retire it. A fossilized finding is a future wrong answer."
                    ),
                }
            )
    return {
        "kind": "staleness-sweep-worklist",
        "generated": t.isoformat(),
        "budget_days": budget,
        "items": items,
    }


def run_staleness_sweep(root, loop=None, *, now: Optional[datetime] = None) -> dict:
    """Builtin runner for the ``stale-finding-refresh`` loop."""
    wl = build_staleness_worklist(root, now=now)
    return {
        "status": "worklist" if wl["items"] else "ok",
        "performed": not wl["items"],
        "kind": "builtin:stale-finding-refresh",
        "worklist": wl,
        "summary": f"{len(wl['items'])} confirmed finding(s) past the {wl['budget_days']}-day budget",
    }


def render_md(wl: dict) -> str:
    if not wl["items"]:
        return (
            "Insight synthesis: nothing to do. "
            f"({wl['cluster_count']} cluster(s), {wl['model_count']} model(s) — all current.)"
        )
    lines = [f"# Insight synthesis worklist — {len(wl['items'])} item(s)", ""]
    for i, item in enumerate(wl["items"], 1):
        lines.append(f"{i}. **{item['action']}** — {item['business_object']}")
        lines.append(f"   {item['instruction']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="synthesis",
        description="Cluster findings, compare against models, emit the synthesis worklist.",
    )
    ap.add_argument("--root", default=".", help="oracle root")
    sub = ap.add_subparsers(dest="cmd", required=False)
    wlp = sub.add_parser("worklist", help="build the synthesis worklist (default)")
    wlp.add_argument("--json", action="store_true")
    wlp.add_argument("--min-cluster", type=int, default=DEFAULT_MIN_CLUSTER)
    args = ap.parse_args(argv)

    wl = build_worklist(
        Path(args.root), min_cluster=getattr(args, "min_cluster", DEFAULT_MIN_CLUSTER)
    )
    if getattr(args, "json", False):
        print(json.dumps(wl, indent=2, default=str))
    else:
        print(render_md(wl))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
