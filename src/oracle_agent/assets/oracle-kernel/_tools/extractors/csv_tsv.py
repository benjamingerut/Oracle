#!/usr/bin/env python3
"""CSV / TSV extractor (stdlib ``csv`` only).

Turns delimited tabular data into a flat, searchable text rendering while
preserving row structure as offsets so downstream chunking and citation can
point at "row N". The header row (if detected) is emitted first and tagged so a
consumer can re-associate cells with column names.

Rendering
---------
Each record becomes one text line of the form ``col=value | col=value | ...``
when a header is present, or ``value | value | ...`` otherwise. This keeps the
indexed text human-readable and keeps column names adjacent to their values
(better for retrieval than a bare comma dump). The original delimiter is
sniffed; .tsv/.tab default to tab.

Robustness
----------
* Empty files -> empty text, no error.
* Undecodable bytes -> ``needs_ocr=True`` with ``meta["error"]``.
* Ragged rows (varying field counts) are handled: extra cells get positional
  ``colN`` keys, missing cells are simply absent.
* The stdlib csv reader can raise on pathological input (e.g. a NUL byte); we
  catch that and fall back to a naive line split so we still return something.

Never raises on content.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import List, Optional

_CODECS = ("utf-8-sig", "utf-8", "utf-16", "latin-1")

# csv has a hard field-size limit; bump it generously but bounded so a
# crafted file cannot exhaust memory through a single giant field.
_MAX_FIELD = 4 * 1024 * 1024  # 4 MiB per field is already absurd for a cell


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


def _default_delimiter(suffix: str) -> str:
    if suffix in (".tsv", ".tab"):
        return "\t"
    return ","


def _sniff_delimiter(sample: str, suffix: str) -> str:
    """Best-effort delimiter sniff; fall back to the suffix default."""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        return dialect.delimiter
    except (csv.Error, Exception):  # noqa: BLE001 - sniffer is fussy, never fatal
        return _default_delimiter(suffix)


def _has_header(sample: str) -> bool:
    try:
        return csv.Sniffer().has_header(sample)
    except (csv.Error, Exception):  # noqa: BLE001
        return False


def _render_row(fields: List[str], header: Optional[List[str]]) -> str:
    cells = []
    for i, val in enumerate(fields):
        val = (val or "").strip()
        if header is not None and i < len(header) and header[i]:
            cells.append(f"{header[i].strip()}={val}")
        else:
            key = header[i].strip() if (header and i < len(header)) else f"col{i + 1}"
            cells.append(f"{key}={val}" if val else key)
    return " | ".join(cells)


def _naive_rows(text: str, delimiter: str) -> List[List[str]]:
    """Fallback row split when the csv module refuses the input."""
    rows: List[List[str]] = []
    for line in text.splitlines():
        rows.append(line.split(delimiter))
    return rows


def extract(path) -> dict:
    """Extract a flattened, row-offset text rendering of a CSV/TSV file."""
    p = Path(path)
    suffix = p.suffix.lower()
    meta = {"extractor": "csv_tsv", "suffix": suffix}

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

    sample = decoded[:8192]
    delimiter = _sniff_delimiter(sample, suffix)
    header_present = _has_header(sample)
    meta["delimiter"] = "\\t" if delimiter == "\t" else delimiter

    # Parse rows. Guard the csv field-size limit and any reader explosion.
    old_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(_MAX_FIELD)
    except (OverflowError, ValueError):
        pass

    rows: List[List[str]] = []
    try:
        reader = csv.reader(io.StringIO(decoded), delimiter=delimiter)
        for record in reader:
            rows.append(record)
    except (csv.Error, Exception) as exc:  # noqa: BLE001 - fall back, never fatal
        meta["csv_warning"] = f"reader-fallback: {exc}"
        rows = _naive_rows(decoded, delimiter)
    finally:
        try:
            csv.field_size_limit(old_limit)
        except (OverflowError, ValueError):
            pass

    if not rows:
        meta["empty"] = True
        return _result(meta=meta)

    header: Optional[List[str]] = None
    data_rows = rows
    if header_present and rows:
        header = [c.strip() for c in rows[0]]
        data_rows = rows[1:]
        meta["columns"] = header

    # Build the flattened text plus row offsets.
    parts: List[str] = []
    offsets: List[dict] = []
    cursor = 0

    # ``end`` covers each block's own span; the joining '\n' lives in the gap
    # between consecutive offsets, so the final offset never overshoots len(text).
    if header is not None:
        header_line = "columns: " + " | ".join(c for c in header if c)
        parts.append(header_line)
        end = cursor + len(header_line)
        offsets.append(
            {"start": cursor, "end": end, "kind": "row", "row": 0, "header": True}
        )
        cursor = end + 1  # +1 for the joining newline

    row_no = 1
    for fields in data_rows:
        line = _render_row(fields, header)
        parts.append(line)
        end = cursor + len(line)
        offsets.append(
            {"start": cursor, "end": end, "kind": "row", "row": row_no}
        )
        cursor = end + 1
        row_no += 1

    text = "\n".join(parts)
    meta["rows"] = len(data_rows)
    meta["chars"] = len(text)
    return _result(text=text, offsets=offsets, meta=meta, needs_ocr=False)


__all__ = ["extract"]
