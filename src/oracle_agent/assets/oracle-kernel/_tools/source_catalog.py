#!/usr/bin/env python3
"""source_catalog.py -- a self-healing catalog of Source-note frontmatter.

The scaling problem this solves: every hot read path (search rerank, answer
preflight, truth-map validation, the Review Inbox) used to re-walk
``Memory.nosync/Sources`` and regex-parse every note's frontmatter per
invocation -- O(notes) file reads per query and O(truth_rows x notes) per
review build. Measured cost: ~66s per ``./oracle status`` at 20k sources.

The fix is a derived, rebuildable catalog (the same doctrine slot as the
knowledge index -- it lives in the SAME SQLite file):

  * one row per Source note: ``(name, mtime_ns, size, parse_version, payload)``
    where payload is the parsed frontmatter plus precomputed match keys;
  * a ``snapshot(root)`` call stat-sweeps the folder, re-parses ONLY new or
    changed notes (mtime/size drift or a ``PARSE_VERSION`` bump), drops rows
    for deleted notes, and returns an in-process-cached snapshot with
    inverted indexes (by id key, authority label, normalized object);
  * markdown notes stay canonical truth. The catalog is a cache: corrupt or
    unavailable SQLite degrades to an in-memory parse of the folder -- reads
    never fail because the cache did.

Consumers must treat the catalog as a SIGNAL, never a GATE: it accelerates
lookups but the matching semantics live in ``answer_protocol`` (see
``source_match_keys``). If those semantics change, bump ``PARSE_VERSION``
here so stale precomputed keys are lazily re-derived.

Stdlib only. Sibling imports are lazy so this module can sit beside
``answer_protocol`` without an import cycle.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

__all__ = ["PARSE_VERSION", "Snapshot", "snapshot", "entries", "db_path"]

# Bump when answer_protocol.source_match_keys (or the frontmatter reader)
# changes meaning: every cached row re-parses lazily on the next snapshot.
PARSE_VERSION = 1

_SOURCES_REL = Path("Memory.nosync") / "Sources"
# Must match knowledge_index.default_db_path (tested in test_source_catalog).
_DB_REL = Path("_data.nosync") / "index" / "knowledge.db"

# In-process snapshot cache: resolved root -> Snapshot. Invalidation is by
# folder signature (stat sweep), so a stale cache is impossible -- at worst a
# call pays one stat per note.
_SNAPSHOTS: dict[str, "Snapshot"] = {}


def _ap():
    """Lazy sibling import (bare-then-package, the kernel pattern)."""
    try:
        import answer_protocol as ap  # type: ignore
    except Exception:  # pragma: no cover - package import path
        from . import answer_protocol as ap  # type: ignore
    return ap


def db_path(root) -> Path:
    """Location of the shared derived DB (same file as the knowledge index)."""
    return Path(root) / _DB_REL


class Snapshot:
    """An immutable view of all Source notes plus inverted match indexes.

    ``entries`` is sorted by note filename (the same order the old folder
    walks produced). Each entry dict:

      ``name``       note filename (e.g. ``src-000001.md``)
      ``path``       root-relative path string
      ``fm``         parsed frontmatter dict ({} when unparseable)
      ``id_keys``    set of normalized id/source_id values
      ``label_keys`` set of normalized authority-label values
      ``objects``    set of normalized business-object claims

    ``derived`` is a scratch dict for consumer-scoped caches that live exactly
    as long as this snapshot generation (e.g. the rerank boost map); it is
    dropped automatically when the folder signature changes.
    """

    __slots__ = ("signature", "entries", "by_id_key", "by_label_key", "by_object", "derived")

    def __init__(self, signature, entries: list[dict]) -> None:
        self.signature = signature
        self.entries = entries
        self.derived: dict = {}
        self.by_id_key: dict[str, list[dict]] = {}
        self.by_label_key: dict[str, list[dict]] = {}
        self.by_object: dict[str, list[dict]] = {}
        for e in entries:
            for k in e["id_keys"]:
                self.by_id_key.setdefault(k, []).append(e)
            for k in e["label_keys"]:
                self.by_label_key.setdefault(k, []).append(e)
            for k in e["objects"]:
                self.by_object.setdefault(k, []).append(e)


def _scan(folder: Path) -> dict[str, tuple[int, int]]:
    """Stat sweep: note filename -> (mtime_ns, size). Cheap (no reads).

    ``os.scandir`` instead of glob+stat: one directory pass with cached stat
    results (~3x faster at 20k notes), since this sweep runs on every
    ``snapshot()`` call as the staleness check.
    """
    out: dict[str, tuple[int, int]] = {}
    try:
        with os.scandir(folder) as it:
            for entry in it:
                name = entry.name
                if not name.endswith(".md") or name.startswith("_"):
                    continue  # _CONTEXT.md / _template.md
                try:
                    st = entry.stat()
                except OSError:
                    continue
                out[name] = (st.st_mtime_ns, st.st_size)
    except OSError:
        return out
    return out


def _connect(root: Path) -> Optional[sqlite3.Connection]:
    """Open (and ensure) the catalog table; None when SQLite is unusable.

    Deliberately never CREATES ``knowledge.db``: read surfaces (status,
    review, dashboard) must stay read-only on a pristine root. The DB is born
    when the first ingest opens the knowledge index; from then on the catalog
    persists inside it. Until then snapshots are in-memory only.
    """
    path = db_path(root)
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(str(path))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS source_catalog("
            "name TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, "
            "size INTEGER NOT NULL, parse_version INTEGER NOT NULL, "
            "payload TEXT NOT NULL)"
        )
        return con
    except (sqlite3.Error, OSError):
        return None


def _load_rows(con: sqlite3.Connection) -> dict[str, tuple[int, int, int, str]]:
    try:
        cur = con.execute(
            "SELECT name, mtime_ns, size, parse_version, payload FROM source_catalog"
        )
        return {str(r[0]): (int(r[1]), int(r[2]), int(r[3]), str(r[4])) for r in cur}
    except sqlite3.Error:
        return {}


def _parse_note(root: Path, name: str) -> dict:
    """Parse one note into a payload dict (sets serialized as sorted lists)."""
    ap = _ap()
    fm = ap.read_frontmatter(root / _SOURCES_REL / name)
    keys = ap.source_match_keys(fm)
    return {
        "fm": fm,
        "id_keys": sorted(keys["id_keys"]),
        "label_keys": sorted(keys["label_keys"]),
        "objects": sorted(keys["objects"]),
    }


def _entry(root_rel_dir: str, name: str, payload: dict) -> dict:
    return {
        "name": name,
        "path": f"{root_rel_dir}/{name}",
        "fm": payload.get("fm") or {},
        "id_keys": set(payload.get("id_keys") or ()),
        "label_keys": set(payload.get("label_keys") or ()),
        "objects": set(payload.get("objects") or ()),
    }


def snapshot(root) -> Snapshot:
    """Current catalog snapshot; re-parses only what changed since last call."""
    root = Path(root)
    cache_key = str(root.resolve())
    disk = _scan(root / _SOURCES_REL)
    signature = (PARSE_VERSION, tuple(sorted(disk.items())))
    cached = _SNAPSHOTS.get(cache_key)
    if cached is not None and cached.signature == signature:
        return cached

    con = _connect(root)
    stored = _load_rows(con) if con is not None else {}

    payloads: dict[str, dict] = {}
    upserts: list[tuple[str, int, int, int, str]] = []
    for name, (mtime_ns, size) in disk.items():
        row = stored.get(name)
        if row is not None and row[0] == mtime_ns and row[1] == size and row[2] == PARSE_VERSION:
            try:
                payloads[name] = json.loads(row[3])
                continue
            except (json.JSONDecodeError, TypeError):
                pass  # corrupt payload: fall through to re-parse
        payload = _parse_note(root, name)
        payloads[name] = payload
        upserts.append(
            (name, mtime_ns, size, PARSE_VERSION, json.dumps(payload, ensure_ascii=False, sort_keys=True))
        )
    deleted = [name for name in stored if name not in disk]

    if con is not None:
        try:
            if upserts:
                con.executemany(
                    "INSERT OR REPLACE INTO source_catalog"
                    "(name, mtime_ns, size, parse_version, payload) VALUES (?,?,?,?,?)",
                    upserts,
                )
            if deleted:
                con.executemany(
                    "DELETE FROM source_catalog WHERE name = ?", [(n,) for n in deleted]
                )
            con.commit()
        except sqlite3.Error:
            pass  # persistence is best-effort; the snapshot is still correct
        finally:
            try:
                con.close()
            except sqlite3.Error:
                pass

    rel_dir = str(_SOURCES_REL).replace("\\", "/")
    snap = Snapshot(
        signature,
        [_entry(rel_dir, name, payloads[name]) for name in sorted(payloads)],
    )
    _SNAPSHOTS[cache_key] = snap
    return snap


def entries(root) -> list[dict]:
    """Convenience: the snapshot's entry list (sorted by note filename)."""
    return snapshot(root).entries


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - thin CLI
    import argparse

    parser = argparse.ArgumentParser(description="Inspect the derived Source catalog")
    parser.add_argument("--root", default=".", help="oracle root")
    parser.add_argument("--json", action="store_true", help="emit machine-readable summary")
    args = parser.parse_args(argv)
    snap = snapshot(Path(args.root))
    summary = {
        "notes": len(snap.entries),
        "objects": len(snap.by_object),
        "authority_labels": len(snap.by_label_key),
        "db": str(db_path(args.root)),
        "parse_version": PARSE_VERSION,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for k, v in summary.items():
            print(f"{k}: {v}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
