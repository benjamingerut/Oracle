#!/usr/bin/env python3
"""truth_map.py -- parse TRUTH-MAP.md and resolve source authority by object.

`TRUTH-MAP.md` is doctrine *and* data: a single GitHub-style markdown table
keyed by business object. This module is the machine reader of that table. It is
consumed by ``answer_protocol.py`` before any material answer and surfaced on the
CLI as ``oracle truth rows`` / ``oracle truth resolve --object <name>``.

Parser contract (mirrors the "How it is read" section of TRUTH-MAP.md):

  * Find the FIRST markdown table whose header row contains, at minimum, the
    columns ``Business object``, ``Primary source``, ``Freshness budget`` and
    ``Status`` (case-insensitive, surrounding whitespace trimmed).
  * Header cells are the machine keys (lowercased, stripped). Every column is
    parsed; the four named ones are load-bearing.
  * ``resolve(object)`` matches a named business object against the
    ``Business object`` column case-insensitively and slash-/whitespace-
    tolerantly, returning the row dict or ``None`` when nothing claims authority.
  * A ``Primary source`` that is empty or the literal ``TBD`` means *no source
    yet claims authority*; callers (answer_protocol) treat that as missing.

A ``Row`` is a plain ``dict[str, str]`` keyed by the lowercased header text, plus
a normalized convenience key ``business_object`` (== the value of the
``Business object`` column). Values are the raw cell text, stripped.

Stdlib only.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional

__all__ = [
    "Row",
    "REQUIRED_COLUMNS",
    "load_rows",
    "parse_table",
    "resolve",
    "primary_source_is_authoritative",
    "normalize_object",
    "propose_row",
    "promote_row",
    "validate_rows",
    "TruthMapError",
    "CellValueError",
]

# The four load-bearing columns (lowercased machine keys). A table must contain
# all four (matched case-insensitively) to be recognized as THE truth map.
REQUIRED_COLUMNS = (
    "business object",
    "primary source",
    "freshness budget",
    "status",
)

# Values in the Primary source column that mean "no source yet claims authority".
# An empty cell, or one that is exactly one of these tokens, is not authoritative.
_NO_AUTHORITY_TOKENS = {"", "tbd", "n/a", "none", "-", "—"}
# A primary source that *begins* with the literal placeholder marker ``TBD``
# (e.g. the seed map's ``TBD connector`` / ``TBD accounting/ERP``) is a
# not-yet-wired placeholder, not a real authority of record.
_TBD_PREFIX = re.compile(r"^tbd\b", re.IGNORECASE)

# A row is just a dict of header_key -> cell_value, with an added
# 'business_object' convenience key.
Row = dict


class TruthMapError(ValueError):
    """Raised when TRUTH-MAP.md contains no recognizable truth-map table."""


class CellValueError(ValueError):
    """Raised when a cell value contains an illegal character (e.g. newline)."""


# --------------------------------------------------------------------------- #
# cell-value helpers: escaping and validation
# --------------------------------------------------------------------------- #
def _escape_cell(value: str) -> str:
    """Escape a cell value for safe markdown-table composition.

    * Pipes ``|`` → ``\\|`` so they are not parsed as column delimiters.
      The parser's ``_split_row`` already restores ``\\|`` → ``|`` on read,
      so propose → parse round-trips correctly.
    * Newlines are refused: a cell value containing ``\\n`` or ``\\r`` would
      silently break table structure and is never legal.
    """
    value = str(value)
    if "\n" in value or "\r" in value:
        raise CellValueError(
            f"cell value contains a newline, which is not allowed in a "
            f"markdown table cell: {value!r}"
        )
    return value.replace("|", "\\|")


def _compose_row(cells: list[str]) -> str:
    """Compose a validated, pipe-escaped markdown table row string.

    Raises ``CellValueError`` for any cell containing a newline. Pipes inside
    cell values are escaped as ``\\|`` so column structure is preserved.
    """
    escaped = [_escape_cell(c) for c in cells]
    return "| " + " | ".join(escaped) + " |"


# --------------------------------------------------------------------------- #
# normalization helpers
# --------------------------------------------------------------------------- #
def normalize_object(name: str) -> str:
    """Normalize a business-object name for tolerant matching.

    Lowercase, treat ``/`` as a space, collapse all whitespace runs to a single
    space, and strip. ``"Customers / accounts"`` and ``"customers  accounts"``
    therefore compare equal.
    """
    if name is None:
        return ""
    s = str(name).replace("/", " ").lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _cell_key(header_cell: str) -> str:
    """Machine key for a header cell: lowercased, whitespace-collapsed, stripped."""
    return re.sub(r"\s+", " ", str(header_cell).strip().lower())


def primary_source_is_authoritative(value: Optional[str]) -> bool:
    """True iff a ``Primary source`` cell names a real authority (not TBD/empty)."""
    if value is None:
        return False
    s = str(value).strip()
    if s.lower() in _NO_AUTHORITY_TOKENS:
        return False
    if _TBD_PREFIX.match(s):
        return False
    return True


# --------------------------------------------------------------------------- #
# table parsing
# --------------------------------------------------------------------------- #
def _split_row(line: str) -> Optional[list[str]]:
    """Split a markdown table row into trimmed cells, or return None if the line
    is not a table row (does not contain a pipe).

    Leading/trailing pipes are tolerated and dropped. Escaped pipes (``\\|``)
    inside a cell are preserved as a literal ``|``.
    """
    if "|" not in line:
        return None
    # Protect escaped pipes, split, then restore.
    placeholder = "\x00PIPE\x00"
    protected = line.replace("\\|", placeholder)
    raw = protected.strip()
    # Drop a single leading/trailing pipe (GitHub style).
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    cells = [c.replace(placeholder, "|").strip() for c in raw.split("|")]
    return cells


def _is_separator_row(cells: list[str]) -> bool:
    """A markdown separator row: every cell is dashes with optional ':' aligners."""
    if not cells:
        return False
    for c in cells:
        if not re.fullmatch(r":?-{1,}:?", c.strip()):
            return False
    return True


def parse_table(text: str) -> list[Row]:
    """Parse the first qualifying markdown table in ``text`` into rows.

    A qualifying table is one whose header row (the row immediately above a
    separator row) contains all of ``REQUIRED_COLUMNS`` (case-insensitive).

    Returns a list of ``Row`` dicts. Raises ``TruthMapError`` if no qualifying
    table is found.
    """
    lines = text.splitlines()
    n = len(lines)

    for i in range(n - 1):
        header_cells = _split_row(lines[i])
        if header_cells is None:
            continue
        sep_cells = _split_row(lines[i + 1])
        if sep_cells is None or not _is_separator_row(sep_cells):
            continue

        keys = [_cell_key(c) for c in header_cells]
        if not all(req in keys for req in REQUIRED_COLUMNS):
            continue

        # Found the truth-map table. Consume contiguous data rows below the
        # separator until a non-table line (blank or no pipe) terminates it.
        rows: list[Row] = []
        j = i + 2
        while j < n:
            cells = _split_row(lines[j])
            if cells is None:
                break
            # A stray separator inside the body terminates the table too.
            if _is_separator_row(cells):
                break
            # Pad/truncate to the header width so ragged rows don't crash.
            if len(cells) < len(keys):
                cells = cells + [""] * (len(keys) - len(cells))
            elif len(cells) > len(keys):
                cells = cells[: len(keys)]
            row: Row = {}
            for key, val in zip(keys, cells):
                row[key] = val
            row["business_object"] = row.get("business object", "")
            rows.append(row)
            j += 1
        return rows

    raise TruthMapError(
        "no truth-map table found: need a markdown table whose header has "
        f"columns {list(REQUIRED_COLUMNS)}"
    )


def _truth_map_path(root: Path) -> Path:
    return Path(root) / "TRUTH-MAP.md"


def load_rows(root) -> list[Row]:
    """Read ``<root>/TRUTH-MAP.md`` and return its parsed rows.

    Returns an empty list when the file is absent OR present but contains no
    qualifying table (bootstrap-empty / not-yet-written). A *malformed* table
    (recognized header but broken structure) still parses what it can; only a
    total absence of a qualifying table yields an empty list here -- callers
    that need to distinguish "no file" from "no table" can check the path.
    """
    path = _truth_map_path(root)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        return parse_table(text)
    except TruthMapError:
        return []


def resolve(business_object: str, root=None, *, rows: Optional[list[Row]] = None) -> Optional[Row]:
    """Resolve a named business object to its truth-map row, or ``None``.

    Provide either ``root`` (to read TRUTH-MAP.md) or a pre-parsed ``rows`` list.
    Matching is case-insensitive and slash-/whitespace-tolerant (see
    ``normalize_object``). The first row whose ``Business object`` normalizes
    equal to the query wins. Returns ``None`` when no row claims authority for
    the object (the answer protocol turns that into a refusal).
    """
    if rows is None:
        if root is None:
            raise ValueError("resolve: provide either root or rows")
        rows = load_rows(root)
    target = normalize_object(business_object)
    if not target:
        return None
    for row in rows:
        if normalize_object(row.get("business_object", "")) == target:
            return row
    return None


# --------------------------------------------------------------------------- #
# table editing (propose / promote) -- v2
# --------------------------------------------------------------------------- #
def _find_table(text: str):
    """Locate the qualifying table: (header_idx, sep_idx, end_idx, keys, lines).

    ``end_idx`` is the line index ONE PAST the last data row. Raises
    ``TruthMapError`` when no qualifying table exists.
    """
    lines = text.splitlines()
    n = len(lines)
    for i in range(n - 1):
        header_cells = _split_row(lines[i])
        if header_cells is None:
            continue
        sep_cells = _split_row(lines[i + 1])
        if sep_cells is None or not _is_separator_row(sep_cells):
            continue
        keys = [_cell_key(c) for c in header_cells]
        if not all(req in keys for req in REQUIRED_COLUMNS):
            continue
        j = i + 2
        while j < n:
            cells = _split_row(lines[j])
            if cells is None or _is_separator_row(cells):
                break
            j += 1
        return i, i + 1, j, keys, lines
    raise TruthMapError(
        "no truth-map table found: need a markdown table whose header has "
        f"columns {list(REQUIRED_COLUMNS)}"
    )


def _write_truth_map(root: Path, text: str) -> None:
    """Write TRUTH-MAP.md atomically under an exclusive fcntl lock.

    Mirrors the discipline of ``ledger.rewrite_atomic``: a temp file is written
    to the same directory, then ``os.replace``d into place while holding an
    exclusive advisory lock on the destination, so a reader never observes a
    partial write and a crash between the temp-write and the replace leaves the
    original intact.
    """
    path = _truth_map_path(Path(root))
    if not text.endswith("\n"):
        text += "\n"
    encoded = text.encode("utf-8")
    # Ensure the destination exists before we try to lock it (for new installs
    # the file is guaranteed to exist already since propose/promote read it
    # first, but be defensive).
    path.touch(exist_ok=True)
    # Hold an exclusive lock on the destination across the temp-write + replace
    # (same pattern as ledger.rewrite_atomic).
    # safe_paths-internal: lock handle on TRUTH-MAP.md (contained oracle path)
    lock_fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)  # safe_paths-internal
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd, tmp_name = tempfile.mkstemp(
            prefix="TRUTH-MAP.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "wb") as tf:  # safe_paths-internal
                tf.write(encoded)
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(tmp_name, str(path))  # safe_paths-internal: atomic truth-map swap
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        # fsync the directory so the rename is durable on Linux.
        try:
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:  # pragma: no cover - platform/fs may not support it
            pass
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _ledger_event(root: Path, event: dict) -> None:
    """Best-effort append to Meta.nosync/ledgers/truth_map.jsonl."""
    try:
        import ledger  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import ledger  # type: ignore
        except Exception:
            return
    try:
        path = Path(root) / "Meta.nosync" / "ledgers" / "truth_map.jsonl"
        ledger.append(path, event, id_prefix="tm")
    except Exception:
        pass


def _evidence_helpers():
    """Lazy import of answer_protocol evidence readers (avoids import cycle)."""
    try:
        import answer_protocol as ap  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        from . import answer_protocol as ap  # type: ignore
    return ap


def propose_row(
    root,
    business_object: str,
    *,
    primary_source: str = "TBD",
    freshness_budget: str = "review on change",
    status: str = "draft",
    actor: str = "system",
    extra: Optional[dict] = None,
) -> dict:
    """Create a draft truth-map row for ``business_object`` if none exists.

    Idempotent: an existing row is never modified (returns action='exists'),
    EXCEPT that an existing row whose primary source is still a TBD placeholder
    is upgraded in place when a real ``primary_source`` is supplied
    (action='source-set'). New rows append to the table with the supplied or
    default cells; unknown columns are left empty. Every change is recorded in
    the truth_map ledger.
    """
    bo = str(business_object or "").strip()
    if not bo:
        raise ValueError("propose_row: business_object is required")
    root = Path(root)
    path = _truth_map_path(root)
    if not path.exists():
        raise TruthMapError(f"TRUTH-MAP.md not found under {root}")
    text = path.read_text(encoding="utf-8")
    header_idx, sep_idx, end_idx, keys, lines = _find_table(text)

    bo_key = "business object"
    existing_idx: Optional[int] = None
    for j in range(sep_idx + 1, end_idx):
        cells = _split_row(lines[j])
        if not cells:
            continue
        row_map = dict(zip(keys, cells + [""] * (len(keys) - len(cells))))
        if normalize_object(row_map.get(bo_key, "")) == normalize_object(bo):
            existing_idx = j
            break

    if existing_idx is not None:
        cells = _split_row(lines[existing_idx]) or []
        cells = cells + [""] * (len(keys) - len(cells))
        row_map = dict(zip(keys, cells))
        if (
            primary_source
            and primary_source_is_authoritative(primary_source)
            and not primary_source_is_authoritative(row_map.get("primary source", ""))
        ):
            cells[keys.index("primary source")] = primary_source
            lines[existing_idx] = _compose_row(cells)
            _write_truth_map(root, "\n".join(lines))
            _ledger_event(
                root,
                {
                    "event": "truth_row_source_set",
                    "business_object": bo,
                    "primary_source": primary_source,
                    "actor": actor,
                },
            )
            return {"action": "source-set", "row": resolve(bo, root)}
        return {"action": "exists", "row": resolve(bo, root)}

    values = {
        "business object": bo,
        "primary source": primary_source or "TBD",
        "freshness budget": freshness_budget or "review on change",
        "status": status or "draft",
    }
    if extra:
        for k, v in extra.items():
            values[_cell_key(k)] = str(v)
    cells = [values.get(k, "") for k in keys]
    new_line = _compose_row(cells)
    lines.insert(end_idx, new_line)
    _write_truth_map(root, "\n".join(lines))
    _ledger_event(
        root,
        {
            "event": "truth_row_proposed",
            "business_object": bo,
            "primary_source": values["primary source"],
            "status": values["status"],
            "actor": actor,
        },
    )
    return {"action": "created", "row": resolve(bo, root)}


def promote_row(
    root,
    business_object: str,
    *,
    actor: str,
    role: str = "admin",
    because: str = "",
    require_evidence: bool = True,
) -> dict:
    """Flip a draft row to ``confirmed`` (admin capability, evidence-checked).

    Requirements: the role must hold ``change_truth_authority`` (policy gate),
    the row must exist with an authoritative primary source, and -- unless
    ``require_evidence=False`` -- at least one ingested Source must resolve to
    the row's authority. Raises PermissionError / TruthMapError / ValueError.
    """
    bo = str(business_object or "").strip()
    if not bo:
        raise ValueError("promote_row: business_object is required")
    root = Path(root)

    try:
        import policy  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        from . import policy  # type: ignore
    policy.require_role(actor, role, "change_truth_authority", root=root)

    path = _truth_map_path(root)
    if not path.exists():
        raise TruthMapError(f"TRUTH-MAP.md not found under {root}")
    text = path.read_text(encoding="utf-8")
    header_idx, sep_idx, end_idx, keys, lines = _find_table(text)

    target_idx: Optional[int] = None
    row_map: dict = {}
    for j in range(sep_idx + 1, end_idx):
        cells = _split_row(lines[j])
        if not cells:
            continue
        padded = cells + [""] * (len(keys) - len(cells))
        m = dict(zip(keys, padded))
        if normalize_object(m.get("business object", "")) == normalize_object(bo):
            target_idx = j
            row_map = m
            break
    if target_idx is None:
        raise TruthMapError(f"no truth-map row for {bo!r}; propose one first")

    primary = row_map.get("primary source", "")
    if not primary_source_is_authoritative(primary):
        raise TruthMapError(
            f"row for {bo!r} has no real primary source ({primary!r}); "
            "set a primary source before promoting"
        )

    evidence_count = 0
    if require_evidence:
        ap = _evidence_helpers()
        evidence_count = len(ap.gather_sources(root, bo, primary))
        if evidence_count == 0:
            raise TruthMapError(
                f"no ingested Source resolves to authority {primary!r} for {bo!r}; "
                "ingest evidence first or pass require_evidence=False"
            )

    if str(row_map.get("status", "")).strip().lower() == "confirmed":
        return {"action": "already-confirmed", "row": resolve(bo, root)}

    cells = _split_row(lines[target_idx]) or []
    cells = cells + [""] * (len(keys) - len(cells))
    cells[keys.index("status")] = "confirmed"
    lines[target_idx] = _compose_row(cells)
    _write_truth_map(root, "\n".join(lines))
    _ledger_event(
        root,
        {
            "event": "truth_row_promoted",
            "business_object": bo,
            "primary_source": primary,
            "evidence_count": evidence_count,
            "actor": actor,
            "role": role,
            "because": because,
        },
    )
    return {"action": "promoted", "row": resolve(bo, root), "evidence_count": evidence_count}


def validate_rows(root) -> list[dict]:
    """Per-row machine diagnostics: authority, evidence, freshness, promotability.

    Returns one dict per row:
      business_object, status, primary_source, authority (bool),
      evidence_count, newest_as_of, freshness (fresh|stale|unknown),
      promotable (draft + real source + >=1 resolving Source),
      needs (next-step hint string).
    """
    root = Path(root)
    rows = load_rows(root)
    if not rows:
        return []
    ap = _evidence_helpers()
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    # One catalog snapshot for the whole pass: per-row gathers become index
    # lookups instead of a folder walk (or staleness stat-sweep) per row.
    snap = ap.source_snapshot(root) if hasattr(ap, "source_snapshot") else None
    out: list[dict] = []
    for row in rows:
        bo = row.get("business_object", "")
        primary = row.get("primary source", "")
        status = str(row.get("status", "")).strip().lower()
        authority = primary_source_is_authoritative(primary)
        sources = ap.gather_sources(root, bo, primary, snap=snap) if authority else []
        candidates = ap.gather_object_evidence(root, bo, snap=snap)
        as_of = ap.newest_as_of(sources) if sources else None
        freshness = ap.freshness_for(as_of, row.get("freshness budget", ""), now)
        promotable = bool(status != "confirmed" and authority and sources)
        if not authority and candidates:
            needs = "set primary source (evidence exists): ./oracle admin truth propose"
        elif not authority:
            needs = "ingest evidence, then set primary source"
        elif not sources:
            needs = "ingest evidence for this authority"
        elif promotable:
            needs = "ready to promote: ./oracle admin truth promote"
        else:
            needs = ""
        out.append(
            {
                "business_object": bo,
                "status": status,
                "primary_source": primary,
                "authority": authority,
                "evidence_count": len(sources),
                "candidate_evidence_count": len(candidates),
                "newest_as_of": as_of.isoformat() if as_of else None,
                "freshness": freshness,
                "promotable": promotable,
                "needs": needs,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _render_rows_table(rows: list[Row]) -> str:
    if not rows:
        return "(no truth-map rows)"
    # Stable column order: business object, primary source, freshness budget,
    # status, then any remaining columns in first-seen order.
    head = ["business object", "primary source", "freshness budget", "status"]
    seen = list(head)
    for r in rows:
        for k in r:
            if k != "business_object" and k not in seen:
                seen.append(k)
    widths = {k: len(k) for k in seen}
    for r in rows:
        for k in seen:
            widths[k] = max(widths[k], len(str(r.get(k, ""))))
    line = " | ".join(k.ljust(widths[k]) for k in seen)
    sep = "-+-".join("-" * widths[k] for k in seen)
    out = [line, sep]
    for r in rows:
        out.append(" | ".join(str(r.get(k, "")).ljust(widths[k]) for k in seen))
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="truth_map",
        description="Parse TRUTH-MAP.md and resolve source authority by business object.",
    )
    ap.add_argument("--root", default=".", help="oracle root containing TRUTH-MAP.md")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("rows", help="print all parsed truth-map rows")

    r = sub.add_parser("resolve", help="resolve a business object to its row")
    r.add_argument("--object", required=True, help="business object name")
    r.add_argument("--json", action="store_true", help="emit JSON")

    pr = sub.add_parser("propose", help="propose a draft row (idempotent)")
    pr.add_argument("--object", required=True, help="business object name")
    pr.add_argument("--source", default="TBD", help="primary source (authority of record)")
    pr.add_argument("--freshness", default="review on change", help="freshness budget")
    pr.add_argument("--actor", default="system", help="who proposes (provenance, logged)")
    pr.add_argument("--json", action="store_true", help="emit JSON")

    pm = sub.add_parser("promote", help="promote a draft row to confirmed (admin)")
    pm.add_argument("--object", required=True, help="business object name")
    pm.add_argument("--actor", required=True, help="who promotes (provenance, logged)")
    pm.add_argument("--role", default="admin", help="role asserting change_truth_authority")
    pm.add_argument("--because", default="", help="reason recorded in the ledger")
    pm.add_argument(
        "--no-evidence-check",
        action="store_true",
        help="skip the >=1-resolving-Source requirement (admin override)",
    )
    pm.add_argument("--json", action="store_true", help="emit JSON")

    va = sub.add_parser("validate", help="machine diagnostics for every row")
    va.add_argument("--json", action="store_true", help="emit JSON")

    args = ap.parse_args(argv)
    root = Path(args.root)

    if args.cmd == "rows":
        rows = load_rows(root)
        print(_render_rows_table(rows))
        return 0

    if args.cmd == "resolve":
        row = resolve(args.object, root)
        if row is None:
            if args.json:
                print(json.dumps({"object": args.object, "row": None}))
            else:
                print(f"no authority row for {args.object!r}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"object": args.object, "row": row}, indent=2))
        else:
            for k, v in row.items():
                if k == "business_object":
                    continue
                print(f"{k}: {v}")
        return 0

    if args.cmd == "propose":
        try:
            result = propose_row(
                root,
                args.object,
                primary_source=args.source,
                freshness_budget=args.freshness,
                actor=args.actor,
            )
        except (TruthMapError, ValueError) as exc:
            print(f"propose failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"{result['action']}: {args.object}")
        return 0

    if args.cmd == "promote":
        try:
            result = promote_row(
                root,
                args.object,
                actor=args.actor,
                role=args.role,
                because=args.because,
                require_evidence=not args.no_evidence_check,
            )
        except PermissionError as exc:
            print(f"promote denied: {exc}", file=sys.stderr)
            return 3
        except (TruthMapError, ValueError) as exc:
            print(f"promote failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"{result['action']}: {args.object}")
        return 0

    if args.cmd == "validate":
        diags = validate_rows(root)
        if args.json:
            print(json.dumps(diags, indent=2, default=str))
        else:
            if not diags:
                print("(no truth-map rows)")
            for d in diags:
                flag = "OK " if d["freshness"] == "fresh" and d["status"] == "confirmed" else "-- "
                print(
                    f"{flag}{d['business_object']}: status={d['status']} "
                    f"authority={d['authority']} evidence={d['evidence_count']} "
                    f"freshness={d['freshness']}"
                    + (f"  NEXT: {d['needs']}" if d["needs"] else "")
                )
        return 0

    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
