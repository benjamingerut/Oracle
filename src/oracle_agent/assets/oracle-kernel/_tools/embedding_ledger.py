#!/usr/bin/env python3
"""embedding_ledger.py -- the metadata-only embedding-batch audit row (P8-T5).

An embedding call IS content egress: a chunk's text is sent to an embeddings
endpoint exactly as it would be to a chat endpoint. Every embedding batch is
therefore ledgered so the re-egress is auditable -- a reindex/_wipe/DB loss now
implies a full-corpus re-embed (P8S-13), and that mass re-send must be visible.

One ``embedding_event`` row is appended per ``(source_id)`` in a vectors-add
batch via ``ledger.append`` to ``Meta.nosync/ledgers/embedding_event.jsonl``.

**METADATA ONLY (the hard rule).** A row carries: ``source_id``, the chunk
``count`` embedded for that source, the ``embedding_model``, and the
caller-stamped ``environment`` + applied ``ceiling``. It carries NEVER the chunk
text and NEVER the vectors -- vectors are content-equivalent and must not transit
a ledger.

**The environment/ceiling fields are ATTESTATION (P8S-15, I6 honesty).** The
KERNEL records what the SHELL claims about the embedding endpoint's post-veto
environment and the ceiling it enforced; the kernel makes no network call and
cannot verify shell egress. The ENFORCED egress guarantees point at the shell
enforcer tests (``test_embedder_enforcer.py``, P8-T4); the kernel-testable
guarantee here is only "every batch is ledgered, metadata-only". The doctor /
SECURITY.md must scope any guarantee that cites these fields as advisory.

Stdlib only; the durable write flows entirely through ``ledger.append`` (the
single durability chokepoint).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

try:  # pragma: no cover - exercised both ways across environments
    import ledger
except Exception:  # pragma: no cover
    from . import ledger  # type: ignore


EMBEDDING_EVENT_LEDGER = "Meta.nosync/ledgers/embedding_event.jsonl"


def ledger_path(root: Path) -> Path:
    return Path(root) / EMBEDDING_EVENT_LEDGER


def append_embedding_event(
    root,
    *,
    source_id: str,
    count: int,
    embedding_model: str,
    environment: Optional[str] = None,
    ceiling: Optional[str] = None,
) -> Optional[str]:
    """Append ONE metadata-only embedding_event row for a source. Returns drop_id.

    ``environment``/``ceiling`` are the shell's attestation (P8S-15) -- recorded,
    not verified. Best-effort: a ledger failure returns None rather than breaking
    the vectors-add that triggered it (the vectors are already committed; the
    audit row is secondary). NEVER carries chunk text or vectors.
    """
    try:
        return ledger.append(
            ledger_path(root),
            {
                "kind": "embedding_event",
                "source_id": str(source_id),
                "count": int(count),
                "embedding_model": str(embedding_model),
                # ATTESTATION fields (shell-claimed, kernel cannot verify):
                "environment": environment,
                "ceiling": ceiling,
            },
            id_prefix="EMB",
        )
    except Exception:
        return None


def append_batch_events(
    root,
    rows: Iterable[dict],
    *,
    embedding_model: str,
    environment: Optional[str] = None,
    ceiling: Optional[str] = None,
) -> int:
    """Ledger one embedding_event per distinct source_id across ``rows``.

    ``rows`` is the vectors-add payload (``[{source_id, chunk_index,
    embedding_model, vector}]``). We group by source_id and emit one row per
    source carrying that source's chunk ``count``. Returns the number of
    embedding_event rows written. The ``embedding_model`` argument is the batch's
    active model (the per-row model is assumed to agree; we trust the caller's
    model string for the attestation, not the heterogeneous per-row values).
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    for r in rows:
        sid = str(r.get("source_id") or "")
        if sid not in counts:
            counts[sid] = 0
            order.append(sid)
        counts[sid] += 1
    written = 0
    for sid in order:
        if append_embedding_event(
            root,
            source_id=sid,
            count=counts[sid],
            embedding_model=embedding_model,
            environment=environment,
            ceiling=ceiling,
        ):
            written += 1
    return written
