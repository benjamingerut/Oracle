"""tests/shell/test_embedder_enforcer.py -- the embedding egress enforcer (P8-T4).

The security pin: an embedding call IS content egress, so the policy bridge's
environment x sensitivity ceiling -- INCLUDING the egress veto -- applies to
embedding requests exactly as to chat requests, enforced in code at the dispatch
(I5), failing closed (I4). These tests prove:

  * over-ceiling chunks/queries NEVER leave (the dispatch boundary holds);
  * the egress veto reclassifies a loopback ``*:cloud`` embedder external;
  * a ceiling-computation error -> zero embedding requests (fail closed);
  * any embed transport failure degrades silently to lexical (no error
    surfaced);
  * the vectors-* CLI is never a model tool (structural, SH-005-style);
  * the query-vector stdin payload is composed shell-side only (model terms
    never reach it).

Named enforcer tests (frozen in the spec): test_embed_dispatch_blocks_over_
ceiling_chunks, test_external_embedder_disables_vector_search_above_public,
test_embed_ceiling_applies_egress_veto, test_ceiling_error_fails_closed_no_egress.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from oracle_agent.agentloop import embedder as emb
from oracle_agent.agentloop import policy_bridge as pb
from oracle_agent.agentloop.verbtools import Dispatcher, run_verb, tool_schemas
from oracle_agent.testkit import FakeEmbedClient

ORDER = list(pb.CANONICAL_ORDER)


@pytest.fixture
def fresh_root(tmp_path):
    """A function-scoped spawned root, so corpus mutations in embed_pending
    round-trip tests do not leak across tests (the session spawned_root is
    shared and accumulates chunks)."""
    from oracle_agent.testkit import spawn_test_root

    root = tmp_path / "root"
    try:
        spawn_test_root(root, name="Embed Enforcer Co")
    except RuntimeError as exc:
        pytest.skip(f"spawn failed: {exc}")
    return root


# --------------------------------------------------------------------------- #
# embedding_ceiling: post-veto classification (P8S-1)
# --------------------------------------------------------------------------- #
def test_embed_ceiling_applies_egress_veto(spawned_root, monkeypatch):
    """A loopback endpoint serving a ``*:cloud`` embedding model is vetoed ->
    reclassified external BEFORE max_sensitivity_for, so its ceiling is public
    (the named enforcer test)."""
    # A genuine loopback URL but a provably cloud-proxied model name.
    ceiling = emb.embedding_ceiling(
        spawned_root, "http://127.0.0.1:11434/v1", "qwen3:cloud")
    assert ceiling == "public", (
        "a :cloud embedding model on a loopback endpoint must be vetoed to "
        "external (public ceiling)"
    )


def test_embed_ceiling_external_url_is_public(spawned_root):
    """An external embedding base_url classifies external -> public ceiling,
    independent of the chat endpoint (P8S-1)."""
    ceiling = emb.embedding_ceiling(
        spawned_root, "https://api.openai.com/v1", "text-embedding-3-small")
    assert ceiling == "public"


def test_ceiling_error_fails_closed_no_egress(monkeypatch):
    """ANY error computing the ceiling -> 'public' (fail closed, I4)."""
    def boom(*_a, **_k):
        raise RuntimeError("classification blew up")

    monkeypatch.setattr(pb, "environment_for", boom)
    ceiling = emb.embedding_ceiling(
        Path("/nonexistent"), "http://127.0.0.1:1/v1", "m")
    assert ceiling == "public"


# --------------------------------------------------------------------------- #
# query_vector_allowed: the frozen query rule (P8S-3)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("retrieval,embed,expected", [
    ("public", "public", True),
    ("internal", "internal", True),
    ("public", "internal", True),       # below embed ceiling -> allowed
    ("internal", "public", False),      # above embed ceiling -> disabled
    ("confidential", "internal", False),
    ("internal", "confidential", True),
])
def test_query_vector_allowed_is_a_ceiling_comparison(retrieval, embed, expected):
    assert emb.query_vector_allowed(retrieval, embed, ORDER) is expected


def test_external_embedder_disables_vector_search_above_public():
    """An external/vetoed embedder (ceiling public) disables vector search for
    every internal-and-above surface (the named enforcer test)."""
    # internal surface, public embedder -> NOT allowed.
    assert emb.query_vector_allowed("internal", "public", ORDER) is False
    # but a public surface still embeds against a public embedder.
    assert emb.query_vector_allowed("public", "public", ORDER) is True


# --------------------------------------------------------------------------- #
# the injected query_embedder callable (query-path seam, P8S-3)
# --------------------------------------------------------------------------- #
def test_query_embedder_returns_none_when_rule_disallows():
    """When the surface ceiling is above the embed ceiling, the callable is a
    constant None (vector search structurally disabled) AND never calls embed."""
    fake = FakeEmbedClient()
    qe = emb.build_query_embedder(
        fake, embed_model="m", embed_ceiling="public",
        retrieval_ceiling="internal", order=ORDER)
    assert qe("any query terms") is None
    fake.assert_no_requests()  # the public embedder never saw the internal query


def test_query_embedder_composes_shell_side_payload():
    """When allowed, the callable returns a {model, vector} payload composed of a
    config-sourced model string + computed floats; the model's terms are NOT a
    payload key."""
    fake = FakeEmbedClient(dim=4)
    qe = emb.build_query_embedder(
        fake, embed_model="emb-1", embed_ceiling="internal",
        retrieval_ceiling="internal", order=ORDER)
    payload = qe("revenue last quarter")
    assert payload is not None
    assert payload["embedding_model"] == "emb-1"
    assert isinstance(payload["vector"], list) and len(payload["vector"]) == 4
    # The only keys are the model string and the vector -- no model text.
    assert set(payload.keys()) == {"embedding_model", "vector"}
    assert "revenue last quarter" not in json.dumps(payload)


def test_query_embedder_silent_lexical_on_transport_failure():
    """ANY transport failure -> None (silent lexical degradation, no raise)."""
    fake = FakeEmbedClient(fail=True)
    qe = emb.build_query_embedder(
        fake, embed_model="m", embed_ceiling="internal",
        retrieval_ceiling="internal", order=ORDER)
    assert qe("revenue") is None  # the LLMError is swallowed -> lexical


# --------------------------------------------------------------------------- #
# Dispatcher seam: _do_oracle_search composes --qvec-stdin shell-side only
# --------------------------------------------------------------------------- #
def _disp(root, query_embedder=None, **kw):
    defaults = dict(root=root, surface="local", environment="local_agent",
                    max_sensitivity="internal", order=ORDER,
                    query_embedder=query_embedder)
    defaults.update(kw)
    return Dispatcher(**defaults)


def test_search_lexical_when_no_query_embedder(spawned_root, monkeypatch):
    """No query_embedder => lexical exactly as today: no --qvec-stdin, no stdin."""
    captured = {}

    def fake_run(self, argv, stdin=None):
        captured["argv"] = argv
        captured["stdin"] = stdin
        return 0, "[]", ""

    monkeypatch.setattr(Dispatcher, "_run", fake_run)
    _disp(spawned_root).dispatch("oracle_search", {"terms": "revenue"})
    assert "--qvec-stdin" not in captured["argv"]
    assert captured["stdin"] is None


def test_search_adds_qvec_stdin_when_embedder_allows(spawned_root, monkeypatch):
    """An allowing embedder => --qvec-stdin flag + a shell-composed stdin payload
    carrying the config model string and computed floats, never the terms."""
    captured = {}

    def fake_run(self, argv, stdin=None):
        captured["argv"] = argv
        captured["stdin"] = stdin
        return 0, "[]", ""

    monkeypatch.setattr(Dispatcher, "_run", fake_run)
    fake = FakeEmbedClient(dim=3)
    qe = emb.build_query_embedder(
        fake, embed_model="emb-1", embed_ceiling="internal",
        retrieval_ceiling="internal", order=ORDER)
    _disp(spawned_root, query_embedder=qe).dispatch(
        "oracle_search", {"terms": "headcount across teams"})
    assert "--qvec-stdin" in captured["argv"]
    payload = json.loads(captured["stdin"])
    assert payload["embedding_model"] == "emb-1"
    assert len(payload["vector"]) == 3
    # The model's terms NEVER enter the stdin payload (shell-composed only).
    assert "headcount" not in captured["stdin"]


def test_search_silent_lexical_when_embedder_fails(spawned_root, monkeypatch):
    """A failing embedder => no --qvec-stdin, stdin None, search still runs
    (silent lexical, no error surfaced to the model)."""
    captured = {}

    def fake_run(self, argv, stdin=None):
        captured["argv"] = argv
        captured["stdin"] = stdin
        return 0, "[]", ""

    monkeypatch.setattr(Dispatcher, "_run", fake_run)
    fake = FakeEmbedClient(fail=True)
    qe = emb.build_query_embedder(
        fake, embed_model="m", embed_ceiling="internal",
        retrieval_ceiling="internal", order=ORDER)
    out = _disp(spawned_root, query_embedder=qe).dispatch(
        "oracle_search", {"terms": "revenue"})
    assert "--qvec-stdin" not in captured["argv"]
    assert captured["stdin"] is None
    assert out.rc == 0  # no error surfaced


def test_search_disabled_embedder_runs_lexical_above_public(spawned_root, monkeypatch):
    """An internal-surface query with a PUBLIC-ceiling embedder is never embedded
    (vector search disabled), lexical results still returned (named acceptance)."""
    captured = {}

    def fake_run(self, argv, stdin=None):
        captured["argv"] = argv
        captured["stdin"] = stdin
        return 0, "[]", ""

    monkeypatch.setattr(Dispatcher, "_run", fake_run)
    fake = FakeEmbedClient()
    qe = emb.build_query_embedder(
        fake, embed_model="m", embed_ceiling="public",
        retrieval_ceiling="internal", order=ORDER)
    _disp(spawned_root, max_sensitivity="internal", query_embedder=qe).dispatch(
        "oracle_search", {"terms": "internal figure"})
    assert "--qvec-stdin" not in captured["argv"]
    assert captured["stdin"] is None
    fake.assert_no_requests()  # the public embedder never saw the internal query


# --------------------------------------------------------------------------- #
# embed_pending: the per-chunk dispatch enforcer (P8S-14)
# --------------------------------------------------------------------------- #
def _build_corpus(root: Path, chunks: list[dict]) -> None:
    """Add chunks to the spawned root's index via the build chokepoint."""
    f = root / "tmp_chunks.json"
    f.write_text(json.dumps(chunks), encoding="utf-8")
    rc, out, err = run_verb(root, ["search", "build", "--file", str(f)])
    f.unlink()
    assert rc == 0, f"build failed: {out} {err}"


