#!/usr/bin/env python3
"""Durable append-only JSONL ledger primitive for the oracle kernel.

This is a FLOOR durability module (stdlib-only). It is the single place where
ledger lines are written. Every line is one JSON object carrying at least
``drop_id`` (str) and ``ts`` (ISO-8601 seconds). Ledgers live at
``Meta.nosync/ledgers/<name>.jsonl`` (tracked) and at
``Workproduct.nosync/{_INPUT,_OUTPUT}/.registry.jsonl``.

Design guarantees:
* ``append`` writes one JSON line under an exclusive advisory lock
  (``fcntl.flock`` LOCK_EX) and ``os.fsync``s the file descriptor so a crash
  cannot leave a torn line behind.
* ``load`` is corruption-tolerant: it parses line-by-line under try/except,
  diverts any line that does not parse to ``<path>.quarantine`` (counted in the
  returned warnings), and NEVER raises. One bad line cannot brick reads.
* ``rewrite_atomic`` writes a temp file in the same directory and ``os.replace``s
  it into place under lock, so a reader never observes a half-written ledger.
* ``next_id`` mints collision-checked ``PREFIX-YYYYMMDD-NNN`` ids while holding
  the lock, so two concurrent writers cannot mint the same id.

The raw ``open(...)`` / ``fcntl`` / ``os.fsync`` / ``os.replace`` calls here are
the legitimate internals of the durability chokepoint and are tagged
``# safe_paths-internal`` so the no-bypass guard allowlists them. Callers pass an
already-contained path in; this module does not itself derive user paths.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    """ISO-8601 to the second (no microseconds), local time."""
    return datetime.now().isoformat(timespec="seconds")


def _today_compact() -> str:
    return datetime.now().strftime("%Y%m%d")


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _quarantine_path(path: Path) -> Path:
    return path.with_name(path.name + ".quarantine")


# --------------------------------------------------------------------------- #
# hash-chain helpers
# --------------------------------------------------------------------------- #
def _row_canonical(row: dict) -> str:
    """Canonical JSON serialization of a row (sorted keys, no spaces).

    Excludes ``row_hash`` so the hash commits to all content fields only.
    """
    stripped = {k: v for k, v in row.items() if k != "row_hash"}
    return json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _compute_row_hash(row: dict, prev_hash: str) -> str:
    """sha256 of canonical(row) concatenated with prev_hash."""
    material = _row_canonical(row) + prev_hash
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _line_sha256(raw_line: str) -> str:
    """sha256 of the raw line string (used for quarantine deduplication)."""
    return hashlib.sha256(raw_line.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# core API
# --------------------------------------------------------------------------- #
def append(path: Path, row: dict, *, id_prefix: str | None = None) -> str:
    """Append a single JSON object as one line, durably.

    Acquires an exclusive advisory lock for the duration of the write, emits a
    compact JSON line terminated by a newline, flushes and ``os.fsync``s before
    releasing the lock. The row is normalised so it always carries ``drop_id``
    and ``ts``; if the caller omitted ``ts`` we stamp it now.

    Collision-safe id minting: if ``id_prefix`` is given, the ``drop_id`` is
    minted *under the same lock* as the write by scanning the rows already on
    disk -- so two concurrent ``append(..., id_prefix=...)`` calls can never
    mint the same id (there is no read/append TOCTOU gap). If the row already
    carries a ``drop_id`` it is preserved. With neither, a generic ``LOG`` id is
    minted under the lock. Returns the final ``drop_id``.
    """
    path = Path(path)
    if not isinstance(row, dict):
        raise TypeError("ledger.append requires a dict row")
    _ensure_parent(path)
    payload = dict(row)
    payload.setdefault("ts", _now_iso())
    prefix = id_prefix or (None if payload.get("drop_id") else "LOG")
    # safe_paths-internal: ledger durability append (caller-supplied contained path)
    with open(path, "a+", encoding="utf-8") as f:  # noqa: SAFEPATHS  # safe_paths-internal
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            if prefix:
                payload["drop_id"] = _next_id_locked(f, prefix)
            # Compute hash chain: read the last row_hash from disk (under the
            # same lock so no concurrent appender can race us).
            prev_hash = _last_row_hash_locked(f)
            payload["row_hash"] = _compute_row_hash(payload, prev_hash)
            line = json.dumps(payload, ensure_ascii=False, sort_keys=False)
            f.seek(0, os.SEEK_END)
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return str(payload["drop_id"])


def _next_id_locked(f, prefix: str) -> str:
    """Mint the next free PREFIX-YYYYMMDD-NNN id from an already-locked handle.

    The caller holds LOCK_EX on ``f``; we read the existing ids from the same
    descriptor (so we see exactly what is durably present) and pick the lowest
    unused sequence number for today.
    """
    prefix = (prefix or "ID").strip() or "ID"
    base = f"{prefix}-{_today_compact()}-"
    existing: set[str] = set()
    try:
        f.seek(0)
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                did = str(obj.get("drop_id", ""))
                if did.startswith(base):
                    existing.add(did)
    except OSError:  # pragma: no cover - defensive
        pass
    n = 1
    while f"{base}{n:03d}" in existing:
        n += 1
    return f"{base}{n:03d}"


def _last_row_hash_locked(f) -> str:
    """Return the ``row_hash`` of the last parseable row in ``f`` (already locked).

    Reads from position 0; returns ``""`` if the file is empty or no row carries
    ``row_hash`` (legacy prefix).
    """
    last_hash = ""
    try:
        f.seek(0)
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and obj.get("row_hash"):
                last_hash = str(obj["row_hash"])
    except OSError:  # pragma: no cover - defensive
        pass
    return last_hash


def load(path: Path) -> tuple[list[dict], list[str]]:
    """Load all rows; corruption-tolerant.

    Returns ``(rows, warnings)``. Each non-blank line is parsed independently;
    a line that is not a JSON object is appended verbatim to
    ``<path>.quarantine`` and a warning is recorded. This function NEVER raises
    for content reasons -- a single corrupt line cannot brick the ledger.
    """
    path = Path(path)
    rows: list[dict] = []
    warnings: list[str] = []
    if not path.exists():
        return rows, warnings
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - filesystem error
        return rows, [f"unreadable ledger {path}: {exc}"]

    # Load the set of raw-line hashes already in quarantine so we never
    # re-quarantine (or re-warn) for a line we have seen before.
    already_quarantined: set[str] = _load_quarantine_hashes(path)

    new_quarantine: list[str] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            lhash = _line_sha256(line)
            if lhash not in already_quarantined:
                new_quarantine.append(line)
                already_quarantined.add(lhash)
                warnings.append(f"line {lineno}: unparseable JSON, quarantined")
            # else: already seen — emit no duplicate warning, no duplicate entry
            continue
        if not isinstance(obj, dict):
            lhash = _line_sha256(line)
            if lhash not in already_quarantined:
                new_quarantine.append(line)
                already_quarantined.add(lhash)
                warnings.append(f"line {lineno}: not a JSON object, quarantined")
            continue
        rows.append(obj)
    if new_quarantine:
        _quarantine(path, new_quarantine)
        warnings.append(
            f"quarantined {len(new_quarantine)} bad line(s) to {_quarantine_path(path).name}"
        )
    return rows, warnings


def _load_quarantine_hashes(path: Path) -> set[str]:
    """Return the set of sha256 hashes of raw lines already in the quarantine file.

    Each quarantine line has format ``<ts>\\t<original_line>``; we extract the
    original line by stripping the leading timestamp+tab prefix and hash it.
    Returns an empty set if the quarantine file does not exist.
    """
    qpath = _quarantine_path(path)
    if not qpath.exists():
        return set()
    hashes: set[str] = set()
    try:
        for qline in qpath.read_text(encoding="utf-8", errors="replace").splitlines():
            # Format written by _quarantine(): "<stamp>\t<original_line>"
            tab_idx = qline.find("\t")
            if tab_idx >= 0:
                original = qline[tab_idx + 1 :]
                hashes.add(_line_sha256(original))
    except OSError:
        pass
    return hashes


def _quarantine(path: Path, bad_lines: list[str]) -> None:
    qpath = _quarantine_path(path)
    _ensure_parent(qpath)
    stamp = _now_iso()
    block = "".join(f"{stamp}\t{line}\n" for line in bad_lines)
    # safe_paths-internal: ledger quarantine sink (derived from caller path)
    with open(qpath, "a", encoding="utf-8") as f:  # noqa: SAFEPATHS  # safe_paths-internal
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(block)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def rewrite_atomic(
    path: Path,
    rows: list[dict],
    *,
    actor: str = "repair",
    reason: str = "rewrite",
) -> None:
    """Replace the entire ledger with ``rows`` atomically.

    Re-chains ``row_hash`` for every surviving row so the hash chain is
    consistent after the rewrite, and appends an auditable rewrite-marker row
    recording ``actor`` and ``reason``.

    Writes a temp file in the SAME directory (so ``os.replace`` is atomic on the
    same filesystem) and replaces the target while holding the lock on the
    destination, so a concurrent reader sees either the old or the new file in
    full, never a partial state.
    """
    path = Path(path)
    _ensure_parent(path)

    # Re-chain hashes: strip any existing row_hash and recompute from scratch.
    rechained: list[dict] = []
    prev_hash = ""
    for r in rows:
        row = {k: v for k, v in r.items() if k != "row_hash"}
        row["row_hash"] = _compute_row_hash(row, prev_hash)
        prev_hash = row["row_hash"]
        rechained.append(row)

    # Append a rewrite-marker row (also part of the chain).
    marker: dict = {
        "drop_id": "REWRITE-MARKER",
        "ts": _now_iso(),
        "event": "ledger_rewrite",
        "actor": actor,
        "reason": reason,
    }
    marker["row_hash"] = _compute_row_hash(marker, prev_hash)
    rechained.append(marker)

    lines = [json.dumps(dict(r), ensure_ascii=False) for r in rechained]
    body = ("\n".join(lines) + "\n") if lines else ""
    # Hold an exclusive lock on the destination across the temp-write + replace.
    # safe_paths-internal: lock handle on destination ledger (caller-supplied path)
    lock_fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)  # safe_paths-internal
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tf:  # safe_paths-internal
                tf.write(body)
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(tmp_name, str(path))  # safe_paths-internal: atomic ledger swap
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        # fsync the directory so the rename is durable
        dir_fd = os.open(str(path.parent), os.O_DIRECTORY)  # safe_paths-internal
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def next_id(path: Path, prefix: str) -> str:
    """Mint a collision-checked ``PREFIX-YYYYMMDD-NNN`` id.

    Reads the existing rows under an exclusive lock and chooses the next free
    sequence number for today's date that does not already appear in the file.
    This guarantees uniqueness against everything already durable on disk.

    NOTE: for fully concurrent writers, prefer ``append(path, row,
    id_prefix=PREFIX)`` which mints the id AND writes the row under a single
    lock, eliminating the read/append window entirely.
    """
    path = Path(path)
    prefix = (prefix or "ID").strip() or "ID"
    day = _today_compact()
    base = f"{prefix}-{day}-"
    _ensure_parent(path)
    # safe_paths-internal: lock handle for collision-safe id minting
    lock_fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)  # safe_paths-internal
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        existing: set[str] = set()
        try:
            with os.fdopen(os.dup(lock_fd), "r", encoding="utf-8", errors="replace") as f:  # safe_paths-internal
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(obj, dict):
                        did = str(obj.get("drop_id", ""))
                        if did.startswith(base):
                            existing.add(did)
        except OSError:
            pass
        n = 1
        while True:
            candidate = f"{base}{n:03d}"
            if candidate not in existing:
                return candidate
            n += 1
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


# --------------------------------------------------------------------------- #
# verify / repair
# --------------------------------------------------------------------------- #
def verify(path: Path) -> dict:
    """Inspect a ledger without modifying it.

    Returns a report dict: total physical lines, parsed-ok count, bad-line
    count + their line numbers, duplicate drop_ids, rows missing required
    keys (``drop_id``/``ts``), hash-chain breaks (line numbers), and legacy
    unhashed prefix row count. ``ok`` is True only when all checks pass.

    Hash-chain semantics:
    - Rows without ``row_hash`` are treated as a legacy prefix; they are
      counted in ``legacy_rows`` and do not participate in chain validation.
    - From the first row that carries ``row_hash`` onward every row is
      validated: the stored ``row_hash`` must equal
      ``sha256(canonical(row_without_row_hash) + prev_row_hash)`` and there
      must be no gaps (deleted/reordered rows break the ``prev`` linkage).
    """
    path = Path(path)
    report: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "lines": 0,
        "ok_rows": 0,
        "bad_lines": [],
        "duplicate_ids": [],
        "missing_keys": [],
        "chain_breaks": [],   # new: line numbers where hash validation fails
        "legacy_rows": 0,     # new: unhashed prefix rows
        "ok": True,
    }
    if not path.exists():
        return report
    raw = path.read_text(encoding="utf-8", errors="replace")
    seen: dict[str, int] = {}
    dups: set[str] = set()

    # Two-pass chain validation:
    # 1. Collect all parsed rows with their line numbers.
    # 2. Walk hashed suffix validating chain continuity.
    parsed_rows: list[tuple[int, dict]] = []  # (lineno, obj)

    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        report["lines"] += 1
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            report["bad_lines"].append(lineno)
            continue
        if not isinstance(obj, dict):
            report["bad_lines"].append(lineno)
            continue
        report["ok_rows"] += 1
        missing = [k for k in ("drop_id", "ts") if not obj.get(k)]
        if missing:
            report["missing_keys"].append({"line": lineno, "missing": missing})
        did = str(obj.get("drop_id", ""))
        if did:
            if did in seen:
                dups.add(did)
            seen[did] = seen.get(did, 0) + 1
        parsed_rows.append((lineno, obj))

    report["duplicate_ids"] = sorted(dups)

    # Walk hash chain: count legacy prefix, then validate hashed suffix.
    chain_started = False
    prev_hash = ""
    for lineno, obj in parsed_rows:
        stored_hash = obj.get("row_hash")
        if not stored_hash:
            if chain_started:
                # A row without row_hash after the chain has started = break.
                report["chain_breaks"].append(lineno)
            else:
                report["legacy_rows"] += 1
            continue
        # This row participates in the chain.
        expected = _compute_row_hash(obj, prev_hash)
        if stored_hash != expected:
            report["chain_breaks"].append(lineno)
            # Keep advancing prev_hash with the stored value so we can detect
            # further individual breaks rather than cascading failures.
        prev_hash = str(stored_hash)
        chain_started = True

    report["ok"] = (
        not report["bad_lines"]
        and not report["duplicate_ids"]
        and not report["missing_keys"]
        and not report["chain_breaks"]
    )
    return report


def repair(path: Path) -> dict:
    """Rewrite the ledger keeping only well-formed, de-duplicated rows.

    Bad lines are quarantined; duplicate drop_ids keep their FIRST occurrence;
    rows missing ``ts`` are stamped now. The clean set is written back via
    ``rewrite_atomic``. Returns a report of what changed.
    """
    path = Path(path)
    result: dict[str, Any] = {
        "path": str(path),
        "kept": 0,
        "quarantined": 0,
        "dropped_duplicates": 0,
        "stamped_ts": 0,
    }
    if not path.exists():
        return result
    raw = path.read_text(encoding="utf-8", errors="replace")
    kept: list[dict] = []
    bad: list[str] = []
    seen_ids: set[str] = set()
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            bad.append(line)
            continue
        if not isinstance(obj, dict):
            bad.append(line)
            continue
        did = str(obj.get("drop_id", ""))
        if did and did in seen_ids:
            result["dropped_duplicates"] += 1
            continue
        if did:
            seen_ids.add(did)
        if not obj.get("ts"):
            obj["ts"] = _now_iso()
            result["stamped_ts"] += 1
        kept.append(obj)
    if bad:
        _quarantine(path, bad)
        result["quarantined"] = len(bad)
    rewrite_atomic(path, kept, actor="repair", reason="repair_bad_lines_dedup")
    result["kept"] = len(kept)
    return result


def render_table(path: Path) -> str:
    """Render a ledger as a small markdown table (for CLI/debug display)."""
    rows, _ = load(path)
    if not rows:
        return "_(empty ledger)_\n"
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    head = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join(["---"] * len(cols)) + "|"
    body = []
    for r in rows:
        body.append(
            "| "
            + " | ".join(str(r.get(c, "")).replace("|", "\\|") for c in cols)
            + " |"
        )
    return "\n".join([head, sep, *body]) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Durable JSONL ledger utility")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_verify = sub.add_parser("verify", help="check a ledger for corruption")
    p_verify.add_argument("path")

    p_repair = sub.add_parser("repair", help="quarantine bad lines + de-dup")
    p_repair.add_argument("path")

    p_render = sub.add_parser("render", help="render a ledger as a markdown table")
    p_render.add_argument("path")

    args = parser.parse_args(argv)
    target = Path(args.path)

    if args.cmd == "verify":
        report = verify(target)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1
    if args.cmd == "repair":
        report = repair(target)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "render":
        sys.stdout.write(render_table(target))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
