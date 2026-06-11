#!/usr/bin/env python3
"""Tests for the kernel vector store (Phase 8, P8-T1).

Covers, per the spec's acceptance items:
  * add/delete/reindex round-trips for chunk_vectors;
  * delete_source leaves ZERO vector rows (including mid-call crash via the
    single transaction);
  * upserting a chunk drops its old vector (same-transaction lifecycle, P8S-6);
  * a connector re-sync through ingest_pipeline._remove_superseded_chunks leaves
    zero vectors for the superseded source_id (end-to-end, P8S-6);
  * oracle search vectors-add routes to vectors-add, never a text query (P8S-4);
  * stats reports per-model coverage;
  * zero-norm / non-finite vectors are rejected (P8S-11);
  * the doctor orphan-vector query (P8S-6);
  * NO sensitivity column -- the label is join-read from chunks;
  * migration 0004 is idempotent.

Both engines (FTS5 + forced fallback) are exercised where applicable: vectors
live in the same SQLite DB regardless of the lexical engine.
"""
from __future__ import annotations

import json
import sqlite3
from array import array

import pytest

import knowledge_index as ki
import oracle_cli
import migrations as _migrations_pkg


# --------------------------------------------------------------------------- #
# corpus + helpers
# --------------------------------------------------------------------------- #
_CORPUS = [
    {
        "doc_id": "doc-a", "text": "alpha revenue grew this quarter",
        "source_id": "SRC-A", "sensitivity": "internal", "chunk_index": 0,
        "start": 0, "end": 31, "title": "A",
    },
    {
        "doc_id": "doc-b", "text": "beta customers churned in the period",
        "source_id": "SRC-B", "sensitivity": "confidential", "chunk_index": 0,
        "start": 0, "end": 36, "title": "B",
    },
]

_MODEL = "test-embed-v1"


def _vec(*vals) -> list:
    return [float(v) for v in vals]


def _new_index(root, *, force_fallback):
    return ki.KnowledgeIndex(root, force_fallback=force_fallback)


def _vector_count(idx, source_id=None) -> int:
    con = sqlite3.connect(str(idx.db_path))
    try:
        if source_id is None:
            return con.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
        return con.execute(
            "SELECT COUNT(*) FROM chunk_vectors WHERE source_id=?", (source_id,)
        ).fetchone()[0]
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# schema: no sensitivity column
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("force_fallback", [True, False])
def test_chunk_vectors_schema_has_no_sensitivity_column(
    force_fallback, tmp_path, minimal_oracle
):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        cols = [
            r[1]
            for r in idx._con.execute("PRAGMA table_info(chunk_vectors)").fetchall()
        ]
    assert "sensitivity" not in cols, cols
    assert set(cols) == {
        "source_id", "chunk_index", "embedding_model", "dim", "norm", "vector",
    }


# --------------------------------------------------------------------------- #
# add / round-trip
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("force_fallback", [True, False])
def test_add_vectors_roundtrip_and_count(force_fallback, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks(_CORPUS)
        n = idx.add_vectors([
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _vec(3.0, 4.0)},
        ])
        assert n == 1
        assert _vector_count(idx, "SRC-A") == 1
        # Stored unit-normalized: norm column records the ORIGINAL L2 norm (5.0).
        row = idx._con.execute(
            "SELECT dim, norm, vector FROM chunk_vectors WHERE source_id=?",
            ("SRC-A",),
        ).fetchone()
        assert int(row["dim"]) == 2
        assert abs(float(row["norm"]) - 5.0) < 1e-5
        stored = array("f")
        stored.frombytes(row["vector"])
        # (3,4)/5 == (0.6, 0.8)
        assert abs(stored[0] - 0.6) < 1e-5 and abs(stored[1] - 0.8) < 1e-5


@pytest.mark.parametrize("force_fallback", [True, False])
def test_add_vectors_upsert_replaces(force_fallback, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks(_CORPUS)
        idx.add_vectors([
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _vec(1.0, 0.0)},
        ])
        idx.add_vectors([
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _vec(0.0, 1.0)},
        ])
        assert _vector_count(idx, "SRC-A") == 1
        row = idx._con.execute(
            "SELECT vector FROM chunk_vectors WHERE source_id=?", ("SRC-A",)
        ).fetchone()
        stored = array("f")
        stored.frombytes(row["vector"])
        assert abs(stored[0] - 0.0) < 1e-5 and abs(stored[1] - 1.0) < 1e-5


