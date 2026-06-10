#!/usr/bin/env python3
"""Plain-text / Markdown / JSON extractor (stdlib only).

Handles the plain-text family: .txt, .md/.markdown, .json/.jsonl, and any
unknown suffix routed here by the registry as a best-effort fallback. Reads the
file as UTF-8 (with a couple of pragmatic fallbacks), returns the text verbatim
plus a per-line offset map.

Behaviour
---------
* **Text passthrough.** ``.txt`` / ``.md`` / config-ish files are returned as-is.
* **JSON pretty-norm.** For ``.json`` we parse and re-serialise with stable
  indentation when it parses, so the indexed text is canonical; if it does not
  parse we fall back to the raw text and note ``meta["json_parse_error"]`` (we
  still index the bytes — malformed JSON is still searchable testimony).
* **JSONL** is left line-structured (one record per line is already chunk-y).
* **Binary / undecodable.** If the bytes do not decode as text under any
  attempted codec, we set ``needs_ocr=True`` and ``meta["error"]`` and return
  empty text rather than emitting mojibake.

Never raises on content; the registry-level ``extract`` also wraps this.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

# We avoid importing the package (circular) and instead build the result dict
# inline with the exact contract keys. ``__init__.make_result`` mirrors this.

_TEXTY_SUFFIXES = {
    ".json",
    ".jsonl",
    ".ndjson",
}

# Codecs tried in order. utf-8-sig transparently strips a BOM; latin-1 always
# decodes any byte sequence, so it is the last-resort "treat as text" codec.
_CODECS = ("utf-8-sig", "utf-8", "utf-16", "latin-1")


def _result(text="", offsets=None, meta=None, needs_ocr=False):
    return {
        "text": text,
        "offsets": list(offsets) if offsets else [],
        "meta": dict(meta) if meta else {},
        "needs_ocr": bool(needs_ocr),
    }


def _looks_binary(raw: bytes) -> bool:
    """Heuristic: a NUL byte (or a high ratio of control bytes) => binary."""
    if b"\x00" in raw:
        return True
    if not raw:
        return False
    sample = raw[:4096]
    # Printable / common-whitespace bytes are "texty".
    textish = sum(
        1
        for b in sample
        if b in (0x09, 0x0A, 0x0D, 0x0C) or 0x20 <= b <= 0x7E or b >= 0x80
    )
    return (textish / len(sample)) < 0.85


def _decode(raw: bytes) -> Optional[str]:
    """Decode bytes to text trying a small codec ladder; None if all fail."""
    for codec in _CODECS:
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def _line_offsets(text: str) -> List[dict]:
    """Build per-line ``[start,end)`` anchors over ``text``.

    ``end`` is the index just past the newline (or end-of-text), so the slices
    tile the whole string without gaps. A trailing line with no newline is still
    captured.
    """
    offsets: List[dict] = []
    n = len(text)
    if n == 0:
        return offsets
    start = 0
    line_no = 1
    for i, ch in enumerate(text):
        if ch == "\n":
            offsets.append(
                {"start": start, "end": i + 1, "kind": "line", "line": line_no}
            )
            start = i + 1
            line_no += 1
    if start < n:
        offsets.append({"start": start, "end": n, "kind": "line", "line": line_no})
    return offsets


def extract(path) -> dict:
    """Extract text+per-line offsets from a plain-text / md / json file."""
    p = Path(path)
    suffix = p.suffix.lower()
    meta = {"extractor": "text_md", "suffix": suffix}

    try:
        raw = p.read_bytes()
    except OSError as exc:
        meta["error"] = f"read-failed: {exc}"
        return _result(meta=meta, needs_ocr=False)

    if raw == b"":
        meta["empty"] = True
        return _result(text="", offsets=[], meta=meta, needs_ocr=False)

    if _looks_binary(raw):
        # Bytes are present but they are not text. For an unknown-suffix binary
        # this is exactly the "we have content we cannot read" case -> needs_ocr.
        meta["error"] = "binary-or-undecodable"
        meta["bytes"] = len(raw)
        return _result(text="", offsets=[], meta=meta, needs_ocr=True)

    text = _decode(raw)
    if text is None:
        meta["error"] = "undecodable"
        meta["bytes"] = len(raw)
        return _result(text="", offsets=[], meta=meta, needs_ocr=True)

    # JSON canonicalisation (single-document .json only; jsonl is line-structured).
    if suffix == ".json":
        try:
            obj = json.loads(text)
            text = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False)
            meta["json"] = True
        except (json.JSONDecodeError, ValueError) as exc:
            # Keep the raw text (still searchable) but record the parse problem.
            meta["json_parse_error"] = str(exc)

    offsets = _line_offsets(text)
    meta["chars"] = len(text)
    meta["lines"] = len(offsets)
    return _result(text=text, offsets=offsets, meta=meta, needs_ocr=False)


__all__ = ["extract"]
