#!/usr/bin/env python3
"""contradiction.py -- the open-contradiction adjudicator.

Contradictions are first-class in the oracle: unresolved conflicts among
sources, findings, models, metrics, or testimony often point at the most
valuable truths, which live at source boundaries. This module reads and writes
schema-valid ``Memory.nosync/Contradictions/`` notes and provides the two
operations the contradiction-resolution loop needs:

  * RANK open contradictions by a transparent, *deterministic* weighting of
    six signals: decision_relevance, severity, freshness, source_authority,
    ease, and risk_if_wrong. The score is a worklist ordering aid only -- it
    NEVER collapses a decision-relevant mismatch into an average (that would
    violate ANALYTIC-DOCTRINE's "do not average decision-relevant mismatches").
    A decision-relevant, high-severity conflict is pinned to the top of the
    worklist regardless of the convenience of resolving it.

  * CLASSIFY each contradiction into exactly one of:
        must_resolve              -- decision-relevant AND (high/critical
                                     severity OR a fresh authoritative source
                                     contradicts a relied-on claim); blocks
                                     grounded answers on the touched object.
        bounded_residual          -- real conflict, decision-relevant, but the
                                     residual is bounded and we can act inside
                                     the band (record the bound, keep watching).
        watch                     -- not (yet) decision-relevant; low severity;
                                     keep an eye on it but do not spend the
                                     adjudication budget now.
        schema_or_definition_debt -- the "conflict" is really a grain/epoch/
                                     definition mismatch, not a fact conflict;
                                     the fix is a schema/definition note, not a
                                     truth adjudication.

Public API:
    load_open(root) -> list[Contradiction]
    rank(items) -> list[(Contradiction, score, factors)]
    classify(item) -> str
    new(root, payload) -> Path           # write a schema-valid note + register
    resolve(root, cid, *, resolving_source, resolution, status='resolved')
    read_note(path) -> Contradiction
    main(argv) -> int                    # CLI: list|rank|open|classify|new|resolve

Writes route through ``safe_paths`` (the containment chokepoint) and register a
metadata row through ``ledger.append`` -- never raw I/O on a user path. Notes
are block-style YAML frontmatter (parsed by the floor's strict ``oracle_yaml``)
followed by a markdown body. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Floor imports. These resolve both when _tools is on sys.path (the spawned
# kernel + the test conftest) and when imported as a package.
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - exercised by both layouts
    import safe_paths
    import ledger
    from oracle_yaml import safe_load, UnsupportedYAML
except Exception:  # pragma: no cover
    from . import safe_paths  # type: ignore
    from . import ledger  # type: ignore
    from .oracle_yaml import safe_load, UnsupportedYAML  # type: ignore

# schema_check is part of the floor; degrade gracefully if a sibling build has
# not landed it yet (we keep a built-in fallback schema either way).
try:  # pragma: no cover
    import schema_check  # type: ignore
except Exception:  # pragma: no cover
    try:
        from . import schema_check  # type: ignore
    except Exception:  # pragma: no cover
        schema_check = None  # type: ignore


CONTRADICTIONS_DIR = "Contradictions"
MEMORY_BASE = "Memory.nosync"
INDEX_LEDGER = "Meta.nosync/ledgers/contradiction_index.jsonl"

OPEN_STATUSES = ("open", "investigating")
ALL_STATUSES = (
    "open",
    "investigating",
    "resolved",
    "accepted_residual",
    "superseded",
)
SEVERITIES = ("low", "medium", "high", "critical")
CLASSES = (
    "must_resolve",
    "bounded_residual",
    "watch",
    "schema_or_definition_debt",
)

# Ordinal weights for the deterministic ranking. Tuned so that
# decision_relevance and severity dominate (those are the load-bearing signals
# for whether a conflict can block a grounded answer); ease and freshness only
# break ties among already-important conflicts.
_SEVERITY_SCORE = {"low": 1, "medium": 2, "high": 3, "critical": 4}
# ease: how cheap it is to resolve. Higher == easier. Used ONLY as a tie-break;
# it can never demote a decision-relevant high-severity conflict.
_EASE_SCORE = {"trivial": 4, "easy": 3, "moderate": 2, "hard": 1, "blocked": 0}
_FRESHNESS_SCORE = {"fresh": 3, "stale": 1, "unknown": 2}
# source authority of the *contradicting* evidence (how much it should move us).
_AUTHORITY_SCORE = {
    "authoritative": 4,
    "corroborating": 3,
    "indicative": 2,
    "weak": 1,
    "unknown": 2,
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("true", "yes", "1", "high", "critical")


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if x is not None]
    return [v]


# --------------------------------------------------------------------------- #
# Contradiction record
# --------------------------------------------------------------------------- #
@dataclass
class Contradiction:
    """An in-memory view of a Contradictions/ note (frontmatter + body)."""

    frontmatter: dict
    body: str = ""
    path: Optional[Path] = None

    # convenience accessors ------------------------------------------------- #
    @property
    def id(self) -> str:
        return str(self.frontmatter.get("id", ""))

    @property
    def status(self) -> str:
        return str(self.frontmatter.get("status", "open"))

    @property
    def severity(self) -> str:
        sev = str(self.frontmatter.get("severity", "medium")).strip().lower()
        return sev if sev in SEVERITIES else "medium"

    @property
    def decision_relevant(self) -> bool:
        return _as_bool(self.frontmatter.get("decision_relevance"))

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def get(self, key: str, default: Any = None) -> Any:
        return self.frontmatter.get(key, default)


# --------------------------------------------------------------------------- #
# Note (de)serialization
# --------------------------------------------------------------------------- #
def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a ``---``-fenced block-YAML frontmatter + markdown body.

    The frontmatter BETWEEN the fences must be block-style (the strict
    oracle_yaml subset). The fences themselves are note delimiters, not a YAML
    multi-doc stream, so we strip them before handing the body to safe_load.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    # first line is the opening fence
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
        raise ValueError(f"contradiction note frontmatter not in safe subset: {exc}")
    if not isinstance(fm, dict):
        raise ValueError("contradiction note frontmatter is not a mapping")
    return fm, body.lstrip("\n")


def read_note(path: Path) -> Contradiction:
    """Read one Contradictions/ note from disk."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)
    return Contradiction(frontmatter=fm, body=body, path=path)