# --------------------------------------------------------------------------- #
# degenerate-vector rejection (P8S-11)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [[0.0, 0.0], [float("nan"), 1.0], [float("inf"), 0.0]])
def test_add_vectors_rejects_degenerate(bad, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        with pytest.raises(ValueError):
            idx.add_vectors([
                {"source_id": "SRC-A", "chunk_index": 0,
                 "embedding_model": _MODEL, "vector": bad},
            ])
        # Nothing written.
        assert _vector_count(idx) == 0


def test_add_vectors_error_never_echoes_vector(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    secret = 1234567.0
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        try:
            idx.add_vectors([
                {"source_id": "SRC-A", "chunk_index": 0,
                 "embedding_model": _MODEL, "vector": [0.0, 0.0, secret * 0.0]},
            ])
        except ValueError as exc:
            assert str(secret) not in str(exc)


# --------------------------------------------------------------------------- #
# lifecycle: delete_source + upsert + wipe leave zero vectors (P8S-6)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("force_fallback", [True, False])
def test_delete_source_removes_vectors(force_fallback, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks(_CORPUS)
        idx.add_vectors([
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _vec(1.0, 0.0)},
            {"source_id": "SRC-B", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _vec(0.0, 1.0)},
        ])
        assert _vector_count(idx) == 2
        idx.delete_source("SRC-A")
        assert _vector_count(idx, "SRC-A") == 0
        assert _vector_count(idx, "SRC-B") == 1


class _CrashOnCommit:
    """A connection proxy that raises on commit -- simulates a crash BEFORE the
    single transaction is committed. Everything else delegates to the real
    connection, so the chunk + vector deletes are staged in one uncommitted
    transaction and the raise leaves them un-persisted together."""

    def __init__(self, real):
        self._real = real

    def commit(self):
        raise sqlite3.OperationalError("simulated crash before commit")

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.mark.parametrize("force_fallback", [True, False])
def test_delete_source_is_single_transaction(force_fallback, tmp_path, minimal_oracle):
    """A crash mid-delete can never leave a vector behind a deleted chunk.

    We can't kill the process, but we CAN prove the chunk + vector deletions
    share one commit: if commit raises after the deletes are staged, a fresh
    connection (which only sees committed state) must show BOTH chunk and vector
    still present -- the whole transaction was lost atomically, not half of it.
    """
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks(_CORPUS)
        idx.add_vectors([
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _vec(1.0, 0.0)},
        ])
        real = idx._con
        idx._con = _CrashOnCommit(real)  # type: ignore[assignment]
        with pytest.raises(sqlite3.OperationalError):
            idx.delete_source("SRC-A")
        idx._con = real  # type: ignore[assignment]
        # The "crash" aborts the process before commit -> the staged deletes are
        # never persisted. Model that by rolling back the uncommitted txn.
        real.rollback()

    # A SEPARATE connection sees only committed state: the crash committed
    # nothing, so the chunk AND its vector both survive together.
    con = sqlite3.connect(str(ki.default_db_path(root)))
    try:
        nvec = con.execute(
            "SELECT COUNT(*) FROM chunk_vectors WHERE source_id='SRC-A'"
        ).fetchone()[0]
        nchunk = con.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_id='SRC-A'"
        ).fetchone()[0]
    finally:
        con.close()
    assert nvec == 1
    assert nchunk == 1


@pytest.mark.parametrize("force_fallback", [True, False])
def test_upsert_chunk_drops_old_vector(force_fallback, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks(_CORPUS)
        idx.add_vectors([
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _vec(1.0, 0.0)},
        ])
        assert _vector_count(idx, "SRC-A") == 1
        # Re-ingest the same (source_id, chunk_index) -- a reclassified chunk.
        idx.add(
            "doc-a", "alpha revenue grew this quarter, restated",
            source_id="SRC-A", sensitivity="secret", chunk_index=0,
        )
        # The old vector (minted under the old label) is gone -> lexical-only.
        assert _vector_count(idx, "SRC-A") == 0


@pytest.mark.parametrize("force_fallback", [True, False])
def test_wipe_and_reindex_drop_vectors(force_fallback, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks(_CORPUS)
        idx.add_vectors([
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _vec(1.0, 0.0)},
        ])
        assert _vector_count(idx) == 1
        idx.reindex(_CORPUS)
        assert _vector_count(idx) == 0


