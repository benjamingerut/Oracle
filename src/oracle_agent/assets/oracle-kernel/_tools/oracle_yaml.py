#!/usr/bin/env python3
"""oracle_yaml.py -- a conservative, security-critical safe-subset YAML loader.

The spawned kernel is stdlib-only and must never *require* pyyaml. This module
exposes a single entrypoint, ``safe_load(text) -> dict | list | scalar``, that:

  * supports ONLY the subset oracle.yml / connector manifests / note
    frontmatter actually use: block mappings, block sequences, scalars,
    single/double-quoted strings, ``#`` comments, and nested indentation;
  * RAISES ``UnsupportedYAML`` on anchors (``&``), aliases (``*``), tags
    (``!``), flow collections (``{...}`` / ``[...]``), or multi-document
    streams (``---``) -- *before* delegating -- so a richer engine can never
    silently accept a construct the rest of the system is not prepared to
    handle.

This "scan-for-forbidden-first, then parse with one fixed parser" ordering is
the whole safety argument: the answer is identical whether or not PyYAML is
installed, and no adversarial document triggers code execution or a silent
mis-parse. Bare ISO dates remain strings; the parser does not apply YAML 1.1
implicit timestamp coercions.

Stdlib only.
"""
from __future__ import annotations

import re

__all__ = ["safe_load", "UnsupportedYAML"]


class UnsupportedYAML(ValueError):
    """Raised when input uses a construct outside the supported safe subset."""


# ----------------------------------------------------------------------------
# Forbidden-construct scan (runs regardless of which backend parses).
# ----------------------------------------------------------------------------

def _strip_comment_outside_quotes(line: str) -> str:
    """Remove a trailing ``#`` comment that is not inside a quoted string."""
    out = []
    in_single = False
    in_double = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            # A comment requires a preceding whitespace or start-of-string.
            if i == 0 or line[i - 1].isspace():
                break
        out.append(ch)
        i += 1
    return "".join(out)


def _scan_forbidden(text: str) -> None:
    """Raise UnsupportedYAML if any forbidden construct appears.

    We scan line-by-line, ignoring content inside quotes and comments, so that
    a perfectly legal value like ``title: "a & b"`` is NOT mistaken for an
    anchor.
    """
    doc_markers = 0
    for raw in text.splitlines():
        stripped = raw.strip()
        # Multi-document / directive end markers.
        if stripped == "---" or stripped.startswith("--- "):
            doc_markers += 1
            if doc_markers >= 1:
                raise UnsupportedYAML("multi-document streams ('---') are not supported")
        if stripped == "...":
            raise UnsupportedYAML("document end marker ('...') is not supported")
        if stripped.startswith("%"):
            raise UnsupportedYAML("YAML directives ('%') are not supported")

        # Examine the comment-free, quote-aware remainder for sigils.
        body = _strip_comment_outside_quotes(raw)
        # Walk char by char tracking quote state to find unquoted sigils.
        in_single = in_double = False
        prev = ""
        for ch in body:
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                if ch == "&":
                    raise UnsupportedYAML("anchors ('&') are not supported")
                if ch == "*":
                    raise UnsupportedYAML("aliases ('*') are not supported")
                if ch == "!":
                    raise UnsupportedYAML("tags ('!') are not supported")
                if ch in "{}":
                    raise UnsupportedYAML("flow mappings ('{}') are not supported")
                if ch in "[]":
                    raise UnsupportedYAML("flow sequences ('[]') are not supported")
                if ch in "|>" and (prev == "" or prev == " " or prev == ":"):
                    raise UnsupportedYAML("block scalars ('|','>') are not supported")
            prev = ch


