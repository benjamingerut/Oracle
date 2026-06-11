"""grounding_report.py -- evaluate the P3-T7 shadow capture against the budgets.

P3-T7 measures, on REAL local-operator traffic captured in OBSERVE mode, whether
ENFORCE may become the local-chat default. The capture sink is the local-only,
operator-consented ``grounding_shadow.jsonl`` under ``profile_dir()`` (claim text
+ verdict + turn timing; written ONLY on the local-OBSERVE branch of the loop,
never on any gateway path, and excluded from backups -- P3S-10 / G5).

This module reads that file and computes the pinned budgets (set 2026-06-11,
P3S-10), making the go/no-go recommendation for the default flip:

  * False-positive budget: <= 5% of flagged claim-units judged non-material by
    the operator, AND <= 10% of turns incur a repair round-trip caused SOLELY by
    false positives.
  * Added-latency budget: p50 added wall-clock <= 0.5s (no-repair path); p95
    added <= +1 model round-trip; mean added tokens per turn <= +20%.
  * Observation window: >= 50 real turns across >= 7 days.

The false-positive labels are HUMAN judgment: the shadow file records the flagged
claim text precisely so the operator can review and mark each as material or
non-material. A label sidecar (``grounding_shadow_labels.jsonl`` -- one
``{"claim": ..., "non_material": true|false}`` row per reviewed claim) supplies
those judgments; absent a label for a claim, this report counts it as UNLABELED
and cannot compute the FP rate until labeling is complete.

The shadow file carries no per-turn id, so "turns" are inferred conservatively
from the timing rows: each captured line records the turn's ``added_seconds``,
``iterations`` and ``repairs``. We group flagged units by ``(ts, added_seconds,
iterations, repairs)`` -- the per-turn timestamp + timing tuple -- to count
distinct turns and the distinct days they span. This is a measurement aid; the
authoritative repair-loop token/second telemetry for ENFORCE lives in the
gateway ledger (P3-T4), which this report does not read (the gateway is never
gated by P3-T7).

Stdlib only.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# Pinned budgets (P3S-10, 2026-06-11). Kept as module constants so the numbers
# live in exactly one place and the report cannot silently drift from the spec.
FP_UNIT_RATE_MAX = 0.05          # <= 5% of flagged units judged non-material
FP_TURN_RATE_MAX = 0.10          # <= 10% of turns with a FP-only repair round-trip
ADDED_P50_SECONDS_MAX = 0.5      # p50 added wall-clock (no-repair path)
WINDOW_MIN_TURNS = 50            # >= 50 real turns
WINDOW_MIN_DAYS = 7              # across >= 7 days

_LABELS_FILENAME = "grounding_shadow_labels.jsonl"


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip a corrupt line; never crash the report
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _turn_key(row: dict) -> tuple:
    """A conservative per-turn identity from the captured timing metadata."""
    return (
        row.get("ts"),
        row.get("added_seconds"),
        row.get("iterations"),
        row.get("repairs"),
    )


def _day_of(ts: object) -> str | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts).date().isoformat()
    except ValueError:
        # Fall back to the leading YYYY-MM-DD substring if present.
        return ts[:10] if len(ts) >= 10 else None


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile of ``values`` (0..1 pct). None on empty input."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = max(0, min(len(s) - 1, int(round(pct * (len(s) - 1)))))
    return s[rank]


def evaluate(shadow_rows: list[dict], label_rows: list[dict]) -> dict:
    """Compute the P3-T7 budget metrics from captured + labeled rows.

    Returns a metrics dict (all derived numbers + the per-budget pass flags and
    the overall go/no-go recommendation). Pure -- does no I/O.
    """
    # --- Label lookup: claim text -> non_material bool (last write wins). ---
    labels: dict[str, bool] = {}
    for lr in label_rows:
        claim = lr.get("claim")
        if isinstance(claim, str):
            labels[claim] = bool(lr.get("non_material", False))

    flagged_units = len(shadow_rows)
    labeled = 0
    fp_units = 0  # flagged units the operator judged non-material
    for row in shadow_rows:
        claim = row.get("claim")
        if isinstance(claim, str) and claim in labels:
            labeled += 1
            if labels[claim]:
                fp_units += 1
    unlabeled = flagged_units - labeled

    # --- Per-turn grouping. ---
    turns: dict[tuple, list[dict]] = {}
    for row in shadow_rows:
        turns.setdefault(_turn_key(row), []).append(row)
    total_turns = len(turns)

    # A turn is "FP-only repair" iff it took >=1 repair AND every flagged unit in
    # that turn is labeled non-material (the repair was bought solely by false
    # positives). A turn with any unlabeled flagged unit is not counted as
    # FP-only (we cannot yet prove the repair was wasted).
    fp_only_repair_turns = 0
    for key, rows in turns.items():
        repairs = rows[0].get("repairs") or 0
        if not isinstance(repairs, int) or repairs < 1:
            continue
        claims = [r.get("claim") for r in rows]
        if claims and all(
            isinstance(c, str) and labels.get(c, None) is True for c in claims
        ):
            fp_only_repair_turns += 1

    # Days spanned.
    days = {d for d in (_day_of(r.get("ts")) for r in shadow_rows) if d}
    n_days = len(days)

    # Added wall-clock: one value per turn (the captured added_seconds). p50 is
    # the budget's no-repair-path bound; we report p50 over all captured turns.
    added_secs = []
    for rows in turns.values():
        v = rows[0].get("added_seconds")
        if isinstance(v, (int, float)):
            added_secs.append(float(v))
    p50_added = _percentile(added_secs, 0.50)
    p95_added = _percentile(added_secs, 0.95)

    # --- Budget pass flags. ---
    fp_unit_rate = (fp_units / labeled) if labeled else None
    fp_turn_rate = (fp_only_repair_turns / total_turns) if total_turns else None

    window_ok = (total_turns >= WINDOW_MIN_TURNS and n_days >= WINDOW_MIN_DAYS)
    labeling_complete = (flagged_units > 0 and unlabeled == 0)

    fp_unit_pass = (fp_unit_rate is not None and fp_unit_rate <= FP_UNIT_RATE_MAX)
    fp_turn_pass = (fp_turn_rate is not None and fp_turn_rate <= FP_TURN_RATE_MAX)
    latency_pass = (p50_added is not None and p50_added <= ADDED_P50_SECONDS_MAX)

    # Go only if the window is met, labeling is complete, and every budget passes.
    go = bool(window_ok and labeling_complete
              and fp_unit_pass and fp_turn_pass and latency_pass)

    return {
        "flagged_units": flagged_units,
        "labeled_units": labeled,
        "unlabeled_units": unlabeled,
        "fp_units": fp_units,
        "fp_unit_rate": fp_unit_rate,
        "fp_unit_rate_budget": FP_UNIT_RATE_MAX,
        "fp_unit_pass": fp_unit_pass,
        "total_turns": total_turns,
        "fp_only_repair_turns": fp_only_repair_turns,
        "fp_turn_rate": fp_turn_rate,
        "fp_turn_rate_budget": FP_TURN_RATE_MAX,
        "fp_turn_pass": fp_turn_pass,
        "p50_added_seconds": p50_added,
        "p95_added_seconds": p95_added,
        "p50_added_budget": ADDED_P50_SECONDS_MAX,
        "latency_pass": latency_pass,
        "days_spanned": n_days,
        "window_min_turns": WINDOW_MIN_TURNS,
        "window_min_days": WINDOW_MIN_DAYS,
        "window_ok": window_ok,
        "labeling_complete": labeling_complete,
        "recommendation": "GO -- flip local default to ENFORCE" if go
        else "NO-GO -- keep local default OBSERVE",
        "go": go,
    }


def _fmt_pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _fmt_sec(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}s"


def render(metrics: dict) -> str:
    """Render a human-readable report from an ``evaluate`` metrics dict."""
    m = metrics
    out: list[str] = []
    out.append("P3-T7 forced-grounding ENFORCE-default budget report")
    out.append("=" * 52)
    out.append("")
    out.append("Observation window (>= 50 turns / >= 7 days):")
    out.append(f"  turns captured : {m['total_turns']}  (need >= {m['window_min_turns']})")
    out.append(f"  days spanned   : {m['days_spanned']}  (need >= {m['window_min_days']})")
    if not m["window_ok"]:
        out.append("  WINDOW UNMET -- the observation window requirement is NOT yet "
                   "satisfied; any pass below is provisional.")
    out.append("")
    out.append("False-positive budget:")
    out.append(f"  flagged claim-units : {m['flagged_units']}  "
               f"(labeled {m['labeled_units']}, unlabeled {m['unlabeled_units']})")
    out.append(f"  FP unit rate        : {_fmt_pct(m['fp_unit_rate'])}  "
               f"(budget <= {_fmt_pct(m['fp_unit_rate_budget'])})  "
               f"-> {'PASS' if m['fp_unit_pass'] else 'FAIL'}")
    out.append(f"  FP-only repair turns: {m['fp_only_repair_turns']} / {m['total_turns']} "
               f"= {_fmt_pct(m['fp_turn_rate'])}  "
               f"(budget <= {_fmt_pct(m['fp_turn_rate_budget'])})  "
               f"-> {'PASS' if m['fp_turn_pass'] else 'FAIL'}")
    if not m["labeling_complete"]:
        out.append("  LABELING INCOMPLETE -- some flagged claim-units have no "
                   "operator material/non-material judgment; the FP rate is not "
                   "final until every unit is labeled.")
    out.append("")
    out.append("Added-latency budget:")
    out.append(f"  p50 added wall-clock: {_fmt_sec(m['p50_added_seconds'])}  "
               f"(budget <= {_fmt_sec(m['p50_added_budget'])})  "
               f"-> {'PASS' if m['latency_pass'] else 'FAIL'}")
    out.append(f"  p95 added wall-clock: {_fmt_sec(m['p95_added_seconds'])}  "
               f"(budget <= +1 model round-trip; compare against gateway ledger)")
    out.append("  NOTE: added-token budget (mean <= +20%) is measured from the "
               "gateway repair telemetry (P3-T4), not from this local shadow file.")
    out.append("")
    out.append(f"RECOMMENDATION: {m['recommendation']}")
    return "\n".join(out)


def cmd_grounding_report(argv: list[str]) -> int:
    """``oracle grounding-report [--json] [--shadow PATH] [--labels PATH]``

    Reads the local-only shadow capture file (default: under ``profile_dir()``)
    plus an optional operator-label sidecar, computes the P3-T7 budgets, and
    prints the go/no-go recommendation. Read-only: it never mutates the capture
    file or any config.
    """
    import argparse

    from . import config

    ap = argparse.ArgumentParser(prog="oracle grounding-report")
    ap.add_argument("--json", action="store_true",
                    help="emit the raw metrics as JSON instead of a report")
    ap.add_argument("--shadow", help="path to grounding_shadow.jsonl "
                                     "(default: under profile_dir())")
    ap.add_argument("--labels", help="path to the operator label sidecar "
                                     "(default: grounding_shadow_labels.jsonl "
                                     "next to the shadow file)")
    ns = ap.parse_args(argv)

    from .agentloop.loop import SHADOW_FILENAME
    shadow_path = (Path(ns.shadow).expanduser() if ns.shadow
                   else config.profile_dir() / SHADOW_FILENAME)
    labels_path = (Path(ns.labels).expanduser() if ns.labels
                   else shadow_path.parent / _LABELS_FILENAME)

    shadow_rows = _read_jsonl(shadow_path)
    label_rows = _read_jsonl(labels_path)

    if not shadow_rows:
        print(
            f"oracle grounding-report: no shadow capture found at {shadow_path}.\n"
            f"  Enable capture with `chat.grounding_shadow: true` in config.json "
            f"and run local chat in OBSERVE mode to accumulate >= "
            f"{WINDOW_MIN_TURNS} turns across >= {WINDOW_MIN_DAYS} days.",
            file=sys.stderr,
        )
        return 1

    metrics = evaluate(shadow_rows, label_rows)
    if ns.json:
        print(json.dumps(metrics, indent=2, sort_keys=True))
    else:
        print(render(metrics))
    return 0
