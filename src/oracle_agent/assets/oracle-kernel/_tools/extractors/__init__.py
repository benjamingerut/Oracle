#!/usr/bin/env python3
"""Text-extractor registry + the single ``extract(path)`` entrypoint.

This package is the FIRST stage of the knowledge-ingestion pipeline
(extractor -> chunker -> index -> source-record -> derive -> classify). It turns
a raw file on disk into plain UTF-8 text plus character offsets, with metadata
and a ``needs_ocr`` flag for the ingest pipeline to act on.

Contract (interface_contracts / file_manifest):
    extract(path) -> {
        "text":      str,           # extracted UTF-8 text ("" when nothing)
        "offsets":   list[dict],    # structural anchors into ``text`` (see below)
        "meta":      dict,          # extractor, suffix, sha-free descriptors,
                                    #   and "error" on any soft failure
        "needs_ocr": bool,          # True when bytes exist but no text could be
                                    #   recovered (scanned PDF, image-only, etc.)
    }

DESIGN INVARIANTS
-----------------
* **Stdlib only.** Every shipped extractor relies on the standard library
  (``csv``, ``html.parser``, ``zipfile``, ``xml.etree.ElementTree``, ...).
  Office formats degrade gracefully: a malformed or password-protected file is
  reported via ``meta["error"]`` / ``needs_ocr`` rather than raising.
* **Never raises on content.** ``extract`` is total: malformed, empty, unknown,
  binary, or unreadable inputs all return a well-formed dict. The only way to
  get an exception out of this package is to pass something that is not a path.
  Even then we try hard to coerce and, failing that, return an error dict.
* **Read-only.** Extractors only ever open files for reading. They never write,
  move, or copy anything, so they are outside the safe_paths write-chokepoint
  (the no-bypass guard only flags WRITE-mode opens / shutil moves).

OFFSET MODEL
------------
``offsets`` is a list of structural anchors, each a dict with at least
``{"start": int, "end": int, "kind": str}`` describing a half-open ``[start,end)``
slice of ``text``. ``kind`` is one of ``line``, ``row``, ``block``, ``cell``,
``paragraph``, ``sheet`` or ``page`` depending on the source grain. Downstream
chunking uses these to keep citations aligned to source structure; consumers
that do not care can ignore them. ``offsets`` is always a (possibly empty) list.

REGISTRY
--------
``REGISTRY`` maps a lowercased suffix (with leading dot) to the extractor
callable. ``register(suffix, fn)`` adds/overrides an entry. ``supported()``
returns the sorted list of known suffixes. Unknown suffixes fall back to a
best-effort plain-text read so we never hard-fail on an unexpected file type.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import csv_tsv, html, office, text_md

# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #

# An ExtractResult is a plain dict so it round-trips through json trivially and
# imposes no class dependency on callers. We document the shape here and provide
# a constructor to keep every extractor emitting the identical key set.
ExtractResult = Dict[str, object]


def make_result(
    text: str = "",
    offsets: Optional[List[dict]] = None,
    meta: Optional[dict] = None,
    needs_ocr: bool = False,
) -> ExtractResult:
    """Build a contract-shaped extractor result dict.

    Centralised so every extractor (and every error path) emits exactly the same
    four keys with the correct types.
    """
    return {
        "text": text if isinstance(text, str) else "",
        "offsets": list(offsets) if offsets else [],
        "meta": dict(meta) if meta else {},
        "needs_ocr": bool(needs_ocr),
    }


# --------------------------------------------------------------------------- #
# Suffix registry
# --------------------------------------------------------------------------- #

Extractor = Callable[[Path], ExtractResult]

REGISTRY: Dict[str, Extractor] = {
    # plain text family
    ".txt": text_md.extract,
    ".text": text_md.extract,
    ".md": text_md.extract,
    ".markdown": text_md.extract,
    ".mdown": text_md.extract,
    ".rst": text_md.extract,
    ".log": text_md.extract,
    ".json": text_md.extract,
    ".jsonl": text_md.extract,
    ".ndjson": text_md.extract,
    ".yaml": text_md.extract,
    ".yml": text_md.extract,
    ".ini": text_md.extract,
    ".cfg": text_md.extract,
    ".toml": text_md.extract,
    # delimited text
    ".csv": csv_tsv.extract,
    ".tsv": csv_tsv.extract,
    ".tab": csv_tsv.extract,
    # markup
    ".html": html.extract,
    ".htm": html.extract,
    ".xhtml": html.extract,
    # office / best-effort
    ".docx": office.extract,
    ".xlsx": office.extract,
    ".pdf": office.extract,
}


def register(suffix: str, fn: Extractor) -> None:
    """Register (or override) an extractor for a lowercased ``.suffix``."""
    if not suffix:
        raise ValueError("suffix must be non-empty")
    key = suffix.lower()
    if not key.startswith("."):
        key = "." + key
    REGISTRY[key] = fn


def supported() -> List[str]:
    """Return the sorted list of registered suffixes."""
    return sorted(REGISTRY)


def extractor_for(path) -> Optional[Extractor]:
    """Return the registered extractor for ``path``'s suffix, or None."""
    suffix = Path(path).suffix.lower()
    return REGISTRY.get(suffix)


