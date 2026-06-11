#!/usr/bin/env python3
"""Tests for the retrieval_event ledger + query_hmac (P8-T7).

Load-bearing guarantees exercised here (per the Phase-8 spec + stress table):

  * Monthly rotation (P8S-8): the ledger file name carries YYYYMM; a month
    rollover starts a fresh file whose fresh hash chain ``ledger.verify``
    accepts (the restarted chain is a legacy-prefix-tolerant new chain).
  * No drop_id minting (P8S-8): rows carry ts + row_hash as identity, no
    drop_id.
  * query_hmac, not sha256 (P8S-9): the stored hash is NOT sha256(query)
    (dictionary test), the salt is 0600 under _data.nosync, never ledgered,
    and the query text never appears in any row.
  * Best-effort, never blocks/fails a search (P8S-8): a read-only ledger dir
    swallows the append and the search still returns hits; the search CLI
    path logs a row on a normal run.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import ledger  # noqa: E402
import knowledge_index as ki  # noqa: E402
import retrieval_ledger as rl  # noqa: E402


# --------------------------------------------------------------------------- #
# query_hmac (P8S-9)
# --------------------------------------------------------------------------- #
def test_query_hmac_is_not_bare_sha256(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    q = "what is the headcount"
    h = rl.query_hmac(root, q)
    # The dictionary-reversibility test: the stored hash must NOT equal a bare
    # sha256 of the query (an attacker's wordlist hash would match that).
    assert h != hashlib.sha256(q.encode("utf-8")).hexdigest()
    # Deterministic within a root.
    assert rl.query_hmac(root, q) == h


def test_salt_is_0600_under_data_nosync_and_not_ledgered(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    rl.query_hmac(root, "x")
    salt_path = root / "_data.nosync" / "retrieval_salt"
    assert salt_path.exists()
    mode = stat.S_IMODE(salt_path.stat().st_mode)
    assert mode == 0o600, oct(mode)
    # The salt bytes must never appear in any ledger row.
    salt = salt_path.read_bytes()
    rl.log_search(
        root, query="secret query", k=5, engine="fallback", hybrid=False,
        vector_coverage=None, result_count=0, top_source_ids=[],
    )
    led = rl.retrieval_ledger_path(root)
    raw = led.read_bytes()
    assert salt not in raw


def test_two_different_roots_get_different_salts(tmp_path, minimal_oracle):
    r1 = minimal_oracle(tmp_path / "a")
    r2 = minimal_oracle(tmp_path / "b")
    q = "same query text"
    # Different per-root salts -> different hmac for the same query.
    assert rl.query_hmac(r1, q) != rl.query_hmac(r2, q)


# --------------------------------------------------------------------------- #
# row shape: metadata only, no drop_id, no query text (P8S-8/9)
# --------------------------------------------------------------------------- #
def test_log_search_row_shape_metadata_only(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    ok = rl.log_search(
        root, query="how many employees", k=10, engine="fts5", hybrid=True,
        vector_coverage=0.5, result_count=3,
        top_source_ids=["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
    )
    assert ok is True
    rows, warnings = ledger.load(rl.retrieval_ledger_path(root))
    assert warnings == []
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "retrieval_event"
    assert "drop_id" not in row          # no drop_id minting (P8S-8)
    assert "row_hash" in row             # but the chain is present
    assert "ts" in row
    assert "query" not in row            # never the query text
    assert "how many employees" not in json.dumps(row)
    assert row["k"] == 10
    assert row["engine"] == "fts5"
    assert row["hybrid"] is True
    assert row["vector_coverage"] == 0.5
    assert row["result_count"] == 3
    assert row["top_source_ids"] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]


def test_monthly_rotation_path_and_fresh_chain_verifies(tmp_path, minimal_oracle):
    import datetime as _dt

    root = minimal_oracle(tmp_path)
    jan = _dt.datetime(2026, 1, 15, 12, 0, 0)
    feb = _dt.datetime(2026, 2, 3, 9, 0, 0)
    rl.log_search(
        root, query="q1", k=5, engine="fallback", hybrid=False,
        vector_coverage=None, result_count=1, top_source_ids=["s1"], now=jan,
    )
    rl.log_search(
        root, query="q2", k=5, engine="fallback", hybrid=False,
        vector_coverage=None, result_count=1, top_source_ids=["s2"], now=feb,
    )
    jan_path = rl.retrieval_ledger_path(root, now=jan)
    feb_path = rl.retrieval_ledger_path(root, now=feb)
    assert jan_path.name == "retrieval_event-202601.jsonl"
    assert feb_path.name == "retrieval_event-202602.jsonl"
    assert jan_path != feb_path
    # Each month is its own fresh hash chain that verify accepts: the chain
    # validates (no breaks, no bad lines) and restarts cleanly per file. (The
    # rows intentionally carry no drop_id -- ts + row_hash are identity, P8S-8 --
    # so we assert chain integrity rather than the drop_id-completeness flag.)
    for p in (jan_path, feb_path):
        rep = ledger.verify(p)
        assert rep["chain_breaks"] == []
        assert rep["bad_lines"] == []


# --------------------------------------------------------------------------- #
# best-effort: never fails/blocks a search (P8S-8)
# --------------------------------------------------------------------------- #
def test_log_search_best_effort_swallows_failure(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)

    def _boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(rl.ledger, "append", _boom)
    # Must NOT raise; reports False.
    assert rl.log_search(
        root, query="q", k=5, engine="fallback", hybrid=False,
        vector_coverage=None, result_count=0, top_source_ids=[],
    ) is False


def test_search_cli_logs_retrieval_event_and_still_returns_hits(
    tmp_path, minimal_oracle
):
    root = minimal_oracle(tmp_path)
    with ki.KnowledgeIndex(root, force_fallback=True) as idx:
        idx.add(
            "doc-a", "annual revenue grew to ten million dollars",
            source_id="SRC-A", sensitivity="internal", chunk_index=0,
        )
    buf = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buf):
        rc = ki.main([
            "--root", str(root), "--force-fallback",
            "query", "--q", "revenue", "--k", "5",
        ])
    assert rc == 0
    hits = json.loads(buf.getvalue())
    assert hits and hits[0]["source_id"] == "SRC-A"
    # A retrieval_event row landed for this search.
    rows, _w = ledger.load(rl.retrieval_ledger_path(root))
    assert len(rows) == 1
    assert rows[0]["engine"] == "fallback"
    assert rows[0]["hybrid"] is False
    assert rows[0]["result_count"] == 1
    assert "SRC-A" in rows[0]["top_source_ids"]


def test_search_cli_read_path_survives_when_append_fails(
    tmp_path, minimal_oracle, monkeypatch
):
    """A read-only ledger (append fails) must NOT break the search (P8S-8)."""
    root = minimal_oracle(tmp_path)
    with ki.KnowledgeIndex(root, force_fallback=True) as idx:
        idx.add("doc-a", "quarterly revenue figures", source_id="SRC-A",
                chunk_index=0)

    def _boom(*a, **k):
        raise OSError("read-only root")

    monkeypatch.setattr(rl.ledger, "append", _boom)
    buf = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buf):
        rc = ki.main([
            "--root", str(root), "--force-fallback",
            "query", "--q", "revenue",
        ])
    assert rc == 0
    hits = json.loads(buf.getvalue())
    assert hits  # search still returned results despite the write failure
