#!/usr/bin/env python3
"""Acceptance tests for K3 — knowledge-index dedup, upsert, and source deletion.

Covers:
  * Re-add same doc 3x -> exactly one set of chunks; search returns no dupes.
  * Updated file re-ingest -> old chunks gone, new chunks present.
  * delete_source removes all chunks for a source (both engines).
  * Migration 0003 on a pre-existing duplicated DB dedupes and is idempotent.
  * Both FTS5 and fallback backends exercised for all upsert / delete tests.
  * Ingest pipeline supersession: re-ingesting a file removes the prior
    source's index chunks.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import knowledge_index as ki
import migrations as _migrations_pkg


def _run_migration(module_basename: str, root):
    """Load and apply a single migration by basename."""
    found = dict(_migrations_pkg.discover())
    # find the seq for this basename
    seq = None
    for s, b in _migrations_pkg.discover():
        if b == module_basename:
            seq = s
            break
    if seq is None:
        raise ValueError(f"migration {module_basename!r} not found")
    mig = _migrations_pkg.load_migration(seq, module_basename)
    return mig.apply(root)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _new_index(root, *, force_fallback: bool):
    return ki.KnowledgeIndex(root, force_fallback=force_fallback)


def _write_input(root: Path, name: str, text: str) -> Path:
    inp = root / "Workproduct.nosync" / "_INPUT"
    inp.mkdir(parents=True, exist_ok=True)
    p = inp / name
    p.write_text(text, encoding="utf-8")
    return p


_CHUNK = {
    "doc_id": "doc-A",
    "text": "Revenue grew strongly in the enterprise segment this quarter.",
    "source_id": "SRC-DEDUP",
    "sensitivity": "internal",
    "provenance": "test",
    "chunk_index": 0,
    "start": 0,
    "end": 60,
    "title": "Rev Note",
}

_CHUNK2 = {
    "doc_id": "doc-A",
    "text": "Customer churn rose slightly but remained within forecast.",
    "source_id": "SRC-DEDUP",
    "sensitivity": "internal",
    "provenance": "test",
    "chunk_index": 1,
    "start": 61,
    "end": 120,
    "title": "Churn Note",
}


# --------------------------------------------------------------------------- #
# K3.1 -- Re-add same doc 3x -> exactly one set of chunks; no dupes in search
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("force_fallback", [True, False])
def test_readd_same_doc_three_times_no_dupes(force_fallback, tmp_path, minimal_oracle):
    """add_chunks with the same (source_id, chunk_index) 3 times -> 1 chunk each."""
    if not force_fallback and not ki.fts5_available():
        pytest.skip("FTS5 not available on this interpreter")
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        # Add the same two chunks three times.
        for _ in range(3):
            idx.add_chunks([_CHUNK, _CHUNK2])
        st = idx.stats()
        # Exactly 2 chunks should be present (one per chunk_index).
        assert st["chunks"] == 2, (
            f"expected 2 chunks after 3 add_chunks calls; got {st['chunks']}"
        )
        hits = idx.search("revenue enterprise")
        # No duplicate hits for the same chunk.
        hit_rowid_src_pairs = [(h["source_id"], h["chunk_index"]) for h in hits]
        assert len(hit_rowid_src_pairs) == len(set(hit_rowid_src_pairs)), (
            "duplicate hits returned from search after upsert"
        )


@pytest.mark.parametrize("force_fallback", [True, False])
def test_readd_updates_text_in_place(force_fallback, tmp_path, minimal_oracle):
    """Upserting with new text replaces old text (not a second row)."""
    if not force_fallback and not ki.fts5_available():
        pytest.skip("FTS5 not available on this interpreter")
    root = minimal_oracle(tmp_path)
    original = dict(_CHUNK, text="Original text about budgets.")
    updated = dict(_CHUNK, text="Updated text about forecasts.")
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks([original])
        idx.add_chunks([updated])
        assert idx.stats()["chunks"] == 1
        hits = idx.search("forecast")
        assert hits, "expected a hit for updated text"
        assert hits[0]["text"] == updated["text"], "old text was not replaced"
        # Old term should no longer score (not present in any surviving row).
        old_hits = idx.search("budget")
        assert not old_hits, "old text chunk should not match after upsert"


# --------------------------------------------------------------------------- #
# K3.2 -- delete_source removes all chunks for a source (both engines)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("force_fallback", [True, False])
def test_delete_source_removes_all_chunks(force_fallback, tmp_path, minimal_oracle):
    """delete_source(sid) removes every chunk for that source."""
    if not force_fallback and not ki.fts5_available():
        pytest.skip("FTS5 not available on this interpreter")
    root = minimal_oracle(tmp_path)
    other = {
        "doc_id": "doc-B",
        "text": "Independent hiring plan for backend engineers.",
        "source_id": "SRC-OTHER",
        "sensitivity": "internal",
        "chunk_index": 0,
        "start": 0,
        "end": 45,
    }
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks([_CHUNK, _CHUNK2, other])
        assert idx.stats()["chunks"] == 3
        n = idx.delete_source("SRC-DEDUP")
        assert n == 2, f"expected 2 deleted; got {n}"
        assert idx.stats()["chunks"] == 1
        # The other source still searchable.
        hits = idx.search("backend engineers")
        assert hits and hits[0]["source_id"] == "SRC-OTHER"
        # Deleted source not searchable.
        assert idx.search("revenue enterprise") == []


@pytest.mark.parametrize("force_fallback", [True, False])
def test_delete_source_noop_for_unknown_source(force_fallback, tmp_path, minimal_oracle):
    """delete_source on an unknown source_id returns 0 and does not raise."""
    if not force_fallback and not ki.fts5_available():
        pytest.skip("FTS5 not available on this interpreter")
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks([_CHUNK])
        n = idx.delete_source("NON-EXISTENT-SOURCE")
        assert n == 0
        assert idx.stats()["chunks"] == 1  # unaffected


@pytest.mark.parametrize("force_fallback", [True, False])
def test_delete_source_cleans_postings_in_fallback(force_fallback, tmp_path, minimal_oracle):
    """In fallback mode, delete_source must also remove postings rows."""
    if not force_fallback:
        # Only meaningful for fallback; skip for FTS5.
        pytest.skip("postings-table check only applies to fallback engine")
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks([_CHUNK])
        idx.delete_source("SRC-DEDUP")
        con = sqlite3.connect(str(idx.db_path))
        n = con.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
        con.close()
        assert n == 0, "orphaned postings rows found after delete_source"


# --------------------------------------------------------------------------- #
# K3.3 -- Migration 0003: dedupes pre-existing duplicates, idempotent
# --------------------------------------------------------------------------- #

def _create_pre_migration_fallback_db(db_path: Path) -> None:
    """Create a fallback knowledge DB in the OLD schema (no UNIQUE constraint).

    This simulates a database that was created before migration 0003, so the
    migration has something to fix.  The DB is created from scratch -- we cannot
    rely on the KnowledgeIndex constructor because it now creates the UNIQUE
    constraint automatically.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL")
    # Metadata table (required by KnowledgeIndex).
    con.execute("CREATE TABLE IF NOT EXISTS index_meta (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("INSERT OR REPLACE INTO index_meta(k,v) VALUES('engine','fallback')")
    # OLD chunks table -- intentionally WITHOUT UNIQUE constraint.
    con.execute(
        "CREATE TABLE IF NOT EXISTS chunks ("
        "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
        "doc_id TEXT, source_id TEXT, sensitivity TEXT, provenance TEXT, "
        "title TEXT, chunk_index INTEGER, start_off INTEGER, end_off INTEGER, "
        "body TEXT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS postings ("
        "term TEXT, chunk_rowid INTEGER, tf INTEGER)"
    )
    con.commit()
    con.close()


def _raw_insert_old_schema(db_path: Path, source_id: str, chunk_index: int, text: str) -> None:
    """Insert a row into an OLD-schema fallback DB (no uniqueness enforcement)."""
    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT INTO chunks(doc_id, source_id, sensitivity, provenance, "
        "title, chunk_index, start_off, end_off, body) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (source_id, source_id, "internal", "", "", chunk_index, 0, 10, text),
    )
    con.commit()
    con.close()


def test_migration_0003_dedupes_fallback_db(tmp_path, minimal_oracle):
    """Migration removes duplicate fallback rows (keep newest), idempotent.

    We create the DB in the OLD schema (without UNIQUE constraint) so we can
    insert duplicate rows, then verify that the migration dedupes them and
    adds the uniqueness constraint.
    """
    root = minimal_oracle(tmp_path)
    db_path = ki.default_db_path(root)
    # Create the DB in the PRE-migration schema (no UNIQUE constraint).
    _create_pre_migration_fallback_db(db_path)
    # Insert two rows with the same (source_id, chunk_index) -- only possible
    # in the old schema.
    _raw_insert_old_schema(db_path, "SRC-DEDUP", 0, "First version of the chunk.")
    _raw_insert_old_schema(db_path, "SRC-DEDUP", 0, "Newer version of the chunk.")
    # Confirm two rows exist before migration.
    con = sqlite3.connect(str(db_path))
    count_before = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.close()
    assert count_before == 2, "expected 2 rows before migration"

    # Apply migration.
    result = _run_migration("0003_knowledge_index_dedup", root)
    assert result.get("changed") is True, f"migration did not report changed: {result}"

    con = sqlite3.connect(str(db_path))
    count_after = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.close()
    assert count_after == 1, f"expected 1 row after migration dedup; got {count_after}"

    # Idempotency: second run reports nothing to do or unchanged.
    result2 = _run_migration("0003_knowledge_index_dedup", root)
    assert result2.get("changed") is False, f"second run should report no change: {result2}"


def test_migration_0003_no_db_is_ok(tmp_path, minimal_oracle):
    """Migration on a root without any DB is a no-op (pristine oracle)."""
    root = minimal_oracle(tmp_path)
    # No index opened -> no DB.
    result = _run_migration("0003_knowledge_index_dedup", root)
    assert result.get("changed") is False
    assert "not found" in result.get("notes", "").lower() or "skipped" in result.get("notes", "").lower()


def test_migration_0003_fts5_creates_shadow_table(tmp_path, minimal_oracle):
    """Migration creates chunks_key shadow table when engine is fts5."""
    if not ki.fts5_available():
        pytest.skip("FTS5 not available on this interpreter")
    root = minimal_oracle(tmp_path)
    with ki.KnowledgeIndex(root, force_fallback=False) as idx:
        assert idx.engine == "fts5"
        idx.add_chunks([_CHUNK])
    # chunks_key should already exist after the new code runs, but migration
    # must still be idempotent.
    result = _run_migration("0003_knowledge_index_dedup", root)
    # Changed may be True (first populate) or False (already done); either is fine.
    assert "error" not in result.get("notes", "").lower(), f"migration error: {result}"
    db_path = ki.default_db_path(root)
    con = sqlite3.connect(str(db_path))
    tbl = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_key'"
    ).fetchone()
    con.close()
    assert tbl is not None, "chunks_key shadow table not found after migration"


