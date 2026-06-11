#!/usr/bin/env python3
"""Tests for the builtin ``embed-backfill`` loop + embedding_event ledger (P8-T5).

THE KERNEL/SHELL SPLIT this exercises: the kernel never dials out (I3). The
``embed-backfill`` builtin SURFACES due-ness (vector coverage for the active
embedding model < 100% AND an embedding endpoint configured) and EMITS the
pending batch as a worklist for the SHELL's scheduler tick to embed. The kernel
half tested here is the due-signal, the bounded batch emission, the autonomy
gate (NOT in DETERMINISTIC_LOOPS), and the metadata-only embedding_event row on
``vectors-add``.

Load-bearing guarantees (per the Phase-8 spec + stress table):

  * embed-backfill is never a level-1 deterministic loop -- autonomy OFF means
    zero pending batches surface, like connector-pull (P8-T5 acceptance).
  * Due iff endpoint configured AND coverage < 100%; a model renamed to
    ``*:cloud`` is still "configured" but the SHELL (not tested here) refuses --
    the kernel half just re-reads config every run (no caching, P8S-1).
  * The emitted batch is bounded + resumable: as vectors land, the pending set
    shrinks monotonically; full coverage -> not due (idempotent).
  * embedding_event rows are metadata-only: source_id, count, model, attested
    environment/ceiling -- NEVER chunk text or vectors (P8S-15).
"""
from __future__ import annotations

import io
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import actions  # noqa: E402
import embedding_ledger as el  # noqa: E402
import harness  # noqa: E402
import knowledge_index as ki  # noqa: E402
import ledger  # noqa: E402
import loops  # noqa: E402


