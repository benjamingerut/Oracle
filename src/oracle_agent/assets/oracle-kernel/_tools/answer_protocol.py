#!/usr/bin/env python3
"""answer_protocol.py -- the material-answer envelope + refusal gate.

Before any **material answer** (a claim, number, conclusion, or recommendation
the leader could act on) the oracle runs this protocol. It operationalizes
``ANALYTIC-DOCTRINE.md`` / ``ANSWER-PROTOCOL.md`` and is the machine-checked
chokepoint named by the doctrine-binding rule.

``preflight(root, business_object, question=None) -> Envelope`` runs the 8
ordered checks and returns an Envelope. The CLI (``oracle answer``) turns the
Envelope into an exit code on the **graduated authority ladder** -- the oracle
answers whenever it has *something*, labeled with exactly what that something
is, and refuses only when it has nothing:

    exit 0  grounded   CONFIRMED row, authoritative primary source, fresh
                       ingested evidence, no open must_resolve contradiction.
    exit 2  supported  evidence exists but authority is not confirmed: a draft
                       row with fresh resolving evidence, OR object-matching
                       candidate evidence where the row/primary is missing.
                       The answer MUST carry the label "supported -- authority
                       not confirmed" and the envelope's suggested_fix.
    exit 3  caveated   authority present but the evidence is stale/undated, OR
                       an open must_resolve contradiction touches the object,
                       OR a real authority exists with no ingested evidence.
    exit 4  refused    nothing: no truth-map row AND no ingested evidence for
                       the object. refusal_reason names why; suggested_fix
                       lists the exact commands that change the verdict.

Precedence: refused (4) > caveated (3) > supported (2) > grounded (0). The
envelope's ``authority_state`` (confirmed|draft|candidate|none) says which rung
of the ladder produced the verdict.

The Envelope fields are the SAME list as the 8 checks in ANSWER-PROTOCOL.md and
must match it exactly:

    business_object        str
    truth_map_row          dict | None
    source_authority       str | None
    freshness_verdict      "fresh" | "stale" | "unknown"
    sensitivity_ceiling    str
    confidence             float (0..1) | None
    disconfirmers          list
    open_contradictions    list
    refusal_reason         str | None

Honesty note: this is a *tool the agent must call*, not a harness-level
interceptor. ``standing_deliverables.py`` routes every emitted claim through
``preflight`` and drops any claim returning exit 4.

Stdlib only. This module reads source/contradiction notes via a small
self-contained frontmatter reader so it does not hard-depend on sibling engine
modules that may still be building; the truth-map is read via ``truth_map``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:  # bare import (conftest puts _tools on sys.path); package fallback
    import truth_map as _truth_map
except Exception:  # pragma: no cover - package import path
    from . import truth_map as _truth_map  # type: ignore


__all__ = [
    "Envelope",
    "ResearchEnvelope",
    "preflight",
    "research_preflight",
    "verdict_exit_code",
    "gather_sources",
    "gather_object_evidence",
    "newest_as_of",
    "freshness_for",
    "FRESHNESS_FRESH",
    "FRESHNESS_STALE",
    "FRESHNESS_UNKNOWN",
    "EXIT_GROUNDED",
    "EXIT_SUPPORTED",
    "EXIT_CAVEATED",
    "EXIT_REFUSED",
    "AUTHORITY_CONFIRMED",
    "AUTHORITY_DRAFT",
    "AUTHORITY_CANDIDATE",
    "AUTHORITY_NONE",
]

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_UNKNOWN = "unknown"

EXIT_GROUNDED = 0
EXIT_SUPPORTED = 2
EXIT_CAVEATED = 3
EXIT_REFUSED = 4

# authority_state rungs (the ladder).
AUTHORITY_CONFIRMED = "confirmed"
AUTHORITY_DRAFT = "draft"
AUTHORITY_CANDIDATE = "candidate"
AUTHORITY_NONE = "none"

# Confidence damping per rung: a draft row is trusted slightly less than a
# confirmed one; candidate evidence (no wired authority) less again.
_DRAFT_CONFIDENCE_FACTOR = 0.85
_CANDIDATE_CONFIDENCE_FACTOR = 0.7

# Sensitivity labels ordered weakest -> strictest (stricter-row-wins).
_SENSITIVITY_ORDER = ["public", "internal", "confidential", "restricted", "secret"]
_DEFAULT_SENSITIVITY = "internal"

# Contradiction statuses that count as "open" (still in conflict).
_OPEN_CONTRADICTION_STATUSES = {"open", "investigating"}
# How the contradiction classifier marks a contradiction that BLOCKS a clean
# answer. answer_protocol treats any of these (case/format tolerant) as the
# "must_resolve" class.
_MUST_RESOLVE_TOKENS = {"must_resolve", "must-resolve", "critical", "high"}


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #
@dataclass
class Envelope:
    """The answer-protocol result. Fields mirror the 8 checks exactly."""

    business_object: str
    truth_map_row: Optional[dict] = None
    source_authority: Optional[str] = None
    freshness_verdict: str = FRESHNESS_UNKNOWN
    sensitivity_ceiling: str = _DEFAULT_SENSITIVITY
    confidence: Optional[float] = None
    disconfirmers: list = field(default_factory=list)
    open_contradictions: list = field(default_factory=list)
    refusal_reason: Optional[str] = None
    # v2 ladder fields
    authority_state: str = AUTHORITY_NONE
    evidence_count: int = 0
    suggested_fix: list = field(default_factory=list)
    # Cited source ids (P8-T7): the Source notes that evidence this answer. Used
    # ONLY for the additive ``answer_event.source_ids`` telemetry field; it never
    # affects the verdict. EXCLUDED from to_dict() so the answer-protocol checklist
    # contract (ANSWER-PROTOCOL.md) stays exactly the 8-check field set -- this is
    # telemetry, not a protocol field. Metadata-tier (ids, never content).
    cited_source_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("cited_source_ids", None)
        return d

    def exit_code(self) -> int:
        return verdict_exit_code(self)


@dataclass
class ResearchEnvelope:
    """Exploratory public-research preflight.

    This is deliberately separate from the authoritative material-answer
    envelope above. It authorizes a *workflow* (go research public material with
    citations) without claiming Oracle truth-map authority for the answer.
    """

    question: str
    mode: str = "exploratory_public_research"
    verdict: str = "allowed"
    environment: str = "external"
    context_sensitivity: str = "public"
    includes_company_context: bool = False
    processing_verdict: str = "allow"
    constraints: list = field(default_factory=list)
    refusal_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def exit_code(self) -> int:
        return EXIT_REFUSED if self.refusal_reason else EXIT_GROUNDED


def verdict_exit_code(env: Envelope) -> int:
    """Map an Envelope to its ladder exit code.

    refused (4) > caveated (3) > supported (2) > grounded (0).
    """
    if env.refusal_reason:
        return EXIT_REFUSED
    # Caveat if the evidence is stale/undated, or any surfaced contradiction is
    # an open must_resolve one touching the object.
    if env.freshness_verdict in (FRESHNESS_STALE, FRESHNESS_UNKNOWN):
        return EXIT_CAVEATED
    if any(_is_must_resolve(c) for c in env.open_contradictions):
        return EXIT_CAVEATED
    # Fresh evidence but authority below 'confirmed' -> supported (labeled).
    if env.authority_state != AUTHORITY_CONFIRMED:
        return EXIT_SUPPORTED
    return EXIT_GROUNDED


def _policy_check_processing(sensitivity: str, environment: str) -> str:
    """Policy check with a conservative fallback.

    ``policy.py`` is a floor module in the shipped kernel. This fallback exists
    only so a partially imported answer protocol fails closed for non-public
    context instead of treating unknown material as public.
    """
    try:
        import policy  # type: ignore
    except Exception:
        try:  # pragma: no cover - package fallback
            from . import policy  # type: ignore
        except Exception:
            return "allow" if str(sensitivity).strip().lower() == "public" else "deny"
    try:
        return str(policy.check_processing(sensitivity, environment))
    except Exception:
        return "deny"


def research_preflight(
    root,
    question: str,
    *,
    context_sensitivity: str = "public",
    includes_company_context: bool = False,
) -> ResearchEnvelope:
    """Authorize exploratory public research without truth-map authority.

    This path is for questions like "research this market/topic for me" where
    the agent is expected to gather public sources and clearly label the result
    as non-authoritative until it is converted into Oracle Sources/Findings and
    wired through ``TRUTH-MAP.md``.

    If the workflow would send private company context to an external service,
    the existing processing matrix decides whether the research step may
    proceed. In the default matrix, anything above public refuses.
    """
    q = str(question or "").strip()
    env = ResearchEnvelope(question=q)
    env.includes_company_context = bool(includes_company_context)
    env.context_sensitivity = (
        str(context_sensitivity or "public").strip().lower() or "public"
    )

    if not q:
        env.verdict = "refused"
        env.processing_verdict = "deny"
        env.refusal_reason = "no-question"
        return env

    effective_sensitivity = env.context_sensitivity if env.includes_company_context else "public"
    env.processing_verdict = _policy_check_processing(effective_sensitivity, env.environment)
    env.context_sensitivity = effective_sensitivity

    env.constraints = [
        "Do not present exploratory research as an Oracle-authoritative answer.",
        "Use public sources and cite them in the final answer.",
        "Do not send non-public Oracle memory or private company context externally.",
        "Convert durable company-relevant conclusions into Sources/Findings and truth-map authority before using them as material Oracle claims.",
    ]

    if env.processing_verdict == "deny":
        env.verdict = "refused"
        env.refusal_reason = "external-processing-denied"
    return env


# --------------------------------------------------------------------------- #
# tiny self-contained frontmatter reader (no sibling dependency)
# --------------------------------------------------------------------------- #
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


def _read_frontmatter(path: Path) -> dict:
    """Parse a note's leading ``--- ... ---`` frontmatter block into a flat dict.

    Deliberately minimal and forgiving: ``key: value`` lines plus simple
    block ``- item`` lists. Used only to read source/contradiction metadata for
    the answer protocol; never used to *write* anything. Unparseable notes
    yield an empty dict rather than raising.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    body = m.group(1)
    data: dict = {}
    cur_list_key: Optional[str] = None
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        # list item belonging to the most recent "key:" with empty value
        stripped = line.strip()
        if stripped.startswith("- ") and cur_list_key is not None:
            # The "key:" line pre-set the value to ""; the first "- item"
            # turns it into a real list (otherwise block lists never parse).
            if not isinstance(data.get(cur_list_key), list):
                data[cur_list_key] = []
            data[cur_list_key].append(_unquote(stripped[2:].strip()))
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if val == "":
                # could start a block list
                data[key] = ""
                cur_list_key = key
            else:
                data[key] = _unquote(val)
                cur_list_key = None
        else:
            cur_list_key = None
    return data


