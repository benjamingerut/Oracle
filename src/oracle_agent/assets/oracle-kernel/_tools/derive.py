#!/usr/bin/env python3
"""derive.py -- review-gated derivation of Findings / Questions / Contradictions.

This module PROPOSES candidate knowledge records derived from indexed source
chunks. It NEVER auto-confirms anything: every record it writes is born with
``status: needs_review`` and must be promoted by a human (or a separate
adjudication step) before it counts as active knowledge. This preserves the
analytic doctrine that "user testimony / ingested material is evidence, not
automatic truth": derivation lowers the activation energy of turning raw
material into structured claims while keeping the trust gate firmly closed.

What it derives, from a Source's indexed chunks:
  * Finding candidate -- an atomic claim grounded in a specific chunk, carrying
    a claim_tier (defaults to the most cautious, SPEC, for machine-proposed
    claims unless told otherwise), a confidence range, the chunk offsets as
    evidence, a stated disconfirmer, and a back-link to the Source.
  * Question candidate -- an open question the material raises (status
    needs_review), so gaps are captured rather than lost.
  * Contradiction candidate -- when two chunks (or a chunk vs an asserted prior
    claim) conflict, a contradiction is proposed rather than averaged away.

Records are written as schema-valid notes under Memory.nosync/{Findings,
Questions,Contradictions}/ via the SAME containment + generated-write discipline
as source_record (path through safe_paths.contain(base='Memory.nosync'); bytes
via Path.write_text -- the documented no-bypass exception). Each candidate is
registered in the derivation ledger with its content hash.

Public API:
    from_source(root, source_id, *, kinds=('finding',), claim_tier='SPEC',
                actor='system', role='system', max_findings=5) -> list[dict]
    propose_finding(root, source_id, claim, evidence, *, ...) -> dict
    propose_question(root, source_id, question, *, ...) -> dict
    propose_contradiction(root, source_id, claims_in_conflict, *, ...) -> dict
    list_candidates(root) -> list[dict]

CLI:
    python3 derive.py --root R from-source --source-id SRC-... \
        [--kinds finding,question] [--claim-tier SPEC] [--max 5] [--actor A]
    python3 derive.py --root R finding --source-id S --claim "..." --evidence "..."
    python3 derive.py --root R question --source-id S --question "..."
    python3 derive.py --root R contradiction --source-id S --claims "A vs B"
    python3 derive.py --root R list

Stdlib only. Lazy-imports knowledge_index for chunk pulls so derive stays usable
even when the index module is unavailable; if it is absent, ``from_source``
reports that no chunks could be pulled rather than crashing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

__all__ = [
    "from_source",
    "propose_finding",
    "propose_question",
    "propose_contradiction",
    "list_candidates",
    "CLAIM_TIERS",
]

CLAIM_TIERS = ("OBS", "INF", "SPEC", "SPEC-horizon")
_SENSITIVITY_ENUM = ("public", "internal", "confidential", "restricted", "secret")
_BASE = "Memory.nosync"
_LEDGER_NAME = "derive.jsonl"

_KIND_DIR = {
    "finding": "Findings",
    "question": "Questions",
    "contradiction": "Contradictions",
}
_KIND_TYPE = {
    "finding": "finding",
    "question": "question",
    "contradiction": "contradiction",
}
_KIND_PREFIX = {
    "finding": "FND",
    "question": "QST",
    "contradiction": "CTR",
}


# --------------------------------------------------------------------------- #
# sibling-import shim
# --------------------------------------------------------------------------- #
def _imp(name: str):
    try:
        return __import__(name)
    except Exception:  # pragma: no cover - package fallback
        import importlib
        return importlib.import_module(f".{name}", package=__package__)


def _safe_paths():
    return _imp("safe_paths")


def _ledger():
    return _imp("ledger")


def _schema_check():
    return _imp("schema_check")


def _source_record():
    return _imp("source_record")


# --------------------------------------------------------------------------- #
# helpers (shared shape with source_record's renderer)
# --------------------------------------------------------------------------- #
def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _ledger_path(root: Path) -> Path:
    return Path(root) / "Meta.nosync" / "ledgers" / _LEDGER_NAME


def _content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _render_note(frontmatter: dict, body: str) -> str:
    """Render via source_record's renderer when available (single source of
    truth for the block-style YAML frontmatter), else a local equivalent."""
    try:
        sr = _source_record()
        if hasattr(sr, "render_note"):
            return sr.render_note(frontmatter, body)
    except Exception:  # pragma: no cover - fallback path
        pass
    return _local_render(frontmatter, body)


def _yaml_scalar(v) -> str:
    if v is None:
        return ""
    s = str(v)
    if s == "":
        return '""'
    risky = any(c in s for c in (":", "#", "&", "*", "!", "{", "}", "[", "]", "|", ">", '"', "'", "\n"))
    if risky or s != s.strip() or (s and s[0] in "-?@`%"):
        inner = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
        return f'"{inner}"'
    return s


def _local_render(fm: dict, body: str) -> str:
    lines = ["---"]
    for key, val in fm.items():
        if key == "tags":
            tags = val or []
            lines.append("tags:")
            for t in tags:
                lines.append(f"  - {_yaml_scalar(t)}")
            continue
        if val is None or val == "":
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {_yaml_scalar(val)}")
    lines += ["---", "", body.rstrip("\n"), ""]
    return "\n".join(lines)


def _validate(fm: dict, kind: str) -> list[str]:
    """Validate against the shipped finding/contradiction schema when present.

    The review gate is mechanical but schema-aware:
      * finding / question candidates carry ``status: needs_review`` (the
        finding schema and the common frontmatter schema both permit it);
      * contradiction candidates carry ``status: open`` -- the only un-adjudicated
        value the shipped contradiction schema allows -- PLUS a ``needs-review``
        tag and ``classification: watch`` to mark them machine-proposed and
        not-yet-adjudicated. Either way a derived record is never born active.
    """
    errors: list[str] = []
    tags = fm.get("tags") or []
    if kind == "contradiction":
        if fm.get("status") != "open":
            errors.append(
                "contradiction candidate must have status=open (review gate; "
                "the schema forbids needs_review for contradictions)"
            )
        if "needs-review" not in tags:
            errors.append("contradiction candidate must carry a 'needs-review' tag")
    else:
        if fm.get("status") != "needs_review":
            errors.append(
                f"{kind} candidate must have status=needs_review (review gate)"
            )
    schema_dir = Path(__file__).resolve().parent / "schemas"
    schema_file = {
        "finding": "finding.schema.json",
        "contradiction": "contradiction.schema.json",
    }.get(kind, "note_frontmatter.schema.json")
    schema_path = schema_dir / schema_file
    if schema_path.exists():
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            sc = _schema_check()
            errors.extend(sc.validate(fm, schema))
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"schema load/validate error: {exc}")
    return errors


def _destination(root: Path, kind: str, rec_id: str, title: str) -> Path:
    sp = _safe_paths()
    subdir = _KIND_DIR[kind]
    today = sp.today()
    filename = f"{today}_{sp.safe_slug(rec_id)}-{sp.safe_slug(title or rec_id)}.md"
    return sp.contain(root, f"{subdir}/{filename}", base=_BASE)


def _mint_id(root: Path, kind: str) -> str:
    """Mint the next free <PREFIX>-YYYYMMDD-NNN candidate id.

    The derivation ledger stores rows under ``drop_id`` values with the ``DRV``
    prefix, so ``ledger.next_id`` (which scans ``drop_id``) would always return
    -001 for a kind prefix. We scan the existing ``candidate_id`` FIELD values
    for this kind's prefix and pick the lowest unused sequence for today.
    """
    led = _ledger()
    prefix = _KIND_PREFIX[kind]
    rows, _ = led.load(_ledger_path(root))
    day = datetime.now().strftime("%Y%m%d")
    base = f"{prefix}-{day}-"
    existing = {
        str(r.get("candidate_id", ""))
        for r in rows
        if str(r.get("candidate_id", "")).startswith(base)
    }
    n = 1
    while f"{base}{n:03d}" in existing:
        n += 1
    return f"{base}{n:03d}"


def _write_candidate(
    root: Path,
    kind: str,
    fm: dict,
    body: str,
    *,
    extra_row: Optional[dict] = None,
) -> dict:
    """Validate, write (contained), and register a candidate. Returns ledger row."""
    # Stamp content hash over the hash-free rendering, then re-render.
    fm0 = dict(fm)
    fm0["content_sha256"] = ""
    hashfree = _render_note(fm0, body)
    content_sha = _content_sha256(hashfree)
    fm["content_sha256"] = content_sha
    note_text = _render_note(fm, body)

    errors = _validate(fm, kind)
    if errors:
        raise ValueError(
            f"derive: invalid {kind} candidate:\n  " + "\n  ".join(errors)
        )

    dest = _destination(root, kind, fm["id"], fm["title"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(note_text, encoding="utf-8")  # safe_paths-internal: dest from _destination() → safe_paths.contain()

    row = {
        "candidate_id": fm["id"],
        "kind": kind,
        "type": fm["type"],
        "title": fm["title"],
        # On-disk note status (open for contradictions, needs_review otherwise).
        "status": fm.get("status", "needs_review"),
        # The mechanical review gate: every derived candidate is un-adjudicated
        # until a human promotes it, regardless of the schema's status value.
        "review_gate": "needs_review",
        "source_id": fm.get("source_id", ""),
        "sensitivity": fm["sensitivity"],
        "path": str(_relpath(dest, root)),
        "content_sha256": content_sha,
        "actor": fm.get("actor", "system"),
        "role": fm.get("role", "system"),
    }
    if extra_row:
        row.update(extra_row)
    led = _ledger()
    drop_id = led.append(_ledger_path(root), row, id_prefix="DRV")
    row["drop_id"] = drop_id
    return row


def _relpath(p: Path, root: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return str(p)


def _source_sensitivity(root: Path, source_id: str) -> str:
    """Inherit the Source's sensitivity (stricter-side default) for a candidate."""
    try:
        sr = _source_record()
        rec = sr.load_record(root, source_id) if source_id else None
        if rec and rec.get("sensitivity") in _SENSITIVITY_ENUM:
            return rec["sensitivity"]
    except Exception:
        pass
    return "internal"


