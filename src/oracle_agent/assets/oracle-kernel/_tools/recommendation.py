#!/usr/bin/env python3
"""recommendation.py -- the accountable-recommendation adjudicator.

A recommendation is accountable oracle advice. Its ORIGINAL substance is
IMMUTABLE: action, rationale, evidence, baseline, expected_signal, and
risk_if_wrong are recorded once and never edited. Only a SEPARATE adjudication
block mutates -- and it mutates by scoring the recommendation's expected signals
against OBSERVED reality (Decisions notes + value_events), NEVER against a human
saying "approved". Asking a person to approve a recommendation manufactures no
evidence; the test is whether the organization actually moved and whether value
showed up where the recommendation predicted it would.

The immutable original block is hash-locked: ``new()`` records a content sha256
of the frozen fields in the recommendation index ledger, and
``_assert_original_immutable`` refuses any write that would mutate them, so
supersession (write-new + supersedes:/superseded_by:) is the only path to change
the advice itself.

Adjudication verdicts:
    conformed     -- observed decisions conform AND the expected signal showed
                     up (net-positive observed value on the target).
    contradicted  -- observed decisions conflict OR the expected signal moved
                     the WRONG way (net-negative observed value).
    partial       -- mixed/insufficient: some conform some conflict, or signal
                     is present but below the expected band.
    pending       -- no observed decisions and no value_events yet (we refuse to
                     score advice the world has not yet tested).

Public API:
    new(root, payload) -> Path
    adjudicate(root, rid) -> dict          # recompute verdict from OBSERVED data
    scorecard(root) -> dict                # portfolio view across recommendations
    observed_signals(root, rid) -> dict    # the evidence the verdict rests on
    read_note(path) -> Recommendation
    main(argv) -> int                      # CLI: new|adjudicate|scorecard|show

Writes route through ``safe_paths`` and register metadata through
``ledger.append``. Notes use block-style YAML frontmatter (strict oracle_yaml
subset) + a markdown body. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Floor imports (work both as bare modules and as a package).
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


RECOMMENDATIONS_DIR = "Recommendations"
DECISIONS_DIR = "Decisions"
MEMORY_BASE = "Memory.nosync"
INDEX_LEDGER = "Meta.nosync/ledgers/recommendation_index.jsonl"
VALUE_EVENT_LEDGER = "Meta.nosync/ledgers/value_event.jsonl"

# The fields whose ORIGINAL values are frozen at creation. The adjudication
# block lives OUTSIDE this set and is the only mutable surface.
IMMUTABLE_FIELDS = (
    "action",
    "rationale",
    "evidence",
    "baseline",
    "expected_signal",
    "risk_if_wrong",
)

VERDICTS = ("conformed", "contradicted", "partial", "pending")
REC_STATUSES = ("open", "adjudicated", "superseded", "retired")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if x is not None]
    return [v]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        # textual polarity fallbacks
        s = str(v).strip().lower()
        return {"positive": 1.0, "up": 1.0, "negative": -1.0, "down": -1.0, "neutral": 0.0}.get(s, default)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class Recommendation:
    frontmatter: dict
    body: str = ""
    path: Optional[Path] = None

    @property
    def id(self) -> str:
        return str(self.frontmatter.get("id", ""))

    @property
    def status(self) -> str:
        return str(self.frontmatter.get("status", "open"))

    def get(self, key: str, default: Any = None) -> Any:
        return self.frontmatter.get(key, default)

    def original(self) -> dict:
        """The immutable original block (only the frozen fields)."""
        return {k: self.frontmatter.get(k) for k in IMMUTABLE_FIELDS}


# --------------------------------------------------------------------------- #
# Note (de)serialization (shared shape with contradiction notes)
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
        raise ValueError(f"recommendation note frontmatter not in safe subset: {exc}")
    if not isinstance(fm, dict):
        raise ValueError("recommendation note frontmatter is not a mapping")
    return fm, body.lstrip("\n")


def read_note(path: Path) -> Recommendation:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)
    return Recommendation(frontmatter=fm, body=body, path=path)


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


def _write_contained(dst: Path, text: str) -> None:
    """Atomic write to a path already validated by safe_paths.contain."""
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
# Schema
# --------------------------------------------------------------------------- #
def _builtin_schema() -> dict:
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
            "action",
            "rationale",
            "evidence",
            "baseline",
            "expected_signal",
        ],
        "properties": {
            "type": {"type": "string", "enum": ["recommendation"]},
            "status": {"type": "string", "enum": list(REC_STATUSES)},
            "sensitivity": {
                "type": "string",
                "enum": ["public", "internal", "confidential", "restricted", "secret"],
            },
            "evidence": {"type": "array"},
            "expected_signal": {"type": "array"},
        },
    }


def _load_schema(root: Path) -> dict:
    schema_path = Path(root) / "_tools" / "schemas" / "recommendation.schema.json"
    if schema_path.exists():
        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return _builtin_schema()


def validate_frontmatter(root: Path, fm: dict) -> list[str]:
    schema = _load_schema(root)
    if schema_check is not None:
        return schema_check.validate(fm, schema)
    errs = []
    for req in schema.get("required", []):
        if req not in fm:
            errs.append(f"missing required property {req!r}")
    return errs


# --------------------------------------------------------------------------- #
# Immutability hash
# --------------------------------------------------------------------------- #
def _original_fingerprint(fm: dict) -> str:
    """Stable sha256_12 of the immutable original fields (order-independent)."""
    import hashlib

    frozen = {k: fm.get(k) for k in IMMUTABLE_FIELDS}
    blob = json.dumps(frozen, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _recorded_fingerprint(root: Path, rid: str) -> Optional[str]:
    """Read the ORIGINAL fingerprint recorded at creation from the index ledger."""
    rows, _ = ledger.load(_index_ledger_path(root))
    fp = None
    for r in rows:
        if r.get("recommendation_id") == rid and r.get("action") == "new":
            fp = r.get("original_sha256")
    return fp


def _assert_original_immutable(root: Path, fm: dict) -> None:
    """Refuse any write whose immutable original block diverges from the one
    recorded at creation. Supersession (a NEW note) is the only legitimate way
    to change the advice itself.
    """
    rid = str(fm.get("id", ""))
    recorded = _recorded_fingerprint(root, rid)
    if recorded is None:
        return  # never registered (first write) -- nothing to compare against
    current = _original_fingerprint(fm)
    if current != recorded:
        raise ValueError(
            f"recommendation {rid!r}: immutable original block changed "
            f"(recorded={recorded} current={current}); supersede instead of editing"
        )


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def _recs_dir(root: Path) -> Path:
    return Path(root) / MEMORY_BASE / RECOMMENDATIONS_DIR


def _decisions_dir(root: Path) -> Path:
    return Path(root) / MEMORY_BASE / DECISIONS_DIR


def load_all(root: Path) -> list[Recommendation]:
    d = _recs_dir(root)
    items: list[Recommendation] = []
    if not d.exists():
        return items
    for p in sorted(d.glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            items.append(read_note(p))
        except ValueError:
            continue
    return items


def _find_by_id(root: Path, rid: str) -> Optional[Recommendation]:
    for r in load_all(root):
        if r.id == rid:
            return r
    return None


# --------------------------------------------------------------------------- #
# Observed evidence
# --------------------------------------------------------------------------- #
def _load_decisions(root: Path) -> list[dict]:
    """Load Decisions/ notes' frontmatter (observed organizational actions)."""
    d = _decisions_dir(root)
    out: list[dict] = []
    if not d.exists():
        return out
    for p in sorted(d.glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            fm, _ = _split_frontmatter(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if fm:
            out.append(fm)
    return out


def _decision_references(fm: dict, rid: str) -> Optional[str]:
    """Return 'conform' | 'conflict' if a decision links to ``rid``, else None.

    A decision records which recommendations it conforms to or conflicts with.
    We read the common link fields without inventing approval semantics.
    """
    conforms = {str(x) for x in _as_list(fm.get("conforms_to"))}
    conforms |= {str(x) for x in _as_list(fm.get("conforms_to_recommendations"))}
    conflicts = {str(x) for x in _as_list(fm.get("conflicts_with"))}
    conflicts |= {str(x) for x in _as_list(fm.get("conflicts_with_recommendations"))}
    if rid in conflicts:
        return "conflict"
    if rid in conforms:
        return "conform"
    return None


def _load_value_events(root: Path, rid: str) -> list[dict]:
    """value_events whose target references this recommendation id.

    value_events are written by capture.py with at least: target, polarity,
    strength, ts. We read defensively -- a missing/empty ledger yields [].
    """
    rows, _ = ledger.load(Path(root) / VALUE_EVENT_LEDGER)
    out = []
    for r in rows:
        target = str(r.get("target", ""))
        if target == rid or rid in str(r.get("targets", "")) or rid in target:
            out.append(r)
    return out


def observed_signals(root: Path, rid: str) -> dict:
    """Gather ONLY observed evidence for a recommendation -- never approvals.

    Returns counts of conforming/conflicting observed decisions and the net
    observed value (sum of polarity*strength across linked value_events).
    """
    decisions = _load_decisions(root)
    conform = 0
    conflict = 0
    for fm in decisions:
        ref = _decision_references(fm, rid)
        if ref == "conform":
            conform += 1
        elif ref == "conflict":
            conflict += 1
    value_events = _load_value_events(root, rid)
    net_value = 0.0
    for ve in value_events:
        polarity = _num(ve.get("polarity", ve.get("polarity_sign", 0)), 0.0)
        # normalize polarity to sign
        if polarity > 0:
            sign = 1.0
        elif polarity < 0:
            sign = -1.0
        else:
            sign = 0.0
        strength = _num(ve.get("strength", 1.0), 1.0)
        net_value += sign * abs(strength)
    return {
        "recommendation_id": rid,
        "decisions_conform": conform,
        "decisions_conflict": conflict,
        "value_events": len(value_events),
        "net_observed_value": net_value,
    }


# --------------------------------------------------------------------------- #
# Adjudication -- verdict from OBSERVED data, never human approval
# --------------------------------------------------------------------------- #
def _verdict_from_signals(sig: dict) -> str:
    conform = sig["decisions_conform"]
    conflict = sig["decisions_conflict"]
    n_value = sig["value_events"]
    net = sig["net_observed_value"]

    # Nothing observed yet: refuse to score (no manufactured-by-approval verdict).
    if conform == 0 and conflict == 0 and n_value == 0:
        return "pending"

    # Decisions conflict OR value moved the wrong way -> contradicted.
    if conflict > 0 and conflict >= conform:
        return "contradicted"
    if n_value > 0 and net < 0 and conform == 0:
        return "contradicted"

    # Clean conform AND positive (or neutral-but-no-conflict) signal -> conformed.
    if conflict == 0 and conform > 0 and net >= 0:
        return "conformed"
    if conflict == 0 and conform == 0 and n_value > 0 and net > 0:
        return "conformed"

    # Everything else is genuinely mixed.
    return "partial"


def adjudicate(root: Path, rid: str) -> dict:
    """Recompute a recommendation's adjudication block from OBSERVED reality and
    persist it WITHOUT touching the immutable original block.

    Returns the adjudication dict. Raises if the original block has drifted
    (forcing supersession) or the recommendation is missing.
    """
    rec = _find_by_id(root, rid)
    if rec is None or rec.path is None:
        raise ValueError(f"adjudicate: no recommendation with id {rid!r}")

    # Guard: the immutable original block must match what was frozen at creation.
    _assert_original_immutable(root, rec.frontmatter)

    sig = observed_signals(root, rid)
    verdict = _verdict_from_signals(sig)
    adjudication = {
        "verdict": verdict,
        "as_of": _today(),
        "decisions_conform": sig["decisions_conform"],
        "decisions_conflict": sig["decisions_conflict"],
        "value_events": sig["value_events"],
        "net_observed_value": sig["net_observed_value"],
        "evidence_basis": "observed_decisions_and_value_events",
    }

    fm = dict(rec.frontmatter)
    # Only the adjudication block + status + updated change. Originals untouched.
    fm["adjudication"] = adjudication
    fm["status"] = "adjudicated" if verdict != "pending" else fm.get("status", "open")
    fm["updated"] = _today()

    # Re-assert: the rendered note's frozen fields must still match the record.
    _assert_original_immutable(root, fm)
    errors = validate_frontmatter(root, fm)
    if errors:
        raise ValueError("adjudicated recommendation invalid: " + "; ".join(errors))

    _write_contained(rec.path, _render_note(fm, rec.body))
    _register(root, fm, rec.path, "adjudicate")
    return adjudication


# --------------------------------------------------------------------------- #
# Scorecard
# --------------------------------------------------------------------------- #
def scorecard(root: Path) -> dict:
    """Portfolio view across all recommendations.

    Recomputes each verdict from observed data (read-only -- does not persist)
    so the scorecard always reflects the current state of the world.
    """
    recs = load_all(root)
    by_verdict = {v: 0 for v in VERDICTS}
    rows = []
    for rec in recs:
        sig = observed_signals(root, rec.id)
        verdict = _verdict_from_signals(sig)
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
        rows.append(
            {
                "id": rec.id,
                "title": rec.get("title", ""),
                "status": rec.status,
                "verdict": verdict,
                "decisions_conform": sig["decisions_conform"],
                "decisions_conflict": sig["decisions_conflict"],
                "value_events": sig["value_events"],
                "net_observed_value": sig["net_observed_value"],
            }
        )
    rows.sort(key=lambda r: r["id"])
    return {
        "as_of": _today(),
        "total": len(recs),
        "by_verdict": by_verdict,
        "recommendations": rows,
    }


# --------------------------------------------------------------------------- #
# Write / register
# --------------------------------------------------------------------------- #
def _index_ledger_path(root: Path) -> Path:
    return Path(root) / INDEX_LEDGER


def _register(root: Path, fm: dict, dst: Path, action: str) -> str:
    led = _index_ledger_path(root)
    led.parent.mkdir(parents=True, exist_ok=True)
    adj = fm.get("adjudication") or {}
    row = {
        "ts": _now_iso(),
        "action": action,
        "recommendation_id": str(fm.get("id", "")),
        "status": str(fm.get("status", "")),
        "verdict": str(adj.get("verdict", "")) if isinstance(adj, dict) else "",
        "original_sha256": _original_fingerprint(fm),
        "note": dst.name,
        "content_sha256": safe_paths.sha256_12(dst),
    }
    return ledger.append(led, row, id_prefix="RECX")


def new(root: Path, payload: dict) -> Path:
    """Create an accountable recommendation note and freeze its original block.

    Required immutable substance (action/rationale/evidence/baseline/
    expected_signal) must be supplied; risk_if_wrong is recommended. The note
    path is derived through safe_paths.contain so a malicious title cannot
    escape the Recommendations folder. The original fingerprint is recorded in
    the index ledger so later writes can prove the original never changed.
    """
    root = Path(root)
    fm = dict(payload)
    fm.setdefault("type", "recommendation")
    fm.setdefault("status", "open")
    fm.setdefault("sensitivity", "internal")
    fm.setdefault("created", _today())
    fm["updated"] = _today()
    fm.setdefault("tags", ["recommendation"])
    fm.setdefault("evidence", [])
    fm.setdefault("expected_signal", [])
    fm.setdefault("risk_if_wrong", "unspecified")

    title = str(fm.get("title") or "untitled recommendation")
    fm["title"] = title
    slug = safe_paths.safe_slug(title)
    if not fm.get("id"):
        fm["id"] = f"rec-{_today()}-{slug}"

    # Adjudication starts empty (pending) and is the only mutable block.
    fm.setdefault(
        "adjudication",
        {
            "verdict": "pending",
            "as_of": _today(),
            "decisions_conform": 0,
            "decisions_conflict": 0,
            "value_events": 0,
            "net_observed_value": 0.0,
            "evidence_basis": "observed_decisions_and_value_events",
        },
    )

    errors = validate_frontmatter(root, fm)
    if errors:
        raise ValueError("recommendation frontmatter invalid: " + "; ".join(errors))

    filename = f"{safe_paths.today()}_{slug}.md"
    dst = safe_paths.contain(
        root,
        f"{RECOMMENDATIONS_DIR}/{filename}",
        base=MEMORY_BASE,
    )
    body = payload.get("body") or _default_body(fm)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _write_contained(dst, _render_note(fm, body))
    _register(root, fm, dst, "new")
    return dst


def _default_body(fm: dict) -> str:
    lines = [f"# {fm.get('title', 'Recommendation')}", ""]
    lines.append("## Action (immutable)")
    lines.append(str(fm.get("action", "")))
    lines.append("")
    lines.append("## Rationale (immutable)")
    lines.append(str(fm.get("rationale", "")))
    lines.append("")
    ev = _as_list(fm.get("evidence"))
    if ev:
        lines.append("## Evidence (immutable)")
        for e in ev:
            lines.append(f"- {e}")
        lines.append("")
    lines.append("## Baseline (immutable)")
    lines.append(str(fm.get("baseline", "")))
    lines.append("")
    sig = _as_list(fm.get("expected_signal"))
    if sig:
        lines.append("## Expected signal (immutable)")
        for s in sig:
            lines.append(f"- {s}")
        lines.append("")
    lines.append("## Adjudication (mutable -- scored vs OBSERVED reality)")
    lines.append(
        "Scored against observed Decisions and value_events, never human approval."
    )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="recommendation", description="recommendation adjudicator"
    )
    parser.add_argument("--root", default=".", help="oracle root (default: .)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new", help="create a recommendation from a JSON payload")
    p_new.add_argument("--payload", required=True, help="path to a JSON payload file")

    p_adj = sub.add_parser("adjudicate", help="score a recommendation vs observed reality")
    p_adj.add_argument("--id", required=True)
    p_adj.add_argument("--json", action="store_true")

    p_sc = sub.add_parser("scorecard", help="portfolio scorecard across recommendations")
    p_sc.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="show observed signals for a recommendation")
    p_show.add_argument("--id", required=True)

    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        if args.cmd == "new":
            payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
            dst = new(root, payload)
            print(str(dst))
            return 0
        if args.cmd == "adjudicate":
            adj = adjudicate(root, args.id)
            if args.json:
                print(json.dumps(adj, indent=2, ensure_ascii=False))
            else:
                print(
                    f"{args.id}: {adj['verdict']} "
                    f"(conform={adj['decisions_conform']} "
                    f"conflict={adj['decisions_conflict']} "
                    f"value_events={adj['value_events']} "
                    f"net={adj['net_observed_value']})"
                )
            return 0
        if args.cmd == "scorecard":
            sc = scorecard(root)
            if args.json:
                print(json.dumps(sc, indent=2, ensure_ascii=False))
            else:
                print(f"recommendations: {sc['total']}  as_of {sc['as_of']}")
                for v in VERDICTS:
                    print(f"  {v:<13} {sc['by_verdict'].get(v, 0)}")
                for r in sc["recommendations"]:
                    print(
                        f"  {r['verdict']:<13} {r['id']}  {r['title']}"
                    )
            return 0
        if args.cmd == "show":
            sig = observed_signals(root, args.id)
            print(json.dumps(sig, indent=2, ensure_ascii=False))
            return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"recommendation: {exc}\n")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
