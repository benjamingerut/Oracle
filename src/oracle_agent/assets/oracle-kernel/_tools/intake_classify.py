#!/usr/bin/env python3
"""intake_classify.py -- heuristic intake sensitivity classifier (stdlib-only).

When material enters the oracle (via ingest or a connector pull) it must be
stamped with a sensitivity label AT LOG TIME so every downstream gate
(policy.check_processing, export, retrieval ceilings) has something to enforce.
This module computes a *conservative* first-pass label from the document's own
content, its filename, its size, and an optional connector default, then takes
the STRICTER of all those signals (stricter-row-wins).

The 5-tier ladder (oracle.yml ``security.sensitivity_labels`` /
PROCESSING-MATRIX.md), least -> most sensitive:

    public < internal < confidential < restricted < secret

Discipline: this is a *floor*, not a ceiling. The classifier never down-labels
below the connector default or an admin override; an admin may always raise the
label, and may lower it only by explicit override (recorded by the caller). When
in doubt the classifier rounds UP -- a false-confidential is a nuisance, a
false-public is a leak.

Signals (each contributes a candidate label; the max wins):
  * Hard secrets (API keys, private keys, connection strings) -> ``secret``,
    detected by the floor ``secret_scan`` module.
  * SSNs, full credit-card-shaped numbers, bank/routing numbers -> ``restricted``.
  * Salary / compensation / payroll / PII keywords, individual health terms,
    legal-privilege markers -> ``restricted``.
  * Explicit "confidential" / "proprietary" / "trade secret" / NDA markers,
    M&A / board / cap-table / customer-PII keywords -> ``confidential``.
  * "internal use only" / draft / employee-handbook markers -> ``internal``.
  * Otherwise -> ``internal`` as the safe default for un-marked business
    material (NOT ``public``; we never assume new intake is publishable).

Public API:
    LABELS                         -- ordered tuple (least->most sensitive)
    classify(text=..., filename=..., size=..., connector_default=...,
             admin_override=None) -> dict
        {label, signals:[{label,reason,evidence?}], floor}
    classify_file(path, ...)       -> dict   (reads + extracts where possible)
    stricter(a, b)                 -> str

Stdlib only. ``secret_scan`` is a sibling floor module (imported lazily so a
partial build cannot break import).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

__all__ = [
    "LABELS",
    "classify",
    "classify_file",
    "stricter",
    "rank",
    "DEFAULT_LABEL",
]

# Least -> most sensitive. Order IS the comparison.
LABELS = ("public", "internal", "confidential", "restricted", "secret")
_RANK = {label: i for i, label in enumerate(LABELS)}

# New, unmarked business intake is never assumed publishable.
DEFAULT_LABEL = "internal"

# How many characters of a large file we sample for keyword/pattern signals.
# Secrets/PII tend to cluster; sampling the head+tail keeps big files cheap
# without missing an obvious marker.
_SAMPLE_HEAD = 200_000
_SAMPLE_TAIL = 50_000


def rank(label: str) -> int:
    """Numeric rank of a label (higher == stricter). Unknown -> DEFAULT."""
    return _RANK.get(_norm(label), _RANK[DEFAULT_LABEL])


def _norm(label: Optional[str]) -> str:
    if not label:
        return DEFAULT_LABEL
    s = str(label).strip().lower()
    return s if s in _RANK else DEFAULT_LABEL


def stricter(a: Optional[str], b: Optional[str]) -> str:
    """Return whichever of two labels is stricter (higher rank)."""
    return a if rank(_norm(a)) >= rank(_norm(b)) else _norm(b)


# --------------------------------------------------------------------------- #
# content patterns
# --------------------------------------------------------------------------- #
# SSN: 3-2-4 with separators, not part of a longer digit run.
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
# Credit-card-shaped 13-16 digit groups (allow spaces/dashes). Validate length.
_CC = re.compile(r"(?<!\d)(?:\d[ -]?){13,16}(?!\d)")
# US bank routing number context.
_ROUTING = re.compile(r"(?i)\b(?:aba|routing)\s*(?:number|no\.?|#)?\s*[:=]?\s*\d{9}\b")
# IBAN-ish.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")

# Keyword groups -> candidate label. Each is a compiled, case-insensitive regex.
_KW_RESTRICTED = re.compile(
    r"(?i)\b("
    r"salary|salaries|compensation|payroll|w-?2|1099|base\s+pay|bonus\s+target|"
    r"social\s+security\s+number|\bssn\b|date\s+of\s+birth|\bdob\b|passport|"
    r"driver'?s?\s+licen[cs]e|medical\s+record|diagnosis|protected\s+health|"
    r"\bphi\b|attorney[- ]client|legal\s+privilege|privileged\s+and\s+confidential|"
    r"individual\s+performance\s+review|termination\s+letter|disciplinary"
    r")\b"
)
_KW_CONFIDENTIAL = re.compile(
    r"(?i)\b("
    r"confidential|strictly\s+confidential|proprietary|trade\s+secret|"
    r"non[- ]?disclosure|\bnda\b|do\s+not\s+distribute|company\s+confidential|"
    r"board\s+(?:deck|materials|minutes|of\s+directors)|cap\s+table|"
    r"capitalization\s+table|merger|acquisition|\bm&a\b|term\s+sheet|"
    r"customer\s+list|pricing\s+strategy|unreleased|under\s+embargo"
    r")\b"
)
_KW_INTERNAL = re.compile(
    r"(?i)\b("
    r"internal\s+use\s+only|internal\s+only|for\s+internal\s+distribution|"
    r"draft|work\s+in\s+progress|employee\s+handbook|onboarding|"
    r"not\s+for\s+external"
    r")\b"
)
# Markers that an author explicitly declared the doc public.
_KW_PUBLIC = re.compile(
    r"(?i)\b("
    r"press\s+release|public\s+announcement|published|for\s+immediate\s+release|"
    r"public\s+domain|cleared\s+for\s+release"
    r")\b"
)

# Filename hints (slug-ish substrings).
_FN_RESTRICTED = re.compile(
    r"(?i)(payroll|salary|salaries|comp(?:ensation)?|ssn|pii|medical|phi|hr[-_]|"
    r"privileged|legal[-_]?hold)"
)
_FN_CONFIDENTIAL = re.compile(
    r"(?i)(confidential|proprietary|nda|board|cap[-_]?table|m&a|merger|"
    r"term[-_]?sheet|secret)"
)
_FN_INTERNAL = re.compile(r"(?i)(internal|draft|wip|handbook)")


def _luhn_ok(digits: str) -> bool:
    """Luhn check for credit-card plausibility (cuts false positives)."""
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    parity = len(nums) % 2
    for i, d in enumerate(nums):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _sample(text: str) -> str:
    """Head+tail sample of a large text for cheap scanning."""
    if len(text) <= _SAMPLE_HEAD + _SAMPLE_TAIL:
        return text
    return text[:_SAMPLE_HEAD] + "\n" + text[-_SAMPLE_TAIL:]


def _scan_secrets(text: str) -> Optional[dict]:
    """Use the floor secret scanner; return a signal dict if any hit."""
    try:
        import secret_scan  # sibling floor module
    except Exception:  # pragma: no cover - secret_scan should always be present
        return None
    try:
        hits = secret_scan.scan_text(text)
    except Exception:  # pragma: no cover - never let a scanner bug raise here
        return None
    if hits:
        first = hits[0]
        return {
            "label": "secret",
            "reason": "secret_scan matched %d pattern(s)" % len(hits),
            "evidence": str(first.get("pattern", "secret")),
        }
    return None


def _content_signals(text: str) -> List[dict]:
    """Derive sensitivity signals from document content."""
    signals: List[dict] = []
    if not text:
        return signals
    sample = _sample(text)

    sec = _scan_secrets(sample)
    if sec:
        signals.append(sec)

    if _SSN.search(sample):
        signals.append(
            {"label": "restricted", "reason": "SSN-shaped number present", "evidence": "SSN"}
        )

    for m in _CC.finditer(sample):
        raw = m.group(0)
        digits = re.sub(r"[ -]", "", raw)
        if _luhn_ok(digits):
            signals.append(
                {
                    "label": "restricted",
                    "reason": "credit-card-shaped number (Luhn-valid)",
                    "evidence": "PAN",
                }
            )
            break

    if _ROUTING.search(sample) or _IBAN.search(sample):
        signals.append(
            {"label": "restricted", "reason": "bank routing/IBAN present", "evidence": "bank"}
        )

    m = _KW_RESTRICTED.search(sample)
    if m:
        signals.append(
            {"label": "restricted", "reason": "PII/comp/legal keyword", "evidence": m.group(1)}
        )

    m = _KW_CONFIDENTIAL.search(sample)
    if m:
        signals.append(
            {"label": "confidential", "reason": "confidential/proprietary keyword", "evidence": m.group(1)}
        )

    m = _KW_INTERNAL.search(sample)
    if m:
        signals.append(
            {"label": "internal", "reason": "internal-use keyword", "evidence": m.group(1)}
        )

    # A public marker is only allowed to *lower* the floor when NOTHING stricter
    # fired; we record it as a candidate but it loses to anything above public.
    m = _KW_PUBLIC.search(sample)
    if m:
        signals.append(
            {"label": "public", "reason": "explicit public marker", "evidence": m.group(1)}
        )

    return signals


def _filename_signals(filename: Optional[str]) -> List[dict]:
    """Derive signals from the filename / slug."""
    signals: List[dict] = []
    if not filename:
        return signals
    name = Path(str(filename)).name
    if _FN_RESTRICTED.search(name):
        signals.append({"label": "restricted", "reason": "filename hint", "evidence": name})
    elif _FN_CONFIDENTIAL.search(name):
        signals.append({"label": "confidential", "reason": "filename hint", "evidence": name})
    elif _FN_INTERNAL.search(name):
        signals.append({"label": "internal", "reason": "filename hint", "evidence": name})
    return signals


def classify(
    *,
    text: Optional[str] = None,
    filename: Optional[str] = None,
    size: Optional[int] = None,
    connector_default: Optional[str] = None,
    admin_override: Optional[str] = None,
) -> dict:
    """Classify intake sensitivity, taking the STRICTER of every signal.

    Args:
        text:              extracted document text (may be None/empty).
        filename:          original filename or slug (for filename hints).
        size:              byte size (reserved for future size-based heuristics).
        connector_default: the source connector's declared default sensitivity;
                           acts as a FLOOR -- the result is never below it.
        admin_override:    an explicit admin label; if given it WINS outright
                           (an admin may raise or lower), and is recorded.

    Returns:
        ``{label, signals, floor, override}`` where ``label`` is the final
        5-tier label, ``signals`` is the list of contributing signal dicts,
        ``floor`` is the connector-default floor applied, and ``override`` is
        the admin override if one was used.
    """
    signals = _content_signals(text or "")
    signals.extend(_filename_signals(filename))

    # Start from the safe default; raise to the strictest signal.
    label = DEFAULT_LABEL
    for sig in signals:
        label = stricter(label, sig["label"])

    # Connector default is a FLOOR -- never go below it.
    floor = _norm(connector_default) if connector_default else None
    if floor:
        label = stricter(label, floor)

    # Admin override wins outright (may raise OR lower); recorded for audit.
    override = None
    if admin_override is not None:
        override = _norm(admin_override)
        label = override

    return {
        "label": label,
        "signals": signals,
        "floor": floor,
        "override": override,
    }


def classify_file(
    path,
    *,
    connector_default: Optional[str] = None,
    admin_override: Optional[str] = None,
    text: Optional[str] = None,
) -> dict:
    """Classify a file on disk.

    If ``text`` is supplied (e.g. already extracted upstream) it is used
    directly; otherwise the file is read best-effort: plain-text-ish files are
    decoded, and for binary/office formats we attempt the extractor registry but
    degrade to filename+size signals if extraction is unavailable.
    """
    p = Path(path)
    size = None
    try:
        size = p.stat().st_size
    except OSError:
        pass

    if text is None:
        text = _best_effort_text(p)

    return classify(
        text=text,
        filename=p.name,
        size=size,
        connector_default=connector_default,
        admin_override=admin_override,
    )


# Suffixes we will read directly as text without invoking an extractor.
_TEXTY = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yml", ".yaml",
    ".html", ".htm", ".xml", ".log", ".rst", ".ini", ".cfg", ".env",
    ".py", ".js", ".ts", ".sql",
}


def _best_effort_text(p: Path) -> str:
    """Read file text for classification; never raise on a single file."""
    suffix = p.suffix.lower()
    if suffix in _TEXTY:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    # Try the extractor registry for office/binary formats (lazy, optional).
    try:
        import extractors  # companion engine module

        result = extractors.extract(p)
        if isinstance(result, dict):
            return str(result.get("text") or "")
    except Exception:
        pass
    # Fall back to a raw decode of the head, which still catches embedded
    # plaintext secrets/markers in many container formats.
    try:
        raw = p.read_bytes()[: _SAMPLE_HEAD]
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


if __name__ == "__main__":  # pragma: no cover - tiny manual harness
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="intake sensitivity classifier")
    ap.add_argument("--file", help="path to classify")
    ap.add_argument("--text", help="inline text to classify (overrides --file read)")
    ap.add_argument("--connector-default", dest="connector_default")
    ap.add_argument("--admin-override", dest="admin_override")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.file:
        out = classify_file(
            args.file,
            connector_default=args.connector_default,
            admin_override=args.admin_override,
            text=args.text,
        )
    else:
        out = classify(
            text=args.text or "",
            connector_default=args.connector_default,
            admin_override=args.admin_override,
        )

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(out["label"])
    # Exit code mirrors strictness so a shell can branch on it: 0 public/internal,
    # 1 confidential, 2 restricted/secret.
    r = rank(out["label"])
    sys.exit(0 if r <= 1 else (1 if r == 2 else 2))