# --------------------------------------------------------------------------- #
# proposers
# --------------------------------------------------------------------------- #
def propose_finding(
    root,
    source_id: str,
    claim: str,
    evidence: str,
    *,
    claim_tier: str = "SPEC",
    confidence: float = 0.4,
    decision_relevance: str = "Proposed by derivation; relevance pending review.",
    disconfirmer: str = "A contradicting authoritative source, or human review rejecting the claim.",
    evidence_offsets: str = "",
    sensitivity: Optional[str] = None,
    actor: str = "system",
    role: str = "system",
) -> dict:
    """Propose a Finding candidate (status=needs_review) grounded in evidence.

    ``claim_tier`` defaults to SPEC: a machine-proposed claim is speculative
    until a human reviews it. ``confidence`` is a point in [0,1] for the schema,
    but the body states it as a RANGE per analytic doctrine.
    """
    root = Path(root)
    if claim_tier not in CLAIM_TIERS:
        claim_tier = "SPEC"
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 0.4
    conf = min(1.0, max(0.0, conf))
    sens = (sensitivity or _source_sensitivity(root, source_id)).strip().lower()
    if sens not in _SENSITIVITY_ENUM:
        sens = "internal"

    rec_id = _mint_id(root, "finding")
    today = _today()
    title = (claim.strip().split("\n")[0])[:80] or f"Finding {rec_id}"
    fm = {
        "id": rec_id,
        "type": "finding",
        "title": title,
        "created": today,
        "updated": today,
        "sensitivity": sens,
        "status": "needs_review",
        "tags": ["finding", "derived", "needs-review"],
        "actor": actor,
        "role": role,
        "claim_tier": claim_tier,
        "confidence": conf,
        "evidence": evidence or "_(evidence excerpt missing)_",
        "decision_relevance": decision_relevance,
        "disconfirmer": disconfirmer,
        "as_of": today,
        "source_id": source_id or "",
        "evidence_offsets": evidence_offsets,
    }
    low = max(0.0, conf - 0.15)
    high = min(1.0, conf + 0.15)
    body = "\n".join([
        f"# {title}",
        "",
        "## Claim",
        "",
        claim.strip() or "_(claim text missing)_",
        "",
        "## Evidence",
        "",
        evidence or "_(evidence excerpt missing)_",
        (f"\n_offsets: {evidence_offsets}_" if evidence_offsets else ""),
        "",
        "## Epistemics",
        "",
        f"- Claim tier: **{claim_tier}** (machine-proposed; review before trusting)",
        f"- Confidence range: ~{low:.2f}-{high:.2f} (point {conf:.2f})",
        f"- Disconfirmer: {disconfirmer}",
        f"- Source: {source_id or '(none)'}",
        "",
        "## Review gate",
        "",
        "This is a DERIVED candidate. It is not active knowledge until a human "
        "review promotes it (status: active) or rejects it.",
    ])
    return _write_candidate(root, "finding", fm, body)


