#!/usr/bin/env python3
"""standing_deliverables.py -- dated standing artifacts on cadence.

Generates the oracle's recurring decision products and lands them in
``Workproduct.nosync/_STANDING/``:

    contradiction-digest   open contradictions ranked by decision-relevance
    rec-scorecard          recommendations adjudicated vs OBSERVED signals
    freshness-report       which authoritative sources are stale vs budget

The binding discipline (the reason this module exists): EVERY claim that would
ship in a deliverable is first routed through ``answer_protocol.preflight``. A
claim whose business object returns **exit 4 (refused -- nothing to stand on)**
is DROPPED entirely (and listed under "needs authority setup"); **exit 3
(caveated)** ships WITH the explicit caveat; **exit 2 (supported)** ships WITH
the mandatory "supported -- authority not confirmed" label; only **exit 0
(grounded)** claims ship clean. No uncited, unauthoritative claim reaches a
leader through a standing deliverable.

The final artifact is published via ``artifact_io`` (lazy import) so the same
policy gate + verified-copy + durable registry that govern every other emitted
artifact also govern these: the deliverable is written into ``_STANDING`` through
``safe_paths.contain`` + a verified copy, the export is policy-gated before any
bytes land, and a row is appended to the ``_STANDING/.registry.jsonl`` ledger.

CLI:
    python3 _tools/standing_deliverables.py --root R gen <kind>
        kind in {contradiction-digest, rec-scorecard, freshness-report}
        [--sensitivity SENS] [--approval REF] [--actor A]

Stdlib only. All companion engine modules (answer_protocol, truth_map,
contradiction, recommendation, artifact_io, policy) are imported lazily so this
file is usable even when one optional companion is unavailable.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import safe_paths
import ledger

_WP = "Workproduct.nosync"
_STANDING = "_STANDING"

KINDS = ("contradiction-digest", "rec-scorecard", "freshness-report")

# Default sensitivity for a standing deliverable: internal (a synthesis of
# internal memory). The caller can raise it; the answer-protocol's per-claim
# sensitivity ceiling is also surfaced inside the document.
_DEFAULT_SENSITIVITY = "internal"


# --------------------------------------------------------------------------- #
# lazy sibling imports
# --------------------------------------------------------------------------- #
def _answer_protocol():
    try:
        import answer_protocol  # type: ignore
        return answer_protocol
    except Exception:  # pragma: no cover - package import fallback
        from . import answer_protocol  # type: ignore
        return answer_protocol


def _artifact_io():
    try:
        import artifact_io  # type: ignore
        return artifact_io
    except Exception:  # pragma: no cover
        from . import artifact_io  # type: ignore
        return artifact_io


def _read_frontmatter_reader():
    """Reuse answer_protocol's tolerant frontmatter reader (no second copy)."""
    ap = _answer_protocol()
    return ap._read_frontmatter  # internal but stable; one reader for the kernel


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iter_notes(folder: Path):
    if not folder.is_dir():
        return
    for p in sorted(folder.glob("*.md")):
        if p.name.startswith("_"):
            continue
        yield p


def _claim_verdict(root: Path, business_object: str):
    """Run the answer protocol for ``business_object`` and return (code, env).

    ``code`` is the exit code (0 grounded / 3 caveated / 4 refused). The caller
    DROPS code==4 claims, caveats code==3, ships code==0.
    """
    ap = _answer_protocol()
    env = ap.preflight(root, business_object)
    return env.exit_code(), env


# --------------------------------------------------------------------------- #
# claim gathering per deliverable
# --------------------------------------------------------------------------- #
def _gather_contradiction_claims(root: Path) -> list[dict]:
    """One claim per OPEN contradiction note, keyed to its business object."""
    read_fm = _read_frontmatter_reader()
    folder = root / "Memory.nosync" / "Contradictions"
    claims: list[dict] = []
    for p in _iter_notes(folder):
        fm = read_fm(p)
        if not fm:
            continue
        status = str(fm.get("status", "")).strip().lower()
        if status not in ("open", "investigating"):
            continue
        obj = (
            fm.get("business_object")
            or fm.get("object")
            or fm.get("title")
            or p.stem
        )
        if isinstance(obj, list):
            obj = obj[0] if obj else p.stem
        claims.append(
            {
                "object": str(obj),
                "note": p.name,
                "severity": str(fm.get("severity", "") or "unknown"),
                "title": str(fm.get("title", "") or p.stem),
                "decision_relevance": str(fm.get("decision_relevance", "") or ""),
            }
        )
    return claims