# --------------------------------------------------------------------------- #
# The single entrypoint
# --------------------------------------------------------------------------- #

def extract(path) -> ExtractResult:
    """Extract text+offsets from ``path``. Total: never raises on file content.

    Routing:
      * a registered suffix -> its extractor
      * an unknown suffix   -> best-effort plain-text read (text_md), which itself
                               degrades to ``needs_ocr`` on binary/undecodable
                               input

    Any extractor exception is caught and converted into a well-formed error
    result so a single malformed file can never break a batch ingest.
    """
    try:
        p = Path(path)
    except TypeError:
        return make_result(meta={"error": f"not-a-path: {path!r}"}, needs_ocr=True)

    suffix = p.suffix.lower()

    # Missing / non-file targets are a soft failure, not an exception.
    try:
        if not p.exists():
            return make_result(
                meta={"error": "file-not-found", "path": str(p), "suffix": suffix},
                needs_ocr=False,
            )
        if not p.is_file():
            return make_result(
                meta={"error": "not-a-regular-file", "path": str(p), "suffix": suffix},
                needs_ocr=False,
            )
    except OSError as exc:
        return make_result(
            meta={"error": f"stat-failed: {exc}", "path": str(p), "suffix": suffix},
            needs_ocr=False,
        )

    fn = REGISTRY.get(suffix, text_md.extract)
    try:
        result = fn(p)
    except Exception as exc:  # noqa: BLE001 - extractors must never escape
        return make_result(
            meta={
                "error": f"extractor-crashed: {type(exc).__name__}: {exc}",
                "extractor": getattr(fn, "__module__", "unknown"),
                "suffix": suffix,
                "path": str(p),
            },
            needs_ocr=True,
        )

    # Defensive: normalise whatever the extractor returned into the contract shape
    # so downstream code can rely on the four keys unconditionally.
    return _normalise(result, suffix, p)


def _normalise(result, suffix: str, p: Path) -> ExtractResult:
    """Coerce an extractor's return value into the contract dict shape."""
    if not isinstance(result, dict):
        return make_result(
            meta={"error": "extractor-returned-non-dict", "suffix": suffix, "path": str(p)},
            needs_ocr=True,
        )
    text = result.get("text", "")
    offsets = result.get("offsets", [])
    meta = result.get("meta", {})
    needs_ocr = result.get("needs_ocr", False)
    out = make_result(
        text=text if isinstance(text, str) else "",
        offsets=offsets if isinstance(offsets, list) else [],
        meta=meta if isinstance(meta, dict) else {},
        needs_ocr=bool(needs_ocr),
    )
    # Always record the routing suffix for provenance unless an extractor set it.
    out["meta"].setdefault("suffix", suffix)
    return out


__all__ = [
    "extract",
    "make_result",
    "register",
    "supported",
    "extractor_for",
    "REGISTRY",
    "ExtractResult",
]