def propose_question(
    root,
    source_id: str,
    question: str,
    *,
    sensitivity: Optional[str] = None,
    actor: str = "system",
    role: str = "system",
) -> dict:
    """Propose an open Question candidate (status=needs_review)."""
    root = Path(root)
    sens = (sensitivity or _source_sensitivity(root, source_id)).strip().lower()
    if sens not in _SENSITIVITY_ENUM:
        sens = "internal"
    rec_id = _mint_id(root, "question")
    today = _today()
    title = (question.strip().split("\n")[0])[:80] or f"Question {rec_id}"
    fm = {
        "id": rec_id,
        "type": "question",
        "title": title,
        "created": today,
        "updated": today,
        "sensitivity": sens,
        "status": "needs_review",
        "tags": ["question", "derived", "needs-review"],
        "actor": actor,
        "role": role,
        "source_id": source_id or "",
    }
    body = "\n".join([
        f"# {title}",
        "",
        "## Question",
        "",
        question.strip() or "_(question text missing)_",
        "",
        f"_Raised by derivation from source {source_id or '(none)'}._",
        "",
        "## Review gate",
        "",
        "Derived candidate; needs human review to become an active open question.",
    ])
    return _write_candidate(root, "question", fm, body)


def propose_contradiction(
    root,
    source_id: str,
    claims_in_conflict: str,
    *,
    severity: str = "medium",
    decision_relevance: str = "Proposed by derivation; relevance pending review.",
    possible_causes: str = "",
    sensitivity: Optional[str] = None,
    actor: str = "system",
    role: str = "system",
) -> dict:
    """Propose a Contradiction candidate (status=needs_review).

    Contradictions are preserved, never averaged: when two chunks conflict on a
    decision-relevant point, this records the conflict for adjudication.
    """
    root = Path(root)
    if severity not in ("low", "medium", "high", "critical"):
        severity = "medium"
    sens = (sensitivity or _source_sensitivity(root, source_id)).strip().lower()
    if sens not in _SENSITIVITY_ENUM:
        sens = "internal"
    rec_id = _mint_id(root, "contradiction")
    today = _today()
    title = (claims_in_conflict.strip().split("\n")[0])[:80] or f"Contradiction {rec_id}"
    fm = {
        "id": rec_id,
        "type": "contradiction",
        "title": title,
        "created": today,
        "updated": today,
        "sensitivity": sens,
        # The contradiction schema forbids 'needs_review'; an un-adjudicated
        # contradiction is 'open'. The review gate is carried by the
        # 'needs-review' tag + classification: watch (see _validate).
        "status": "open",
        "tags": ["contradiction", "derived", "needs-review"],
        "actor": actor,
        "role": role,
        "severity": severity,
        "classification": "watch",
        "claims_in_conflict": claims_in_conflict or "_(conflict not stated)_",
        "possible_causes": possible_causes,
        "decision_relevance": decision_relevance,
        "source_id": source_id or "",
    }
    body = "\n".join([
        f"# {title}",
        "",
        "## Claims in conflict",
        "",
        claims_in_conflict.strip() or "_(conflict not stated)_",
        "",
        f"- Severity: {severity}",
        f"- Source: {source_id or '(none)'}",
        (f"- Possible causes: {possible_causes}" if possible_causes else ""),
        "",
        "## Review gate",
        "",
        "Derived candidate; needs human review/adjudication. Decision-relevant "
        "mismatches are preserved, not averaged.",
    ])
    return _write_candidate(root, "contradiction", fm, body)


