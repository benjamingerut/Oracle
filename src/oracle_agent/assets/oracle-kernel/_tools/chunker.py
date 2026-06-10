#!/usr/bin/env python3
"""chunker.py -- offset-tracked overlapping text chunker (stdlib-only).

The ingestion engine extracts a document into a flat ``text`` string plus an
optional list of ``offsets`` (structural markers an extractor emits -- e.g. one
``(char_offset, label)`` per page, row, or paragraph). The chunker slices that
text into overlapping windows, preserving for every chunk:

  * ``text``   -- the chunk's substring,
  * ``start``  -- inclusive 0-based char offset into the ORIGINAL document,
  * ``end``    -- exclusive 0-based char offset into the ORIGINAL document,
  * ``index``  -- 0-based ordinal of the chunk,
  * ``markers``-- the structural offset markers (if any) that fall inside it.

The start/end offsets are load-bearing: the derivation step (derive.py) cites
evidence by char offset, so a chunk must always be able to point back at the
exact span of the source it came from. Chunking is therefore offset-exact:
``text[chunk.start:chunk.end] == chunk.text`` for every chunk.

Public API:
    Chunk                      -- lightweight dataclass-like record (dict-able)
    chunk(text, *, size=..., overlap=..., offsets=None) -> list[Chunk]
    chunk_dicts(...)           -> list[dict]   # same, as plain dicts

Design choices:
  * Window boundaries prefer to fall on a natural break (newline, then
    sentence end, then whitespace) within a small look-back band, so chunks do
    not slice words in half when avoidable -- but the offsets stay exact
    regardless of where the cut lands.
  * Overlap is measured in characters and is clamped to ``< size`` so the
    window always advances (no infinite loop on pathological input).
  * Empty / whitespace-only text yields zero chunks.

Stdlib only.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

__all__ = ["Chunk", "chunk", "chunk_dicts", "DEFAULT_SIZE", "DEFAULT_OVERLAP"]

# Defaults chosen so a chunk is a few paragraphs of prose: large enough to carry
# context for retrieval, small enough that an evidence citation is precise.
DEFAULT_SIZE = 1200
DEFAULT_OVERLAP = 200

# How far back from a hard window edge we will look for a nicer break point.
_BREAK_LOOKBACK = 200

# Characters we treat as "nice" break points, best first.
_HARD_BREAKS = ("\n\n", "\n")
_SENTENCE_ENDS = (". ", "? ", "! ", ".\n", "?\n", "!\n")


class Chunk:
    """An offset-tracked slice of a source document.

    Behaves enough like a record for tests and downstream code: attribute
    access, ``__eq__``, ``to_dict()``, and a readable ``repr``.
    """

    __slots__ = ("text", "start", "end", "index", "markers")

    def __init__(
        self,
        text: str,
        start: int,
        end: int,
        index: int,
        markers: Optional[List[dict]] = None,
    ) -> None:
        self.text = text
        self.start = int(start)
        self.end = int(end)
        self.index = int(index)
        self.markers = list(markers or [])

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "index": self.index,
            "markers": list(self.markers),
        }

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Chunk):
            return self.to_dict() == other.to_dict()
        if isinstance(other, dict):
            return self.to_dict() == other
        return NotImplemented

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        preview = (self.text[:40] + "...") if len(self.text) > 40 else self.text
        return (
            f"Chunk(index={self.index}, start={self.start}, end={self.end}, "
            f"text={preview!r})"
        )


def _normalize_offsets(
    offsets: Optional[Sequence],
) -> List[dict]:
    """Coerce extractor offsets into a uniform list of marker dicts.

    Accepts any of:
      * ``[(char_offset, label), ...]``
      * ``[{"offset": int, ...}, ...]``
      * ``[{"start": int, "end": int, "kind": ...}, ...]``  (extractor markers)
      * ``[int, ...]`` (bare char offsets)
    The extractor-registry shape uses ``start``/``end``/``kind``; we map
    ``start`` -> ``offset`` and preserve the rest so a chunk keeps the full
    structural marker. Anything unparseable is skipped rather than raising --
    offsets are an advisory aid, never a correctness dependency.
    """
    out: List[dict] = []
    if not offsets:
        return out
    for item in offsets:
        try:
            if isinstance(item, dict):
                marker = dict(item)
                if "offset" in item and item.get("offset") is not None:
                    marker["offset"] = int(item["offset"])
                elif "start" in item and item.get("start") is not None:
                    # Extractor marker shape: {start,end,kind,...}.
                    marker["offset"] = int(item["start"])
                    if "label" not in marker:
                        marker["label"] = item.get("kind") or item.get("label")
                else:
                    continue
                out.append(marker)
            elif isinstance(item, (tuple, list)) and item:
                off = int(item[0])
                label = item[1] if len(item) > 1 else None
                out.append({"offset": off, "label": label})
            else:
                out.append({"offset": int(item), "label": None})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda m: m["offset"])
    return out


def _markers_in_span(markers: List[dict], start: int, end: int) -> List[dict]:
    """Return the markers whose offset falls within ``[start, end)``."""
    return [m for m in markers if start <= m["offset"] < end]


def _find_break(text: str, hard_end: int, window_start: int) -> int:
    """Choose a chunk end at or before ``hard_end`` that lands on a nice break.

    Looks back up to ``_BREAK_LOOKBACK`` chars from ``hard_end`` for (in order)
    a paragraph break, a single newline, then a sentence terminator, then any
    whitespace. Returns ``hard_end`` unchanged if no nicer break is available or
    if a nicer break would make the chunk trivially small.
    """
    n = len(text)
    if hard_end >= n:
        return n
    band_start = max(window_start + 1, hard_end - _BREAK_LOOKBACK)
    band = text[band_start:hard_end]
    if not band:
        return hard_end

    # Paragraph / line breaks: cut AFTER the break so the newline stays with the
    # earlier chunk and the next chunk starts clean.
    for brk in _HARD_BREAKS:
        pos = band.rfind(brk)
        if pos != -1:
            return band_start + pos + len(brk)

    # Sentence terminators: cut after the terminator+space.
    best = -1
    for term in _SENTENCE_ENDS:
        pos = band.rfind(term)
        if pos != -1:
            best = max(best, band_start + pos + len(term))
    if best != -1:
        return best

    # Any whitespace.
    for i in range(len(band) - 1, -1, -1):
        if band[i].isspace():
            return band_start + i + 1

    return hard_end


def chunk(
    text: str,
    *,
    size: int = DEFAULT_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    offsets: Optional[Sequence] = None,
) -> List[Chunk]:
    """Slice ``text`` into overlapping, offset-exact chunks.

    Args:
        text:    the full extracted document text.
        size:    target maximum chunk length in characters (> 0).
        overlap: characters of overlap between consecutive chunks; clamped to
                 ``[0, size-1]`` so the window always advances.
        offsets: optional structural markers from the extractor; each chunk
                 records the markers that fall inside its span.

    Returns:
        A list of :class:`Chunk`. ``text[c.start:c.end] == c.text`` always.
        Whitespace-only / empty input yields ``[]``.

    Raises:
        ValueError: if ``size`` is not a positive integer.
    """
    if text is None:
        return []
    if not isinstance(size, int) or size <= 0:
        raise ValueError(f"chunk: size must be a positive int, got {size!r}")
    if not isinstance(overlap, int) or overlap < 0:
        overlap = 0
    # Clamp overlap so the window strictly advances each step.
    if overlap >= size:
        overlap = size - 1

    if not text.strip():
        return []

    markers = _normalize_offsets(offsets)
    n = len(text)
    chunks: List[Chunk] = []

    pos = 0
    index = 0
    # A guard against any pathological non-advance (should be impossible given
    # the overlap clamp, but cheap insurance against an infinite loop).
    guard = 0
    max_iters = 2 * (n // max(1, size - overlap) + 2)

    while pos < n:
        guard += 1
        if guard > max_iters:  # pragma: no cover - defensive
            break

        hard_end = min(pos + size, n)
        end = _find_break(text, hard_end, pos)
        # _find_break must never go past the hard window or before the start.
        if end <= pos:
            end = hard_end if hard_end > pos else n
        if end > n:
            end = n

        piece = text[pos:end]
        # Skip an all-whitespace chunk (can happen at a trailing newline run)
        # but still advance.
        if piece.strip():
            chunks.append(
                Chunk(
                    text=piece,
                    start=pos,
                    end=end,
                    index=index,
                    markers=_markers_in_span(markers, pos, end),
                )
            )
            index += 1

        if end >= n:
            break

        # Advance: next window starts ``overlap`` chars before this end, but
        # never before (or at) the current start, so we always move forward.
        next_pos = end - overlap
        if next_pos <= pos:
            next_pos = pos + 1
        pos = next_pos

    return chunks


def chunk_dicts(
    text: str,
    *,
    size: int = DEFAULT_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    offsets: Optional[Sequence] = None,
) -> List[dict]:
    """Convenience wrapper returning plain dicts instead of :class:`Chunk`."""
    return [c.to_dict() for c in chunk(text, size=size, overlap=overlap, offsets=offsets)]


if __name__ == "__main__":  # pragma: no cover - tiny manual harness
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="offset-tracked overlapping chunker")
    ap.add_argument("--file", help="path to a text file to chunk; '-' for stdin")
    ap.add_argument("--size", type=int, default=DEFAULT_SIZE)
    ap.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    ap.add_argument("--json", action="store_true", help="emit chunks as JSON")
    args = ap.parse_args()

    if not args.file or args.file == "-":
        data = sys.stdin.read()
    else:
        with open(args.file, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()

    result = chunk(data, size=args.size, overlap=args.overlap)
    if args.json:
        print(json.dumps([c.to_dict() for c in result], ensure_ascii=False, indent=2))
    else:
        for c in result:
            print(f"[{c.index}] {c.start}:{c.end} ({len(c.text)} chars)")