def test_embed_dispatch_blocks_over_ceiling_chunks(fresh_root):
    """An internal chunk is NEVER in any embedding request when the embedder
    ceiling is public (the named enforcer test). Below-ceiling chunks embed."""
    _build_corpus(fresh_root, [
        {"doc_id": "d-pub", "source_id": "s-pub", "sensitivity": "public",
         "chunk_index": 0, "text": "PUBLIC_MARKER public revenue figure"},
        {"doc_id": "d-int", "source_id": "s-int", "sensitivity": "internal",
         "chunk_index": 0, "text": "INTERNAL_MARKER confidential headcount detail"},
    ])
    fake = FakeEmbedClient(dim=4)
    summary = emb.embed_pending(
        fake, fresh_root, ceiling="public", embed_model="emb-1",
        order=ORDER, ledger=False)
    # The internal chunk's distinctive text must NEVER have egressed.
    fake.assert_no_text("INTERNAL_MARKER")
    assert summary["skipped_over_ceiling"] >= 1
    assert summary["embedded"] >= 1
    # The public chunk DID embed.
    assert any("PUBLIC_MARKER" in t for t in fake.all_texts)


def test_embed_pending_internal_ceiling_embeds_internal(fresh_root):
    """With an internal ceiling, internal chunks embed; the round-trip writes
    vectors the kernel accepts ({"added": N} shape validated)."""
    _build_corpus(fresh_root, [
        {"doc_id": "d2", "source_id": "s2", "sensitivity": "internal",
         "chunk_index": 0, "text": "internal headcount 412"},
    ])
    fake = FakeEmbedClient(dim=4)
    summary = emb.embed_pending(
        fake, fresh_root, ceiling="internal", embed_model="emb-2",
        order=ORDER, ledger=False)
    assert summary["embedded"] == 1
    # Confirm coverage landed via the kernel stats.
    rc, out, _ = run_verb(fresh_root, ["search", "stats"])
    stats = json.loads(out)
    assert stats["by_embedding_model"].get("emb-2") == 1


