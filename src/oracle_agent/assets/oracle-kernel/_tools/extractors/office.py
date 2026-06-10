#!/usr/bin/env python3
"""Office-format extractor: docx / xlsx / pdf (stdlib-first, graceful degradation).

Strategy per format:

* **.docx** -- an OOXML zip. Read ``word/document.xml`` with ``zipfile`` and walk
  it with ``xml.etree.ElementTree``: concatenate ``<w:t>`` runs, treat ``<w:p>``
  as paragraph breaks and ``<w:tab>``/``<w:br>`` as whitespace. Pure stdlib, no
  python-docx required.

* **.xlsx** -- an OOXML zip. Read ``xl/sharedStrings.xml`` (the dedup string
  table) and each ``xl/worksheets/sheet*.xml``; emit ``Sheet -> cell=value`` text
  with one offset per sheet. Pure stdlib, no openpyxl required.

* **.pdf** -- a genuinely hard format. Best-effort, in order:
    1. an OPTIONAL library (pdfminer.six) IF it happens to be importable;
    2. otherwise a naive stdlib pass that inflates FlateDecode streams with
       ``zlib`` and pulls text out of ``BT ... ET`` text objects via the
       ``(...) Tj`` / ``[...] TJ`` operators.
  If neither recovers meaningful text (e.g. a scanned/image-only PDF), we set
  ``needs_ocr=True`` and return empty text with a descriptive ``meta``. We NEVER
  hard-require the optional library and NEVER raise on a malformed PDF.

Optional-library detection is done lazily and defensively: a missing or broken
optional dependency simply means we use the stdlib path; it is reported in
``meta`` for transparency, never as an error that blocks ingest.

Every entrypoint is total: malformed/encrypted/empty/unknown office files return
a well-formed result dict, never an exception.
"""
from __future__ import annotations

import re
import zipfile
import zlib
from pathlib import Path
from typing import List, Optional
from xml.etree import ElementTree as ET

# OOXML namespaces
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _result(text="", offsets=None, meta=None, needs_ocr=False):
    return {
        "text": text,
        "offsets": list(offsets) if offsets else [],
        "meta": dict(meta) if meta else {},
        "needs_ocr": bool(needs_ocr),
    }


def _localname(tag: str) -> str:
    """Strip an XML namespace from a tag, e.g. '{ns}p' -> 'p'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #

def _extract_docx(p: Path, meta: dict) -> dict:
    try:
        with zipfile.ZipFile(p) as zf:
            names = set(zf.namelist())
            if "word/document.xml" not in names:
                meta["error"] = "no-document-xml"
                return _result(meta=meta, needs_ocr=True)
            xml_bytes = zf.read("word/document.xml")
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError) as exc:
        meta["error"] = f"bad-docx-zip: {exc}"
        return _result(meta=meta, needs_ocr=True)

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        meta["error"] = f"docx-xml-parse: {exc}"
        return _result(meta=meta, needs_ocr=True)

    paragraphs: List[str] = []
    # Each <w:p> is a paragraph; collect descendant <w:t> text, honouring
    # <w:tab> and <w:br>/<w:cr> as whitespace within the paragraph.
    for para in root.iter(f"{_W_NS}p"):
        buf: List[str] = []
        for node in para.iter():
            ln = _localname(node.tag)
            if ln == "t":
                if node.text:
                    buf.append(node.text)
            elif ln == "tab":
                buf.append("\t")
            elif ln in ("br", "cr"):
                buf.append(" ")
        line = "".join(buf).strip()
        if line:
            paragraphs.append(line)

    if not paragraphs:
        meta["empty_text"] = True
        return _result(text="", offsets=[], meta=meta, needs_ocr=True)

    return _join_blocks(paragraphs, meta, kind="paragraph")


# --------------------------------------------------------------------------- #
# XLSX
# --------------------------------------------------------------------------- #

def _xlsx_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    """Parse xl/sharedStrings.xml into an indexed list of strings."""
    try:
        data = zf.read("xl/sharedStrings.xml")
    except (KeyError, OSError):
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    strings: List[str] = []
    for si in root.findall(f"{_S_NS}si"):
        # An <si> can be a single <t> or several <r><t> runs.
        parts: List[str] = []
        for t in si.iter(f"{_S_NS}t"):
            if t.text:
                parts.append(t.text)
        strings.append("".join(parts))
    return strings


def _xlsx_sheet_names(zf: zipfile.ZipFile) -> List[str]:
    names = [n for n in zf.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
    # numeric sort so sheet2 < sheet10
    def _key(n: str) -> int:
        m = re.search(r"sheet(\d+)\.xml$", n)
        return int(m.group(1)) if m else 0

    return sorted(names, key=_key)


def _xlsx_cell_value(cell: ET.Element, shared: List[str]) -> str:
    """Resolve one <c> cell's display value."""
    ctype = cell.get("t", "n")  # default numeric
    v = cell.find(f"{_S_NS}v")
    if ctype == "s":  # shared string index
        if v is not None and v.text is not None:
            try:
                idx = int(v.text)
            except ValueError:
                return ""
            if 0 <= idx < len(shared):
                return shared[idx]
        return ""
    if ctype == "inlineStr":
        parts = [t.text or "" for t in cell.iter(f"{_S_NS}t")]
        return "".join(parts)
    if v is not None and v.text is not None:
        return v.text
    # boolean / formula-with-cached-string fall through to any <t>
    parts = [t.text or "" for t in cell.iter(f"{_S_NS}t")]
    return "".join(parts)


