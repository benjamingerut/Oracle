"""agentloop/grounding.py -- forced-grounding gate (Phase 3).

A deterministic, server-side step between the model's draft answer and the
user. It detects *material company claims* in a draft, checks each against the
answer-protocol envelopes obtained this turn, and reports which claims are
unbacked (no covering envelope) or mismatched (asserting on a refused-class or
withheld envelope). The loop integration (P3-T3) turns that report into a
repair loop or a redaction; this module is a pure, tested function library.

Design pins (from docs/roadmap/PHASE-3-forced-grounding.md):

  * Claim units are sentences, list items, and table rows (P3S-3). Quoted
    text, list items, table rows, and code-block content ARE extracted --
    exempting them would be a smuggling channel. Hedge words do NOT exempt a
    unit that references a known object or carries a figure/date/named entity
    (P3S-15).
  * Materiality (P3S-17): a unit is material iff it is a declarative unit AND
    (it references an object in ``objects_seen`` OR asserts a figure/date/
    named entity as company fact). Tuned for *recall*; false positives cost an
    extra repair turn, not a leak.
  * ``known_objects(root)`` enumerates truth-map object names server-side via
    the vendored ``truth_map.load_rows`` reader (P3S-5). The list NEVER enters
    model context, so STRESS H1 holds; reading through the *vendored* reader
    (not the target root's possibly-skewed copy) handles STRESS A6.
  * Coverage (P3S-2): a claim is covered ONLY by an envelope whose
    ``business_object`` equals the claim's ``object_guess`` under
    ``truth_map.normalize_object`` EQUALITY -- never substring containment. A
    claim with ``object_guess is None`` is ALWAYS unbacked (fail-closed).
  * Verdict obligations (P3S-6): grounded -> plain assert ok; supported and
    caveated -> satisfied by the deterministic authority footer (prose is NOT
    scanned for label strings); refused-class or withheld envelope with an
    assertion -> ``mismatched``.
  * Governing envelope (P3S-13): the LATEST envelope for an object governs.
    Verdict read from ``exit_code``, falling back to the ``verdict`` string.
  * Withheld (P3S-1): a ``"withheld": true`` envelope is treated as
    refused-class -- the model never saw the grounded payload.
  * Fail-closed (P3S-8): every regex is linear-time (no nested quantifiers);
    drafts are attacker-influenced input. Any internal failure raises
    ``GateError`` so the caller (P3-T3) withholds the whole reply.
  * Footer-lookalike stripping (P3S-18): body lines matching the authority
    footer prefix are dropped before extraction so a model cannot spoof the
    deterministic footer in prose.

Stdlib only.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__all__ = [
    "Claim",
    "ClaimCheck",
    "GateError",
    "known_objects",
    "extract_claims",
    "check_grounding",
    "repair_prompt",
]


class GateError(Exception):
    """Raised when the grounding gate cannot run to completion.

    The caller (P3-T3) catches this and withholds the ENTIRE reply (generic
    notice + footer) -- the gate fails CLOSED, never open. Extraction/checking
    wrap their own internals so an attacker-shaped draft surfaces here rather
    than crashing or, worse, releasing an ungated draft.
    """


@dataclass
class Claim:
    text: str                      # the asserting claim unit (sentence/list item/table row)
    object_guess: Optional[str]    # best-effort business object it concerns


@dataclass
class ClaimCheck:
    claims: list = field(default_factory=list)        # material claims found
    unbacked: list = field(default_factory=list)      # no covering envelope this turn
    mismatched: list = field(default_factory=list)    # asserts on refused/withheld envelope


# --------------------------------------------------------------------------- #
# known_objects -- server-side truth-map enumeration (P3S-5)
# --------------------------------------------------------------------------- #
def _vendored_kernel_tools() -> str:
    """Absolute path to the agent's vendored kernel ``_tools`` directory.

    Reading the truth map through the VENDORED reader (not the target root's
    possibly version-skewed ``_tools/truth_map.py``) gives one stable parser
    regardless of the root's kernel version -- handles STRESS A6.
    """
    return str(Path(__file__).resolve().parents[1] / "assets" / "oracle-kernel" / "_tools")


def _truth_map_module():
    """Import the vendored ``truth_map`` reader (load_rows / normalize_object).

    The module is stdlib-only at import time (its ledger/policy/answer_protocol
    imports are lazy), so adding ``_tools`` to ``sys.path`` is safe and cheap.
    """
    tools = _vendored_kernel_tools()
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import truth_map  # type: ignore

    return truth_map


def known_objects(root: Path) -> list[str]:
    """Truth-map business-object names for ``root``, read server-side.

    Enumerated via the vendored ``truth_map.load_rows`` reader; the returned
    list NEVER enters model context (STRESS H1). Returns ``[]`` when the root
    has no TRUTH-MAP.md or no qualifying table (bootstrap-empty). Any reader
    failure raises ``GateError`` (fail-closed): a gate that cannot enumerate
    objects must not silently treat every claim as object-less and release it.
    """
    try:
        tm = _truth_map_module()
        rows = tm.load_rows(Path(root))
    except Exception as exc:  # reader failure -> fail closed
        raise GateError(f"known_objects: truth-map read failed: {type(exc).__name__}") from exc
    out: list[str] = []
    for row in rows:
        name = str(row.get("business_object", "")).strip()
        if name and name not in out:
            out.append(name)
    return out


def _normalize_object(name: str) -> str:
    """``truth_map.normalize_object`` with a stdlib fallback.

    Used for coverage equality and object-mention matching. Falls back to the
    same algorithm if the vendored module is somehow unavailable so the gate
    never crashes mid-check.
    """
    try:
        return _truth_map_module().normalize_object(name)
    except Exception:
        if name is None:
            return ""
        s = str(name).replace("/", " ").lower()
        s = re.sub(r"\s+", " ", s).strip()
        return s


# --------------------------------------------------------------------------- #
# extraction (P3S-3, P3S-15, P3S-17, P3S-18)
# --------------------------------------------------------------------------- #

# Authority-footer lookalike lines: a model could echo the deterministic footer
# in prose to fake a verdict label. Strip any body line that begins (after
# optional markdown emphasis/quote markers) with the footer prefix. Linear-time.
_FOOTER_LOOKALIKE_RE = re.compile(
    r"^\s*[>*_\-]*\s*(?:—|--)\s*(?:authority|conversational)\b",
    re.IGNORECASE,
)

# Figures: currency, percentages, plain numbers (incl. grouped / decimal /
# k-m-b-suffixed). Linear-time alternation, no nested quantifiers.
_FIGURE_RE = re.compile(
    r"(?<![\w])"
    r"(?:"
    r"[$€£¥]\s?\d[\d,]*(?:\.\d+)?(?:\s?[kmbtKMBT][a-zA-Z]*)?"   # $1,234.5  $4M
    r"|\d[\d,]*(?:\.\d+)?\s?%"                                  # 12.5%
    r"|\d[\d,]*(?:\.\d+)?\s?(?:[kmbtKMBT]\b|million|billion|thousand|trillion)"  # 4M, 3 million
    r"|\d[\d,]{2,}(?:\.\d+)?"                                   # 1,234  / 12345
    r")"
)

# Dates: ISO, slashed, and month-name forms. Linear-time, anchored alternation.
_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_DATE_RE = re.compile(
    r"(?<![\w])"
    r"(?:"
    r"\d{4}-\d{2}-\d{2}"                                  # 2026-06-10
    r"|\d{1,2}/\d{1,2}/\d{2,4}"                           # 6/10/2026
    r"|(?:" + _MONTHS + r")\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?"  # June 10, 2026
    r"|\d{1,2}\s+(?:" + _MONTHS + r")\.?(?:,?\s+\d{4})?"  # 10 June 2026
    r"|(?:fy|q[1-4])\s?\d{2,4}"                           # FY2026 / Q3 2026
    r"|\b\d{4}\b(?=.*(?:fiscal|year|revenue|quarter))"   # bare year in a fiscal context
    r")",
    re.IGNORECASE,
)

# Named entities asserted as company fact: a capitalized multi-word proper noun,
# or a single Capitalized token that is not a sentence-initial common word. We
# keep this conservative-but-recall-tuned: TitleCase runs of >=1 word that are
# not purely the first word of the sentence. Linear-time (no backtracking trap).
_PROPER_RUN_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*)+\b"   # Acme Corp, Northwind Bank
)
# All-caps acronyms / system names (ERP, CRM, SAP, AWS, Q3FY26-style), >=2 caps.
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9]*\b")

# Hedge openers. Per P3S-15 these do NOT exempt a unit that references a known
# object or carries a figure/date/entity -- recorded only so a *pure* hedge with
# no material content is correctly treated as non-asserting.
_HEDGE_RE = re.compile(
    r"\b(?:i\s+(?:believe|think|guess|suspect|feel)|probably|maybe|perhaps|"
    r"i'?m\s+not\s+sure|it\s+seems|possibly|might\s+be|could\s+be|"
    r"as\s+far\s+as\s+i\s+know)\b",
    re.IGNORECASE,
)

# Interrogative / imperative openers that are not assertions. A unit that is a
# pure question is not a declarative claim. Conservative: only a trailing '?'
# with no figure/date/object makes a unit non-declarative.
_QUESTION_RE = re.compile(r"\?\s*$")

# A markdown table separator row: |---|---|.  Not a claim.
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")

# Sentence splitter: split on ., !, ? followed by whitespace. Linear-time; we
# accept light over-/under-splitting because extraction favors recall and the
# unit is later checked for materiality.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Fenced code-block delimiter.
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")

# Conversational / meta openers that are never material on their own. Used ONLY
# to keep the purely-conversational fixture class at zero flags; a unit that
# also carries a figure/date/object is still material (recall wins).
_CONVERSATIONAL_RE = re.compile(
    r"^(?:hi|hello|hey|thanks|thank\s+you|sure|okay|ok|got\s+it|"
    r"happy\s+to\s+help|let\s+me|i\s+can|i'?ll|here'?s|how\s+can\s+i|"
    r"is\s+there|would\s+you|do\s+you|what\s+can|feel\s+free|no\s+problem|"
    r"you'?re\s+welcome|of\s+course|absolutely|certainly)\b",
    re.IGNORECASE,
)


def _strip_footer_lookalikes(draft: str) -> str:
    """Drop body lines that imitate the deterministic authority footer (P3S-18)."""
    kept = [ln for ln in draft.splitlines() if not _FOOTER_LOOKALIKE_RE.match(ln)]
    return "\n".join(kept)


def _split_units(draft: str) -> list[str]:
    """Split a draft into candidate claim units.

    Units are: list items, table rows (each non-separator row, split per cell
    is NOT done -- the whole row is one unit), code-block lines, and otherwise
    sentences within a paragraph. Quotes are NOT exempt: a quoted sentence is
    still split and yielded as a unit. Linear-time over the input.
    """
    units: list[str] = []
    in_fence = False
    for raw in draft.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if not stripped:
            continue
        if in_fence:
            # Code-block content IS extracted (smuggling channel, P3S-3).
            units.append(stripped)
            continue
        # Markdown table rows: a row containing pipes that is not a separator.
        if "|" in stripped and stripped.count("|") >= 1:
            if _TABLE_SEP_RE.match(stripped):
                continue
            units.append(stripped)
            continue
        # List items: bullet or ordered. Strip the marker, keep the content.
        m = re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)(.*)$", stripped)
        if m:
            units.append(m.group(1).strip())
            continue
        # Blockquote: strip the leading '>' but DO extract the content.
        if stripped.startswith(">"):
            stripped = stripped.lstrip(">").strip()
            if not stripped:
                continue
        # Heading: strip leading '#'.
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
            if not stripped:
                continue
        # Otherwise: split the line into sentences.
        for sent in _SENTENCE_SPLIT_RE.split(stripped):
            sent = sent.strip()
            if sent:
                units.append(sent)
    return units


def _mentioned_object(unit: str, objects_seen: list[str]) -> Optional[str]:
    """Return the truth-map object this unit references, normalized, or None.

    Matching is on ``normalize_object`` of each known object appearing as a
    normalized-substring of the normalized unit text. The returned value is the
    ORIGINAL object name (so ``object_guess`` round-trips to envelopes via
    normalize-equality). Longest object name wins (most specific).
    """
    norm_unit = _normalize_object(unit)
    if not norm_unit:
        return None
    best: Optional[str] = None
    best_len = -1
    for obj in objects_seen:
        norm_obj = _normalize_object(obj)
        if not norm_obj:
            continue
        # Word-ish boundary match on the normalized strings.
        if _norm_contains(norm_unit, norm_obj) and len(norm_obj) > best_len:
            best = obj
            best_len = len(norm_obj)
    return best


def _norm_contains(haystack: str, needle: str) -> bool:
    """True iff ``needle`` appears in ``haystack`` on whitespace boundaries.

    Both are already ``normalize_object`` output (lowercase, single-spaced), so
    a boundary check is a simple padded-substring test -- linear-time.
    """
    if not needle:
        return False
    return f" {needle} " in f" {haystack} "


def _has_material_token(unit: str) -> bool:
    """True iff the unit asserts a figure, date, or named entity as fact."""
    if _FIGURE_RE.search(unit):
        return True
    if _DATE_RE.search(unit):
        return True
    if _PROPER_RUN_RE.search(unit):
        return True
    if _ACRONYM_RE.search(unit):
        return True
    return False


def _is_declarative(unit: str) -> bool:
    """A unit is declarative unless it is a pure question.

    Hedges do NOT make a unit non-declarative (P3S-15): only an interrogative
    with no material token is exempted (a bare question carries no claim).
    """
    if _QUESTION_RE.search(unit) and not _has_material_token(unit):
        return False
    return True


def extract_claims(draft: str, *, objects_seen: list[str]) -> list[Claim]:
    """Deterministically extract material company claims from ``draft``.

    A unit (sentence / list item / table row / code line / quoted text) is a
    material claim iff it is declarative AND (it references an object in
    ``objects_seen`` OR it asserts a figure/date/named entity). Tuned for
    recall: false positives cost a repair turn, a miss is a leak.

    ``object_guess`` is the referenced truth-map object (original casing) when a
    known object is mentioned, else ``None`` (which the checker treats as always
    unbacked, fail-closed). Raises ``GateError`` on any internal failure so the
    gate fails closed.
    """
    try:
        if not isinstance(draft, str):
            raise TypeError("draft must be str")
        body = _strip_footer_lookalikes(draft)
        units = _split_units(body)
        claims: list[Claim] = []
        seen_texts: set[str] = set()
        for unit in units:
            if not _is_declarative(unit):
                continue
            obj = _mentioned_object(unit, objects_seen or [])
            material_token = _has_material_token(unit)
            if obj is None and not material_token:
                # Non-material: no known object and no figure/date/entity. A
                # purely-conversational opener lands here; one that DOES carry a
                # material token or object falls through and is flagged (recall).
                continue
            key = unit.strip()
            if key in seen_texts:
                continue
            seen_texts.add(key)
            claims.append(Claim(text=unit, object_guess=obj))
        return claims
    except GateError:
        raise
    except Exception as exc:  # attacker-shaped input must not crash open
        raise GateError(f"extract_claims failed: {type(exc).__name__}") from exc


# --------------------------------------------------------------------------- #
# checking (P3S-1, P3S-2, P3S-6, P3S-13)
# --------------------------------------------------------------------------- #
# Refused-class verdict labels (string fallback when exit_code is absent).
_REFUSED_LABELS = {"refused", "refuse", "do-not-claim", "do not claim"}
# Verdicts whose obligations the deterministic footer satisfies, or that allow a
# plain assertion. grounded(0) plain-ok; supported(2)/caveated(3) footer-ok.
_OK_EXIT_CODES = {0, 2, 3}
_OK_LABELS = {"grounded", "supported", "supported, authority not confirmed", "caveated"}


def _envelope_object(env: dict) -> str:
    return str(env.get("business_object", "") or "")


def _latest_per_object(envelopes: list[dict]) -> dict[str, dict]:
    """Map normalized object -> the LATEST envelope for it (P3S-13).

    Later entries in the list override earlier ones (the kernel appends
    envelopes in turn order; the most recent re-run governs).
    """
    out: dict[str, dict] = {}
    for env in envelopes:
        if not isinstance(env, dict):
            continue
        key = _normalize_object(_envelope_object(env))
        if not key:
            continue
        out[key] = env  # last write wins == latest governs
    return out


def _is_withheld(env: dict) -> bool:
    return bool(env.get("withheld") is True)


def _envelope_satisfies(env: dict) -> bool:
    """True iff this (governing) envelope lets the claim stand.

    Withheld -> never (refused-class, P3S-1). Otherwise read ``exit_code`` with
    a ``verdict``-string fallback (P3S-13). grounded/supported/caveated satisfy
    (footer carries the label, P3S-6); refused does not.
    """
    if _is_withheld(env):
        return False
    code = env.get("exit_code")
    if isinstance(code, bool):  # guard: bools are ints in Python
        code = None
    if isinstance(code, int):
        return code in _OK_EXIT_CODES
    # Fallback to the verdict string.
    verdict = str(env.get("verdict", "")).strip().lower()
    if verdict in _REFUSED_LABELS:
        return False
    if verdict in _OK_LABELS or verdict.startswith("supported"):
        return True
    # Unknown verdict and no exit_code -> fail closed (does not satisfy).
    return False


def _envelope_is_refused_class(env: dict) -> bool:
    """True iff the governing envelope is refused-class OR withheld (P3S-1/6).

    Distinguishes ``mismatched`` (an envelope exists for the object but refuses
    or was withheld) from ``unbacked`` (no envelope at all).
    """
    if _is_withheld(env):
        return True
    code = env.get("exit_code")
    if isinstance(code, bool):
        code = None
    if isinstance(code, int):
        return code == 4
    verdict = str(env.get("verdict", "")).strip().lower()
    return verdict in _REFUSED_LABELS


def check_grounding(draft: str, envelopes: list[dict], *,
                    objects_seen: list[str]) -> ClaimCheck:
    """Map extracted claims to envelopes under the pinned coverage semantics.

    For each material claim:
      * ``object_guess is None`` -> unbacked (fail-closed, P3S-2).
      * an envelope whose ``business_object`` equals ``object_guess`` under
        ``normalize_object`` EQUALITY (never containment) is the cover; the
        LATEST such envelope governs (P3S-13).
      * no covering envelope -> unbacked.
      * covering envelope is refused-class or withheld -> mismatched (P3S-1/6).
      * otherwise (grounded / supported / caveated) -> backed; the footer
        carries any required label (P3S-6).

    Raises ``GateError`` on any internal failure (fail-closed, P3S-8).
    """
    try:
        claims = extract_claims(draft, objects_seen=objects_seen)
        by_object = _latest_per_object(list(envelopes or []))
        unbacked: list[Claim] = []
        mismatched: list[Claim] = []
        for claim in claims:
            if claim.object_guess is None:
                unbacked.append(claim)
                continue
            key = _normalize_object(claim.object_guess)
            env = by_object.get(key)
            if env is None:
                unbacked.append(claim)
                continue
            if _envelope_is_refused_class(env):
                mismatched.append(claim)
                continue
            if not _envelope_satisfies(env):
                # Unknown/unparseable verdict with an envelope present: treat as
                # mismatched (an envelope exists but does not license the claim).
                mismatched.append(claim)
                continue
            # backed: nothing to record.
        return ClaimCheck(claims=claims, unbacked=unbacked, mismatched=mismatched)
    except GateError:
        raise
    except Exception as exc:
        raise GateError(f"check_grounding failed: {type(exc).__name__}") from exc


# --------------------------------------------------------------------------- #
# repair prompt
# --------------------------------------------------------------------------- #
def repair_prompt(check: ClaimCheck) -> str:
    """The user-turn message sent back to the model on an unbacked/mismatched check.

    Names the specific objects/claims that need grounding and tells the model to
    either call ``oracle_answer`` for the object or retract the claim. A claim
    with ``object_guess is None`` is told to name the object or retract (P3S-2).
    """
    lines: list[str] = [
        "GROUNDING REQUIRED. Before this reply can be released, every material "
        "company claim must be backed by an answer-protocol envelope obtained "
        "this turn. The following claims are not yet grounded:",
        "",
    ]
    n = 0
    for claim in list(check.unbacked) + list(check.mismatched):
        n += 1
        obj = claim.object_guess
        snippet = claim.text.strip()
        if len(snippet) > 200:
            snippet = snippet[:197] + "..."
        if obj:
            lines.append(
                f"  {n}. \"{snippet}\" -- call oracle_answer for the business "
                f"object \"{obj}\" and obey its verdict, or retract this claim."
            )
        else:
            lines.append(
                f"  {n}. \"{snippet}\" -- name the specific business object this "
                f"concerns and call oracle_answer for it, or retract this claim."
            )
    lines.append("")
    lines.append(
        "Call oracle_answer for each object above (the tool is enabled again), "
        "then re-state your answer. Do NOT assert any of these claims without a "
        "covering envelope -- an unbacked claim will be withheld from the user."
    )
    return "\n".join(lines)