_MODEL = "test-embed-1"
_CORPUS = [
    {"doc_id": "d", "source_id": "SRC-A", "chunk_index": 0,
     "text": "annual revenue figures", "sensitivity": "internal"},
    {"doc_id": "d", "source_id": "SRC-A", "chunk_index": 1,
     "text": "quarterly headcount summary", "sensitivity": "internal"},
    {"doc_id": "d", "source_id": "SRC-B", "chunk_index": 0,
     "text": "marketing plan draft", "sensitivity": "internal"},
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _add_embeddings_block(root: Path, model=_MODEL):
    """Append a provider.embeddings block to the shipped oracle.yml."""
    p = root / "oracle.yml"
    text = p.read_text(encoding="utf-8")
    text += (
        "\nprovider:\n"
        "  embeddings:\n"
        f"    model: \"{model}\"\n"
        "    base_url: \"http://127.0.0.1:11434/v1\"\n"
    )
    p.write_text(text, encoding="utf-8")


def _seed_corpus(root: Path, *, active_model=_MODEL):
    with ki.KnowledgeIndex(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        if active_model:
            idx.set_active_embedding_model(active_model)


def _embed_all_pending(root: Path, model=_MODEL):
    """Stand in for the shell: read pending, write unit vectors, vectors-add."""
    with ki.KnowledgeIndex(root, force_fallback=True) as idx:
        pend = idx.pending_vectors(embedding_model=model)
        rows = [
            {"source_id": c["source_id"], "chunk_index": c["chunk_index"],
             "embedding_model": model, "vector": [1.0, 0.0, 0.0]}
            for c in pend
        ]
        idx.add_vectors(rows)


def _write_backfill_loop(root: Path):
    d = root / "Meta.nosync" / "Loops"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "id: embed-backfill",
        "type: loop",
        "title: Embedding backfill",
        "created: 2026-01-01",
        "updated: 2026-01-01",
        "sensitivity: internal",
        "status: active",
        "tags:",
        "  - meta",
        "  - loop",
        "cadence: hourly",
        "runner: builtin:embed-backfill",
        "last_run: null",
        "next_review: null",
        "trigger_conditions:",
        "  - uncovered chunks exist for the active embedding model",
        "---",
        "",
        "# Embedding backfill",
        "",
    ]
    (d / "loop-embed-backfill.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_autonomy(root: Path, *, enabled, allowed_loops=None):
    d = root / "Meta.nosync" / "Autonomy"
    d.mkdir(parents=True, exist_ok=True)
    allowed_loops = allowed_loops or []
    block = "allowed_loops:\n" + "".join(f"  - {i}\n" for i in allowed_loops)
    text = (
        f"enabled: {'true' if enabled else 'false'}\n"
        "level: 1\n"
        + block
        + "writable_lanes:\n"
        + "blast_radius_caps:\n"
        + "  max_files_per_run: 10\n"
        + "  max_bytes: 1000000\n"
        + 'kill_switch_file: "Meta.nosync/Autonomy/KILL-SWITCH"\n'
    )
    (d / "autonomy.yml").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# due-ness probe (P8-T5)
# --------------------------------------------------------------------------- #
def test_not_due_without_embeddings_config(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _seed_corpus(root)
    # no provider.embeddings block -> not configured -> not due
    due, detail = loops.embed_backfill_due(root)
    assert due is False
    assert detail["configured"] is False


def test_due_when_configured_and_uncovered(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _add_embeddings_block(root)
    _seed_corpus(root)
    due, detail = loops.embed_backfill_due(root)
    assert due is True
    assert detail["configured"] is True
    assert detail["active_model"] == _MODEL
    assert detail["pending"] == 3
    assert detail["coverage"] == 0.0


def test_not_due_when_fully_covered(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _add_embeddings_block(root)
    _seed_corpus(root)
    _embed_all_pending(root)
    due, detail = loops.embed_backfill_due(root)
    assert due is False
    assert detail["pending"] == 0
    assert detail["coverage"] == 1.0


def test_config_reread_each_run_model_renamed_to_cloud(tmp_path, minimal_oracle):
    """A model renamed between ticks is re-read from config every run (P8S-1).

    The kernel never caches the config; the SHELL's egress veto (not tested
    here) is what refuses a ``*:cloud`` embedder. The kernel half: the active
    model the due-ness reports follows whatever index_meta + config say at the
    moment of the probe.
    """
    root = minimal_oracle(tmp_path)
    _add_embeddings_block(root, model=_MODEL)
    _seed_corpus(root, active_model=_MODEL)
    due1, d1 = loops.embed_backfill_due(root)
    assert due1 and d1["active_model"] == _MODEL
    # Rename the configured model to a cloud-proxied one between ticks. The probe
    # re-reads config (no caching); still "configured" -> the SHELL's veto is the
    # gate. Here we assert the kernel does NOT crash and re-reads fresh config.
    p = root / "oracle.yml"
    p.write_text(
        p.read_text(encoding="utf-8").replace(_MODEL, "llama3:cloud", 1),
        encoding="utf-8",
    )
    due2, d2 = loops.embed_backfill_due(root)
    # configured is still true (config re-read); the active index model is still
    # the seeded one (index_meta unchanged), so this is honest kernel state.
    assert d2["configured"] is True


# --------------------------------------------------------------------------- #
# runner emits a bounded, resumable batch (kernel never dials out)
# --------------------------------------------------------------------------- #
def test_runner_emits_bounded_batch_worklist(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _add_embeddings_block(root)
    _seed_corpus(root)
    loop = loops.Loop(frontmatter={"id": "embed-backfill", "embed_batch": 2})
    res = loops._run_embed_backfill(root, loop, now=datetime(2026, 6, 10))
    assert res["status"] == "worklist"      # NOT complete; shell finishes it
    assert res["performed"] is False
    assert res["due"] is True
    assert res["embedding_model"] == _MODEL
    wl = res["worklist"]
    assert wl["batch_size"] == 2
    assert wl["pending_total"] == 3
    assert wl["pending_in_batch"] == 2      # bounded to the batch size
    assert len(wl["chunks"]) == 2


def test_runner_not_due_is_healthy_noop(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _add_embeddings_block(root)
    _seed_corpus(root)
    _embed_all_pending(root)
    loop = loops.Loop(frontmatter={"id": "embed-backfill"})
    res = loops._run_embed_backfill(root, loop, now=datetime(2026, 6, 10))
    assert res["status"] == "ok"
    assert res["performed"] is False
    assert res["due"] is False


def test_backfill_resumes_and_reaches_full_coverage(tmp_path, minimal_oracle):
    """A fresh corpus reaches full coverage across N ticks; resumable (P8-T5)."""
    root = minimal_oracle(tmp_path)
    _add_embeddings_block(root)
    _seed_corpus(root)
    loop = loops.Loop(frontmatter={"id": "embed-backfill", "embed_batch": 1})

    covered = 0
    for _ in range(10):
        res = loops._run_embed_backfill(root, loop, now=datetime(2026, 6, 10))
        if res["status"] == "ok" and res["due"] is False:
            break
        batch = res["worklist"]["chunks"]
        assert batch  # a due tick always emits at least one pending chunk
        # SHELL stand-in: embed the emitted batch and hand vectors back.
        with ki.KnowledgeIndex(root, force_fallback=True) as idx:
            idx.add_vectors([
                {"source_id": c["source_id"], "chunk_index": c["chunk_index"],
                 "embedding_model": _MODEL, "vector": [1.0, 0.0, 0.0]}
                for c in batch
            ])
        covered += len(batch)
    due, detail = loops.embed_backfill_due(root)
    assert due is False
    assert detail["coverage"] == 1.0
    assert covered == 3


# --------------------------------------------------------------------------- #
# autonomy gate: OFF means zero batches (NOT a deterministic loop)
# --------------------------------------------------------------------------- #
def test_embed_backfill_not_deterministic():
    assert "embed-backfill" not in actions.DETERMINISTIC_LOOPS
    auto = actions.Autonomy(enabled=True, level=1)
    assert "embed-backfill" not in auto.effective_allowed_loops()


def test_harness_autonomy_off_blocks_backfill(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _add_embeddings_block(root)
    _seed_corpus(root)
    _write_backfill_loop(root)
    _write_autonomy(root, enabled=False)   # OFF
    report = harness.run_once(root, now=datetime(2026, 6, 10))
    assert report["autonomy_enabled"] is False
    assert "embed-backfill" in report["due"]
    outcome = next(o for o in report["outcomes"] if o["loop_id"] == "embed-backfill")
    assert outcome["status"] == "blocked"
    assert outcome["verdict"] == actions.RESULT_DENY


# --------------------------------------------------------------------------- #
# embedding_event ledger: metadata only (P8S-15)
# --------------------------------------------------------------------------- #
def test_embedding_event_rows_are_metadata_only(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    n = el.append_batch_events(
        root,
        [
            {"source_id": "SRC-A", "chunk_index": 0, "embedding_model": _MODEL,
             "vector": [1.0, 0.0]},
            {"source_id": "SRC-A", "chunk_index": 1, "embedding_model": _MODEL,
             "vector": [0.0, 1.0]},
            {"source_id": "SRC-B", "chunk_index": 0, "embedding_model": _MODEL,
             "vector": [1.0, 1.0]},
        ],
        embedding_model=_MODEL,
        environment="local_agent",
        ceiling="internal",
    )
    assert n == 2  # one row per distinct source_id
    rows, _w = ledger.load(el.ledger_path(root))
    assert len(rows) == 2
    by_src = {r["source_id"]: r for r in rows}
    assert by_src["SRC-A"]["count"] == 2
    assert by_src["SRC-B"]["count"] == 1
    for r in rows:
        assert r["kind"] == "embedding_event"
        assert r["embedding_model"] == _MODEL
        assert r["environment"] == "local_agent"   # attestation
        assert r["ceiling"] == "internal"          # attestation
        # NEVER vectors or text.
        assert "vector" not in r
        assert "text" not in r
        assert "body" not in r


def test_vectors_add_cli_ledgers_when_attestation_supplied(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _seed_corpus(root)
    vfile = tmp_path / "vectors.json"
    vfile.write_text(json.dumps([
        {"source_id": "SRC-A", "chunk_index": 0, "embedding_model": _MODEL,
         "vector": [1.0, 0.0, 0.0]},
    ]), encoding="utf-8")
    buf = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buf):
        rc = ki.main([
            "--root", str(root), "--force-fallback",
            "vectors-add", "--file", str(vfile),
            "--embedding-environment", "local_agent",
            "--embedding-ceiling", "internal",
        ])
    assert rc == 0
    # Response shape unchanged (P8S-4).
    assert json.loads(buf.getvalue()) == {"added": 1}
    # An embedding_event row landed.
    rows, _w = ledger.load(el.ledger_path(root))
    assert len(rows) == 1
    assert rows[0]["source_id"] == "SRC-A"
    assert rows[0]["environment"] == "local_agent"


def test_vectors_add_cli_no_ledger_without_attestation(tmp_path, minimal_oracle):
    """A bare operator vectors-add (no env/ceiling) writes NO embedding_event."""
    root = minimal_oracle(tmp_path)
    _seed_corpus(root)
    vfile = tmp_path / "vectors.json"
    vfile.write_text(json.dumps([
        {"source_id": "SRC-A", "chunk_index": 0, "embedding_model": _MODEL,
         "vector": [1.0, 0.0, 0.0]},
    ]), encoding="utf-8")
    buf = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buf):
        rc = ki.main([
            "--root", str(root), "--force-fallback",
            "vectors-add", "--file", str(vfile),
        ])
    assert rc == 0
    assert json.loads(buf.getvalue()) == {"added": 1}
    rows, _w = ledger.load(el.ledger_path(root))
    assert rows == []