def _scalar_yaml(value: Any) -> str:
    """Render a scalar for frontmatter, quoting when needed to stay parseable."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    # Quote anything that could be misread by the block parser.
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
    """Render a dict to block-style YAML frontmatter (oracle_yaml-safe).

    Lists become ``- item`` lines; empty lists/maps become a bare ``key:``
    (which parses back to None) -- NEVER ``[]`` or ``{}``. Nested one-level
    mappings (e.g. the immutable adjudication block) are emitted with two-space
    indentation.
    """
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


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #
def _builtin_schema() -> dict:
    """Fallback contradiction schema used when the packaged JSON schema is not
    available -- keeps validation meaningful in isolation.
    """
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
            "severity",
            "claims_in_conflict",
            "decision_relevance",
        ],
        "properties": {
            "type": {"type": "string", "enum": ["contradiction"]},
            "status": {"type": "string", "enum": list(ALL_STATUSES)},
            "severity": {"type": "string", "enum": list(SEVERITIES)},
            "sensitivity": {
                "type": "string",
                "enum": ["public", "internal", "confidential", "restricted", "secret"],
            },
            "claims_in_conflict": {"type": "array"},
        },
    }


def _load_schema(root: Path) -> dict:
    """Prefer the shipped JSON schema; fall back to the built-in one."""
    schema_path = (
        Path(root) / "_tools" / "schemas" / "contradiction.schema.json"
    )
    if schema_path.exists():
        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return _builtin_schema()


def validate_frontmatter(root: Path, fm: dict) -> list[str]:
    """Validate a contradiction frontmatter dict; returns a list of errors."""
    schema = _load_schema(root)
    if schema_check is not None:
        return schema_check.validate(fm, schema)
    # Minimal fallback if schema_check itself is unavailable.
    errs = []
    for req in schema.get("required", []):
        if req not in fm:
            errs.append(f"missing required property {req!r}")
    return errs


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def _contradictions_dir(root: Path) -> Path:
    return Path(root) / MEMORY_BASE / CONTRADICTIONS_DIR


def load_all(root: Path) -> list[Contradiction]:
    """Load every Contradictions/ note (skips templates + context files)."""
    d = _contradictions_dir(root)
    items: list[Contradiction] = []
    if not d.exists():
        return items
    for p in sorted(d.glob("*.md")):
        if p.name.startswith("_"):  # _CONTEXT.md, _template.md
            continue
        try:
            items.append(read_note(p))
        except ValueError:
            # A malformed note must not brick the whole worklist.
            continue
    return items


def load_open(root: Path) -> list[Contradiction]:
    """Load only contradictions whose status is open|investigating."""
    return [c for c in load_all(root) if c.is_open]


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def _factors(item: Contradiction) -> dict[str, Any]:
    """Extract the six ranking signals from a contradiction, with safe defaults."""
    fm = item.frontmatter
    severity = item.severity
    freshness = str(fm.get("freshness", "unknown")).strip().lower()
    if freshness not in _FRESHNESS_SCORE:
        freshness = "unknown"
    authority = str(fm.get("source_authority", "unknown")).strip().lower()
    if authority not in _AUTHORITY_SCORE:
        authority = "unknown"
    ease = str(fm.get("ease", "moderate")).strip().lower()
    if ease not in _EASE_SCORE:
        ease = "moderate"
    risk = str(fm.get("risk_if_wrong", "medium")).strip().lower()
    risk_score = _SEVERITY_SCORE.get(risk, 2)
    return {
        "decision_relevance": item.decision_relevant,
        "severity": severity,
        "freshness": freshness,
        "source_authority": authority,
        "ease": ease,
        "risk_if_wrong": risk,
        "_severity_n": _SEVERITY_SCORE[severity],
        "_freshness_n": _FRESHNESS_SCORE[freshness],
        "_authority_n": _AUTHORITY_SCORE[authority],
        "_ease_n": _EASE_SCORE[ease],
        "_risk_n": risk_score,
    }


def score(item: Contradiction) -> float:
    """Deterministic worklist score for a single contradiction.

    The score is a *lexicographic-flavoured* weighted sum where
    decision_relevance and severity dominate so that a decision-relevant,
    high-severity conflict can never be demoted below a trivial-but-easy one.
    Ease and freshness contribute small, tie-break-scale weights only.

    IMPORTANT: this score orders the worklist; it does NOT average away a
    decision-relevant mismatch. Two conflicting claims are preserved as
    distinct claims_in_conflict on the note regardless of score.
    """
    f = _factors(item)
    # Big buckets first so they dominate any combination of small ones.
    dr = 1000 if f["decision_relevance"] else 0
    sev = 100 * f["_severity_n"]
    risk = 40 * f["_risk_n"]
    auth = 6 * f["_authority_n"]
    fresh = 3 * f["_freshness_n"]
    ease = 1 * f["_ease_n"]
    return float(dr + sev + risk + auth + fresh + ease)


def rank(items: list[Contradiction]) -> list[tuple[Contradiction, float, dict]]:
    """Return contradictions ordered most-urgent-first with score + factors.

    Stable secondary ordering by id keeps the worklist deterministic across
    runs even when scores tie.
    """
    scored = [(c, score(c), _factors(c)) for c in items]
    scored.sort(key=lambda t: (-t[1], t[0].id))
    return scored


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def classify(item: Contradiction) -> str:
    """Assign exactly one class.

    Order of checks matters: a definition/schema mismatch is diagnosed FIRST
    (so we never try to "resolve" what is really a grain/epoch debt), then the
    decision-relevant must-resolve gate, then bounded residual, then watch.
    """
    fm = item.frontmatter

    # Explicit operator override wins (lets the agent pin a class deliberately).
    forced = str(fm.get("classification", "")).strip().lower()
    if forced in CLASSES:
        return forced

    causes = " ".join(str(c).lower() for c in _as_list(fm.get("possible_causes")))
    kind = str(fm.get("conflict_kind", "")).strip().lower()
    schema_signals = (
        "definition",
        "grain",
        "epoch",
        "units",
        "timezone",
        "as-of",
        "as_of",
        "rounding",
        "scope mismatch",
        "currency",
    )
    if kind in ("schema", "definition", "grain", "epoch") or any(
        sig in causes for sig in schema_signals
    ):
        # A definition/grain/epoch mismatch is debt, not a fact conflict --
        # unless it is ALSO decision-relevant and high severity, in which case
        # the wrong number is already reaching decisions and it must be fixed.
        if not (item.decision_relevant and item.severity in ("high", "critical")):
            return "schema_or_definition_debt"

    f = _factors(item)
    fresh_authority_contradicts = (
        f["_freshness_n"] >= _FRESHNESS_SCORE["fresh"]
        and f["_authority_n"] >= _AUTHORITY_SCORE["corroborating"]
    )

    if item.decision_relevant and (
        item.severity in ("high", "critical") or fresh_authority_contradicts
    ):
        return "must_resolve"

    # A real, decision-relevant conflict that we can bound and act inside.
    bounded = _as_bool(fm.get("residual_bounded"))
    if item.decision_relevant and (bounded or item.severity == "medium"):
        return "bounded_residual"

    return "watch"


def must_resolve_open(root: Path) -> list[Contradiction]:
    """Open contradictions classified must_resolve (used by answer_protocol)."""
    return [c for c in load_open(root) if classify(c) == "must_resolve"]


# --------------------------------------------------------------------------- #
# Write / register
# --------------------------------------------------------------------------- #
def _index_ledger_path(root: Path) -> Path:
    return Path(root) / INDEX_LEDGER


def _register(root: Path, fm: dict, dst: Path, action: str) -> str:
    """Append a metadata row to the contradiction index ledger.

    Metadata only -- id, status, severity, decision_relevance, classification,
    note filename, content sha256. No claim text or evidence payload is copied
    into the tracked ledger (it stays in the .nosync note).
    """
    led = _index_ledger_path(root)
    led.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": _now_iso(),
        "action": action,
        "contradiction_id": str(fm.get("id", "")),
        "status": str(fm.get("status", "")),
        "severity": str(fm.get("severity", "")),
        "decision_relevance": bool(_as_bool(fm.get("decision_relevance"))),
        "classification": classify(Contradiction(frontmatter=fm)),
        "note": dst.name,
        "content_sha256": safe_paths.sha256_12(dst),
    }
    return ledger.append(led, row, id_prefix="CTRDX")


def new(root: Path, payload: dict) -> Path:
    """Create a schema-valid Contradictions/ note and register it.

    ``payload`` supplies the contradiction's substance; required defaults
    (id/type/created/updated/status/sensitivity/tags) are filled if absent. The
    note path is derived through safe_paths.contain (Memory.nosync base) from a
    slugged title, so a malicious title can never escape the Contradictions
    folder. Returns the written note Path.
    """
    root = Path(root)
    fm = dict(payload)
    fm.setdefault("type", "contradiction")
    fm.setdefault("status", "open")
    fm.setdefault("sensitivity", "internal")
    fm.setdefault("created", _today())
    fm["updated"] = _today()
    fm.setdefault("severity", "medium")
    fm.setdefault("tags", ["contradiction"])
    fm.setdefault("claims_in_conflict", [])
    fm.setdefault("decision_relevance", False)

    title = str(fm.get("title") or "untitled contradiction")
    fm["title"] = title
    slug = safe_paths.safe_slug(title)
    if not fm.get("id"):
        fm["id"] = f"ctr-{_today()}-{slug}"

    errors = validate_frontmatter(root, fm)
    if errors:
        raise ValueError("contradiction frontmatter invalid: " + "; ".join(errors))

    filename = f"{safe_paths.today()}_{slug}.md"
    # Containment: the destination MUST live under Memory.nosync/Contradictions.
    dst = safe_paths.contain(
        root,
        f"{CONTRADICTIONS_DIR}/{filename}",
        base=MEMORY_BASE,
    )
    body = payload.get("body") or _default_body(fm)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _write_contained(dst, _render_note(fm, body))
    _register(root, fm, dst, "new")
    return dst


def _default_body(fm: dict) -> str:
    claims = _as_list(fm.get("claims_in_conflict"))
    lines = [f"# {fm.get('title', 'Contradiction')}", ""]
    if claims:
        lines.append("## Claims in conflict")
        for c in claims:
            lines.append(f"- {c}")
        lines.append("")
    causes = _as_list(fm.get("possible_causes"))
    if causes:
        lines.append("## Possible causes")
        for c in causes:
            lines.append(f"- {c}")
        lines.append("")
    plan = fm.get("resolution_plan")
    if plan:
        lines.append("## Resolution plan")
        lines.append(str(plan))
        lines.append("")
    return "\n".join(lines)


def _write_contained(dst: Path, text: str) -> None:
    """Write text to a path that safe_paths.contain has already validated.

    The path is contained (it provably lives under Memory.nosync); we still go
    through a temp-file + os.replace so a partial write never leaves a torn
    note. This raw write lives behind a contained destination -- the
    no-bypass guard tolerates it because the target came from contain().
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile

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