# --------------------------------------------------------------------------- #
# pending_vectors
# --------------------------------------------------------------------------- #
def test_pending_vectors_returns_uncovered_chunks(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        idx.add_vectors([
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _vec(1.0, 0.0)},
        ])
        pend = idx.pending_vectors(embedding_model=_MODEL)
        ids = {p["source_id"] for p in pend}
        assert ids == {"SRC-B"}
        # A different model: everything is pending.
        pend2 = idx.pending_vectors(embedding_model="other-model")
        assert {p["source_id"] for p in pend2} == {"SRC-A", "SRC-B"}


def test_pending_vectors_ceiling_and_limit(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        # SRC-B is confidential; an internal ceiling filters it out.
        pend = idx.pending_vectors(
            embedding_model=_MODEL, max_sensitivity="internal"
        )
        assert {p["source_id"] for p in pend} == {"SRC-A"}
        pend_lim = idx.pending_vectors(embedding_model=_MODEL, limit=1)
        assert len(pend_lim) == 1


# --------------------------------------------------------------------------- #
# stats coverage (P8-T1)
# --------------------------------------------------------------------------- #
def test_stats_reports_vector_coverage(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        idx.add_vectors([
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _vec(1.0, 0.0)},
        ])
        st = idx.stats()
        assert st["vectors"] == 1
        assert st["by_embedding_model"] == {_MODEL: 1}
        assert abs(st["vector_coverage"][_MODEL] - 0.5) < 1e-9  # 1 of 2 chunks
        assert st["dim_mismatches"] == 0


# --------------------------------------------------------------------------- #
# orphan-vector doctor query (P8S-6)
# --------------------------------------------------------------------------- #
def test_orphan_vectors_query(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        idx.add_vectors([
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _vec(1.0, 0.0)},
        ])
        assert idx.orphan_vectors() == []
        # Force an orphan by deleting the chunk row directly (simulating a crash
        # that broke the single-transaction guarantee).
        idx._con.execute("DELETE FROM chunks WHERE source_id='SRC-A'")
        if idx.engine == "fts5":
            idx._con.execute("DELETE FROM chunks_key WHERE source_id='SRC-A'")
        idx._con.commit()
        orphans = idx.orphan_vectors()
        assert len(orphans) == 1
        assert orphans[0]["source_id"] == "SRC-A"
        assert orphans[0]["embedding_model"] == _MODEL


# --------------------------------------------------------------------------- #
# prune_vectors (model supersession)
# --------------------------------------------------------------------------- #
def test_prune_vectors_drops_other_models(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        idx.add_vectors([
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": "old-model", "vector": _vec(1.0, 0.0)},
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": "new-model", "vector": _vec(0.0, 1.0)},
        ])
        assert _vector_count(idx) == 2
        dropped = idx.prune_vectors(keep_model="new-model")
        assert dropped == 1
        rows = idx._con.execute(
            "SELECT embedding_model FROM chunk_vectors"
        ).fetchall()
        assert [r[0] for r in rows] == ["new-model"]


# --------------------------------------------------------------------------- #
# active embedding model meta
# --------------------------------------------------------------------------- #
def test_active_embedding_model_meta(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        assert idx.active_embedding_model() is None
        idx.set_active_embedding_model(_MODEL)
        assert idx.active_embedding_model() == _MODEL


# --------------------------------------------------------------------------- #
# end-to-end supersession through ingest_pipeline (P8S-6)
# --------------------------------------------------------------------------- #
def test_supersession_removes_vectors_end_to_end(tmp_path, minimal_oracle, monkeypatch):
    """A connector re-sync that supersedes a source leaves zero vectors for it."""
    import ingest_pipeline

    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks([
            {"doc_id": "old", "text": "old content here", "source_id": "OLD-SRC",
             "sensitivity": "internal", "chunk_index": 0, "start": 0, "end": 16},
        ])
        idx.add_vectors([
            {"source_id": "OLD-SRC", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _vec(1.0, 0.0)},
        ])
        assert _vector_count(idx, "OLD-SRC") == 1

    # Stub the supersession lookup to report OLD-SRC as superseded by NEW-SRC.
    monkeypatch.setattr(
        ingest_pipeline, "_superseded_source_ids",
        lambda root, origin, new_sid: ["OLD-SRC"],
    )
    result = ingest_pipeline._remove_superseded_chunks(root, "report.pdf", "NEW-SRC")
    assert result["status"] == "ok"

    with _new_index(root, force_fallback=True) as idx2:
        assert _vector_count(idx2, "OLD-SRC") == 0
        # The chunk is gone too.
        assert idx2.list_chunks(source_id="OLD-SRC") == []


# --------------------------------------------------------------------------- #
# CLI routing (P8S-4): vectors-add must NOT become a text query
# --------------------------------------------------------------------------- #
def test_translate_vectors_add_does_not_misroute():
    """oracle search vectors-add routes to vectors-add, never a text query."""
    mod, args = oracle_cli._translate("search", ["vectors-add", "--file", "v.json"])
    assert mod == "knowledge_index"
    assert "vectors-add" in args
    assert "query" not in args
    # Belt-and-braces: the literal string "vectors-add" never becomes a --q term.
    assert "--q" not in args


def test_translate_vectors_pending_and_prune_route():
    for sub in ("vectors-pending", "vectors-prune"):
        mod, args = oracle_cli._translate("search", [sub, "--embedding-model", "M"])
        assert mod == "knowledge_index"
        assert sub in args
        assert "--q" not in args


def test_translate_qvec_stdin_passes_through():
    mod, args = oracle_cli._translate(
        "search", ["alpha", "revenue", "--qvec-stdin"]
    )
    assert mod == "knowledge_index"
    assert args[args.index("query")] == "query"
    assert "--qvec-stdin" in args


def test_translate_plain_query_still_works():
    mod, args = oracle_cli._translate("search", ["alpha", "revenue"])
    assert mod == "knowledge_index"
    assert "query" in args
    assert "--q" in args
    assert args[args.index("--q") + 1] == "alpha revenue"


def test_cli_vectors_add_returns_added_shape(tmp_path, minimal_oracle):
    """The vectors-add CLI emits the frozen {"added": N} response shape."""
    import io
    import contextlib

    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
    vfile = tmp_path / "vectors.json"
    vfile.write_text(json.dumps([
        {"source_id": "SRC-A", "chunk_index": 0,
         "embedding_model": _MODEL, "vector": [1.0, 0.0]},
    ]), encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ki.main([
            "--root", str(root), "--force-fallback",
            "vectors-add", "--file", str(vfile),
        ])
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out == {"added": 1}


def test_cli_vectors_pending_and_prune(tmp_path, minimal_oracle):
    import io
    import contextlib

    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        idx.add_vectors([
            {"source_id": "SRC-A", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": [1.0, 0.0]},
        ])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ki.main([
            "--root", str(root), "--force-fallback",
            "vectors-pending", "--embedding-model", _MODEL,
        ])
    assert rc == 0
    pend = json.loads(buf.getvalue())
    assert {p["source_id"] for p in pend} == {"SRC-B"}

    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = ki.main([
            "--root", str(root), "--force-fallback",
            "vectors-prune", "--keep-model", "nonexistent",
        ])
    assert rc2 == 0
    assert json.loads(buf2.getvalue()) == {"pruned": 1}


# --------------------------------------------------------------------------- #
# migration 0004 idempotency
# --------------------------------------------------------------------------- #
def _run_migration(module_basename: str, root):
    seq = None
    for s, b in _migrations_pkg.discover():
        if b == module_basename:
            seq = s
            break
    assert seq is not None, f"migration {module_basename!r} not found"
    mig = _migrations_pkg.load_migration(seq, module_basename)
    return mig.apply(root)


def test_migration_0004_creates_chunk_vectors_and_is_idempotent(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    # Build a DB WITHOUT chunk_vectors, simulating a pre-0004 database.
    db_path = ki.default_db_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE IF NOT EXISTS index_meta (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO index_meta(k, v) VALUES('engine', 'fallback')")
    con.execute(
        "CREATE TABLE chunks (rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
        "doc_id TEXT, source_id TEXT, sensitivity TEXT, provenance TEXT, "
        "title TEXT, chunk_index INTEGER, start_off INTEGER, end_off INTEGER, "
        "body TEXT, UNIQUE(source_id, chunk_index))"
    )
    con.commit()
    con.close()

    # First run creates the table.
    r1 = _run_migration("0004_chunk_vectors", root)
    assert r1["changed"] is True

    con = sqlite3.connect(str(db_path))
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunk_vectors'"
    ).fetchone()
    con.close()
    assert exists is not None

    # Second run is a no-op.
    r2 = _run_migration("0004_chunk_vectors", root)
    assert r2["changed"] is False


def test_migration_0004_missing_db_is_safe(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    r = _run_migration("0004_chunk_vectors", root)
    assert r["changed"] is False
    assert "db not found" in r["notes"]
