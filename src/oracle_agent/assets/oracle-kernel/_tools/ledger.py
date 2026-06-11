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
#: Sentinel for ``id_prefix`` meaning "mint NO drop_id at all" -- the row's
#: identity is ``ts`` + ``row_hash``. This skips the id-collision full-file scan
#: ``append`` otherwise performs, which is the load-bearing property for the
#: monthly-rotated retrieval ledger's per-search hot path (P8S-8): a search must
#: not pay a quadratic id-minting scan. Distinct from ``id_prefix=None`` (which
#: defaults to a generic ``LOG`` id when the row carries no drop_id).
NO_ID = object()


def append(path: Path, row: dict, *, id_prefix=None, auto_rotate: bool = False) -> str:
    """Append a single JSON object as one line, durably.

    Acquires an exclusive advisory lock for the duration of the write, emits a
    compact JSON line terminated by a newline, flushes and ``os.fsync``s before
    releasing the lock. The row is normalised so it always carries ``ts``; if the
    caller omitted ``ts`` we stamp it now.

    Collision-safe id minting: if ``id_prefix`` is a string, the ``drop_id`` is
    minted *under the same lock* as the write by scanning the rows already on
    disk -- so two concurrent ``append(..., id_prefix=...)`` calls can never
    mint the same id (there is no read/append TOCTOU gap). If the row already
    carries a ``drop_id`` it is preserved. With ``id_prefix=None`` and no
    drop_id, a generic ``LOG`` id is minted under the lock.

    Pass ``id_prefix=ledger.NO_ID`` to mint NO drop_id at all -- the row's
    identity is ``ts`` + ``row_hash``. This deliberately SKIPS the id-collision
    scan, which is what keeps the per-search retrieval ledger off the quadratic
    path (P8S-8). Returns the final ``drop_id`` ('' when NO_ID is used).

    Auto-rotation (P5-T8 / P5S-9): when ``auto_rotate=True`` the appender, while
    *already holding* ``LOCK_EX`` on ``path``, checks whether the current open
    segment has crossed the size/age threshold and, if so, closes it (writing a
    rotation marker as the final row) and opens a fresh segment BEFORE writing
    the caller's row -- so the row always lands in the open segment and no row
    can ever follow a rotation marker (the no-row-after-marker invariant). The
    rotation happens under the exact same lock the append takes, eliminating the
    TOCTOU window that a rotation decided outside the lock would create. The
    audit-critical ledgers (``action_event``, ``dream_session``,
    ``gateway_event``) append with ``auto_rotate=True``.
    """
    path = Path(path)
    if not isinstance(row, dict):
        raise TypeError("ledger.append requires a dict row")
    _ensure_parent(path)
    payload = dict(row)
    payload.setdefault("ts", _now_iso())
    if id_prefix is NO_ID:
        prefix = None
    else:
        prefix = id_prefix or (None if payload.get("drop_id") else "LOG")
    # safe_paths-internal: ledger durability append (caller-supplied contained path)
    with open(path, "a+", encoding="utf-8") as f:  # noqa: SAFEPATHS  # safe_paths-internal
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            if auto_rotate and _should_rotate_locked(f, path):
                # Seal the current open segment IN PLACE under THIS lock: its
                # rows (plus a rotation marker) are flushed to a sealed segment
                # file and recorded in the manifest, then the open file is
                # truncated and re-anchored. The caller's row is then written
                # into the freshly-anchored open segment -- so it can never
                # follow a rotation marker in a closed segment (P5S-9). The fd
                # (and its lock) are never released or swapped.
                _rotate_in_place_locked(f, path)
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
    return str(payload.get("drop_id", ""))


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


