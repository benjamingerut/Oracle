#!/usr/bin/env python3
"""capture.py -- the value / feedback / failure capture path.

This is the writer side of the oracle's self-improvement loop. Three event
kinds are captured, each as BOTH a durable ledger row (machine-consumable by the
loops) AND a schema-valid Meta note (human-readable, linkable, supersedable):

  * ``feedback_event`` -- a user's reaction to the oracle's output (praise,
    correction, a steer). Polarity + strength + an excerpt + the target it
    refers to. Consumed by the active ``user-feedback-learning`` and
    ``skill-repository-learning`` loops.
  * ``value_event`` -- an observed instance of the oracle creating (or
    destroying) value: it helped a decision, surfaced a risk, found an
    opportunity -- or misled. Polarity + strength + a value_kind + the target.
    Consumed by the active self-improvement loops AND by
    ``recommendation.adjudicate`` (which reads ``value_event.jsonl`` rows by
    ``target``), so the ledger field names here are load-bearing: every
    value_event row carries at least ``target``, ``polarity`` (signed int) and
    ``strength`` (float).
  * ``failure_event`` -- something the oracle got wrong or could not do: a
    refusal it should not have made, a stale answer, a crash, a missed
    contradiction. Severity + a failure_mode + the target. Consumed by the
    active self-improvement loops.

Why both a note and a ledger row? The ledger is the append-only, race-safe,
loop-queryable spine (mirroring how recommendation.py reads value_events). The
note is the rich, interlinked memory artifact the agent reasons over and can
supersede. The note's frontmatter is validated against the common
note_frontmatter schema (when present) so a malformed capture never silently
enters Memory/Meta.

Polarity convention (shared with recommendation.py's adjudicator):
    +1 positive / helped, 0 neutral, -1 negative / harmed.
Strength is a non-negative magnitude (default 1.0). The signed contribution a
value_event makes to a scorecard is ``sign(polarity) * strength``.

Public API (binding):
    feedback_event(root, *, target, polarity, strength=1.0, excerpt=None,
                   actor=None, sensitivity='internal', tags=None, body=None,
                   now=None) -> dict
    value_event(root, *, target, polarity, strength=1.0, value_kind=None,
                excerpt=None, actor=None, sensitivity='internal', tags=None,
                body=None, now=None) -> dict
    failure_event(root, *, target, severity='medium', failure_mode=None,
                  excerpt=None, actor=None, sensitivity='internal', tags=None,
                  body=None, now=None, polarity=-1, strength=1.0) -> dict
    scorecard(root) -> dict     # roll-up the captured signals (read-only)
    main(argv) -> int           # CLI: feedback|value|failure|scorecard

Each writer returns {drop_id, note_path, ledger}. All note writes route through
``safe_paths.contain`` (Meta base); ledger rows via ``ledger.append``. The note
is written with ``os.fdopen`` on a mkstemp fd (never ``open(<var>,'w')``), so the
no-bypass guard passes. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Floor imports (bare-module first, package fallback).
# --------------------------------------------------------------------------- #
try:  # pragma: no cover
    import safe_paths
    import ledger
    from oracle_yaml import safe_load, UnsupportedYAML
except Exception:  # pragma: no cover
    from . import safe_paths  # type: ignore
    from . import ledger  # type: ignore
    from .oracle_yaml import safe_load, UnsupportedYAML  # type: ignore

try:  # pragma: no cover
    import schema_check  # type: ignore
except Exception:  # pragma: no cover
    try:
        from . import schema_check  # type: ignore
    except Exception:  # pragma: no cover
        schema_check = None  # type: ignore


META_BASE = "Meta.nosync"
SELF_IMPROVEMENT_CONSUMERS = [
    "user-feedback-learning",
    "skill-repository-learning",
]

# (event kind) -> (Meta folder, note type, ledger file, id prefix)
_EVENT_SPECS = {
    "feedback": {
        "folder": "Feedback",
        "type": "feedback_event",
        "ledger": "Meta.nosync/ledgers/feedback_event.jsonl",
        "prefix": "FBK",
        "consumed_by": SELF_IMPROVEMENT_CONSUMERS,
    },
    "value": {
        "folder": "Value-Events",
        "type": "value_event",
        "ledger": "Meta.nosync/ledgers/value_event.jsonl",
        "prefix": "VAL",
        "consumed_by": SELF_IMPROVEMENT_CONSUMERS,
    },
    "failure": {
        "folder": "Failure-Events",
        "type": "failure_event",
        "ledger": "Meta.nosync/ledgers/failure_event.jsonl",
        "prefix": "FAIL",
        "consumed_by": SELF_IMPROVEMENT_CONSUMERS,
    },
}

SEVERITIES = ("low", "medium", "high", "critical")
VALUE_KINDS = (
    "understand",
    "decide",
    "act",
    "avoid_risk",
    "discover_opportunity",
    "other",
)
# severity -> the implied magnitude a failure contributes (negative).
_SEVERITY_STRENGTH = {"low": 1.0, "medium": 2.0, "high": 3.0, "critical": 5.0}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now_default() -> datetime:
    return datetime.now()


def _now_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _today_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _norm_polarity(value: Any) -> int:
    """Normalize a polarity to a signed int in {-1, 0, 1}."""
    if isinstance(value, bool):
        return 1 if value else -1
    if isinstance(value, (int, float)):
        if value > 0:
            return 1
        if value < 0:
            return -1
        return 0
    s = str(value).strip().lower()
    return {
        "positive": 1, "pos": 1, "up": 1, "+": 1, "+1": 1, "good": 1, "helped": 1,
        "negative": -1, "neg": -1, "down": -1, "-": -1, "-1": -1, "bad": -1, "harmed": -1,
        "neutral": 0, "0": 0, "mixed": 0,
    }.get(s, 0)


def _norm_strength(value: Any, default: float = 1.0) -> float:
    try:
        if isinstance(value, bool):
            return default
        f = float(value)
    except (TypeError, ValueError):
        return default
    return abs(f)


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if x is not None]
    return [v]


# --------------------------------------------------------------------------- #
# Note (de)serialization -- block-style frontmatter (strict oracle_yaml subset)
# --------------------------------------------------------------------------- #
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


def _render_frontmatter(fm: dict) -> str:
    out: list[str] = []
    for key, value in fm.items():
        if isinstance(value, list):
            if not value:
                out.append(f"{key}:")
            else:
                out.append(f"{key}:")
                for item in value:
                    out.append(f"  - {_scalar_yaml(item)}")
        elif isinstance(value, dict):
            if not value:
                out.append(f"{key}:")
            else:
                out.append(f"{key}:")
                for sk, sv in value.items():
                    if isinstance(sv, list):
                        if not sv:
                            out.append(f"  {sk}:")
                        else:
                            out.append(f"  {sk}:")
                            for item in sv:
                                out.append(f"    - {_scalar_yaml(item)}")
                    else:
                        out.append(f"  {sk}: {_scalar_yaml(sv)}")
        else:
            out.append(f"{key}: {_scalar_yaml(value)}")
    return "\n".join(out)


def _render_note(fm: dict, body: str) -> str:
    return "---\n" + _render_frontmatter(fm) + "\n---\n\n" + (body or "") + "\n"


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
        raise ValueError(f"capture note frontmatter not in safe subset: {exc}")
    if not isinstance(fm, dict):
        raise ValueError("capture note frontmatter is not a mapping")
    return fm, body.lstrip("\n")


def _write_contained(dst: Path, text: str) -> None:
    """Atomic write to a contained path (mkstemp fd + os.replace; no open(var,'w'))."""
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
# Schema (the common note frontmatter, validated when the schema file is present)
# --------------------------------------------------------------------------- #
def _builtin_common_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "id",
            "type",
            "title",
            "created",
            "updated",
            "sensitivity",
            "status",
            "tags",
        ],
        "properties": {
            "sensitivity": {
                "type": "string",
                "enum": ["public", "internal", "confidential", "restricted", "secret"],
            },
            "tags": {"type": "array"},
        },
    }


def _load_common_schema(root: Path) -> dict:
    schema_path = Path(root) / "_tools" / "schemas" / "note_frontmatter.schema.json"
    if schema_path.exists():
        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return _builtin_common_schema()


def _validate_note(root: Path, fm: dict) -> list[str]:
    schema = _load_common_schema(root)
    if schema_check is not None:
        return schema_check.validate(fm, schema)
    errs = []
    for req in schema.get("required", []):
        if fm.get(req) in (None, ""):
            errs.append(f"missing required property {req!r}")
    return errs


# --------------------------------------------------------------------------- #
# Core writer
# --------------------------------------------------------------------------- #
def _ledger_path(root: Path, kind: str) -> Path:
    return Path(root) / _EVENT_SPECS[kind]["ledger"]


def _write_event(
    root: Path,
    kind: str,
    *,
    target: str,
    fm_extra: dict,
    ledger_extra: dict,
    title: str,
    sensitivity: str,
    actor: Optional[str],
    role: str,
    tags: Optional[list],
    body: Optional[str],
    now: datetime,
) -> dict:
    """Shared implementation: append a ledger row, then write a schema-valid note.

    Ordering: the ledger row (race-safe, loop-consumed) is appended FIRST and is
    the durable record of the event. The note is then written and back-links the
    ledger drop_id, so a note can always be traced to its ledger row. If the note
    write fails the event is still durably captured in the ledger.
    """
    root = Path(root)
    spec = _EVENT_SPECS[kind]
    target = str(target).strip()
    if not target:
        raise ValueError(f"{kind}_event: target is required")
    sensitivity = str(sensitivity or "internal").strip().lower()

    # 1) durable, loop-consumable ledger row. drop_id minted under one lock.
    led = _ledger_path(root, kind)
    row = {
        "ts": _now_iso(now),
        "kind": spec["type"],
        "target": target,
        "actor": actor or "",
        "role": str(role or "unknown"),
        "consumed_by": list(spec["consumed_by"]),
    }
    row.update(ledger_extra)
    drop_id = ledger.append(led, row, id_prefix=spec["prefix"])

    # 2) schema-valid Meta note that back-links the ledger row.
    slug = safe_paths.safe_slug(f"{kind}-{target}-{drop_id}")
    base_tags = ["meta", spec["type"], "self-improvement"]
    if tags:
        for t in tags:
            if t not in base_tags:
                base_tags.append(str(t))
    fm: dict = {
        "id": drop_id,
        "type": spec["type"],
        "title": title,
        "created": _today_str(now),
        "updated": _today_str(now),
        "sensitivity": sensitivity,
        "status": "captured",
        "tags": base_tags,
        "target": target,
        "drop_id": drop_id,
        "consumed_by": list(spec["consumed_by"]),
    }
    if actor:
        fm["actor"] = actor
    fm["role"] = str(role or "unknown")
    fm.update(fm_extra)

    errors = _validate_note(root, fm)
    if errors:
        raise ValueError(f"{kind}_event note invalid: " + "; ".join(errors))

    filename = f"{safe_paths.today()}_{slug}.md"
    dst = safe_paths.contain(
        root,
        f"{spec['folder']}/{filename}",
        base=META_BASE,
    )
    note_body = body or _default_body(kind, fm)
    _write_contained(dst, _render_note(fm, note_body))

    return {
        "drop_id": drop_id,
        "kind": spec["type"],
        "target": target,
        "note_path": str(dst),
        "ledger": str(led),
        "consumed_by": list(spec["consumed_by"]),
    }


def _default_body(kind: str, fm: dict) -> str:
    spec = _EVENT_SPECS[kind]
    consumers = ", ".join(str(x) for x in spec["consumed_by"])
    lines = [f"# {fm.get('title', spec['type'])}", ""]
    lines.append(f"- **kind**: {spec['type']}")
    lines.append(f"- **target**: {fm.get('target', '')}")
    if "polarity" in fm:
        lines.append(f"- **polarity**: {fm.get('polarity')}")
    if "strength" in fm:
        lines.append(f"- **strength**: {fm.get('strength')}")
    if "value_kind" in fm:
        lines.append(f"- **value_kind**: {fm.get('value_kind')}")
    if "severity" in fm:
        lines.append(f"- **severity**: {fm.get('severity')}")
    if "failure_mode" in fm:
        lines.append(f"- **failure_mode**: {fm.get('failure_mode')}")
    lines.append(f"- **consumed_by**: {consumers}")
    lines.append("")
    excerpt = fm.get("excerpt")
    if excerpt:
        lines.append("## Excerpt")
        lines.append("")
        lines.append(f"> {excerpt}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Public writers
# --------------------------------------------------------------------------- #
def feedback_event(
    root: Path,
    *,
    target: str,
    polarity: Any,
    strength: Any = 1.0,
    excerpt: Optional[str] = None,
    actor: Optional[str] = None,
    role: str = "unknown",
    sensitivity: str = "internal",
    tags: Optional[list] = None,
    body: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Capture a user's feedback on the oracle's output.

    ``polarity`` is normalized to {-1,0,1}; ``strength`` to a non-negative
    magnitude. The ledger row carries target/polarity/strength/excerpt so the
    user-feedback-learning loop can roll it up.
    """
    now = now or _now_default()
    pol = _norm_polarity(polarity)
    strg = _norm_strength(strength)
    title = f"Feedback on {target} ({'+' if pol > 0 else '-' if pol < 0 else '0'})"
    fm_extra = {"polarity": pol, "strength": strg}
    led_extra = {"polarity": pol, "strength": strg}
    if excerpt:
        fm_extra["excerpt"] = str(excerpt)
        led_extra["excerpt"] = str(excerpt)
    return _write_event(
        root,
        "feedback",
        target=target,
        fm_extra=fm_extra,
        ledger_extra=led_extra,
        title=title,
        sensitivity=sensitivity,
        actor=actor,
        role=role,
        tags=tags,
        body=body,
        now=now,
    )