# --------------------------------------------------------------------------- #
# K3.4 -- Ingest pipeline supersession
# --------------------------------------------------------------------------- #

def test_ingest_pipeline_supersession_removes_old_chunks(tmp_path, minimal_oracle):
    """Re-ingesting a file with new content removes old chunks, keeps new ones."""
    import ingest_pipeline

    root = minimal_oracle(tmp_path)

    # First ingest: write v1 of the file.
    src_v1 = _write_input(root, "memo.txt", "Alpha document about revenue metrics.")
    result1 = ingest_pipeline.run(root, src_v1, connector="manual", actor="tester")
    assert result1["index"]["status"] == "indexed"
    sha_v1 = result1["sha256"][:12]

    # Check v1 chunks are indexed.
    rows_v1 = ki.list_chunks(root, source_id=sha_v1)
    assert len(rows_v1) >= 1

    # Overwrite the file with new content (simulates a re-ingest after update).
    src_v1.write_text(
        "Beta document about entirely different hiring plans.", encoding="utf-8"
    )
    result2 = ingest_pipeline.run(root, src_v1, connector="manual", actor="tester")
    assert result2["index"]["status"] == "indexed"
    sha_v2 = result2["sha256"][:12]

    # The two hashes must differ (different content -> different source_id).
    assert sha_v1 != sha_v2, "test requires two distinct sha256 hashes"

    # Old source's chunks must be gone.
    rows_old = ki.list_chunks(root, source_id=sha_v1)
    assert rows_old == [], (
        f"old source chunks still present after re-ingest: {rows_old}"
    )

    # New source's chunks must be present.
    rows_new = ki.list_chunks(root, source_id=sha_v2)
    assert len(rows_new) >= 1


