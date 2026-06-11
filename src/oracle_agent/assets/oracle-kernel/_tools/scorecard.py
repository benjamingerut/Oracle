#!/usr/bin/env python3
"""scorecard.py -- the value scorecard: the oracle measures itself, by ledger.

Self-improvement is only real if it is measured. This module is the
deterministic read-side that rolls the captured signal ledgers into one dated
scorecard note per window under ``Meta.nosync/Value-Scorecards/`` -- every
number computed from ledger rows and cited by drop_id, never asserted from
memory. It is the builtin runner behind the ``value-scorecard`` loop (ACTIVE
at spawn) and the evidence base the ``architecture-retrospective`` and
``meta-health`` loops read.

KPIs computed per window (all from ledgers/notes alone):

  * answers      -- exit-code distribution of ``answer_event`` rows; the
                    grounded-rate (exit 0 share) is the master efficiency
                    signal: the whole knowledge pipeline exists to move
                    objects from 4 -> 2 -> 0.
  * value        -- net signed value (sign(polarity) * strength) and the
                    by-kind breakdown from ``value_event`` rows.
  * feedback     -- count + net signed from ``feedback_event`` rows.
  * failures     -- count, by-severity, and RECURRING failure_modes (a mode
                    seen twice in one window is an improvement that did not
                    close).
  * signal_latency -- median days from event capture to event consumption
                    (rows in ``event_consumption.jsonl``); plus the current
                    unconsumed backlog.
  * improvements -- current status counts plus verified-in-window throughput
                    (read from ``Meta.nosync/Improvements/`` via the sibling
                    ``improvements`` module when present).
  * admin_actions -- best-effort count of admin-driven ledger rows in the
                    window (export_event + autonomy_event), and the ratio per
                    verified improvement: "requiring less from the admin" as
                    a number, expected to trend down.
  * dream        -- dream-session count and ok-rate from
                    ``dream_session.jsonl`` (Phase D), when present.

Trend: each scorecard stores a ``composite`` (net value + 10 * grounded_rate -
severity-weighted failure mass). The next scorecard compares against it and
stamps ``trend: improving|flat|regressing``. ``latest_trend(root)`` exposes
that verdict so a regressing window can make the architecture-retrospective
loop due immediately (see ``loops.due``).

Window semantics: the window ends at the injected ``now`` and starts at the
previous scorecard's ``window_end`` (so windows tile with no gaps); the first
scorecard reaches back ``window_days`` (default 30). ``now`` is only read from
the wall clock at the CLI edge.

Stdlib only. Note writes go through ``safe_paths.contain`` +
``os.fdopen``-on-mkstemp (no-bypass-guard clean); all rows via ``ledger``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - exercised both ways across environments
    import safe_paths
    import ledger
    from oracle_yaml import safe_load, UnsupportedYAML
except Exception:  # pragma: no cover
    from . import safe_paths  # type: ignore
    from . import ledger  # type: ignore
    from .oracle_yaml import safe_load, UnsupportedYAML  # type: ignore


META_BASE = "Meta.nosync"
SCORECARDS_DIR = "Meta.nosync/Value-Scorecards"

FEEDBACK_LEDGER = "Meta.nosync/ledgers/feedback_event.jsonl"
VALUE_LEDGER = "Meta.nosync/ledgers/value_event.jsonl"
FAILURE_LEDGER = "Meta.nosync/ledgers/failure_event.jsonl"
ANSWER_LEDGER = "Meta.nosync/ledgers/answer_event.jsonl"
CONSUMPTION_LEDGER = "Meta.nosync/ledgers/event_consumption.jsonl"
EXPORT_LEDGER = "Meta.nosync/ledgers/export_event.jsonl"
AUTONOMY_LEDGER = "Meta.nosync/ledgers/autonomy_event.jsonl"
DREAM_LEDGER = "Meta.nosync/ledgers/dream_session.jsonl"
# Retrieval telemetry (P8-T7): monthly-rotated, so we glob rather than name one
# file. The query text is never present -- only a salted query_hmac + metadata.
LEDGERS_DIR = "Meta.nosync/ledgers"
RETRIEVAL_LEDGER_GLOB = "retrieval_event-*.jsonl"
SOURCES_DIR = "Memory.nosync/Sources"

DEFAULT_WINDOW_DAYS = 30
# A composite has to move by more than this to count as a real direction.
TREND_EPSILON = 0.5
_SEVERITY_WEIGHT = {"low": 1.0, "medium": 2.0, "high": 3.0, "critical": 5.0}
_CITE_CAP = 20  # max drop_ids cited per section (keeps notes bounded)


# --------------------------------------------------------------------------- #
# time helpers (wall clock only at the CLI edge)
# --------------------------------------------------------------------------- #
def _now_default() -> datetime:
    return datetime.now()


def _today_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip().replace("Z", "").replace("z", "")
    if not s or s.lower() in ("null", "none", "~"):
        return None
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _in_window(row: dict, start: datetime, end: datetime) -> bool:
    ts = parse_dt(row.get("ts"))
    return ts is not None and start < ts <= end


def _sign(value: Any) -> int:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    return 1 if f > 0 else (-1 if f < 0 else 0)


def _strength(value: Any, default: float = 1.0) -> float:
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return default


def _ids(rows: list[dict]) -> list[str]:
    out = [str(r.get("drop_id", "")).strip() for r in rows]
    return [i for i in out if i][:_CITE_CAP]


def _load(root: Path, rel: str) -> list[dict]:
    rows, _warnings = ledger.load(Path(root) / rel)
    return rows


# --------------------------------------------------------------------------- #
# note (de)serialization -- shared block-style frontmatter shape
# --------------------------------------------------------------------------- #
def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        fm = safe_load(fm_text) if fm_text.strip() else {}
    except UnsupportedYAML as exc:
        raise ValueError(f"scorecard note frontmatter not in safe subset: {exc}")
    if not isinstance(fm, dict):
        raise ValueError("scorecard note frontmatter is not a mapping")
    return fm, body.lstrip("\n")


def _scalar_yaml(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    needs_quote = (
        s == ""
        or s.strip() != s
        or any(c in s for c in (":", "#", "'", '"', "[", "]", "{", "}", "&", "*", "!", "|", ">"))
        or s.lower() in ("true", "false", "null", "yes", "no")
    )
    if needs_quote:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _render_yaml_value(value: Any, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        out: list[str] = []
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                out.append(f"{pad}{key}:")
                out.extend(_render_yaml_value(child, indent + 2))
            else:
                out.append(f"{pad}{key}: {_scalar_yaml(child)}")
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, (dict, list)):
                out.append(f"{pad}-")
                out.extend(_render_yaml_value(item, indent + 2))
            else:
                out.append(f"{pad}- {_scalar_yaml(item)}")
        return out
    return [f"{pad}{_scalar_yaml(value)}"]


def _render_note(fm: dict, body: str) -> str:
    return "---\n" + "\n".join(_render_yaml_value(fm, 0)) + "\n---\n\n" + (body or "") + "\n"


def _write_contained(dst: Path, text: str) -> None:
    """Atomic write to a contained path (mkstemp fd + os.replace)."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dst.name + ".", suffix=".tmp", dir=str(dst.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:  # contained dst (safe_paths.contain)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(dst))  # safe_paths-internal: atomic swap, dst from safe_paths.contain()
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# prior scorecards
# --------------------------------------------------------------------------- #
def load_scorecards(root: Path) -> list[dict]:
    """All scorecard note frontmatters, oldest first by window_end."""
    folder = Path(root) / SCORECARDS_DIR
    out: list[dict] = []
    if not folder.is_dir():
        return out
    for p in sorted(folder.glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            fm, _body = _split_frontmatter(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if str(fm.get("type", "")) != "value_scorecard":
            continue
        fm["_path"] = str(p)
        out.append(fm)
    out.sort(key=lambda f: str(f.get("window_end", "")))
    return out


def latest_scorecard(root: Path) -> Optional[dict]:
    cards = load_scorecards(root)
    return cards[-1] if cards else None


def latest_trend(root: Path) -> str:
    """The most recent scorecard's trend verdict ('' when none exists)."""
    card = latest_scorecard(root)
    return str(card.get("trend", "")) if card else ""


# --------------------------------------------------------------------------- #
# KPI computation (pure read-side)
# --------------------------------------------------------------------------- #
def _kpi_answers(root: Path, start: datetime, end: datetime) -> dict:
    rows = [r for r in _load(root, ANSWER_LEDGER) if _in_window(r, start, end)]
    by_exit: dict[str, int] = {"0": 0, "2": 0, "3": 0, "4": 0}
    for r in rows:
        code = str(r.get("exit_code", "")).strip()
        if code in by_exit:
            by_exit[code] += 1
    total = sum(by_exit.values())
    grounded_rate = round(by_exit["0"] / total, 4) if total else None
    return {
        "count": total,
        "by_exit": by_exit,
        "grounded_rate": grounded_rate,
        "evidence": _ids(rows),
    }


def _kpi_value(root: Path, start: datetime, end: datetime) -> dict:
    rows = [r for r in _load(root, VALUE_LEDGER) if _in_window(r, start, end)]
    by_kind: dict[str, float] = {}
    net = 0.0
    for r in rows:
        signed = _sign(r.get("polarity", 0)) * _strength(r.get("strength", 1.0))
        net += signed
        vk = str(r.get("value_kind", "other")).strip().lower() or "other"
        by_kind[vk] = round(by_kind.get(vk, 0.0) + signed, 4)
    return {"count": len(rows), "net_signed": round(net, 4), "by_kind": by_kind, "evidence": _ids(rows)}


def _kpi_feedback(root: Path, start: datetime, end: datetime) -> dict:
    rows = [r for r in _load(root, FEEDBACK_LEDGER) if _in_window(r, start, end)]
    net = sum(_sign(r.get("polarity", 0)) * _strength(r.get("strength", 1.0)) for r in rows)
    return {"count": len(rows), "net_signed": round(net, 4), "evidence": _ids(rows)}


def _kpi_failures(root: Path, start: datetime, end: datetime) -> dict:
    rows = [r for r in _load(root, FAILURE_LEDGER) if _in_window(r, start, end)]
    by_severity: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    weighted = 0.0
    for r in rows:
        sev = str(r.get("severity", "medium")).strip().lower() or "medium"
        by_severity[sev] = by_severity.get(sev, 0) + 1
        weighted += _SEVERITY_WEIGHT.get(sev, 2.0)
        mode = str(r.get("failure_mode", "unspecified")).strip() or "unspecified"
        by_mode[mode] = by_mode.get(mode, 0) + 1
    recurring = sorted(m for m, n in by_mode.items() if n >= 2 and m != "unspecified")
    return {
        "count": len(rows),
        "by_severity": by_severity,
        "weighted": round(weighted, 4),
        "recurring_modes": recurring,
        "evidence": _ids(rows),
    }


def _event_ts_index(root: Path) -> dict[str, datetime]:
    """drop_id -> capture ts across the three captured-event ledgers."""
    idx: dict[str, datetime] = {}
    for rel in (FEEDBACK_LEDGER, VALUE_LEDGER, FAILURE_LEDGER):
        for r in _load(root, rel):
            eid = str(r.get("drop_id", "")).strip()
            ts = parse_dt(r.get("ts"))
            if eid and ts is not None:
                idx[eid] = ts
    return idx


def _kpi_signal_latency(root: Path, start: datetime, end: datetime) -> dict:
    consumed = [r for r in _load(root, CONSUMPTION_LEDGER) if _in_window(r, start, end)]
    captured = _event_ts_index(root)
    latencies: list[float] = []
    for r in consumed:
        eid = str(r.get("event_drop_id", "")).strip()
        cts = parse_dt(r.get("ts"))
        ets = captured.get(eid)
        if cts is not None and ets is not None and cts >= ets:
            latencies.append((cts - ets).total_seconds() / 86400.0)
    consumed_ids = {str(r.get("event_drop_id", "")).strip() for r in _load(root, CONSUMPTION_LEDGER)}
    backlog = [eid for eid in captured if eid not in consumed_ids]
    return {
        "consumed_in_window": len(latencies),
        "median_days": round(statistics.median(latencies), 2) if latencies else None,
        "unconsumed_backlog": len(backlog),
    }


def _kpi_improvements(root: Path, start: datetime, end: datetime) -> dict:
    try:
        import improvements as _imp  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import improvements as _imp  # type: ignore
        except Exception:
            return {"available": False}
    try:
        return _imp.kpis(root, start=start, end=end)
    except Exception:
        return {"available": False}


def _kpi_admin_actions(root: Path, start: datetime, end: datetime, verified: Optional[int]) -> dict:
    n = 0
    for rel in (EXPORT_LEDGER, AUTONOMY_LEDGER):
        n += sum(1 for r in _load(root, rel) if _in_window(r, start, end))
    per_verified = None
    if verified:
        per_verified = round(n / verified, 2)
    return {"count": n, "per_verified_improvement": per_verified}


def _kpi_dream(root: Path, start: datetime, end: datetime) -> dict:
    rows = [r for r in _load(root, DREAM_LEDGER) if _in_window(r, start, end)]
    ok = sum(1 for r in rows if str(r.get("result", "")) == "ok")
    return {"sessions": len(rows), "ok": ok}


def _load_retrieval_rows(root: Path) -> list[dict]:
    """Load every monthly-rotated retrieval_event ledger, concatenated.

    Each month is a separate file with its own hash chain; we only need the
    rows. ``ledger.load`` is corruption-tolerant and never raises.
    """
    folder = Path(root) / LEDGERS_DIR
    rows: list[dict] = []
    if not folder.is_dir():
        return rows
    for p in sorted(folder.glob(RETRIEVAL_LEDGER_GLOB)):
        these, _w = ledger.load(p)
        rows.extend(these)
    return rows


def _answer_cited_source_ids(root: Path, start: datetime, end: datetime) -> set[str]:
    """All source_ids cited by an exit-0 ``answer_event`` in the window.

    ``answer_event`` rows gained an additive ``source_ids`` field (the cited
    sources); a legacy row without it contributes nothing.
    """
    cited: set[str] = set()
    for r in _load(root, ANSWER_LEDGER):
        if not _in_window(r, start, end):
            continue
        if str(r.get("exit_code", "")).strip() != "0":
            continue
        for sid in r.get("source_ids") or []:
            s = str(sid).strip()
            if s:
                cited.add(s)
    return cited


def _source_ingest_dates(root: Path) -> dict[str, datetime]:
    """Map source_id -> earliest ingest/as_of/created date from Source notes.

    Used for time_to_first_grounded_answer. The source_id is the note's
    ``source_id``/``id``; the date is the first of created/ingested/as_of that
    parses. Best-effort: a malformed note is skipped, never fatal.
    """
    out: dict[str, datetime] = {}
    folder = Path(root) / SOURCES_DIR
    if not folder.is_dir():
        return out
    for p in sorted(folder.glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            fm, _body = _split_frontmatter(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        sid = ""
        for key in ("source_id", "id"):
            v = str(fm.get(key, "") or "").strip()
            if v:
                sid = v
                break
        if not sid:
            continue
        dt = None
        for key in ("ingested", "created", "as_of"):
            dt = parse_dt(fm.get(key))
            if dt is not None:
                break
        if dt is not None:
            out[sid] = dt
    return out


def _kpi_retrieval(root: Path, start: datetime, end: datetime) -> dict:
    """The ``retrieval`` KPI section (P8-T7), all metadata-only.

      * searches / non_empty_rate / hybrid_share / vector_coverage --
        straight off the retrieval_event rows in the window.
      * retrieval_hit_rate -- share of window searches whose top_source_ids
        intersect the source_ids cited by an exit-0 answer_event in the window
        (a proxy: did the searches surface what the grounded answers cited?).
      * time_to_first_grounded_answer -- median days from a source's ingest to
        the first exit-0 answer citing it (honestly derivable from
        answer_event.source_ids timestamps + Source-note ingest dates).
    """
    rows = [r for r in _load_retrieval_rows(root) if _in_window(r, start, end)]
    searches = len(rows)
    non_empty = sum(1 for r in rows if int(r.get("result_count", 0) or 0) > 0)
    hybrid = sum(1 for r in rows if bool(r.get("hybrid")))
    # Latest non-null coverage seen in the window (coverage drifts as backfill
    # progresses; the most recent reading is the current state).
    coverage = None
    for r in rows:
        cov = r.get("vector_coverage")
        if cov is not None:
            coverage = cov

    cited = _answer_cited_source_ids(root, start, end)
    hits = 0
    for r in rows:
        top = {str(s).strip() for s in (r.get("top_source_ids") or []) if str(s).strip()}
        if top and (top & cited):
            hits += 1
    hit_rate = round(hits / searches, 4) if searches else None

    ttfga = _time_to_first_grounded_answer(root, start, end)

    return {
        "searches": searches,
        "non_empty_rate": round(non_empty / searches, 4) if searches else None,
        "hybrid_share": round(hybrid / searches, 4) if searches else None,
        "vector_coverage": coverage,
        "retrieval_hit_rate": hit_rate,
        "time_to_first_grounded_answer": ttfga,
    }


def _time_to_first_grounded_answer(root: Path, start: datetime, end: datetime):
    """Median days from a source's ingest to the FIRST exit-0 answer citing it.

    Considers exit-0 answer_event rows in the window; for each cited source_id
    with a known ingest date, the latency is (first citing answer ts - ingest).
    Returns the median across sources first-grounded in the window, or None.
    """
    ingest = _source_ingest_dates(root)
    if not ingest:
        return None
    first_cite: dict[str, datetime] = {}
    for r in _load(root, ANSWER_LEDGER):
        if not _in_window(r, start, end):
            continue
        if str(r.get("exit_code", "")).strip() != "0":
            continue
        ts = parse_dt(r.get("ts"))
        if ts is None:
            continue
        for sid in r.get("source_ids") or []:
            s = str(sid).strip()
            if not s:
                continue
            if s not in first_cite or ts < first_cite[s]:
                first_cite[s] = ts
    latencies: list[float] = []
    for sid, cite_ts in first_cite.items():
        ing = ingest.get(sid)
        if ing is not None and cite_ts >= ing:
            latencies.append((cite_ts - ing).total_seconds() / 86400.0)
    if not latencies:
        return None
    return round(statistics.median(latencies), 2)


def compute_kpis(root: Path, *, start: datetime, end: datetime) -> dict:
    """All KPI sections for (start, end]. Read-only and deterministic."""
    root = Path(root)
    answers = _kpi_answers(root, start, end)
    value = _kpi_value(root, start, end)
    feedback = _kpi_feedback(root, start, end)
    failures = _kpi_failures(root, start, end)
    improvements = _kpi_improvements(root, start, end)
    verified = improvements.get("verified_in_window") if isinstance(improvements, dict) else None
    return {
        "answers": answers,
        "value": value,
        "feedback": feedback,
        "failures": failures,
        "signal_latency": _kpi_signal_latency(root, start, end),
        "improvements": improvements,
        "admin_actions": _kpi_admin_actions(root, start, end, verified),
        "dream": _kpi_dream(root, start, end),
        "retrieval": _kpi_retrieval(root, start, end),
    }


def composite_score(kpis: dict) -> float:
    """One comparable number per window: net value + grounded-rate bonus -
    severity-weighted failure mass. Used ONLY for trend direction."""
    value_net = float(kpis.get("value", {}).get("net_signed", 0.0) or 0.0)
    gr = kpis.get("answers", {}).get("grounded_rate")
    grounded_bonus = 10.0 * float(gr) if gr is not None else 0.0
    failure_mass = float(kpis.get("failures", {}).get("weighted", 0.0) or 0.0)
    return round(value_net + grounded_bonus - failure_mass, 4)


def trend_verdict(composite: float, prior_composite: Optional[float]) -> str:
    if prior_composite is None:
        return "baseline"
    if composite > prior_composite + TREND_EPSILON:
        return "improving"
    if composite < prior_composite - TREND_EPSILON:
        return "regressing"
    return "flat"


# --------------------------------------------------------------------------- #
# scorecard generation (the builtin loop runner's write side)
# --------------------------------------------------------------------------- #
def _carry_forward(kpis: dict) -> str:
    """The one concrete improvement to carry forward, picked deterministically."""
    failures = kpis.get("failures", {})
    recurring = failures.get("recurring_modes") or []
    if recurring:
        return (
            f"Close the recurring failure mode {recurring[0]!r}: open/verify an "
            "Improvement with a machine-checkable expected_signal."
        )
    backlog = kpis.get("signal_latency", {}).get("unconsumed_backlog", 0)
    if backlog:
        return f"Drain the {backlog} unconsumed event(s): run the due learning loops."
    gr = kpis.get("answers", {}).get("grounded_rate")
    if gr is not None and gr < 1.0:
        return "Raise the grounded-rate: promote ready truth-map rows (./oracle review)."
    return "Keep capturing feedback/value/failure events -- the scorecard is only as honest as its inputs."


def generate(root: Path, *, now: Optional[datetime] = None,
             window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Compute this window's scorecard and write the dated note. Returns a
    summary dict {note_path, window_start, window_end, trend, composite, kpis}."""
    root = Path(root)
    now = now or _now_default()
    prior = latest_scorecard(root)
    start = None
    prior_composite = None
    if prior is not None:
        start = parse_dt(prior.get("window_end"))
        try:
            prior_composite = float(prior.get("composite"))
        except (TypeError, ValueError):
            prior_composite = None
    if start is None:
        start = now - timedelta(days=window_days)

    kpis = compute_kpis(root, start=start, end=now)
    composite = composite_score(kpis)
    trend = trend_verdict(composite, prior_composite)

    date = _today_str(now)
    fm: dict = {
        "id": f"SC-{date}",
        "type": "value_scorecard",
        "title": f"Value scorecard {date}",
        "created": date,
        "updated": date,
        "sensitivity": "internal",
        "status": "active",
        "tags": ["meta", "value_scorecard", "self-improvement"],
        "window_start": _today_str(start),
        "window_end": date,
        "trend": trend,
        "composite": composite,
        "kpis": _fm_kpis(kpis),
    }
    body = _render_body(fm, kpis, prior)
    dst = safe_paths.contain(root, f"Value-Scorecards/scorecard-{date}.md", base=META_BASE)
    _write_contained(dst, _render_note(fm, body))
    return {
        "note_path": str(dst),
        "window_start": fm["window_start"],
        "window_end": fm["window_end"],
        "trend": trend,
        "composite": composite,
        "kpis": kpis,
    }


def _fm_kpis(kpis: dict) -> dict:
    """The frontmatter (machine) projection of the KPIs: numbers only, no
    evidence lists (those are cited in the body)."""
    answers = kpis.get("answers", {})
    value = kpis.get("value", {})
    failures = kpis.get("failures", {})
    latency = kpis.get("signal_latency", {})
    improvements = kpis.get("improvements", {})
    admin = kpis.get("admin_actions", {})
    out = {
        "answers_count": answers.get("count", 0),
        "grounded_rate": answers.get("grounded_rate"),
        "value_net": value.get("net_signed", 0.0),
        "value_count": value.get("count", 0),
        "feedback_net": kpis.get("feedback", {}).get("net_signed", 0.0),
        "failure_count": failures.get("count", 0),
        "failure_weighted": failures.get("weighted", 0.0),
        "recurring_failure_modes": len(failures.get("recurring_modes") or []),
        "signal_latency_median_days": latency.get("median_days"),
        "unconsumed_backlog": latency.get("unconsumed_backlog", 0),
        "admin_actions": admin.get("count", 0),
        "dream_sessions": kpis.get("dream", {}).get("sessions", 0),
    }
    if isinstance(improvements, dict) and improvements.get("available", True):
        out["improvements_proposed_open"] = improvements.get("proposed_open", 0)
        out["improvements_verified_in_window"] = improvements.get("verified_in_window", 0)
        out["improvements_regressed_in_window"] = improvements.get("regressed_in_window", 0)
    retrieval = kpis.get("retrieval", {})
    if isinstance(retrieval, dict):
        out["retrieval_searches"] = retrieval.get("searches", 0)
        out["retrieval_hit_rate"] = retrieval.get("retrieval_hit_rate")
        out["retrieval_hybrid_share"] = retrieval.get("hybrid_share")
        out["retrieval_vector_coverage"] = retrieval.get("vector_coverage")
        out["time_to_first_grounded_answer"] = retrieval.get(
            "time_to_first_grounded_answer"
        )
    return out


def _render_body(fm: dict, kpis: dict, prior: Optional[dict]) -> str:
    answers = kpis["answers"]
    value = kpis["value"]
    feedback = kpis["feedback"]
    failures = kpis["failures"]
    latency = kpis["signal_latency"]
    lines = [
        f"# {fm['title']}",
        "",
        f"Window: {fm['window_start']} -> {fm['window_end']}. "
        f"Trend vs prior: **{fm['trend']}** (composite {fm['composite']}"
        + (f", prior {prior.get('composite')}" if prior else "")
        + ").",
        "",
        "## Answers (grounded-rate is the master efficiency signal)",
        "",
        f"- {answers['count']} preflighted answer(s); exits {answers['by_exit']}; "
        f"grounded-rate {answers['grounded_rate']}",
        f"- evidence: {', '.join(answers['evidence']) or '(none)'}",
        "",
        "## Value and feedback",
        "",
        f"- value: {value['count']} event(s), net {value['net_signed']}, by kind {value['by_kind']}",
        f"- value evidence: {', '.join(value['evidence']) or '(none)'}",
        f"- feedback: {feedback['count']} event(s), net {feedback['net_signed']}",
        f"- feedback evidence: {', '.join(feedback['evidence']) or '(none)'}",
        "",
        "## Failures",
        "",
        f"- {failures['count']} failure(s); by severity {failures['by_severity']}; "
        f"weighted mass {failures['weighted']}",
        f"- recurring modes (an improvement that did not close): "
        f"{', '.join(failures['recurring_modes']) or '(none)'}",
        f"- evidence: {', '.join(failures['evidence']) or '(none)'}",
        "",
        "## Loop closure",
        "",
        f"- signal latency: median {latency['median_days']} day(s) capture->consumption "
        f"({latency['consumed_in_window']} consumed this window); "
        f"unconsumed backlog now: {latency['unconsumed_backlog']}",
        f"- improvements: {json.dumps(kpis['improvements'], default=str)}",
        f"- admin actions: {json.dumps(kpis['admin_actions'], default=str)}",
        f"- dream sessions: {json.dumps(kpis['dream'], default=str)}",
        f"- retrieval: {json.dumps(kpis.get('retrieval', {}), default=str)}",
        "",
        "## Carry forward",
        "",
        f"- {_carry_forward(kpis)}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# builtin loop runner
# --------------------------------------------------------------------------- #
def run_value_scorecard_loop(root, loop=None, *, now: Optional[datetime] = None) -> dict:
    """Builtin runner for the ``value-scorecard`` loop (deterministic)."""
    try:
        result = generate(root, now=now)
    except Exception as exc:
        return {
            "status": "fail",
            "performed": False,
            "kind": "builtin:value-scorecard",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "status": "ok",
        "performed": True,
        "kind": "builtin:value-scorecard",
        "health_signal": "healthy" if result["trend"] != "regressing" else "degraded",
        "trend": result["trend"],
        "note_path": result["note_path"],
        "summary": (
            f"scorecard {result['window_start']} -> {result['window_end']}: "
            f"trend {result['trend']} (composite {result['composite']})"
        ),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scorecard", description="value scorecard: KPIs from ledgers")
    parser.add_argument("--root", default=".", help="oracle root (default: .)")
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_gen = sub.add_parser("gen", help="compute + write this window's scorecard (default)")
    p_gen.add_argument("--now", help="ISO datetime override (deterministic windows)")
    p_gen.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    p_gen.add_argument("--json", action="store_true")

    p_kpi = sub.add_parser("kpis", help="compute KPIs read-only (no note written)")
    p_kpi.add_argument("--now", help="ISO datetime override")
    p_kpi.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)

    sub.add_parser("trend", help="print the latest scorecard's trend verdict")

    args = parser.parse_args(argv)
    root = Path(args.root)
    cmd = args.cmd or "gen"

    try:
        if cmd == "gen":
            now = parse_dt(args.now) if args.now else None
            res = generate(root, now=now, window_days=args.window_days)
            if args.json:
                print(json.dumps(res, indent=2, default=str))
            else:
                print(
                    f"scorecard: {res['window_start']} -> {res['window_end']} "
                    f"trend={res['trend']} composite={res['composite']} -> {res['note_path']}"
                )
            return 0
        if cmd == "kpis":
            now = parse_dt(args.now) if args.now else _now_default()
            start = now - timedelta(days=args.window_days)
            print(json.dumps(compute_kpis(root, start=start, end=now), indent=2, default=str))
            return 0
        if cmd == "trend":
            print(latest_trend(root) or "(no scorecard yet)")
            return 0
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"scorecard: {exc}\n")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