def value_event(
    root: Path,
    *,
    target: str,
    polarity: Any,
    strength: Any = 1.0,
    value_kind: Optional[str] = None,
    excerpt: Optional[str] = None,
    actor: Optional[str] = None,
    role: str = "unknown",
    sensitivity: str = "internal",
    tags: Optional[list] = None,
    body: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Capture an observed instance of the oracle creating/destroying value.

    Writes ``value_event.jsonl`` rows whose ``target``/``polarity``/``strength``
    fields are read verbatim by ``recommendation.adjudicate`` -- so a value_event
    targeting a recommendation id feeds that recommendation's verdict directly.
    ``value_kind`` (understand/decide/act/avoid_risk/discover_opportunity) tags
    which dimension of value showed up, for the read-side scorecard.
    """
    now = now or _now_default()
    pol = _norm_polarity(polarity)
    strg = _norm_strength(strength)
    vk = (str(value_kind).strip().lower() if value_kind else "other")
    if vk not in VALUE_KINDS:
        vk = "other"
    title = f"Value event on {target} [{vk}]"
    fm_extra = {"polarity": pol, "strength": strg, "value_kind": vk}
    led_extra = {"polarity": pol, "strength": strg, "value_kind": vk}
    if excerpt:
        fm_extra["excerpt"] = str(excerpt)
        led_extra["excerpt"] = str(excerpt)
    return _write_event(
        root,
        "value",
        target=target,
        fm_extra=fm_extra,
        ledger_extra=led_extra,
        title=title,
        sensitivity=sensitivity,
        actor=actor,
        role=role,
        tags=tags,
        body=body,
        now=now,
    )


def failure_event(
    root: Path,
    *,
    target: str,
    severity: str = "medium",
    failure_mode: Optional[str] = None,
    excerpt: Optional[str] = None,
    actor: Optional[str] = None,
    role: str = "unknown",
    sensitivity: str = "internal",
    tags: Optional[list] = None,
    body: Optional[str] = None,
    now: Optional[datetime] = None,
    polarity: Any = -1,
    strength: Any = None,
) -> dict:
    """Capture something the oracle got wrong or could not do.

    Severity (low/medium/high/critical) sets the default negative magnitude the
    failure contributes to the retrospective scorecard (unless an explicit
    ``strength`` is supplied). ``failure_mode`` is a short machine tag (e.g.
    'stale-answer', 'wrong-refusal', 'crash', 'missed-contradiction'). Polarity
    is negative by default -- a failure is a negative value signal.
    """
    now = now or _now_default()
    sev = str(severity or "medium").strip().lower()
    if sev not in SEVERITIES:
        sev = "medium"
    pol = _norm_polarity(polarity)
    if strength is None:
        strg = _SEVERITY_STRENGTH[sev]
    else:
        strg = _norm_strength(strength)
    fmode = str(failure_mode).strip() if failure_mode else "unspecified"
    title = f"Failure on {target} [{sev}: {fmode}]"
    fm_extra = {
        "severity": sev,
        "failure_mode": fmode,
        "polarity": pol,
        "strength": strg,
    }
    led_extra = {
        "severity": sev,
        "failure_mode": fmode,
        "polarity": pol,
        "strength": strg,
    }
    if excerpt:
        fm_extra["excerpt"] = str(excerpt)
        led_extra["excerpt"] = str(excerpt)
    result = _write_event(
        root,
        "failure",
        target=target,
        fm_extra=fm_extra,
        ledger_extra=led_extra,
        title=title,
        sensitivity=sensitivity,
        actor=actor,
        role=role,
        tags=tags,
        body=body,
        now=now,
    )
    if sev == "critical":
        # Fail-closed at the source: a critical failure immediately runs the
        # autonomy demotion sweep (actions.enforce_demotion_policy). Lazy and
        # best-effort -- capture must never fail because actions is absent.
        try:
            import actions as _actions  # type: ignore
        except Exception:  # pragma: no cover - package fallback
            try:
                from . import actions as _actions  # type: ignore
            except Exception:
                _actions = None  # type: ignore
        if _actions is not None:
            try:
                result["demotion"] = _actions.enforce_demotion_policy(root, now=now)
            except Exception:
                pass
    return result


# --------------------------------------------------------------------------- #
# Read-side roll-up (the shape the scorecard and self-improvement loops use)
# --------------------------------------------------------------------------- #
def _net_signed(rows: list[dict]) -> float:
    total = 0.0
    for r in rows:
        pol = _norm_polarity(r.get("polarity", 0))
        strg = _norm_strength(r.get("strength", 1.0))
        total += pol * strg
    return total


def scorecard(root: Path) -> dict:
    """Roll up captured events into the signal the self-improvement loops read.

    Read-only. Returns per-kind counts, net signed value, the value-by-kind
    breakdown and a failure-by-severity breakdown. Defensive against
    missing/empty ledgers.
    """
    root = Path(root)
    feedback_rows, _ = ledger.load(_ledger_path(root, "feedback"))
    value_rows, _ = ledger.load(_ledger_path(root, "value"))
    failure_rows, _ = ledger.load(_ledger_path(root, "failure"))

    value_by_kind: dict[str, float] = {k: 0.0 for k in VALUE_KINDS}
    for r in value_rows:
        vk = str(r.get("value_kind", "other")).strip().lower()
        if vk not in value_by_kind:
            vk = "other"
        value_by_kind[vk] += _norm_polarity(r.get("polarity", 0)) * _norm_strength(
            r.get("strength", 1.0)
        )

    failure_by_severity: dict[str, int] = {s: 0 for s in SEVERITIES}
    for r in failure_rows:
        sev = str(r.get("severity", "medium")).strip().lower()
        if sev not in failure_by_severity:
            sev = "medium"
        failure_by_severity[sev] += 1

    return {
        "as_of": _today_str(_now_default()),
        "feedback": {
            "count": len(feedback_rows),
            "net_signed": _net_signed(feedback_rows),
        },
        "value": {
            "count": len(value_rows),
            "net_signed": _net_signed(value_rows),
            "by_kind": value_by_kind,
        },
        "failure": {
            "count": len(failure_rows),
            "by_severity": failure_by_severity,
            "net_signed": _net_signed(failure_rows),
        },
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="capture", description="value/feedback/failure capture path"
    )
    parser.add_argument("--root", default=".", help="oracle root (default: .)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fb = sub.add_parser("feedback", help="capture a user feedback event")
    p_fb.add_argument("--target", required=True, help="what the feedback is about")
    p_fb.add_argument("--polarity", required=True, help="positive|neutral|negative or +1/0/-1")
    p_fb.add_argument("--strength", default="1.0")
    p_fb.add_argument("--excerpt")
    p_fb.add_argument("--actor")
    p_fb.add_argument("--role", default="unknown",
                      help="attribution only -- recorded, never gates capture "
                           "(P5S-13); 'unknown' is for bare kernel-CLI writes (P5S-14)")
    p_fb.add_argument("--sensitivity", default="internal")
    p_fb.add_argument("--json", action="store_true")

    p_val = sub.add_parser("value", help="capture an observed value event")
    p_val.add_argument("--target", required=True)
    p_val.add_argument("--polarity", required=True)
    p_val.add_argument("--strength", default="1.0")
    p_val.add_argument("--value-kind", choices=list(VALUE_KINDS))
    p_val.add_argument("--excerpt")
    p_val.add_argument("--actor")
    p_val.add_argument("--role", default="unknown",
                       help="attribution only -- recorded, never gates capture "
                            "(P5S-13); 'unknown' is for bare kernel-CLI writes (P5S-14)")
    p_val.add_argument("--sensitivity", default="internal")
    p_val.add_argument("--json", action="store_true")

    p_fail = sub.add_parser("failure", help="capture a failure event")
    p_fail.add_argument("--target", required=True)
    p_fail.add_argument("--severity", default="medium", choices=list(SEVERITIES))
    p_fail.add_argument("--failure-mode")
    p_fail.add_argument("--excerpt")
    p_fail.add_argument("--actor")
    p_fail.add_argument("--role", default="unknown",
                        help="attribution only -- recorded, never gates capture "
                             "(P5S-13); 'unknown' is for bare kernel-CLI writes (P5S-14)")
    p_fail.add_argument("--sensitivity", default="internal")
    p_fail.add_argument("--json", action="store_true")

    p_sc = sub.add_parser("scorecard", help="roll up captured events (read-only)")
    p_sc.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        if args.cmd == "feedback":
            res = feedback_event(
                root,
                target=args.target,
                polarity=args.polarity,
                strength=args.strength,
                excerpt=args.excerpt,
                actor=args.actor,
                role=args.role,
                sensitivity=args.sensitivity,
            )
        elif args.cmd == "value":
            res = value_event(
                root,
                target=args.target,
                polarity=args.polarity,
                strength=args.strength,
                value_kind=args.value_kind,
                excerpt=args.excerpt,
                actor=args.actor,
                role=args.role,
                sensitivity=args.sensitivity,
            )
        elif args.cmd == "failure":
            res = failure_event(
                root,
                target=args.target,
                severity=args.severity,
                failure_mode=args.failure_mode,
                excerpt=args.excerpt,
                actor=args.actor,
                role=args.role,
                sensitivity=args.sensitivity,
            )
        elif args.cmd == "scorecard":
            sc = scorecard(root)
            print(json.dumps(sc, indent=2, ensure_ascii=False))
            return 0
        else:  # pragma: no cover - argparse guards this
            return 2
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"capture: {exc}\n")
        return 2

    if getattr(args, "json", False):
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(f"{res['kind']}: {res['drop_id']} -> {res['note_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
