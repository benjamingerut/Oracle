#!/usr/bin/env python3
"""HTML extractor (stdlib ``html.parser`` only).

Strips tags and returns the visible text, preserving block structure as
paragraph offsets. Script, style, and other non-content elements are dropped.
Character entities (``&amp;``, ``&#8212;`` ...) are unescaped to their real
characters.

Note on the module name: this file is ``extractors/html.py`` but Python 3 uses
absolute imports by default, so ``from html.parser import HTMLParser`` and
``import html as _stdlib_html`` resolve to the standard library, NOT to this
sibling module. We still bind the stdlib references explicitly at import time to
make that unambiguous.

Output
------
* ``text``    -- visible text with block elements separated by blank lines and
                 inline whitespace collapsed.
* ``offsets`` -- one ``{"start","end","kind":"block"}`` anchor per emitted
                 text block, so citations can point at a paragraph.

Robustness: malformed markup is tolerated by ``HTMLParser`` itself; we additional
guard decoding and any parser explosion, returning an error result instead of
raising. Never raises on content.
"""
from __future__ import annotations

import html as _stdlib_html
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional

_CODECS = ("utf-8-sig", "utf-8", "utf-16", "latin-1")

# Elements whose text content is never user-visible prose.
_SKIP_CONTENT = {"script", "style", "head", "noscript", "template", "svg", "canvas"}

# Block-level elements that force a paragraph break around their content.
_BLOCK = {
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "table", "tr",
    "thead", "tbody", "blockquote", "pre", "br", "hr", "figure", "figcaption",
    "nav", "form", "fieldset", "dl", "dt", "dd", "address", "details", "summary",
}


def _result(text="", offsets=None, meta=None, needs_ocr=False):
    return {
        "text": text,
        "offsets": list(offsets) if offsets else [],
        "meta": dict(meta) if meta else {},
        "needs_ocr": bool(needs_ocr),
    }


def _decode(raw: bytes) -> Optional[str]:
    for codec in _CODECS:
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


class _TextExtractor(HTMLParser):
    """Collect visible text, splitting on block boundaries.

    We accumulate runs of inline text into the "current block"; when a block
    element opens or closes we flush the current block into ``blocks``. Content
    inside skip elements (script/style/...) is ignored entirely.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[str] = []
        self._buf: List[str] = []
        self._skip_depth = 0
        self._title: Optional[str] = None
        self._in_title = False

    # -- block accounting --------------------------------------------------- #
    def _flush(self) -> None:
        if self._buf:
            chunk = "".join(self._buf)
            # collapse internal whitespace runs to single spaces; trim ends
            collapsed = " ".join(chunk.split())
            if collapsed:
                self.blocks.append(collapsed)
            self._buf = []

    # -- parser callbacks --------------------------------------------------- #
    def handle_starttag(self, tag, attrs):  # noqa: D401
        tag = tag.lower()
        if tag in _SKIP_CONTENT:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag in _BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_CONTENT:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            self._flush()
        if tag in _BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._in_title and self._title is None:
            t = " ".join(data.split())
            if t:
                self._title = t
        self._buf.append(data)

    def close(self):  # noqa: D401
        super().close()
        self._flush()


def extract(path) -> dict:
    """Extract visible text + block offsets from an HTML file."""
    p = Path(path)
    suffix = p.suffix.lower()
    meta = {"extractor": "html", "suffix": suffix}

    try:
        raw = p.read_bytes()
    except OSError as exc:
        meta["error"] = f"read-failed: {exc}"
        return _result(meta=meta, needs_ocr=False)

    if raw == b"":
        meta["empty"] = True
        return _result(meta=meta)

    decoded = _decode(raw)
    if decoded is None:
        meta["error"] = "undecodable"
        meta["bytes"] = len(raw)
        return _result(meta=meta, needs_ocr=True)

    parser = _TextExtractor()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - HTMLParser is lenient, but guard anyway
        # Last-resort: strip tags crudely so we still return text.
        meta["html_warning"] = f"parser-fallback: {exc}"
        crude = _stdlib_html.unescape(_strip_tags_crudely(decoded))
        blocks = [b for b in (s.strip() for s in crude.split("\n")) if b]
        return _blocks_to_result(blocks, meta)

    if parser._title:
        meta["title"] = parser._title

    return _blocks_to_result(parser.blocks, meta)


def _strip_tags_crudely(s: str) -> str:
    out = []
    depth = 0
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            if depth > 0:
                depth -= 1
            out.append(" ")
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _blocks_to_result(blocks: List[str], meta: dict) -> dict:
    blocks = [b for b in blocks if b]
    if not blocks:
        meta.setdefault("empty_text", True)
        return _result(text="", offsets=[], meta=meta, needs_ocr=False)

    parts: List[str] = []
    offsets: List[dict] = []
    cursor = 0
    for i, block in enumerate(blocks):
        parts.append(block)
        end = cursor + len(block)  # block's own span; '\n' lives in the gap
        offsets.append({"start": cursor, "end": end, "kind": "block", "block": i})
        cursor = end + 1  # +1 for the joining newline

    text = "\n".join(parts)
    meta["blocks"] = len(blocks)
    meta["chars"] = len(text)
    return _result(text=text, offsets=offsets, meta=meta, needs_ocr=False)


__all__ = ["extract"]