# --------------------------------------------------------------------------- #
# rotation / compaction (P5-T8, P5S-8/9) -- audit-critical ledgers only
# --------------------------------------------------------------------------- #
#
# Layout for a rotated ledger ``<name>`` living in ``<dir>``:
#   <dir>/<name>.jsonl              -- the LIVE open segment (appenders lock this)
#   <dir>/<name>.seg-NNNN.jsonl     -- sealed (closed) segments, NNNN zero-padded
#   <dir>/<name>.manifest.jsonl     -- tamper-evident hash-chained segment manifest
#
# The manifest is the ONLY discovery mechanism (never a filesystem glob,
# P5S-8): each sealed segment contributes one chained ``segment`` row, and a
# trailing ``head`` row names the open segment. Because the manifest is itself
# a hash chain AND records each sealed segment's terminal ``row_hash`` plus the
# open segment's expected anchor hash, deleting/renumbering/reordering ANY
# segment -- a middle one OR the newest sealed one OR the open HEAD -- is
# detectable by ``verify_chain``.
#
# Auto-rotation thresholds (module constants, overridable per-call):
ROTATE_MAX_BYTES = 8 * 1024 * 1024   # seal the open segment past ~8 MiB
ROTATE_MAX_AGE_DAYS = 90             # ...or once its first row is this old
#: Ledger *names* (basenames without ``.jsonl``) that are audit-critical and
#: therefore append with cross-segment rotation enabled. ``retrieval_event-*``
#: is deliberately absent: it keeps its fresh-chain-per-file telemetry design
#: (P5S-8) and is NOT rotated through this protocol.
AUDIT_CRITICAL_LEDGERS = ("action_event", "dream_session", "gateway_event")

#: Marker drop_id sealing a closed segment (mirrors REWRITE-MARKER precedent).
ROTATION_MARKER = "ROTATION-MARKER"
#: drop_id of the first row in a freshly opened segment, recording its
#: predecessor segment name + terminal row_hash so the chain re-anchors visibly.
ROTATION_ANCHOR = "ROTATION-ANCHOR"


def _seg_name(name: str, seq: int) -> str:
    return f"{name}.seg-{seq:04d}.jsonl"


def _manifest_path(ledger_dir: Path, name: str) -> Path:
    return Path(ledger_dir) / f"{name}.manifest.jsonl"


def _open_segment_path(ledger_dir: Path, name: str) -> Path:
    return Path(ledger_dir) / f"{name}.jsonl"


def _manifest_canonical(entry: dict) -> str:
    stripped = {k: v for k, v in entry.items() if k != "manifest_hash"}
    return json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _compute_manifest_hash(entry: dict, prev_hash: str) -> str:
    material = _manifest_canonical(entry) + prev_hash
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _read_rows_from_handle(f) -> list[dict]:
    """Parse all dict rows from an already-locked open file handle (from pos 0)."""
    rows: list[dict] = []
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
                rows.append(obj)
    except OSError:  # pragma: no cover - defensive
        pass
    return rows


def _first_row_ts(rows: list[dict]) -> str:
    for r in rows:
        ts = r.get("ts")
        if ts:
            return str(ts)
    return ""


def _ts_age_days(ts: str) -> float:
    """Best-effort age in days of an ISO-8601 (seconds) timestamp; 0 on parse fail."""
    if not ts:
        return 0.0
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return 0.0
    now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
    return max(0.0, (now - parsed).total_seconds() / 86400.0)


def _should_rotate_locked(f, path: Path, *, max_bytes=None, max_age_days=None) -> bool:
    """Decide, under the append lock, whether the open segment crossed threshold.

    Size is the byte length of the open file; age is the wall-clock age of the
    segment's first row. An empty segment never rotates (nothing to seal).
    """
    mb = ROTATE_MAX_BYTES if max_bytes is None else max_bytes
    md = ROTATE_MAX_AGE_DAYS if max_age_days is None else max_age_days
    try:
        size = os.fstat(f.fileno()).st_size
    except OSError:  # pragma: no cover - defensive
        size = 0
    if size <= 0:
        return False
    if mb is not None and size >= mb:
        return True
    if md is not None:
        rows = _read_rows_from_handle(f)
        if rows and _ts_age_days(_first_row_ts(rows)) >= md:
            return True
    return False


def _load_manifest(ledger_dir: Path, name: str) -> list[dict]:
    """Return the manifest entries (segment rows then a trailing head row).

    Corruption-tolerant like ``load``: unparseable lines are skipped. An absent
    manifest yields an empty list (a never-rotated / legacy ledger).
    """
    mpath = _manifest_path(ledger_dir, name)
    entries: list[dict] = []
    if not mpath.exists():
        return entries
    try:
        raw = mpath.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - defensive
        return entries
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def _manifest_segments(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("kind") == "segment"]