def test_embed_pending_writes_metadata_only_ledger(fresh_root):
    """Each batch lands one metadata-only embedding_event row -- never text,
    never vectors."""
    _build_corpus(fresh_root, [
        {"doc_id": "d3", "source_id": "s3", "sensitivity": "public",
         "chunk_index": 0, "text": "LEDGER_TEXT_MARKER public note"},
    ])
    fake = FakeEmbedClient(dim=4)
    emb.embed_pending(fake, fresh_root, ceiling="public",
                      embed_model="emb-3", order=ORDER, ledger=True)
    ledger = fresh_root / "Meta.nosync" / "ledgers" / "embedding_event.jsonl"
    assert ledger.exists()
    body = ledger.read_text(encoding="utf-8")
    assert "embedding_event" in body
    assert "emb-3" in body
    # NEVER text, NEVER vectors.
    assert "LEDGER_TEXT_MARKER" not in body
    assert "vector" not in body


def test_embed_pending_handoff_file_is_gone_after(fresh_root):
    """The 0600 in-root tmp.nosync handoff file does not outlive vectors-add
    (P8S-10)."""
    _build_corpus(fresh_root, [
        {"doc_id": "d4", "source_id": "s4", "sensitivity": "public",
         "chunk_index": 0, "text": "handoff lifecycle public"},
    ])
    fake = FakeEmbedClient(dim=4)
    emb.embed_pending(fake, fresh_root, ceiling="public",
                      embed_model="emb-4", order=ORDER, ledger=False)
    tmp_dir = fresh_root / "tmp.nosync"
    leftover = list(tmp_dir.glob("vectors-*.json")) if tmp_dir.exists() else []
    assert leftover == [], f"handoff file outlived vectors-add: {leftover}"