def read_frontmatter(path) -> dict:
    """Public alias: parse a note's frontmatter (forgiving, read-only)."""
    return _read_frontmatter(Path(path))


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
        return v[1:-1]
    return v


def _iter_notes(folder: Path):
    if not folder.is_dir():
        return
    for p in sorted(folder.glob("*.md")):
        if p.name.startswith("_"):  # _CONTEXT.md / _template.md
            continue
        yield p


# --------------------------------------------------------------------------- #
# freshness
# --------------------------------------------------------------------------- #
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdwy])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}


def _parse_budget_seconds(budget: str) -> Optional[int]:
    """Parse a freshness budget like ``30d`` / ``24h`` / ``7d`` into seconds.

    Non-duration budgets (``review on change``, ``document-specific``, empty)
    return ``None`` -- the protocol cannot time-compare them, so freshness is
    driven purely by whether a source ``as_of`` exists.
    """
    if not budget:
        return None
    m = _DURATION_RE.match(budget)
    if not m:
        return None
    qty = int(m.group(1))
    unit = m.group(2).lower()
    return qty * _UNIT_SECONDS[unit]


def _parse_as_of(value: str) -> Optional[datetime]:
    """Parse a source ``as_of`` timestamp (ISO-8601 date or datetime) to UTC."""
    if not value:
        return None
    v = str(value).strip()
    # Accept trailing 'Z'.
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    fmts = None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        # Try a few common explicit formats.
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(v, fmt)
                break
            except ValueError:
                dt = None  # type: ignore
        else:
            return None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _freshness_verdict(as_of: Optional[datetime], budget: str, now: datetime) -> str:
    """fresh | stale | unknown from a source as_of and the row's budget."""
    if as_of is None:
        return FRESHNESS_UNKNOWN
    budget_s = _parse_budget_seconds(budget)
    if budget_s is None:
        # No time-comparable budget but we DO have a timestamp -> treat as fresh
        # (we cannot prove staleness; 'review on change' is event-driven).
        return FRESHNESS_FRESH
    age = (now - as_of).total_seconds()
    if age < 0:
        age = 0
    return FRESHNESS_FRESH if age <= budget_s else FRESHNESS_STALE