def _extract_xlsx(p: Path, meta: dict) -> dict:
    try:
        with zipfile.ZipFile(p) as zf:
            shared = _xlsx_shared_strings(zf)
            sheet_files = _xlsx_sheet_names(zf)
            if not sheet_files:
                meta["error"] = "no-worksheets"
                return _result(meta=meta, needs_ocr=True)

            text_parts: List[str] = []
            offsets: List[dict] = []
            cursor = 0
            sheet_idx = 0
            nonempty_cells = 0

            for sf in sheet_files:
                try:
                    data = zf.read(sf)
                    root = ET.fromstring(data)
                except (KeyError, OSError, ET.ParseError):
                    continue
                rows_text: List[str] = []
                for row in root.iter(f"{_S_NS}row"):
                    cells: List[str] = []
                    for c in row.findall(f"{_S_NS}c"):
                        val = _xlsx_cell_value(c, shared).strip()
                        if val:
                            ref = c.get("r", "")
                            cells.append(f"{ref}={val}" if ref else val)
                            nonempty_cells += 1
                    if cells:
                        rows_text.append(" | ".join(cells))
                sheet_label = re.search(r"(sheet\d+)\.xml$", sf)
                sheet_name = sheet_label.group(1) if sheet_label else f"sheet{sheet_idx + 1}"
                block = f"[{sheet_name}]\n" + "\n".join(rows_text) if rows_text else f"[{sheet_name}]"
                text_parts.append(block)
                # blocks are joined by "\n\n" (2 chars) below; the offset covers
                # only the block's own span, the separator lives in the gap.
                end = cursor + len(block)
                offsets.append(
                    {"start": cursor, "end": end, "kind": "sheet", "sheet": sheet_idx}
                )
                cursor = end + 2  # +2 for the "\n\n" sheet separator
                sheet_idx += 1
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        meta["error"] = f"bad-xlsx-zip: {exc}"
        return _result(meta=meta, needs_ocr=True)

    if nonempty_cells == 0:
        meta["empty_text"] = True
        meta["sheets"] = sheet_idx
        return _result(text="", offsets=[], meta=meta, needs_ocr=True)

    text = "\n\n".join(text_parts)
    meta["sheets"] = sheet_idx
    meta["cells"] = nonempty_cells
    meta["chars"] = len(text)
    return _result(text=text, offsets=offsets, meta=meta, needs_ocr=False)


# --------------------------------------------------------------------------- #
# PDF (best-effort)
# --------------------------------------------------------------------------- #

def _pdf_optional_lib(p: Path, meta: dict) -> Optional[str]:
    """Try an OPTIONAL pdf library if present. Return text or None.

    We never require this. If the import fails (library not installed) or the
    call fails (broken/encrypted file), we silently fall back to the stdlib pass.
    """
    try:
        from pdfminer.high_level import extract_text  # type: ignore
    except Exception:  # noqa: BLE001 - absence is the normal case
        meta["pdf_optional_lib"] = "absent"
        return None
    try:
        text = extract_text(str(p)) or ""
    except Exception as exc:  # noqa: BLE001
        meta["pdf_optional_lib"] = f"present-but-failed: {exc}"
        return None
    meta["pdf_optional_lib"] = "pdfminer"
    return text


# PDF content-stream text operators:
#   (literal string) Tj     and    [ (a) -k (b) ] TJ
_TJ_STR = re.compile(rb"\((?:\\.|[^\\()])*\)")
_TEXT_OBJ = re.compile(rb"BT(.*?)ET", re.DOTALL)


def _unescape_pdf_literal(raw: bytes) -> str:
    """Decode a PDF literal-string body (without the surrounding parens)."""
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b == 0x5C and i + 1 < n:  # backslash escape
            nxt = raw[i + 1]
            mapping = {
                0x6E: 0x0A,  # \n
                0x72: 0x0D,  # \r
                0x74: 0x09,  # \t
                0x62: 0x08,  # \b
                0x66: 0x0C,  # \f
                0x28: 0x28,  # \(
                0x29: 0x29,  # \)
                0x5C: 0x5C,  # \\
            }
            if nxt in mapping:
                out.append(mapping[nxt])
                i += 2
                continue
            # octal escape \ddd
            if 0x30 <= nxt <= 0x37:
                j = i + 1
                octal = b""
                while j < n and len(octal) < 3 and 0x30 <= raw[j] <= 0x37:
                    octal += bytes([raw[j]])
                    j += 1
                try:
                    out.append(int(octal, 8) & 0xFF)
                except ValueError:
                    pass
                i = j
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(b)
        i += 1
    # PDF literals are commonly Latin-1 / PDFDocEncoding; latin-1 always decodes.
    return out.decode("latin-1", errors="replace")