def test_ingest_pipeline_no_supersession_on_first_ingest(tmp_path, minimal_oracle):
    """First ingest of a file does not produce supersession warnings or errors."""
    import ingest_pipeline

    root = minimal_oracle(tmp_path)
    src = _write_input(root, "first_ever.txt", "Brand new document about strategy.")
    result = ingest_pipeline.run(root, src, connector="manual", actor="tester")
    assert result["index"]["status"] == "indexed"
    supersede = result["index"].get("supersede", {})
    # No old chunks should have been removed.
    assert supersede.get("removed") == [] or supersede.get("removed") is None
    # Must not have emitted a warning.
    assert supersede.get("status") != "warning", f"unexpected warning: {supersede}"


def test_ingest_pipeline_same_file_twice_no_dupes(tmp_path, minimal_oracle):
    """Ingesting the exact same file twice yields exactly one set of index chunks."""
    import ingest_pipeline

    root = minimal_oracle(tmp_path)
    content = "Identical content about quarterly performance review results."
    src = _write_input(root, "perf.txt", content)

    result1 = ingest_pipeline.run(root, src, connector="manual", actor="tester")
    result2 = ingest_pipeline.run(root, src, connector="manual", actor="tester")

    sha = result1["sha256"][:12]
    assert result1["sha256"] == result2["sha256"], "same content should hash identically"

    rows = ki.list_chunks(root, source_id=sha)
    expected = result1["chunk_count"]
    assert len(rows) == expected, (
        f"expected {expected} chunks (no dupes); got {len(rows)}"
    )


