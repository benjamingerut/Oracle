"""agentloop/embedder.py -- the embedding egress enforcer (Phase 8, the security pin).

An embedding call IS content egress: sending a chunk's text (or a query) to an
embeddings endpoint is, for policy purposes, indistinguishable from sending it
to a chat endpoint. The policy bridge's environment x sensitivity ceiling --
INCLUDING the egress veto (``policy_bridge.egress_veto``, the loopback-Ollama
``*:cloud`` reclassification) -- therefore applies to embedding requests exactly
as it applies to chat requests, enforced HERE in code at the dispatch (I5),
failing closed (I4). Chunks above the embedding endpoint's post-veto ceiling
stay lexical-only.

This module is the shell's enforcer. It never lives in the kernel (the kernel
never dials out, I3) and it is never reachable as a model tool. Three public
entry points:

  * :func:`embedding_ceiling` -- the post-veto environment x sensitivity ceiling
    for the embedding endpoint (P8S-1). Computed at ``build_loop`` AND recomputed
    at the start of every ``embed_pending`` run/batch (the ``ollama pull *:cloud``
    between ticks is the TOCTOU window); NEVER per-search.
  * :func:`query_vector_allowed` -- the frozen query rule: a query is embedded
    iff ``rank(retrieval_ceiling) <= rank(embed_ceiling)``, a comparison of two
    labels and an early return (no sanitized-query cleverness).
  * :func:`embed_pending` -- the chunk-embedding enforcer: re-read each chunk's
    CURRENT sensitivity at dispatch (P8S-14), drop any chunk above the ceiling,
    embed the remainder, hand the vectors back through the ``vectors-add``
    chokepoint via a 0600 in-root ``tmp.nosync/`` file deleted afterwards
    (P8S-10), validating the ``{"added": N}`` response shape (P8S-4).

The query-path seam is :func:`build_query_embedder`: ``build_loop`` injects an
optional PURE callable ``query_embedder: Callable[[str], dict | None]`` into the
Dispatcher (``None`` => lexical exactly as today). The callable closes over the
embed client, the post-veto ceiling and the ``query_vector_allowed`` decision,
so ``verbtools`` itself holds no client and no ceiling logic and stays
egress-free by design (P8S-3). Any transport failure (network, the 10 s timeout,
malformed response) degrades silently to lexical -- the same silent-but-correct
degradation as every policy refusal.

Stdlib only.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path
from typing import Callable, Optional

from . import policy_bridge as pb

__all__ = [
    "embedding_ceiling",
    "query_vector_allowed",
    "embed_pending",
    "build_query_embedder",
    "QUERY_EMBED_TIMEOUT",
]

#: Query-path embed timeout (seconds), frozen by the spec (P8S-3). A slow or
#: unresponsive embedder must never stall a search; it degrades to lexical.
QUERY_EMBED_TIMEOUT: float = 10.0


# --------------------------------------------------------------------------- #
# ceiling computation (post-veto)
# --------------------------------------------------------------------------- #
def embedding_ceiling(root: Path, embed_base_url: str, embed_model: str) -> str:
    """Post-veto environment x sensitivity ceiling for the embedding endpoint.

    Classifies the EMBEDDING base_url INDEPENDENTLY of the chat endpoint via
    ``environment_for``, then applies ``egress_veto`` (P8S-1): a loopback Ollama
    listener serving a ``*:cloud`` embedding model -- or one whose ``/api/tags``
    entry carries a non-empty ``remote_host`` -- is reclassified ``external``
    BEFORE ``max_sensitivity_for``. A local chat model paired with an external
    embedder therefore does NOT inherit ``local_agent``.

    Fail closed (I4): ANY error computing the ceiling yields ``"public"`` -- the
    strictest label, identical to the policy bridge's own failure posture. This
    means a ceiling-computation error disables embedding for every internal+
    surface, and no request leaves.

    Computed at ``build_loop`` and recomputed at the start of every
    ``embed_pending`` run/batch -- NEVER per-search (the veto's 3 s probe must
    not ride the search path).
    """
    try:
        environment = pb.environment_for(embed_base_url)
        if environment == "local_agent":
            veto = pb.egress_veto(embed_base_url, embed_model)
            if veto:
                environment = "external"
        return pb.max_sensitivity_for(root, environment)
    except Exception:
        # Fail closed: any error -> strictest label, no egress above public.
        return "public"


def query_vector_allowed(retrieval_ceiling: str, embed_ceiling: str,
                         order: list[str]) -> bool:
    """The frozen query rule (security pin, P8S-3): a ceiling COMPARISON.

    A retrieval query may itself contain internal content, so it is
    conservatively classified at the surface's retrieval ceiling. The query is
    embedded -- and vector search participates at all -- iff::

        rank(retrieval_ceiling) <= rank(embed_ceiling_post_veto)

    An external-classified OR vetoed embedder (ceiling ``public``) therefore
    disables vector search for every internal-and-above surface; retrieval falls
    back to lexical, silently and correctly. Fail-closed both ways: an unknown
    label ranks strictest (``sensitivity_rank``), so an unparseable retrieval
    ceiling never slips under an embed ceiling and an unparseable embed ceiling
    never admits a query.
    """
    return (pb.sensitivity_rank(retrieval_ceiling, order)
            <= pb.sensitivity_rank(embed_ceiling, order))


# --------------------------------------------------------------------------- #
# chunk-embedding enforcer (P8-T4 dispatch check + P8-T5 backfill body)
# --------------------------------------------------------------------------- #
def embed_pending(client, root: Path, *, ceiling: str, embed_model: str,
                  batch: int = 64, limit: Optional[int] = None,
                  order: Optional[list[str]] = None,
                  ledger: bool = True) -> dict:
    """Embed pending chunks at/below ``ceiling`` and hand vectors to the kernel.

    The pipeline, per batch:

      1. ``vectors-pending`` -> chunk rows carrying each chunk's CURRENT
         sensitivity (read LIVE from the ``chunks`` table by the kernel join, so
         the value is authoritative as of this single indexed query -- the
         zero-reclassification-window branch of P8S-14).
      2. ENFORCER: drop every chunk whose sensitivity ranks ABOVE ``ceiling``.
         This is the dispatch boundary (I5): an above-ceiling chunk is never in
         any embedding request. The drop is applied to the live-read label, so a
         chunk reclassified upward since it was minted is caught here.
      3. Embed the survivors via ``client.embed`` (one network call per batch).
      4. Hand the vectors back through the ``vectors-add`` chokepoint via a 0600
         in-root ``tmp.nosync/`` file (P8S-10), deleted after the call returns,
         validating the frozen ``{"added": N}`` response shape (P8S-4).
      5. Best-effort metadata-only ``embedding_event`` ledger row per batch
         (P8S-15: the environment/ceiling fields are shell ATTESTATION).

    Idempotent and resumable by construction: the pending set shrinks
    monotonically and ``vectors-add`` upserts on (source_id, chunk_index,
    embedding_model). Killing mid-backfill loses at most the in-flight batch.

    Returns a metadata-only summary dict (counts only; never text, never
    vectors).
    """
    order = order or pb.sensitivity_order(Path(root))
    ceiling_rank = pb.sensitivity_rank(ceiling, order)

    pending = _vectors_pending(root, embed_model=embed_model, limit=limit)

    embedded = 0
    skipped_over_ceiling = 0
    batches = 0
    n = len(pending)
    i = 0
    while i < n:
        window = pending[i:i + max(1, batch)]
        i += len(window)
        # -- ENFORCER: re-read CURRENT sensitivity at dispatch (P8S-14) -------
        allowed: list[dict] = []
        for ch in window:
            sens = ch.get("sensitivity")
            if pb.sensitivity_rank(_norm_label(sens), order) > ceiling_rank:
                skipped_over_ceiling += 1
                continue
            allowed.append(ch)
        if not allowed:
            continue
        texts = [str(ch.get("text") or "") for ch in allowed]
        # -- embed (the egress) ----------------------------------------------
        vectors = client.embed(texts, model=embed_model)
        if len(vectors) != len(allowed):
            raise ValueError(
                f"embed returned {len(vectors)} vectors for {len(allowed)} "
                "chunks (batch shape mismatch)"
            )
        rows = [
            {
                "source_id": ch.get("source_id"),
                "chunk_index": ch.get("chunk_index"),
                "embedding_model": embed_model,
                "vector": vec,
            }
            for ch, vec in zip(allowed, vectors)
        ]
        added = _vectors_add(root, rows)
        embedded += added
        batches += 1
        if ledger:
            _append_embedding_event(
                root,
                source_ids=sorted({str(ch.get("source_id") or "")
                                   for ch in allowed}),
                chunk_count=added,
                embedding_model=embed_model,
                ceiling=ceiling,
            )

    return {
        "embedded": embedded,
        "skipped_over_ceiling": skipped_over_ceiling,
        "batches": batches,
        "pending_seen": n,
        "embedding_model": embed_model,
        "ceiling": ceiling,
    }


def _norm_label(label) -> str:
    """Normalize a kernel-reported sensitivity label for ranking.

    The kernel stores labels lowercased; ``sensitivity_rank`` matches the shell's
    canonical (lowercase) order, so a missing/blank label maps to ``""`` which
    ranks strictest (fail-closed) -- an unlabeled chunk is never embedded below
    the strictest ceiling.
    """
    if label is None:
        return ""
    return str(label).strip().lower()


# --------------------------------------------------------------------------- #
# kernel chokepoint helpers (vectors-pending / vectors-add)
# --------------------------------------------------------------------------- #
def _vectors_pending(root: Path, *, embed_model: str,
                     limit: Optional[int]) -> list[dict]:
    """Fetch chunks lacking a vector for ``embed_model`` (with live sensitivity).

    Goes through the ``oracle search vectors-pending`` chokepoint. No ceiling is
    passed to the kernel: the convenience pre-filter is NOT the security
    boundary; the enforcer re-checks each row's live sensitivity at dispatch
    (P8S-14).
    """
    from .verbtools import run_verb

    argv = ["search", "vectors-pending", "--embedding-model", embed_model]
    if limit is not None:
        argv += ["--limit", str(int(limit))]
    rc, out, _err = run_verb(Path(root), argv, timeout=120.0)
    if rc != 0:
        raise RuntimeError(f"vectors-pending failed (rc={rc})")
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"vectors-pending returned non-JSON: {exc}") from None
    if not isinstance(data, list):
        raise RuntimeError("vectors-pending did not return a list")
    return [d for d in data if isinstance(d, dict)]


def _vectors_add(root: Path, rows: list[dict]) -> int:
    """Hand vectors to the kernel via a 0600 in-root tmp.nosync handoff (P8S-10).

    Vectors are content-equivalent to their chunks (embedding inversion is
    real), so the handoff file is created ``0o600`` via ``os.open`` UNDER THE
    ROOT at ``tmp.nosync/`` -- never a world-readable /tmp path -- and is
    DELETED after ``vectors-add`` returns (even on error). The frozen response
    shape ``{"added": N}`` is validated (P8S-4): the shell trusts the shape, not
    rc 0, so the ``_translate`` mis-route (a text query for the literal string
    "vectors-add" exiting 0) cannot silently no-op the pipeline.
    """
    from .verbtools import run_verb

    if not rows:
        return 0
    tmp_dir = Path(root) / "tmp.nosync"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # 0600 in-root handoff file via os.open (O_CREAT|O_WRONLY|O_TRUNC, 0o600).
    fname = f"vectors-{os.getpid()}-{_mono_token()}.json"
    fpath = tmp_dir / fname
    fd = os.open(str(fpath), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"vectors": rows}, fh)
        argv = ["search", "vectors-add", "--file", str(fpath)]
        rc, out, _err = run_verb(Path(root), argv, timeout=120.0)
        if rc != 0:
            raise RuntimeError(f"vectors-add failed (rc={rc})")
        # Validate the frozen {"added": N} shape (P8S-4) -- never trust rc 0.
        try:
            resp = json.loads(out or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"vectors-add returned non-JSON: {exc}") from None
        if not isinstance(resp, dict) or "added" not in resp:
            raise RuntimeError(
                "vectors-add did not return the expected {\"added\": N} shape "
                "-- the kernel may have mis-routed the subcommand to a text query"
            )
        return int(resp["added"])
    finally:
        # The handoff file must not outlive the call (P8S-10).
        try:
            fpath.unlink()
        except OSError:
            pass


def _mono_token() -> str:
    import time
    return f"{int(time.monotonic_ns())}"


# --------------------------------------------------------------------------- #
# embedding_event ledger (metadata only -- P8S-15 attestation)
# --------------------------------------------------------------------------- #
def _append_embedding_event(root: Path, *, source_ids: list[str],
                            chunk_count: int, embedding_model: str,
                            ceiling: str) -> None:
    """Best-effort metadata-only ``embedding_event`` ledger row per batch.

    NEVER text, NEVER vectors (the security invariant). The environment/ceiling
    field is shell ATTESTATION (P8S-15) -- the kernel records what the shell
    claims and cannot verify shell egress; the ENFORCED guarantees point at the
    shell enforcer tests, not at this row. Best-effort: a write failure never
    raises (a read-only root must still embed where it can).
    """
    row = {
        "kind": "embedding_event",
        "source_ids": source_ids,
        "chunk_count": int(chunk_count),
        "embedding_model": embedding_model,
        "applied_ceiling": ceiling,  # ATTESTATION (P8S-15)
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    path = Path(root) / "Meta.nosync" / "ledgers" / "embedding_event.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"oracle: embedding_event ledger write failed: "
              f"{type(exc).__name__}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# query-path seam (P8S-3): the pure injected callable
# --------------------------------------------------------------------------- #
def build_query_embedder(
    client,
    *,
    embed_model: str,
    embed_ceiling: str,
    retrieval_ceiling: str,
    order: list[str],
) -> Callable[[str], Optional[dict]]:
    """Build the PURE ``query_embedder`` callable injected into the Dispatcher.

    The returned callable maps the query terms (a single ``str``, the frozen
    interface ``Callable[[str], dict | None]``) to a stdin payload
    ``{"embedding_model": M, "vector": [...]}`` -- or ``None`` to run lexical
    exactly as today. It closes over the embed client, the post-veto embed
    ceiling, the surface's RETRIEVAL CEILING (the ``max_sensitivity`` the search
    runs with) and the ``query_vector_allowed`` decision, so ``verbtools`` holds
    no client and no ceiling logic and stays egress-free by design.

    Per the frozen query rule, the call:

      1. compares the retrieval ceiling against the post-veto embed ceiling once
         (closed over at build time); if the query is not allowed, returns
         ``None`` (lexical, silently) -- an external/vetoed embedder disables
         vector search for every internal+ surface;
      2. embeds the query terms under a 10 s timeout (the client's own default);
      3. on ANY transport failure (network, timeout, malformed response, or any
         unexpected error) returns ``None`` (lexical, silently) -- the same
         silent-but-correct degradation as every policy refusal.

    The payload's ``embedding_model`` is the config-sourced model string and the
    ``vector`` is computed floats; the model-supplied ``terms`` NEVER enter the
    payload keys, only the embedded vector, so the stdin channel is not
    model-influencable in its structure and the argv chokepoint discipline (I2)
    is preserved.
    """
    # The frozen query rule is a comparison of two STATIC labels (the surface's
    # retrieval ceiling and the post-veto embed ceiling), both known at
    # build_loop. Decide once: if the surface may not embed, the callable is a
    # constant None (vector search structurally disabled for this surface).
    _allowed = query_vector_allowed(retrieval_ceiling, embed_ceiling, order)

    def query_embedder(terms: str) -> Optional[dict]:
        if not _allowed:
            return None
        text = str(terms or "").strip()
        if not text:
            return None
        try:
            vectors = client.embed([text], model=embed_model)
        except Exception:
            # ANY transport failure (network, 10 s timeout, malformed response,
            # policy refusal raised by the client) degrades silently to lexical.
            return None
        if not vectors or not isinstance(vectors[0], list) or not vectors[0]:
            return None
        # Shell-composed payload ONLY: a config-sourced model string + computed
        # floats. The model's terms never enter the payload keys (P8S-3 / I2).
        return {"embedding_model": embed_model, "vector": vectors[0]}

    return query_embedder