def resolve(
    root: Path,
    cid: str,
    *,
    resolving_source: str,
    resolution: str,
    status: str = "resolved",
) -> Path:
    """Record a resolution on an existing contradiction note.

    Supersession-by-update: the resolution fields and the new status are
    written back to the SAME note (contradictions are mutable investigation
    objects, unlike immutable findings/sources). ``status`` must be a terminal
    or near-terminal status. A metadata row is appended to the index ledger.
    """
    if status not in ("resolved", "accepted_residual", "superseded", "investigating"):
        raise ValueError(f"resolve: bad status {status!r}")
    item = _find_by_id(root, cid)
    if item is None or item.path is None:
        raise ValueError(f"resolve: no open contradiction with id {cid!r}")
    fm = dict(item.frontmatter)
    fm["status"] = status
    fm["resolving_source"] = resolving_source
    fm["resolution"] = resolution
    fm["resolution_date"] = _today()
    fm["updated"] = _today()
    errors = validate_frontmatter(root, fm)
    if errors:
        raise ValueError("resolved contradiction invalid: " + "; ".join(errors))
    body = item.body
    extra = f"\n## Resolution ({status}, {_today()})\n\n{resolution}\n\nResolving source: {resolving_source}\n"
    _write_contained(item.path, _render_note(fm, body + extra))
    _register(root, fm, item.path, "resolve")
    return item.path