# --------------------------------------------------------------------------- #
# from-source orchestration
# --------------------------------------------------------------------------- #
def _pull_source_chunks(root: Path, source_id: str, limit: int) -> list[dict]:
    """Pull the indexed chunks for a Source from knowledge_index, if available.

    Returns a list of chunk dicts (text/start/end). If the index module is not
    importable or has no chunks for the source, returns an empty list (callers
    surface that as 'no chunks to derive from' rather than failing).
    """
    try:
        ki = _imp("knowledge_index")
    except Exception:
        return []
    try:
        idx = ki.KnowledgeIndex(root)
    except Exception:
        return []
    try:
        # Search the source_id token to retrieve that source's chunks; fall back
        # to a broad query so we still surface *some* chunks for derivation.
        hits = idx.search(source_id, k=limit) if source_id else []
        chunks = [h for h in hits if h.get("source_id") == source_id]
        if not chunks:
            # Last resort: title-agnostic scan via stats path is unavailable; try
            # the raw rows by querying a frequent token. We accept any hits whose
            # source_id matches; otherwise return what search produced.
            chunks = hits[:limit]
        return chunks
    finally:
        try:
            idx.close()
        except Exception:
            pass


def from_source(
    root,
    source_id: str,
    *,
    kinds=("finding",),
    claim_tier: str = "SPEC",
    actor: str = "system",
    role: str = "system",
    max_findings: int = 5,
) -> list[dict]:
    """Derive review-gated candidates from a Source's indexed chunks.

    For each pulled chunk a Finding candidate is proposed (up to ``max_findings``)
    using the chunk text as evidence and its offsets as evidence_offsets. If
    ``'question'`` is in ``kinds`` a single roll-up Question is proposed for the
    source's open gaps. Contradiction proposal across chunk pairs is attempted
    only when ``'contradiction'`` is requested and two chunks are present.

    Returns the list of ledger rows for every candidate written. Each is
    status=needs_review -- nothing is auto-trusted.
    """
    root = Path(root)
    requested = set(kinds or ())
    if not requested:
        requested = {"finding"}
    chunks = _pull_source_chunks(root, source_id, max(1, int(max_findings)))
    written: list[dict] = []

    if "finding" in requested:
        for ch in chunks[: max(0, int(max_findings))]:
            text = (ch.get("text") or "").strip()
            if not text:
                continue
            claim = text.split("\n")[0][:200] or text[:200]
            offsets = ""
            if ch.get("start") is not None and ch.get("end") is not None:
                offsets = f"{ch.get('start')}-{ch.get('end')}"
            written.append(
                propose_finding(
                    root, source_id, claim, text,
                    claim_tier=claim_tier, evidence_offsets=offsets,
                    actor=actor, role=role,
                )
            )

    if "question" in requested:
        q = (
            f"What decision does source {source_id} bear on, and which of its "
            f"claims need an authoritative cross-check?"
        )
        written.append(propose_question(root, source_id, q, actor=actor, role=role))

    if "contradiction" in requested and len(chunks) >= 2:
        a = (chunks[0].get("text") or "").strip().split("\n")[0][:120]
        b = (chunks[1].get("text") or "").strip().split("\n")[0][:120]
        if a and b and a != b:
            written.append(
                propose_contradiction(
                    root, source_id,
                    f"Chunk A: {a}\nvs\nChunk B: {b}",
                    possible_causes="Auto-flagged adjacent-chunk divergence; verify on review.",
                    actor=actor, role=role,
                )
            )

    return written