# --------------------------------------------------------------------------- #
# sensitivity
# --------------------------------------------------------------------------- #
def _sensitivity_rank(label: str) -> int:
    try:
        return _SENSITIVITY_ORDER.index(str(label).strip().lower())
    except ValueError:
        return _SENSITIVITY_ORDER.index(_DEFAULT_SENSITIVITY)


def _strictest(labels) -> str:
    best = -1
    chosen = _DEFAULT_SENSITIVITY
    for lab in labels:
        if not lab:
            continue
        r = _sensitivity_rank(lab)
        if r > best:
            best = r
            chosen = _SENSITIVITY_ORDER[r]
    return chosen


# --------------------------------------------------------------------------- #
# sources + contradictions readers (graceful when folders absent)
# --------------------------------------------------------------------------- #
def _row_object_match(note_object: str, business_object: str) -> bool:
    return _truth_map.normalize_object(note_object) == _truth_map.normalize_object(
        business_object
    )


def _as_list(value) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def _norm_authority(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _source_object_values(fm: dict) -> list:
    values: list = []
    for key in ("business_object", "object", "authoritative_for"):
        values.extend(_as_list(fm.get(key)))
    return values


def _source_object_match(fm: dict, business_object: str) -> bool:
    return any(_row_object_match(c, business_object) for c in _source_object_values(fm))


def source_match_keys(fm: dict) -> dict:
    """Precomputed match keys for one Source frontmatter dict.

    Returns ``{"id_keys", "label_keys", "objects"}`` (sets of normalized
    strings) reproducing exactly what ``_source_matches_authority`` and
    ``_source_object_match`` test: a primary source matches when it equals an
    id key, OR equals a label key AND the normalized object is claimed.
    ``source_catalog`` stores these per note so the hot paths do set lookups
    instead of re-parsing every note. If you change the semantics here (or in
    the helpers it mirrors), bump ``source_catalog.PARSE_VERSION``.
    """
    id_keys: set[str] = set()
    for key in ("id", "source_id"):
        id_keys.update(_norm_authority(v) for v in _as_list(fm.get(key)))
    id_keys.discard("")
    label_keys: set[str] = set()
    for key in ("authority_id", "primary_source", "source_system", "connector", "title"):
        label_keys.update(_norm_authority(v) for v in _as_list(fm.get(key)))
    label_keys.discard("")
    objects = {_truth_map.normalize_object(str(v)) for v in _source_object_values(fm)}
    objects.discard("")
    return {"id_keys": id_keys, "label_keys": label_keys, "objects": objects}


def _source_snapshot(root: Path):
    """Catalog-backed Sources snapshot, or None to use the direct folder walk.

    The catalog is an accelerator, never a gate: any failure degrades to the
    walk so answering cannot break because a derived cache did.
    """
    try:
        import source_catalog  # type: ignore
    except Exception:  # pragma: no cover - package import path
        try:
            from . import source_catalog  # type: ignore
        except Exception:
            return None
    try:
        return source_catalog.snapshot(root)
    except Exception:
        return None


def _source_matches_authority(fm: dict, primary_source: str, business_object: str) -> bool:
    """True iff a Source note can evidence the truth-map primary authority.

    A truth-map primary source may name a concrete Source id directly, or it may
    name an authority label such as a source system / connector. Label matches
    must also claim the same business object via business_object/object/
    authoritative_for, so a fresh but unrelated note cannot ground the row.
    """
    primary = _norm_authority(primary_source)
    if not primary:
        return False

    id_values = []
    for key in ("id", "source_id"):
        id_values.extend(_as_list(fm.get(key)))
    if any(_norm_authority(v) == primary for v in id_values):
        return True

    authority_values = []
    for key in (
        "authority_id",
        "primary_source",
        "source_system",
        "connector",
        "title",
    ):
        authority_values.extend(_as_list(fm.get(key)))

    if not any(_norm_authority(v) == primary for v in authority_values):
        return False
    return _source_object_match(fm, business_object)


def _gather_sources(root: Path, business_object: str, primary_source: str, *, snap=None) -> list[dict]:
    """Return Source frontmatter dicts matching object AND authority of record.

    A Source may match by concrete Source id, or by an exact authority label
    (``authority_id``, ``primary_source``, ``source_system``, ``connector`` or
    ``title``) plus a matching object claim. Notes that merely share the object
    but do not match the row's primary source are ignored.
    """
    primary = _norm_authority(primary_source)
    if not primary:
        return []
    if snap is None:
        snap = _source_snapshot(root)
    if snap is not None:
        bo = _truth_map.normalize_object(business_object)
        matched: dict[str, dict] = {}
        for e in snap.by_id_key.get(primary, ()):
            matched[e["name"]] = e
        for e in snap.by_label_key.get(primary, ()):
            if bo in e["objects"]:
                matched.setdefault(e["name"], e)
        return [matched[n]["fm"] for n in sorted(matched) if matched[n]["fm"]]
    out: list[dict] = []
    folder = root / "Memory.nosync" / "Sources"
    for p in _iter_notes(folder):
        fm = _read_frontmatter(p)
        if not fm:
            continue
        if _source_matches_authority(fm, primary_source, business_object):
            out.append(fm)
    return out


def _source_ids_of(sources: list[dict]) -> list[str]:
    """Cited source ids from a list of Source frontmatters (P8-T7).

    Prefers the index-side ``source_id`` (the captured-sha256[:12] the knowledge
    index keys chunks by, so the answer_event ids line up with the retrieval
    ledger's top_source_ids), then falls back to the note's ``source_id``/``id``.
    De-dupes, preserves order. Metadata only -- never content.
    """
    out: list[str] = []
    seen: set[str] = set()
    for fm in sources or []:
        if not isinstance(fm, dict):
            continue
        full = str(fm.get("captured_sha256") or fm.get("sha256") or "").strip()
        sid = (
            full[:12]
            if full
            else str(
                fm.get("sha256_12")
                or fm.get("source_id")
                or fm.get("id")
                or ""
            ).strip()
        )
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _gather_object_evidence(root: Path, business_object: str, *, snap=None) -> list[dict]:
    """All Source notes claiming ``business_object`` regardless of authority.

    This is the 'candidate evidence' reader behind the supported (exit 2) rung:
    an ingested Source that names the object via business_object/object/
    authoritative_for counts, even when no truth-map primary source is wired.
    """
    if snap is None:
        snap = _source_snapshot(root)
    if snap is not None:
        bo = _truth_map.normalize_object(business_object)
        hits = snap.by_object.get(bo, ())
        return [e["fm"] for e in sorted(hits, key=lambda e: e["name"]) if e["fm"]]
    out: list[dict] = []
    folder = root / "Memory.nosync" / "Sources"
    for p in _iter_notes(folder):
        fm = _read_frontmatter(p)
        if not fm:
            continue
        if _source_object_match(fm, business_object):
            out.append(fm)
    return out


# Public evidence helpers (consumed by truth_map.validate_rows, review_queue,
# briefing -- keeps the matching semantics in ONE module).
def gather_sources(root, business_object: str, primary_source: str, *, snap=None) -> list[dict]:
    """Public: Source notes resolving to ``primary_source`` for the object.

    ``snap`` lets a batch caller (``truth_map.validate_rows``) fetch ONE
    catalog snapshot via ``source_snapshot`` and reuse it across many rows,
    instead of paying the staleness stat-sweep per call.
    """
    return _gather_sources(Path(root), business_object, primary_source, snap=snap)


def gather_object_evidence(root, business_object: str, *, snap=None) -> list[dict]:
    """Public: Source notes claiming the object, authority wired or not."""
    return _gather_object_evidence(Path(root), business_object, snap=snap)


def source_snapshot(root):
    """Public: the catalog snapshot for batch gathers (None = use the walk)."""
    return _source_snapshot(Path(root))


def newest_as_of(sources: list[dict]):
    """Public: newest as_of/updated/created timestamp across source dicts."""
    return _newest_as_of(sources)


def freshness_for(as_of, budget: str, now=None) -> str:
    """Public: fresh|stale|unknown for an as_of under a freshness budget."""
    if now is None:
        now = datetime.now(timezone.utc)
    return _freshness_verdict(as_of, budget, now)


def _gather_open_contradictions(root: Path, business_object: str) -> list[dict]:
    """Return open contradictions touching ``business_object``.

    Each result carries the note's frontmatter plus a normalized ``severity``
    and a derived ``must_resolve`` flag. Graceful when the folder is absent.
    """
    out: list[dict] = []
    folder = root / "Memory.nosync" / "Contradictions"
    for p in _iter_notes(folder):
        fm = _read_frontmatter(p)
        if not fm:
            continue
        status = str(fm.get("status", "")).strip().lower()
        if status not in _OPEN_CONTRADICTION_STATUSES:
            continue
        # Does it touch this object?
        touches = False
        for key in ("business_object", "object", "decision_relevance", "title"):
            v = fm.get(key)
            vals = v if isinstance(v, list) else [v]
            for item in vals:
                if item and _truth_map.normalize_object(
                    business_object
                ) in _truth_map.normalize_object(str(item)):
                    touches = True
                    break
            if touches:
                break
        # claims_in_conflict often names the object too.
        if not touches:
            cic = fm.get("claims_in_conflict")
            cic_vals = cic if isinstance(cic, list) else [cic]
            for item in cic_vals:
                if item and _truth_map.normalize_object(
                    business_object
                ) in _truth_map.normalize_object(str(item)):
                    touches = True
                    break
        if not touches:
            continue
        record = dict(fm)
        record["_note"] = p.name
        record["must_resolve"] = _is_must_resolve(fm)
        out.append(record)
    return out


def _is_must_resolve(contradiction: dict) -> bool:
    """True iff a contradiction is in the blocking 'must_resolve' class.

    The contradiction adjudicator classifies into
    must_resolve|bounded_residual|watch|schema_debt; until that field is set we
    fall back to high/critical severity (which the schema marks as the strongest
    classes). Both signals are honored, tolerant of casing/hyphenation.
    """
    if not isinstance(contradiction, dict):
        return False
    klass = str(
        contradiction.get("contradiction_class")
        or contradiction.get("classification")
        or ""
    ).strip().lower().replace("-", "_")
    if klass in {"must_resolve"}:
        return True
    if klass in {"bounded_residual", "watch", "schema_debt"}:
        return False
    sev = str(contradiction.get("severity", "")).strip().lower()
    return sev in {"high", "critical"}


def _max_confidence_from_sources(sources: list[dict]) -> Optional[float]:
    """Highest stated source confidence, or None when no source states one."""
    best: Optional[float] = None
    for s in sources:
        v = s.get("confidence")
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        f = max(0.0, min(1.0, f))
        if best is None or f > best:
            best = f
    return best


def _collect_disconfirmers(sources: list[dict]) -> list[str]:
    out: list[str] = []
    for s in sources:
        for key in ("disconfirmer", "disconfirmers"):
            v = s.get(key)
            if isinstance(v, list):
                out.extend([str(x) for x in v if x])
            elif v:
                out.append(str(v))
    # de-dup preserving order
    seen = set()
    result = []
    for d in out:
        if d not in seen:
            seen.add(d)
            result.append(d)
    return result


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #
def preflight(root, business_object: str, question: Optional[str] = None) -> Envelope:
    """Run the 8-step answer protocol for ``business_object`` under ``root``.

    Returns an :class:`Envelope`. Does NOT exit; the CLI translates the Envelope
    into an exit code via :func:`verdict_exit_code`.

    Steps:
      1. business object named            -> business_object
      2. truth-map row resolved           -> truth_map_row   (None => refuse 4)
      3. source authority identified      -> source_authority(empty/TBD => 4)
      4. freshness verdict vs budget      -> freshness_verdict
      5. sensitivity ceiling computed     -> sensitivity_ceiling
      6. confidence as range/None         -> confidence
      7. disconfirmers named              -> disconfirmers
      8. open contradictions surfaced     -> open_contradictions
    """
    root = Path(root)
    now = datetime.now(timezone.utc)

    bo = (business_object or "").strip()
    env = Envelope(business_object=bo)

    # Check 1: a business object must be named.
    if not bo:
        env.refusal_reason = "no-business-object"
        return env

    # Check 2: resolve the truth-map row.
    rows = _truth_map.load_rows(root)
    row = _truth_map.resolve(bo, rows=rows)
    env.truth_map_row = row
    if row is None:
        # No row claims authority. Ladder: ingested candidate evidence still
        # SUPPORTS a labeled answer (exit 2); nothing at all refuses (exit 4)
        # with the exact commands that change the verdict.
        return _candidate_or_refuse(
            env, root, bo, question, now, refusal="no-authority-bootstrap"
        )

    # Check 3: source authority of record (the row's primary source).
    primary = row.get("primary source", "")
    if not _truth_map.primary_source_is_authoritative(primary):
        # Row exists but its primary source is still TBD/empty. Same ladder:
        # candidate evidence supports, nothing refuses.
        return _candidate_or_refuse(env, root, bo, question, now, refusal="no-authority")
    env.source_authority = primary
    status = str(row.get("status", "")).strip().lower()
    env.authority_state = (
        AUTHORITY_CONFIRMED if status == "confirmed" else AUTHORITY_DRAFT
    )

    # Gather ingested sources for this authority+object pair (may be empty
    # pre-ingest, which caveats rather than grounds).
    sources = _gather_sources(root, bo, primary)
    env.evidence_count = len(sources)
    env.cited_source_ids = _source_ids_of(sources)

    # Check 4: freshness verdict vs the row's budget.
    budget = row.get("freshness budget", "")
    as_of = _newest_as_of(sources)
    env.freshness_verdict = _freshness_verdict(as_of, budget, now)

    # Check 5: sensitivity ceiling (stricter-row-wins across row+sources+question).
    env.sensitivity_ceiling = _compute_ceiling(row, sources, question)

    # Check 6: confidence stated as a band/None.
    # No ingested evidence yet -> confidence None, freshness unknown (caveat).
    if not sources:
        env.confidence = None
        if env.freshness_verdict == FRESHNESS_FRESH:
            env.freshness_verdict = FRESHNESS_UNKNOWN
        env.suggested_fix = _fix_ingest(bo, primary)
    else:
        conf = _max_confidence_from_sources(sources)
        # A draft row lowers stated confidence; a confirmed row keeps it.
        if conf is not None and env.authority_state != AUTHORITY_CONFIRMED:
            conf = round(conf * _DRAFT_CONFIDENCE_FACTOR, 4)
        env.confidence = conf
        if env.authority_state != AUTHORITY_CONFIRMED:
            env.suggested_fix = _fix_promote(bo)

    # Check 7: disconfirmers + resolving sources.
    env.disconfirmers = _collect_disconfirmers(sources)

    # Check 8: open contradictions touching the object.
    env.open_contradictions = _gather_open_contradictions(root, bo)

    return env


def _candidate_or_refuse(
    env: Envelope,
    root: Path,
    bo: str,
    question: Optional[str],
    now: datetime,
    *,
    refusal: str,
) -> Envelope:
    """Shared no-row / TBD-source tail of the ladder.

    Candidate evidence (object-matching ingested Sources) -> supported (exit 2),
    labeled and capped; no evidence at all -> refused (exit 4) with the exact
    commands that change the verdict.
    """
    evidence = _gather_object_evidence(root, bo)
    env.open_contradictions = _gather_open_contradictions(root, bo)
    env.sensitivity_ceiling = _compute_ceiling(env.truth_map_row or {}, evidence, question)
    env.evidence_count = len(evidence)
    env.cited_source_ids = _source_ids_of(evidence)

    if not evidence:
        env.authority_state = AUTHORITY_NONE
        env.refusal_reason = refusal
        env.freshness_verdict = FRESHNESS_UNKNOWN
        env.confidence = None
        env.suggested_fix = _fix_bootstrap(bo)
        return env

    env.authority_state = AUTHORITY_CANDIDATE
    as_of = _newest_as_of(evidence)
    # No comparable budget on this rung: a dated source reads fresh, an undated
    # one reads unknown (which caveats -- still answerable, with the caveat).
    env.freshness_verdict = _freshness_verdict(as_of, "", now)
    conf = _max_confidence_from_sources(evidence)
    env.confidence = (
        round(conf * _CANDIDATE_CONFIDENCE_FACTOR, 4) if conf is not None else None
    )
    env.disconfirmers = _collect_disconfirmers(evidence)
    env.suggested_fix = _fix_wire_authority(bo, evidence)
    return env


def _fix_bootstrap(bo: str) -> list:
    return [
        f'./oracle ingest <evidence file or folder>  # evidence about "{bo}"',
        f'./oracle admin truth propose --object "{bo}" --source "<authority of record>"',
        f'./oracle answer --object "{bo}"  # re-run: verdict upgrades to supported',
    ]


def _fix_ingest(bo: str, primary: str) -> list:
    return [
        f'./oracle ingest <evidence file or folder>  # evidence from "{primary}" about "{bo}"',
        f'./oracle answer --object "{bo}"  # re-run after ingest',
    ]


def _fix_wire_authority(bo: str, evidence: list) -> list:
    src = ""
    for fm in evidence:
        for key in ("source_system", "connector", "primary_source", "title"):
            v = fm.get(key)
            if v:
                src = str(v)
                break
        if src:
            break
    return [
        f'./oracle admin truth propose --object "{bo}" --source "{src or "<authority of record>"}"',
        f'./oracle admin truth promote --object "{bo}" --actor "<admin>"  # confirms authority',
    ]


def _fix_promote(bo: str) -> list:
    return [
        f'./oracle admin truth promote --object "{bo}" --actor "<admin>"  # draft -> confirmed',
    ]


def _newest_as_of(sources: list[dict]) -> Optional[datetime]:
    best: Optional[datetime] = None
    for s in sources:
        for key in ("as_of", "as-of", "updated", "created"):
            dt = _parse_as_of(str(s.get(key, "")))
            if dt is not None:
                if best is None or dt > best:
                    best = dt
                break
    return best


def _compute_ceiling(row: dict, sources: list[dict], question: Optional[str]) -> str:
    labels = []
    if row:
        labels.append(row.get("sensitivity", ""))
    for s in sources:
        labels.append(s.get("sensitivity", ""))
    # A question is treated at the default floor unless a source/row raises it.
    return _strictest(labels)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _verdict_label(code: int) -> str:
    return {
        EXIT_GROUNDED: "grounded",
        EXIT_SUPPORTED: "supported",
        EXIT_CAVEATED: "caveated",
        EXIT_REFUSED: "refused",
    }.get(code, "unknown")


def render_md(env: Envelope) -> str:
    code = env.exit_code()
    lines = [
        f"# Answer preflight: {env.business_object}",
        "",
        f"- **verdict**: {_verdict_label(code)} (exit {code})",
        f"- **authority**: {env.authority_state} ({env.evidence_count} evidence source(s))",
        f"- **source authority**: {env.source_authority or '(none)'}",
        f"- **freshness**: {env.freshness_verdict}",
        f"- **sensitivity ceiling**: {env.sensitivity_ceiling}",
        f"- **confidence**: {env.confidence if env.confidence is not None else 'null'}",
    ]
    if code == EXIT_SUPPORTED:
        lines.append(
            "- **required label**: supported -- authority not confirmed; "
            "state this in the answer"
        )
    if env.refusal_reason:
        lines.append(f"- **refusal reason**: {env.refusal_reason}")
    if env.suggested_fix:
        lines.append("- **to upgrade this verdict**:")
        for f in env.suggested_fix:
            lines.append(f"  - `{f}`")
    if env.disconfirmers:
        lines.append("- **disconfirmers**:")
        for d in env.disconfirmers:
            lines.append(f"  - {d}")
    if env.open_contradictions:
        lines.append("- **open contradictions**:")
        for c in env.open_contradictions:
            note = c.get("_note", c.get("id", "?"))
            mr = " (must_resolve)" if c.get("must_resolve") else ""
            lines.append(f"  - {note}{mr}")
    return "\n".join(lines)


def render_research_md(env: ResearchEnvelope) -> str:
    lines = [
        f"# Research preflight: {env.question}",
        "",
        f"- **verdict**: {env.verdict} (exit {env.exit_code()})",
        f"- **mode**: {env.mode}",
        f"- **environment**: {env.environment}",
        f"- **context sensitivity**: {env.context_sensitivity}",
        f"- **processing verdict**: {env.processing_verdict}",
        f"- **includes company context**: {str(env.includes_company_context).lower()}",
    ]
    if env.refusal_reason:
        lines.append(f"- **refusal reason**: {env.refusal_reason}")
    if env.constraints:
        lines.append("- **constraints**:")
        for c in env.constraints:
            lines.append(f"  - {c}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# answer telemetry -- the master efficiency signal
# --------------------------------------------------------------------------- #
ANSWER_EVENT_LEDGER = "Meta.nosync/ledgers/answer_event.jsonl"


def log_answer_event(root, env: Envelope, *, interface: str = "cli") -> Optional[str]:
    """Append one metadata-only ``answer_event`` row for a preflighted answer.

    Row shape: {drop_id, ts, kind, business_object, exit_code, authority_state,
    interface} -- the object and the verdict, never the claim text. The value
    scorecard reads this ledger to compute the grounded-rate (the 4 -> 2 -> 0
    migration is knowledge growth made visible). Logged at the CLI edge so the
    pure ``preflight`` stays read-only for internal claim-gating callers
    (standing deliverables preflight dozens of claims per document; their
    verdicts are already visible in the artifacts they emit).

    Best-effort by design: telemetry must never break an answer. Returns the
    drop_id, or None when the ledger module is unavailable or the write fails.
    """
    try:
        import ledger as _ledger  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import ledger as _ledger  # type: ignore
        except Exception:
            return None
    try:
        return _ledger.append(
            Path(root) / ANSWER_EVENT_LEDGER,
            {
                "kind": "answer_event",
                "business_object": env.business_object,
                "exit_code": env.exit_code(),
                "authority_state": env.authority_state,
                "interface": str(interface or "cli"),
                # Additive cited-sources field (P8-T7): the scorecard's
                # retrieval_hit_rate + time_to_first_grounded_answer read this.
                # Metadata only (ids), never the claim text.
                "source_ids": list(env.cited_source_ids or []),
            },
            id_prefix="ANS",
        )
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="answer_protocol",
        description="Material-answer preflight: ground, caveat, or refuse.",
    )
    ap.add_argument("--root", default=".", help="oracle root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("answer", help="run preflight for a business object")
    a.add_argument("--object", required=True, help="business object the answer is about")
    a.add_argument("--question", default=None, help="the question being answered")
    a.add_argument("--format", choices=["md", "json"], default="md")

    r = sub.add_parser(
        "research",
        help="preflight exploratory public research (not Oracle-authoritative)",
    )
    r.add_argument("--question", required=True, help="public research question")
    r.add_argument(
        "--context-sensitivity",
        default="public",
        help="sensitivity of any company context that would be sent externally",
    )
    r.add_argument(
        "--includes-company-context",
        action="store_true",
        help="set when the external research prompt includes Oracle/company context",
    )
    r.add_argument("--format", choices=["md", "json"], default="md")

    args = ap.parse_args(argv)

    if args.cmd == "answer":
        env = preflight(args.root, args.object, question=args.question)
        code = env.exit_code()
        log_answer_event(args.root, env, interface="cli")
        if args.format == "json":
            payload = env.to_dict()
            payload["exit_code"] = code
            payload["verdict"] = _verdict_label(code)
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(render_md(env))
        return code

    if args.cmd == "research":
        env = research_preflight(
            args.root,
            args.question,
            context_sensitivity=args.context_sensitivity,
            includes_company_context=args.includes_company_context,
        )
        code = env.exit_code()
        if args.format == "json":
            payload = env.to_dict()
            payload["exit_code"] = code
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(render_research_md(env))
        return code

    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
