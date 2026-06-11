#!/usr/bin/env python3
"""0004_chunk_vectors -- the optional embedding vector store (Phase 8, P8-T1).

Creates the ``chunk_vectors`` table (and its model index) in the shared
knowledge DB if it is not already present. The table lives in the SAME SQLite
DB for BOTH engines (FTS5 and the pure-python fallback): it is an ordinary
relational table, independent of the full-text engine, so a single migration
covers both builds.

Schema (frozen by the spec):

    chunk_vectors(source_id TEXT, chunk_index INTEGER, embedding_model TEXT,
                  dim INTEGER, norm REAL, vector BLOB,
                  PRIMARY KEY (source_id, chunk_index, embedding_model))

NO sensitivity column: sensitivity is ALWAYS join-read from ``chunks`` on
(source_id, chunk_index), so a reclassified chunk's label is authoritative the
instant its chunk row changes (P8S-6). Vectors are content-equivalent to their
chunk and are removed in the SAME transaction as the chunk mutation by
``knowledge_index`` at runtime; this migration only installs the table.

Fully idempotent: a second run is a no-op and reports ``changed=False``. If the
DB does not exist yet (a pristine root with no ingestions), the table is
created by ``KnowledgeIndex._ensure_schema`` on first open, so the migration
reports ``changed=False, notes="db not found; created on first index open"``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

VERSION = "4.0.0"
DESCRIPTION = (
    "Install the chunk_vectors embedding store (keyed source_id, chunk_index, "
    "embedding_model; no sensitivity column -- join-read from chunks)."
)

# Must match knowledge_index.default_db_path and the 0003 migration.
_DB_REL = Path("_data.nosync") / "index" / "knowledge.db"

_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS chunk_vectors ("
    "source_id TEXT NOT NULL, chunk_index INTEGER NOT NULL, "
    "embedding_model TEXT NOT NULL, dim INTEGER NOT NULL, "
    "norm REAL NOT NULL, vector BLOB NOT NULL, "
    "PRIMARY KEY (source_id, chunk_index, embedding_model))"
)
_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_chunk_vectors_model "
    "ON chunk_vectors(embedding_model)"
)


def _db_path(root: Path) -> Path:
    return root / _DB_REL


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def apply(root: Path) -> dict:
    """Apply the migration idempotently. Returns ``{changed, notes}``."""
    root = Path(root)
    db = _db_path(root)
    if not db.exists():
        return {
            "changed": False,
            "notes": "db not found; chunk_vectors created on first index open",
        }

    try:
        con = sqlite3.connect(str(db))
        con.execute("PRAGMA journal_mode=WAL")
    except (sqlite3.Error, OSError) as exc:
        return {"changed": False, "notes": f"could not open db: {exc}"}

    try:
        already = _table_exists(con, "chunk_vectors")
        con.execute(_CREATE_TABLE)
        con.execute(_CREATE_INDEX)
        con.commit()
        if already:
            return {"changed": False, "notes": "chunk_vectors already present"}
        return {"changed": True, "notes": "created chunk_vectors table + index"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"changed": False, "notes": f"migration error: {exc}"}
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