def _gather_recommendation_claims(root: Path) -> list[dict]:
    """One claim per recommendation note, keyed to its business object."""
    read_fm = _read_frontmatter_reader()
    folder = root / "Memory.nosync" / "Recommendations"
    claims: list[dict] = []
    for p in _iter_notes(folder):
        fm = read_fm(p)
        if not fm:
            continue
        obj = (
            fm.get("business_object")
            or fm.get("object")
            or fm.get("title")
            or p.stem
        )
        if isinstance(obj, list):
            obj = obj[0] if obj else p.stem
        claims.append(
            {
                "object": str(obj),
                "note": p.name,
                "action": str(fm.get("action", "") or fm.get("title", "") or p.stem),
                "status": str(fm.get("status", "") or "open"),
                "expected_signal": str(fm.get("expected_signal", "") or ""),
                "adjudication": str(fm.get("adjudication", "") or ""),
            }
        )
    return claims


def _gather_freshness_claims(root: Path) -> list[dict]:
    """One claim per truth-map row (each names a business object + source)."""
    try:
        import truth_map  # type: ignore
    except Exception:  # pragma: no cover
        from . import truth_map  # type: ignore
    rows = truth_map.load_rows(root)
    claims: list[dict] = []
    for row in rows:
        obj = (
            row.get("business object")
            or row.get("business_object")
            or row.get("object")
            or ""
        )
        if not obj:
            continue
        claims.append(
            {
                "object": str(obj),
                "primary_source": str(row.get("primary source", "") or ""),
                "freshness_budget": str(row.get("freshness budget", "") or ""),
            }
        )
    return claims


# --------------------------------------------------------------------------- #
# render -- build the markdown, routing each claim through the protocol
# --------------------------------------------------------------------------- #
def _verdict_section(env, code: int) -> str:
    ap = _answer_protocol()
    label = {0: "grounded", 2: "supported", 3: "caveated", 4: "refused"}.get(code, "unknown")
    bits = [
        f"verdict: **{label}** (exit {code})",
        f"authority: {env.source_authority or '(none)'}",
        f"freshness: {env.freshness_verdict}",
        f"sensitivity ceiling: {env.sensitivity_ceiling}",
    ]
    if env.confidence is not None:
        bits.append(f"confidence: {env.confidence}")
    if env.refusal_reason:
        bits.append(f"refusal: {env.refusal_reason}")
    return "; ".join(bits)


