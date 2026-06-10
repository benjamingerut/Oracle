#!/usr/bin/env python3
"""0003_knowledge_index_dedup -- knowledge index dedup + uniqueness constraint.

Finds the shared knowledge DB (same file used by ``knowledge_index`` and
``source_catalog``) and:

  1. For the FTS5 engine:
     * Creates the ``chunks_key`` shadow table (PRIMARY KEY (source_id,
       chunk_index) -> fts_rowid) if it does not exist.
     * Populates ``chunks_key`` from existing ``chunks`` rows, keeping only the
       NEWEST row per ``(source_id, chunk_index)`` (highest rowid = most
       recently inserted) and deleting all older duplicates from the FTS5
       virtual table.

  2. For the pure-python fallback engine:
     * Adds a UNIQUE constraint on ``(source_id, chunk_index)`` to the chunks
       table if it is not already present (done by recreating the table with
       the constraint and copying surviving rows).
     * Dedupes: keep the newest row per key (highest rowid); cleans orphaned
       postings rows for the deleted duplicates.

Both paths are fully idempotent: running the migration twice produces no
change on the second run and reports ``changed=False``.

The DB may not exist yet (a pristine oracle root with no ingestions). In that
case the migration reports ``changed=False, notes="db not found"``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

VERSION = "3.0.0"
DESCRIPTION = (
    "Dedup knowledge-index chunks (keep newest per (source_id, chunk_index)) "
    "and install uniqueness constraint / FTS5 shadow key table."
)

# Relative path from oracle root to the shared derived DB -- must match
# knowledge_index.default_db_path and source_catalog.db_path.
_DB_REL = Path("_data.nosync") / "index" / "knowledge.db"


def _db_path(root: Path) -> Path:
    return root / _DB_REL


def _get_engine(con: sqlite3.Connection) -> str:
    """Read the engine stored in index_meta; default 'fallback'."""
    try:
        row = con.execute(
            "SELECT v FROM index_meta WHERE k='engine'"
        ).fetchone()
        if row:
            return str(row[0])
    except sqlite3.Error:
        pass
    return "fallback"


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','shadow') AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_names(con: sqlite3.Connection, table: str) -> list[str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _has_unique_constraint(con: sqlite3.Connection) -> bool:
    """True iff chunks table already has UNIQUE(source_id, chunk_index)."""
    rows = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks'"
    ).fetchall()
    if not rows:
        return False
    sql = (rows[0][0] or "").lower()
    return "unique" in sql


def _apply_fts5(con: sqlite3.Connection) -> dict:
    """Dedup the FTS5 chunks table and build/populate chunks_key."""
    changed = False
    notes_parts: list[str] = []

    # 1. Ensure the shadow key table exists.
    if not _table_exists(con, "chunks_key"):
        con.execute(
            "CREATE TABLE IF NOT EXISTS chunks_key ("
            "source_id TEXT NOT NULL, chunk_index INTEGER NOT NULL, "
            "fts_rowid INTEGER NOT NULL, "
            "PRIMARY KEY (source_id, chunk_index))"
        )
        con.commit()
        changed = True
        notes_parts.append("created chunks_key shadow table")

    # 2. Find all (source_id, chunk_index) combos that have more than one row
    #    in the FTS5 chunks table (duplicates). Keep the highest rowid (newest).
    dupes = con.execute(
        "SELECT source_id, chunk_index, MAX(rowid) AS keep_rowid "
        "FROM chunks GROUP BY source_id, chunk_index HAVING COUNT(*) > 1"
    ).fetchall()
    deleted_count = 0
    for row in dupes:
        sid, cidx, keep_rowid = row[0], int(row[1]), int(row[2])
        # Delete all but the keeper.
        old_rows = con.execute(
            "SELECT rowid FROM chunks WHERE source_id=? AND chunk_index=? AND rowid!=?",
            (sid, cidx, keep_rowid),
        ).fetchall()
        for old in old_rows:
            con.execute("DELETE FROM chunks WHERE rowid=?", (old[0],))
            deleted_count += 1
    if deleted_count:
        con.commit()
        changed = True
        notes_parts.append(f"deleted {deleted_count} duplicate FTS5 chunk(s)")

    # 3. (Re)populate chunks_key from the surviving FTS5 rows.
    #    INSERT OR REPLACE so partial prior state is healed idempotently.
    all_rows = con.execute(
        "SELECT rowid, source_id, chunk_index FROM chunks"
    ).fetchall()
    if all_rows:
        con.executemany(
            "INSERT OR REPLACE INTO chunks_key(source_id, chunk_index, fts_rowid) "
            "VALUES(?,?,?)",
            [(str(r[1]), int(r[2]), int(r[0])) for r in all_rows],
        )
        con.commit()
        notes_parts.append(f"populated chunks_key with {len(all_rows)} row(s)")
        changed = True

    return {
        "changed": changed,
        "notes": "; ".join(notes_parts) if notes_parts else "nothing to do",
    }


def _apply_fallback(con: sqlite3.Connection) -> dict:
    """Dedup fallback chunks table and add UNIQUE(source_id, chunk_index)."""
    changed = False
    notes_parts: list[str] = []

    if not _table_exists(con, "chunks"):
        return {"changed": False, "notes": "chunks table absent; nothing to do"}

    # 1. Dedup: for each (source_id, chunk_index) group, keep the highest rowid.
    dupes = con.execute(
        "SELECT source_id, chunk_index, MAX(rowid) AS keep_rowid "
        "FROM chunks GROUP BY source_id, chunk_index HAVING COUNT(*) > 1"
    ).fetchall()
    deleted_count = 0
    for row in dupes:
        sid, cidx, keep_rowid = row[0], int(row[1]), int(row[2])
        old_rows = con.execute(
            "SELECT rowid FROM chunks WHERE source_id=? AND chunk_index=? AND rowid!=?",
            (sid, cidx, keep_rowid),
        ).fetchall()
        for old in old_rows:
            con.execute("DELETE FROM postings WHERE chunk_rowid=?", (old[0],))
            con.execute("DELETE FROM chunks WHERE rowid=?", (old[0],))
            deleted_count += 1
    if deleted_count:
        con.commit()
        changed = True
        notes_parts.append(f"deleted {deleted_count} duplicate fallback chunk(s)")

    # 2. Install UNIQUE(source_id, chunk_index) if not already present.
    #    SQLite cannot ADD a UNIQUE constraint to an existing table; we must
    #    recreate the table.
    if not _has_unique_constraint(con):
        cols = _column_names(con, "chunks")
        col_list = ", ".join(cols)
        # Build a renamed copy with the new constraint.
        con.execute(
            "CREATE TABLE _chunks_new ("
            "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
            "doc_id TEXT, source_id TEXT, sensitivity TEXT, provenance TEXT, "
            "title TEXT, chunk_index INTEGER, start_off INTEGER, end_off INTEGER, "
            "body TEXT, UNIQUE(source_id, chunk_index))"
        )
        # Copy surviving rows, preserving rowids so postings references stay valid.
        try:
            con.execute(
                f"INSERT INTO _chunks_new(rowid, {col_list}) "
                f"SELECT rowid, {col_list} FROM chunks"
            )
        except sqlite3.Error:
            # rowid may not be in PRAGMA columns; try without it.
            non_rowid_cols = [c for c in cols if c.lower() != "rowid"]
            col_list2 = ", ".join(non_rowid_cols)
            con.execute(
                f"INSERT INTO _chunks_new({col_list2}) "
                f"SELECT {col_list2} FROM chunks"
            )
        con.execute("DROP TABLE chunks")
        con.execute("ALTER TABLE _chunks_new RENAME TO chunks")
        # Recreate the postings indexes (CREATE INDEX is idempotent via IF NOT EXISTS).
        con.execute("CREATE INDEX IF NOT EXISTS ix_postings_term ON postings(term)")
        con.execute(
            "CREATE INDEX IF NOT EXISTS ix_postings_chunk ON postings(chunk_rowid)"
        )
        con.commit()
        changed = True
        notes_parts.append("added UNIQUE(source_id, chunk_index) to chunks table")

    return {
        "changed": changed,
        "notes": "; ".join(notes_parts) if notes_parts else "nothing to do",
    }


def apply(root: Path) -> dict:
    """Apply the migration idempotently. Returns ``{changed, notes}``."""
    root = Path(root)
    db = _db_path(root)
    if not db.exists():
        return {"changed": False, "notes": "db not found; skipped"}

    try:
        con = sqlite3.connect(str(db))
        con.execute("PRAGMA journal_mode=WAL")
    except (sqlite3.Error, OSError) as exc:
        return {"changed": False, "notes": f"could not open db: {exc}"}

    try:
        engine = _get_engine(con)
        if engine == "fts5":
            result = _apply_fts5(con)
        else:
            result = _apply_fallback(con)
        result["engine"] = engine
        return result
    except Exception as exc:
        return {"changed": False, "notes": f"migration error: {exc}"}
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