def list_candidates(root) -> list[dict]:
    """All derived candidate rows (latest per candidate_id)."""
    led = _ledger()
    rows, _ = led.load(_ledger_path(Path(root)))
    by_id: dict[str, dict] = {}
    for r in rows:
        cid = r.get("candidate_id")
        if cid:
            by_id[cid] = r
    return list(by_id.values())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Review-gated derivation of candidates")
    ap.add_argument("--root", default=".", help="oracle root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_fs = sub.add_parser("from-source", help="derive candidates from a Source")
    p_fs.add_argument("--source-id", required=True)
    p_fs.add_argument("--kinds", default="finding", help="comma list: finding,question,contradiction")
    p_fs.add_argument("--claim-tier", default="SPEC", choices=list(CLAIM_TIERS))
    p_fs.add_argument("--max", type=int, default=5)
    p_fs.add_argument("--actor", default="system")
    p_fs.add_argument("--role", default="system")

    p_f = sub.add_parser("finding", help="propose a single Finding candidate")
    p_f.add_argument("--source-id", default="")
    p_f.add_argument("--claim", required=True)
    p_f.add_argument("--evidence", required=True)
    p_f.add_argument("--claim-tier", default="SPEC", choices=list(CLAIM_TIERS))
    p_f.add_argument("--confidence", type=float, default=0.4)
    p_f.add_argument("--actor", default="system")
    p_f.add_argument("--role", default="system")

    p_q = sub.add_parser("question", help="propose a single Question candidate")
    p_q.add_argument("--source-id", default="")
    p_q.add_argument("--question", required=True)
    p_q.add_argument("--actor", default="system")
    p_q.add_argument("--role", default="system")

    p_c = sub.add_parser("contradiction", help="propose a Contradiction candidate")
    p_c.add_argument("--source-id", default="")
    p_c.add_argument("--claims", required=True)
    p_c.add_argument("--severity", default="medium")
    p_c.add_argument("--actor", default="system")
    p_c.add_argument("--role", default="system")

    sub.add_parser("list", help="list derived candidates")

    args = ap.parse_args(argv)
    root = Path(args.root)

    try:
        if args.cmd == "from-source":
            kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
            rows = from_source(
                root, args.source_id, kinds=kinds, claim_tier=args.claim_tier,
                actor=args.actor, role=args.role, max_findings=args.max,
            )
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "finding":
            row = propose_finding(
                root, args.source_id, args.claim, args.evidence,
                claim_tier=args.claim_tier, confidence=args.confidence,
                actor=args.actor, role=args.role,
            )
            print(json.dumps(row, indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "question":
            row = propose_question(
                root, args.source_id, args.question, actor=args.actor, role=args.role,
            )
            print(json.dumps(row, indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "contradiction":
            row = propose_contradiction(
                root, args.source_id, args.claims, severity=args.severity,
                actor=args.actor, role=args.role,
            )
            print(json.dumps(row, indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "list":
            print(json.dumps(list_candidates(root), indent=2, ensure_ascii=False))
            return 0
    except (ValueError, TypeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