def build_document(root: Path, kind: str) -> dict:
    """Build the deliverable markdown for ``kind``.

    Returns ``{title, body, kind, sensitivity_ceiling, shipped, dropped,
    caveated}``. Every candidate claim is run through the answer protocol;
    exit-4 claims are DROPPED (counted in ``dropped``), exit-3 claims ship with a
    CAVEAT (counted in ``caveated``), exit-0 claims ship clean.
    """
    root = Path(root)
    if kind not in KINDS:
        raise ValueError(f"unknown deliverable kind {kind!r}; expected {KINDS}")

    if kind == "contradiction-digest":
        candidates = _gather_contradiction_claims(root)
        title = "Contradiction Digest"
        intro = (
            "Open contradictions in company memory, each routed through the "
            "answer protocol. Items whose object has no authority are dropped."
        )
    elif kind == "rec-scorecard":
        candidates = _gather_recommendation_claims(root)
        title = "Recommendation Scorecard"
        intro = (
            "Standing recommendations and their adjudication against observed "
            "signals, each gated by the answer protocol."
        )
    else:  # freshness-report
        candidates = _gather_freshness_claims(root)
        title = "Source Freshness Report"
        intro = (
            "Authoritative sources vs their freshness budget. Rows with no "
            "authority are dropped; stale rows ship caveated."
        )

    shipped: list[str] = []
    dropped_items: list[dict] = []
    dropped = 0
    caveated = 0
    ceilings: list[str] = []

    body_lines = [
        f"# {title}",
        "",
        f"_Generated {_stamp()} -- every claim routed through "
        f"`answer_protocol.preflight`._",
        "",
        intro,
        "",
    ]

    if not candidates:
        body_lines.append("_No candidate items found at generation time._")
    for c in candidates:
        obj = c["object"]
        code, env = _claim_verdict(root, obj)
        ceilings.append(env.sensitivity_ceiling)
        if code == 4:
            # DROP: no authority claims this object. Never ship it.
            dropped += 1
            dropped_items.append(
                {
                    "object": obj,
                    "refusal_reason": env.refusal_reason or "refused",
                    "source_authority": env.source_authority or "",
                }
            )
            continue
        caveat = ""
        if code == 3:
            caveated += 1
            caveat = "  \n  > CAVEAT: source is stale or an open must_resolve "\
                     "contradiction touches this object; treat as provisional."
        elif code == 2:
            caveat = "  \n  > SUPPORTED -- authority not confirmed: evidence "\
                     "exists but the truth-map row is not yet confirmed; an "\
                     "admin can promote it (`./oracle admin truth promote`)."
        shipped.append(obj)
        body_lines.append(f"## {obj}")
        body_lines.append("")
        body_lines.append(_claim_detail(kind, c))
        body_lines.append("")
        body_lines.append(_verdict_section(env, code))
        if caveat:
            body_lines.append(caveat)
        body_lines.append("")

    body_lines.append("---")
    body_lines.append("")
    body_lines.append(
        f"Items shipped: {len(shipped)}; caveated: {caveated}; "
        f"dropped (no authority): {dropped}."
    )
    body_lines.append("")
    if dropped_items:
        body_lines.append("## Needs Authority Setup")
        body_lines.append("")
        body_lines.append(
            "These objects were omitted because the answer protocol found no "
            "usable authority. Add or wire authority before treating them as "
            "Oracle-backed claims."
        )
        body_lines.append("")
        for item in dropped_items:
            body_lines.append(
                f"- `{item['object']}`: {item['refusal_reason']}"
            )
        body_lines.append("")

    ap = _answer_protocol()
    ceiling = ap._strictest(ceilings) if ceilings else _DEFAULT_SENSITIVITY

    return {
        "title": title,
        "kind": kind,
        "body": "\n".join(body_lines) + "\n",
        "sensitivity_ceiling": ceiling,
        "shipped": shipped,
        "dropped_items": dropped_items,
        "dropped": dropped,
        "caveated": caveated,
    }


def _claim_detail(kind: str, c: dict) -> str:
    if kind == "contradiction-digest":
        return (
            f"- note: `{c['note']}`\n"
            f"- severity: {c['severity']}\n"
            f"- decision relevance: {c['decision_relevance'] or '(unstated)'}"
        )
    if kind == "rec-scorecard":
        return (
            f"- note: `{c['note']}`\n"
            f"- action: {c['action']}\n"
            f"- status: {c['status']}\n"
            f"- expected signal: {c['expected_signal'] or '(unstated)'}\n"
            f"- adjudication: {c['adjudication'] or '(pending)'}"
        )
    # freshness-report
    return (
        f"- primary source: {c['primary_source'] or '(none)'}\n"
        f"- freshness budget: {c['freshness_budget'] or '(none)'}"
    )


# --------------------------------------------------------------------------- #
# emit -- land the deliverable in _STANDING via the floor (policy-gated)
# --------------------------------------------------------------------------- #
def _standing_registry(root: Path) -> Path:
    return root / _WP / _STANDING / ".registry.jsonl"