def test_embed_pending_no_egress_when_all_over_ceiling(fresh_root):
    """A public embedder against an all-internal corpus makes zero requests
    (the enforcer drops every chunk before the embed call)."""
    _build_corpus(fresh_root, [
        {"doc_id": "d5", "source_id": "s5", "sensitivity": "internal",
         "chunk_index": 0, "text": "OVER_CEILING_ONLY internal"},
    ])
    fake = FakeEmbedClient(dim=4)
    summary = emb.embed_pending(
        fake, fresh_root, ceiling="public", embed_model="emb-5",
        order=ORDER, ledger=False)
    fake.assert_no_requests()
    assert summary["embedded"] == 0
    assert summary["skipped_over_ceiling"] >= 1


# --------------------------------------------------------------------------- #
# structural: vectors-* never a model tool (SH-005-style, P8S-10)
# --------------------------------------------------------------------------- #
def test_vectors_subcommands_never_in_tool_schemas():
    """The vectors-* CLI surface (bulk corpus-text export + vector injection) is
    structurally absent from every tool schema on every surface."""
    for surface in ("local", "gateway"):
        for env in ("local_agent", "external"):
            for t in tool_schemas(surface, env):
                fn = t["function"]
                blob = json.dumps(fn)
                assert "vectors-add" not in blob
                assert "vectors-pending" not in blob
                assert "vectors-prune" not in blob
                assert "qvec" not in blob