# ----------------------------------------------------------------------------
# Pure-stdlib block parser for the supported subset.
# ----------------------------------------------------------------------------

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def _scalar(token: str):
    """Convert a bare scalar token to a Python value."""
    s = token.strip()
    if s == "" or s == "~" or s.lower() == "null":
        return None
    if (len(s) >= 2) and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        inner = s[1:-1]
        if s[0] == '"':
            # Minimal double-quote escape handling.
            inner = inner.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")
        else:
            inner = inner.replace("''", "'")
        return inner
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if _INT_RE.match(s):
        try:
            return int(s)
        except ValueError:
            return s
    if _FLOAT_RE.match(s) and any(c in s for c in ".eE"):
        try:
            return float(s)
        except ValueError:
            return s
    return s


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Return (indent, content) for each significant line; drop blanks/comments.

    Tabs in indentation are a YAML error; we reject them to avoid ambiguity.
    """
    out: list[tuple[int, str]] = []
    for raw in text.split("\n"):
        if "\t" in (raw[: len(raw) - len(raw.lstrip())]):
            raise UnsupportedYAML("tab characters in indentation are not supported")
        content = _strip_comment_outside_quotes(raw).rstrip()
        if content.strip() == "":
            continue
        out.append((_indent(content), content.strip()))
    return out


def _split_key_value(content: str):
    """Split 'key: value' respecting quotes. Returns (key, value_str_or_None)."""
    in_single = in_double = False
    for i, ch in enumerate(content):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ":" and not in_single and not in_double:
            # 'key:' or 'key: value'
            after = content[i + 1 :]
            if after == "" or after.startswith(" "):
                key = content[:i].strip()
                val = after.strip()
                return key, (val if val != "" else None)
    return None, None


def _unquote_key(k: str) -> str:
    if len(k) >= 2 and ((k[0] == '"' and k[-1] == '"') or (k[0] == "'" and k[-1] == "'")):
        return _scalar(k)
    return k


def _parse_block(lines: list[tuple[int, str]], idx: int, indent: int):
    """Parse a block (mapping or sequence) starting at lines[idx] whose items
    are at column ``indent``. Returns (value, next_idx).
    """
    # Determine block kind from the first line at this indent.
    first = lines[idx][1]
    is_seq = first.startswith("- ") or first == "-"

    if is_seq:
        result_list: list = []
        i = idx
        while i < len(lines):
            cur_indent, content = lines[i]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise UnsupportedYAML(f"unexpected indentation at: {content!r}")
            if not (content.startswith("- ") or content == "-"):
                break
            item_body = content[1:].lstrip() if content != "-" else ""
            if item_body == "":
                # Nested block belongs to this sequence item.
                child, ni = _consume_child(lines, i + 1, indent)
                result_list.append(child)
                i = ni
            else:
                # Could be an inline scalar OR an inline 'key: value' opening a map.
                k, v = _split_key_value(item_body)
                if k is not None:
                    # Inline mapping item; its key sits at indent + (offset of body).
                    body_col = cur_indent + (len(content) - len(content[1:].lstrip()))
                    # Rebuild a synthetic line list position: parse the map made
                    # of this inline pair plus any deeper-indented following lines.
                    synthetic = [(body_col, item_body)]
                    j = i + 1
                    while j < len(lines) and lines[j][0] > cur_indent:
                        synthetic.append(lines[j])
                        j += 1
                    mval, _ = _parse_block(synthetic, 0, body_col)
                    result_list.append(mval)
                    i = j
                else:
                    result_list.append(_scalar(item_body))
                    i += 1
        return result_list, i

    # Mapping block.
    result_map: dict = {}
    i = idx
    while i < len(lines):
        cur_indent, content = lines[i]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise UnsupportedYAML(f"unexpected indentation at: {content!r}")
        if content.startswith("- ") or content == "-":
            break
        key, val = _split_key_value(content)
        if key is None:
            raise UnsupportedYAML(f"cannot parse mapping line: {content!r}")
        key = _unquote_key(key)
        if val is None:
            # Value is a nested block (or null if nothing follows deeper).
            child, ni = _consume_child(lines, i + 1, cur_indent)
            result_map[key] = child
            i = ni
        else:
            result_map[key] = _scalar(val)
            i += 1
    return result_map, i


def _consume_child(lines: list[tuple[int, str]], idx: int, parent_indent: int):
    """Consume a nested block deeper than parent_indent, or return None."""
    if idx >= len(lines):
        return None, idx
    child_indent, _ = lines[idx]
    if child_indent <= parent_indent:
        return None, idx
    return _parse_block(lines, idx, child_indent)


def _pure_load(text: str):
    lines = _logical_lines(text)
    if not lines:
        return None
    base_indent = lines[0][0]
    value, end = _parse_block(lines, 0, base_indent)
    if end != len(lines):
        # Trailing content at an unexpected indent.
        raise UnsupportedYAML(f"could not fully parse document near: {lines[end][1]!r}")
    return value


# ----------------------------------------------------------------------------
# Public entrypoint.
# ----------------------------------------------------------------------------

def safe_load(text: str):
    """Parse ``text`` and return a dict / list / scalar.

    Forbidden constructs raise ``UnsupportedYAML`` before parsing. The parser is
    intentionally single-backend and stdlib-only so scalar behavior cannot vary
    with optional site packages.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        raise TypeError("safe_load expects str")

    # Step 1: scan for forbidden constructs. This is the security guarantee and
    # runs before parsing, so the verdict is input-independent.
    _scan_forbidden(text)

    # Step 2: pure-stdlib parse.
    return _pure_load(text)


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    data = safe_load(sys.stdin.read())
    print(json.dumps(data, indent=2, default=str))