def _pdf_text_from_stream(stream: bytes) -> str:
    """Pull text out of a decompressed content stream."""
    pieces: List[str] = []
    for bt in _TEXT_OBJ.findall(stream):
        for lit in _TJ_STR.findall(bt):
            body = lit[1:-1]  # strip parens
            s = _unescape_pdf_literal(body)
            if s:
                pieces.append(s)
    return "".join(pieces)


def _pdf_stdlib(p: Path, meta: dict) -> str:
    """Naive stdlib PDF text recovery: inflate FlateDecode streams, scan text ops."""
    try:
        raw = p.read_bytes()
    except OSError as exc:
        meta["pdf_stdlib_error"] = f"read-failed: {exc}"
        return ""

    collected: List[str] = []
    streams_seen = 0
    streams_decoded = 0

    # Walk every "stream ... endstream" span. Try zlib-inflate (FlateDecode);
    # if that fails, scan the raw bytes too (uncompressed content streams exist).
    idx = 0
    marker = b"stream"
    endmarker = b"endstream"
    while True:
        s = raw.find(marker, idx)
        if s == -1:
            break
        # content begins after the EOL following 'stream'
        body_start = s + len(marker)
        if raw[body_start:body_start + 2] == b"\r\n":
            body_start += 2
        elif raw[body_start:body_start + 1] in (b"\n", b"\r"):
            body_start += 1
        e = raw.find(endmarker, body_start)
        if e == -1:
            break
        blob = raw[body_start:e]
        idx = e + len(endmarker)
        streams_seen += 1

        decoded_text = ""
        try:
            inflated = zlib.decompress(blob)
            streams_decoded += 1
            decoded_text = _pdf_text_from_stream(inflated)
        except (zlib.error, Exception):  # noqa: BLE001
            # Maybe an uncompressed content stream.
            try:
                decoded_text = _pdf_text_from_stream(blob)
            except Exception:  # noqa: BLE001
                decoded_text = ""
        if decoded_text:
            collected.append(decoded_text)

    meta["pdf_streams_seen"] = streams_seen
    meta["pdf_streams_decoded"] = streams_decoded
    return "\n".join(c for c in collected if c.strip())


def _extract_pdf(p: Path, meta: dict) -> dict:
    # 1) optional library, if it happens to be importable
    text = _pdf_optional_lib(p, meta)
    # 2) stdlib best-effort fallback
    if not text or not text.strip():
        text = _pdf_stdlib(p, meta)

    if not text or not text.strip():
        # Bytes exist but no recoverable text layer -> scanned/image PDF.
        meta.setdefault("error", "no-text-layer")
        meta["needs_ocr_reason"] = "pdf-has-no-extractable-text"
        return _result(text="", offsets=[], meta=meta, needs_ocr=True)

    # Split into pseudo-paragraphs on blank lines for offset structure.
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if not blocks:
        blocks = [text.strip()]
    return _join_blocks(blocks, meta, kind="page")


# --------------------------------------------------------------------------- #
# shared block joiner
# --------------------------------------------------------------------------- #

def _join_blocks(blocks: List[str], meta: dict, kind: str) -> dict:
    parts: List[str] = []
    offsets: List[dict] = []
    cursor = 0
    for i, block in enumerate(blocks):
        parts.append(block)
        end = cursor + len(block)  # block's own span; '\n' lives in the gap
        offsets.append({"start": cursor, "end": end, "kind": kind, kind: i})
        cursor = end + 1  # +1 for the joining newline
    text = "\n".join(parts)
    meta["chars"] = len(text)
    meta[f"{kind}s"] = len(blocks)
    return _result(text=text, offsets=offsets, meta=meta, needs_ocr=False)


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

def extract(path) -> dict:
    """Extract text from a docx / xlsx / pdf file, degrading gracefully."""
    p = Path(path)
    suffix = p.suffix.lower()
    meta = {"extractor": "office", "suffix": suffix}

    try:
        size = p.stat().st_size
    except OSError as exc:
        meta["error"] = f"stat-failed: {exc}"
        return _result(meta=meta, needs_ocr=True)

    if size == 0:
        meta["empty"] = True
        return _result(meta=meta, needs_ocr=False)

    try:
        if suffix == ".docx":
            return _extract_docx(p, meta)
        if suffix == ".xlsx":
            return _extract_xlsx(p, meta)
        if suffix == ".pdf":
            return _extract_pdf(p, meta)
    except Exception as exc:  # noqa: BLE001 - belt-and-suspenders; must never raise
        meta["error"] = f"office-extractor-crashed: {type(exc).__name__}: {exc}"
        return _result(meta=meta, needs_ocr=True)

    # Unknown office-ish suffix routed here by mistake: signal we can't handle it.
    meta["error"] = f"unsupported-office-suffix: {suffix}"
    return _result(meta=meta, needs_ocr=True)


__all__ = ["extract"]