def _find_by_id(root: Path, cid: str) -> Optional[Contradiction]:
    for c in load_all(root):
        if c.id == cid:
            return c
    return None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_rank(root: Path, *, as_json: bool, open_only: bool) -> int:
    items = load_open(root) if open_only else load_all(root)
    ranked = rank(items)
    rows = []
    for c, sc, f in ranked:
        rows.append(
            {
                "id": c.id,
                "title": c.get("title", ""),
                "status": c.status,
                "severity": c.severity,
                "decision_relevance": c.decision_relevant,
                "score": sc,
                "class": classify(c),
            }
        )
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        if not rows:
            print("(no contradictions)")
        for r in rows:
            print(
                f"[{r['score']:>6.0f}] {r['class']:<24} {r['severity']:<8} "
                f"dr={str(r['decision_relevance']):<5} {r['id']}  {r['title']}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="contradiction", description="contradiction adjudicator"
    )
    parser.add_argument("--root", default=".", help="oracle root (default: .)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list all contradictions")
    p_list.add_argument("--json", action="store_true")

    p_open = sub.add_parser("open", help="list open contradictions (ranked)")
    p_open.add_argument("--json", action="store_true")

    p_rank = sub.add_parser("rank", help="rank all contradictions")
    p_rank.add_argument("--json", action="store_true")

    p_cls = sub.add_parser("classify", help="classify one contradiction by id")
    p_cls.add_argument("--id", required=True)

    p_new = sub.add_parser("new", help="create a contradiction note from JSON payload")
    p_new.add_argument("--payload", required=True, help="path to a JSON payload file")

    p_res = sub.add_parser("resolve", help="record a resolution")
    p_res.add_argument("--id", required=True)
    p_res.add_argument("--resolving-source", required=True)
    p_res.add_argument("--resolution", required=True)
    p_res.add_argument(
        "--status",
        default="resolved",
        choices=["resolved", "accepted_residual", "superseded", "investigating"],
    )

    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        if args.cmd == "list":
            return _print_rank(root, as_json=args.json, open_only=False)
        if args.cmd == "open":
            return _print_rank(root, as_json=args.json, open_only=True)
        if args.cmd == "rank":
            return _print_rank(root, as_json=args.json, open_only=False)
        if args.cmd == "classify":
            item = _find_by_id(root, args.id)
            if item is None:
                sys.stderr.write(f"no contradiction with id {args.id!r}\n")
                return 1
            print(classify(item))
            return 0
        if args.cmd == "new":
            payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
            dst = new(root, payload)
            print(str(dst))
            return 0
        if args.cmd == "resolve":
            dst = resolve(
                root,
                args.id,
                resolving_source=args.resolving_source,
                resolution=args.resolution,
                status=args.status,
            )
            print(str(dst))
            return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"contradiction: {exc}\n")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