def _manifest_head(entries: list[dict]) -> dict | None:
    for e in reversed(entries):
        if e.get("kind") == "head":
            return e
    return None


def _next_seq(entries: list[dict]) -> int:
    segs = _manifest_segments(entries)
    if not segs:
        return 1
    return max(int(e.get("seq", 0)) for e in segs) + 1


def _write_manifest_atomic(ledger_dir: Path, name: str, entries: list[dict]) -> None:
    """Rewrite the manifest atomically (temp + os.replace) in the ledger dir."""
    mpath = _manifest_path(ledger_dir, name)
    _ensure_parent(mpath)
    lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    body = ("\n".join(lines) + "\n") if lines else ""
    fd, tmp_name = tempfile.mkstemp(
        prefix=mpath.name + ".", suffix=".tmp", dir=str(mpath.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tf:  # safe_paths-internal
            tf.write(body)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_name, str(mpath))  # safe_paths-internal: atomic manifest swap
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    dir_fd = os.open(str(mpath.parent), os.O_DIRECTORY)  # safe_paths-internal
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _write_segment_atomic(seg_path: Path, rows: list[dict]) -> None:
    """Write a sealed segment file atomically (temp + os.replace)."""
    _ensure_parent(seg_path)
    lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    body = ("\n".join(lines) + "\n") if lines else ""
    fd, tmp_name = tempfile.mkstemp(
        prefix=seg_path.name + ".", suffix=".tmp", dir=str(seg_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tf:  # safe_paths-internal
            tf.write(body)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_name, str(seg_path))  # safe_paths-internal: atomic segment seal
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    dir_fd = os.open(str(seg_path.parent), os.O_DIRECTORY)  # safe_paths-internal
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _rotate_in_place_locked(f, path: Path) -> None:
    """Seal the open segment and re-anchor it, all under the caller's LOCK_EX.

    The caller holds LOCK_EX on ``f`` (the open ``<name>.jsonl``). Steps:

    1. Read the open segment's rows; append a ROTATION-MARKER row chained onto
       the segment's terminal ``row_hash``. The marker's hash is the sealed
       segment's terminal hash.
    2. Write those rows (incl. the marker) to a fresh ``<name>.seg-NNNN.jsonl``
       atomically. NO row can follow the marker in this closed file.
    3. Extend the manifest with a chained ``segment`` entry (filename, terminal
       hash, predecessor hash, row count) and rewrite the trailing ``head``
       entry to point at the open segment with its expected anchor hash.
    4. Truncate the open file in place and write a ROTATION-ANCHOR first row
       that chains off the sealed segment's terminal hash and records the
       predecessor segment name -- so the row chain visibly re-anchors and the
       open segment is provably the continuation of the sealed one.

    The fd is never closed and the lock is never released, so concurrent
    appenders stay blocked until the open segment is re-anchored. Returns None;
    the caller continues appending into the same fd.
    """
    name = path.name[: -len(".jsonl")] if path.name.endswith(".jsonl") else path.name
    ledger_dir = path.parent

    rows = _read_rows_from_handle(f)
    if not rows:  # pragma: no cover - guarded by _should_rotate_locked
        return

    prev_terminal = ""
    for r in rows:
        if r.get("row_hash"):
            prev_terminal = str(r["row_hash"])

    entries = _load_manifest(ledger_dir, name)
    seg_entries_existing = _manifest_segments(entries)
    seq = _next_seq(entries)
    seg_filename = _seg_name(name, seq)
    # The hash this segment's FIRST row anchored onto: the predecessor segment's
    # terminal hash, or "" for the very first segment (genesis / legacy prefix).
    # This is what ``verify_chain`` walks for cross-segment continuity.
    anchor_prev_hash = (
        str(seg_entries_existing[-1].get("terminal_row_hash", ""))
        if seg_entries_existing
        else ""
    )

    # 1. Seal with a rotation marker chained onto the segment tail.
    marker: dict = {
        "drop_id": ROTATION_MARKER,
        "ts": _now_iso(),
        "event": "ledger_rotation",
        "segment": seg_filename,
        "seq": seq,
        "rows": len(rows),
    }
    marker["row_hash"] = _compute_row_hash(marker, prev_terminal)
    sealed_rows = rows + [marker]
    terminal_hash = marker["row_hash"]

    # 2. Write the sealed segment atomically.
    _write_segment_atomic(ledger_dir / seg_filename, sealed_rows)

    # 3. Extend the hash-chained manifest + rewrite the HEAD entry.
    seg_entries = seg_entries_existing
    prev_manifest_hash = ""
    if seg_entries:
        prev_manifest_hash = str(seg_entries[-1].get("manifest_hash", ""))
    seg_entry: dict = {
        "kind": "segment",
        "seq": seq,
        "segment": seg_filename,
        "terminal_row_hash": terminal_hash,
        "predecessor_row_hash": anchor_prev_hash,
        "rows": len(sealed_rows),
        "ts": _now_iso(),
    }
    seg_entry["manifest_hash"] = _compute_manifest_hash(seg_entry, prev_manifest_hash)
    head_entry: dict = {
        "kind": "head",
        "open_segment": path.name,
        "last_sealed_seq": seq,
        "last_sealed_terminal_hash": terminal_hash,
        "open_anchor_prev_hash": terminal_hash,
        "ts": _now_iso(),
    }
    head_entry["manifest_hash"] = _compute_manifest_hash(
        head_entry, seg_entry["manifest_hash"]
    )
    new_entries = seg_entries + [seg_entry, head_entry]
    _write_manifest_atomic(ledger_dir, name, new_entries)

    # 4. Truncate the open file and write the re-anchoring first row.
    anchor: dict = {
        "drop_id": ROTATION_ANCHOR,
        "ts": _now_iso(),
        "event": "ledger_segment_open",
        "seq": seq + 1,
        "predecessor_segment": seg_filename,
        "predecessor_row_hash": terminal_hash,
    }
    anchor["row_hash"] = _compute_row_hash(anchor, terminal_hash)
    f.seek(0)
    f.truncate(0)
    f.write(json.dumps(anchor, ensure_ascii=False) + "\n")
    f.flush()
    os.fsync(f.fileno())


def rotate(path: Path, *, max_bytes=None, max_age_days=None) -> dict:
    """Explicitly close the open segment and open a fresh one, if warranted.

    Takes LOCK_EX on ``path`` (the open ``<name>.jsonl``) -- the SAME lock
    ``append`` takes -- evaluates the size/age threshold, and rotates in place
    if crossed. With ``max_bytes=0`` rotation is forced for any non-empty
    segment (used by tests). Returns a report dict ``{rotated, segment, seq,
    terminal_row_hash}``.

    This is the manual / cadence-driven entrypoint; the appender also rotates
    inline via ``append(..., auto_rotate=True)`` when it observes the threshold
    while already holding the lock (P5S-9), so no separate scheduler is required.
    """
    path = Path(path)
    _ensure_parent(path)
    name = path.name[: -len(".jsonl")] if path.name.endswith(".jsonl") else path.name
    report = {"rotated": False, "segment": None, "seq": None, "terminal_row_hash": None}
    # safe_paths-internal: rotation holds the append lock on the open segment
    with open(path, "a+", encoding="utf-8") as f:  # noqa: SAFEPATHS  # safe_paths-internal
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            if not _should_rotate_locked(
                f, path, max_bytes=max_bytes, max_age_days=max_age_days
            ):
                return report
            _rotate_in_place_locked(f, path)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    # Re-read the manifest (outside the lock) to report the sealed segment.
    entries = _load_manifest(path.parent, name)
    segs = _manifest_segments(entries)
    if segs:
        last = segs[-1]
        report.update(
            rotated=True,
            segment=last.get("segment"),
            seq=last.get("seq"),
            terminal_row_hash=last.get("terminal_row_hash"),
        )
    return report


def _verify_rows_from_anchor(rows: list[dict], anchor_prev_hash: str) -> bool:
    """Validate a row list's internal row_hash chain starting from an anchor.

    A re-anchored segment's first hashed row commits to its predecessor
    segment's terminal hash, NOT to the genesis empty string -- so the plain
    ``verify`` (which assumes ``prev=""``) cannot validate a sealed/open segment
    past seq 1. This walks the chain from ``anchor_prev_hash`` and returns True
    iff every hashed row's stored hash matches and there are no gaps. A legacy
    unhashed prefix (rows with no ``row_hash`` before the chain begins) is
    tolerated exactly as ``verify`` tolerates it.
    """
    prev_hash = str(anchor_prev_hash)
    chain_started = False
    for r in rows:
        stored = r.get("row_hash")
        if not stored:
            if chain_started:
                return False  # unhashed row inside the chain = break
            continue  # legacy prefix
        if str(stored) != _compute_row_hash(r, prev_hash):
            return False
        prev_hash = str(stored)
        chain_started = True
    return True


def verify_chain(ledger_dir: Path, name: str) -> dict:
    """Cross-SEGMENT chain verification driven by the manifest (P5S-8).

    Discovers segments via the tamper-evident manifest ONLY (never a filesystem
    glob), validates:
      * the manifest's own hash chain (edit/reorder/removal of a manifest entry),
      * each sealed segment's internal row_hash chain (via ``verify``),
      * that each sealed segment's terminal row_hash matches its manifest entry
        (a removed/renumbered/swapped segment fails here),
      * that each segment re-anchors onto its predecessor's terminal hash
        (cross-segment continuity -- a removed MIDDLE segment breaks this),
      * that the open HEAD segment exists, anchors onto the last sealed
        segment's terminal hash, and that the HEAD manifest entry is present
        (a removed HEAD/latest segment fails here -- P5S-8).

    Returns a report dict; ``ok`` is True only when every check passes. A ledger
    with NO manifest (never rotated / legacy single file) is treated as a single
    open segment and validated with ``verify`` -- backward compatible.
    """
    ledger_dir = Path(ledger_dir)
    open_path = _open_segment_path(ledger_dir, name)
    report: dict[str, Any] = {
        "name": name,
        "dir": str(ledger_dir),
        "segments": [],
        "open_segment": str(open_path),
        "manifest_breaks": [],
        "segment_breaks": [],
        "anchor_breaks": [],
        "missing_segments": [],
        "head_ok": True,
        "ok": True,
    }
    entries = _load_manifest(ledger_dir, name)

    # No manifest => never rotated. Validate the single open file (legacy path).
    if not entries:
        single = verify(open_path)
        report["segments"] = []
        report["legacy_single_file"] = True
        report["open_ok"] = single["ok"]
        report["ok"] = single["ok"]
        return report

    segs = _manifest_segments(entries)
    head = _manifest_head(entries)

    # 1. Validate the manifest's own hash chain (segment rows, then head).
    prev_mhash = ""
    for e in segs:
        expected = _compute_manifest_hash(e, prev_mhash)
        if e.get("manifest_hash") != expected:
            report["manifest_breaks"].append(int(e.get("seq", -1)))
        prev_mhash = str(e.get("manifest_hash", ""))
    if head is not None:
        expected_head = _compute_manifest_hash(head, prev_mhash)
        if head.get("manifest_hash") != expected_head:
            report["manifest_breaks"].append("head")
    else:
        # Manifest exists but carries no HEAD pointer -- cannot vouch for the
        # open segment's existence/anchoring; treat as a head break.
        report["head_ok"] = False
        report["manifest_breaks"].append("head-missing")

    # 2. Validate each sealed segment: it must exist, verify internally, its
    #    terminal hash must match the manifest, and it must anchor onto its
    #    predecessor's terminal hash.
    prev_terminal = ""
    for e in segs:
        seq = int(e.get("seq", -1))
        seg_file = ledger_dir / str(e.get("segment", ""))
        rep = {"seq": seq, "segment": e.get("segment"), "ok": True}
        if not seg_file.exists():
            report["missing_segments"].append(e.get("segment"))
            report["segment_breaks"].append(seq)
            rep["ok"] = False
            report["segments"].append(rep)
            # Cannot continue the cross-segment anchor walk past a gap.
            prev_terminal = str(e.get("terminal_row_hash", ""))
            continue
        seg_rows, _ = load(seg_file)
        # Cross-segment continuity: this segment's chain must anchor onto its
        # predecessor's terminal hash. seq 1 anchors off "" (genesis / legacy
        # prefix); seq N onto seg N-1's terminal. The recorded predecessor must
        # equal what the walk says, AND the rows must actually validate from it.
        recorded_pred = str(e.get("predecessor_row_hash", ""))
        if recorded_pred != prev_terminal:
            report["anchor_breaks"].append(seq)
            rep["ok"] = False
        # Internal chain validation FROM the anchor (a re-anchored segment does
        # not validate under the genesis-prev assumption of plain ``verify``).
        if not _verify_rows_from_anchor(seg_rows, prev_terminal):
            report["segment_breaks"].append(seq)
            rep["ok"] = False
        # Terminal hash must match the manifest's record for this segment.
        actual_terminal = ""
        for r in seg_rows:
            if r.get("row_hash"):
                actual_terminal = str(r["row_hash"])
        if actual_terminal != str(e.get("terminal_row_hash", "")):
            report["segment_breaks"].append(seq)
            rep["ok"] = False
        report["segments"].append(rep)
        prev_terminal = str(e.get("terminal_row_hash", ""))

    # 3. Validate the open HEAD segment: it must exist, verify internally, and
    #    its anchor row must chain off the last sealed segment's terminal hash.
    if head is not None:
        if not open_path.exists():
            report["head_ok"] = False
            report["missing_segments"].append(open_path.name)
        else:
            open_rows, _ = load(open_path)
            expected_anchor = str(head.get("open_anchor_prev_hash", ""))
            # The open segment re-anchors onto the last sealed terminal hash, so
            # validate its internal chain FROM that anchor (not genesis-prev).
            if not _verify_rows_from_anchor(open_rows, expected_anchor):
                report["head_ok"] = False
            anchor_row = next(
                (r for r in open_rows if r.get("drop_id") == ROTATION_ANCHOR), None
            )
            if anchor_row is None:
                report["head_ok"] = False
            elif str(anchor_row.get("predecessor_row_hash", "")) != expected_anchor:
                report["head_ok"] = False
            # The last sealed segment's terminal hash must equal what HEAD claims.
            if segs and str(head.get("last_sealed_terminal_hash", "")) != prev_terminal:
                report["head_ok"] = False

    report["ok"] = (
        not report["manifest_breaks"]
        and not report["segment_breaks"]
        and not report["anchor_breaks"]
        and not report["missing_segments"]
        and report["head_ok"]
    )
    return report


def load_window(ledger_dir: Path, name: str, *, since) -> tuple[list[dict], list[str]]:
    """Windowed read: rows with ``ts >= since``, newest segments first, bounded.

    Reads the open segment, then walks sealed segments newest-first via the
    manifest, stopping as soon as a whole segment lies entirely before
    ``since`` -- so a year of history is never fully parsed to answer a
    recent-window query. ``since`` is an ISO-8601 string compared
    lexicographically (ISO-8601 seconds sort chronologically). Returns
    ``(rows, warnings)`` in chronological (oldest-first) order, EXCLUDING the
    rotation marker / anchor bookkeeping rows. Result equals a full ``load``
    of every segment filtered to ``ts >= since``.
    """
    ledger_dir = Path(ledger_dir)
    since_s = str(since)
    warnings: list[str] = []
    collected: list[dict] = []

    def _keep(r: dict) -> bool:
        if r.get("drop_id") in (ROTATION_MARKER, ROTATION_ANCHOR):
            return False
        return str(r.get("ts", "")) >= since_s

    # Open segment (always the newest rows).
    open_path = _open_segment_path(ledger_dir, name)
    open_rows, w = load(open_path)
    warnings.extend(w)
    collected.extend(r for r in open_rows if _keep(r))

    # Sealed segments, newest-first; stop once an entire segment predates since.
    entries = _load_manifest(ledger_dir, name)
    for e in reversed(_manifest_segments(entries)):
        seg_file = ledger_dir / str(e.get("segment", ""))
        if not seg_file.exists():
            warnings.append(f"missing segment {e.get('segment')}")
            continue
        seg_rows, w = load(seg_file)
        warnings.extend(w)
        kept = [r for r in seg_rows if _keep(r)]
        collected.extend(kept)
        # If NONE of this segment's data rows fall in the window, every older
        # segment is also out of range (segments are time-ordered) -- stop.
        data_rows = [
            r for r in seg_rows if r.get("drop_id") not in (ROTATION_MARKER, ROTATION_ANCHOR)
        ]
        if data_rows and not kept:
            break

    collected.sort(key=lambda r: str(r.get("ts", "")))
    return collected, warnings


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