# --------------------------------------------------------------------------- #
# K3.5 -- FTS5 shadow table consistency after upsert + delete
# --------------------------------------------------------------------------- #

def test_fts5_chunks_key_consistent_after_upsert(tmp_path, minimal_oracle):
    """After upserts, chunks_key rowids must match actual FTS5 rowids."""
    if not ki.fts5_available():
        pytest.skip("FTS5 not available on this interpreter")
    root = minimal_oracle(tmp_path)
    with ki.KnowledgeIndex(root, force_fallback=False) as idx:
        assert idx.engine == "fts5"
        # Add, then update the same chunk.
        idx.add_chunks([_CHUNK])
        idx.add_chunks([dict(_CHUNK, text="Updated revenue analysis content.")])
        db = idx.db_path

    # After close, verify every chunks_key.fts_rowid points to a real FTS5 row.
    con = sqlite3.connect(str(db))
    key_rows = con.execute(
        "SELECT source_id, chunk_index, fts_rowid FROM chunks_key"
    ).fetchall()
    for sid, cidx, fts_rowid in key_rows:
        fts_row = con.execute(
            "SELECT rowid FROM chunks WHERE rowid=?", (fts_rowid,)
        ).fetchone()
        assert fts_row is not None, (
            f"chunks_key({sid},{cidx}) points to missing FTS5 rowid {fts_rowid}"
        )
    con.close()


def test_fts5_chunks_key_cleaned_after_delete_source(tmp_path, minimal_oracle):
    """delete_source clears the chunks_key shadow table entries for that source."""
    if not ki.fts5_available():
        pytest.skip("FTS5 not available on this interpreter")
    root = minimal_oracle(tmp_path)
    with ki.KnowledgeIndex(root, force_fallback=False) as idx:
        assert idx.engine == "fts5"
        idx.add_chunks([_CHUNK, _CHUNK2])
        n = idx.delete_source("SRC-DEDUP")
        assert n == 2
        db = idx.db_path

    con = sqlite3.connect(str(db))
    remaining = con.execute(
        "SELECT COUNT(*) FROM chunks_key WHERE source_id='SRC-DEDUP'"
    ).fetchone()[0]
    con.close()
    assert remaining == 0, "chunks_key entries not cleaned after delete_source"