def emit(
    root: Path,
    kind: str,
    *,
    sensitivity: Optional[str] = None,
    approval: Optional[str] = None,
    actor: Optional[str] = None,
    role: str = "user",
    doc: Optional[dict] = None,
) -> dict:
    """Build + publish a standing deliverable into ``Workproduct.nosync/_STANDING``.

    The artifact is written through ``safe_paths.contain`` (so its destination is
    proven to live under ``_STANDING``), the export is policy-gated via
    ``artifact_io`` BEFORE any bytes land, and a metadata row is appended to the
    ``_STANDING/.registry.jsonl`` ledger. Returns a report dict.

    ``doc`` lets a sibling generator (briefing.py) publish a prebuilt document
    through this same gate+registry pipeline; it must carry the
    ``build_document`` shape (title/body/sensitivity_ceiling/shipped/dropped).
    """
    root = Path(root).resolve()
    doc = doc or build_document(root, kind)
    sens = sensitivity or doc["sensitivity_ceiling"] or _DEFAULT_SENSITIVITY

    name = f"{_today()}_{kind}.md"
    # Contained destination under _STANDING (proves containment even though the
    # name derives from a fixed kind enum + date).
    dest = safe_paths.contain(root, f"{_STANDING}/{name}", base=_WP)

    # Stage the body in a temp file we own, then route through the policy gate +
    # verified-copy. The staging file lives in an OS temp dir (not user-derived).
    fd, tmp_name = tempfile.mkstemp(prefix="standing-", suffix=".md")
    staged = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:  # safe_paths-internal: temp staging
            f.write(doc["body"])
            f.flush()
            os.fsync(f.fileno())

        # POLICY GATE FIRST -- nothing lands in _STANDING until the export is
        # authorized. Reuse artifact_io's gate (lazy) so deliverables obey the
        # same export policy as every other emitted artifact.
        aio = _artifact_io()
        canonical_rel = f"{_WP}/{_STANDING}/{name}"
        aio._gate_export(
            root,
            sensitivity=sens,
            classification=sens,
            approval=approval,
            actor=actor or "",
            role=role,
            destination=canonical_rel,
            purpose=f"standing-deliverable:{kind}",
        )

        # Verified copy into the contained _STANDING destination (gate passed).
        sha = aio._verified_copy_preserving(staged, dest)
    finally:
        if staged.exists():
            try:
                staged.unlink()
            except OSError:
                pass

    # build_document carries shipped as a list; sibling generators (briefing)
    # may carry a count directly.
    shipped_n = len(doc["shipped"]) if isinstance(doc.get("shipped"), (list, tuple)) else int(doc.get("shipped") or 0)

    row = {
        "sha256_12": sha,
        "artifact_name": name,
        "kind": kind,
        "created_at": _stamp(),
        "sensitivity": sens,
        "items_shipped": shipped_n,
        "items_caveated": doc["caveated"],
        "items_dropped": doc["dropped"],
        "actor": actor or "",
        "canonical_location": str(dest.relative_to(root)),
    }
    drop_id = ledger.append(_standing_registry(root), row, id_prefix="STD")

    return {
        "drop_id": drop_id,
        "path": str(dest.relative_to(root)),
        "kind": kind,
        "sensitivity": sens,
        "shipped": shipped_n,
        "caveated": doc["caveated"],
        "dropped": doc["dropped"],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate dated standing deliverables (answer-protocol gated)"
    )
    ap.add_argument("--root", default=".", help="oracle root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("gen", help="generate + publish a standing deliverable")
    p_gen.add_argument("kind", choices=KINDS)
    p_gen.add_argument("--sensitivity", default=None)
    p_gen.add_argument("--approval", default=None)
    p_gen.add_argument("--actor", default=None)
    p_gen.add_argument("--role", default="user")

    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    try:
        if args.cmd == "gen":
            report = emit(
                root,
                args.kind,
                sensitivity=args.sensitivity,
                approval=args.approval,
                actor=args.actor,
                role=args.role,
            )
            import json

            print(json.dumps(report, indent=2))
            return 0
    except (ValueError, PermissionError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
