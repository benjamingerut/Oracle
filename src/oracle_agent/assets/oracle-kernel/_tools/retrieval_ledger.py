#!/usr/bin/env python3
"""retrieval_ledger.py -- the never-blocking, monthly-rotated retrieval ledger.

Every search appends ONE metadata-only row recording that a query happened and
which sources it surfaced -- the read-side telemetry the scorecard's
``retrieval`` section rolls into hit-rate and coverage numbers (P8-T7). This is
a thin consuming-module helper on top of ``ledger.append``; it deliberately does
NOT touch ``ledger.py`` itself (the spec prefers a thin helper here over editing
the durability primitive).

Three load-bearing properties, each pinned by the stress pass:

* **Monthly rotation (P8S-8).** ``ledger.append`` scans the WHOLE file under
  LOCK_EX per row, so an unbounded per-search ledger is a quadratic hot path
  riding inside search latency. We rotate by calendar month:
  ``Meta.nosync/ledgers/retrieval_event-YYYYMM.jsonl``. The hash chain restarts
  per file, which ``ledger.verify`` already tolerates as a legacy prefix (the
  first hashed row in a fresh month is validated against an empty prev_hash).

* **No drop_id minting (P8S-8).** We pass ``id_prefix=None`` and carry no
  ``drop_id``: ``ts`` + ``row_hash`` are the row identity. This removes one of
  the two full-file scans ``ledger.append`` would otherwise do per append
  (the id-collision scan), leaving only the hash-chain tail read.

* **Best-effort, never fails/blocks/delays a search (P8S-8).** ``log_search``
  swallows EVERY exception: a read-only root, a full disk, an import failure --
  none of them may break or measurably delay retrieval. The read path must not
  become write-dependent. Returns True on a successful append, False otherwise;
  callers ignore the result.

**query_hmac, not query_sha256 (P8S-9).** A bare ``sha256`` of a short
natural-language query is dictionary-reversible (an attacker hashes a wordlist
and matches). We store ``HMAC-SHA256(query)`` under a per-root random salt minted
0600 on FIRST USE under ``_data.nosync/retrieval_salt`` -- the salt is NEVER
exported, NEVER ledgered, and is excluded from sync (``.nosync``). The query
text itself never appears in any row.

Named accepted residual (per the spec): the ``top_source_ids`` field correlates
queries to sources -- a metadata-tier signal, accepted.

Stdlib only. The salt file is created via ``os.open(..., 0o600)`` (a constant,
non-user-influenced internal path under the trusted root), outside the no-bypass
guard's remit just like the index DB.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

try:  # pragma: no cover - exercised both ways across environments
    import ledger
except Exception:  # pragma: no cover
    from . import ledger  # type: ignore


LEDGERS_DIR = "Meta.nosync/ledgers"
#: The per-root HMAC salt lives beside the derived index, under the synced-out
#: ``_data.nosync`` partition. It is content-equivalent to a secret key: 0600,
#: never exported, never ledgered.
_SALT_REL = ("_data.nosync", "retrieval_salt")
_SALT_BYTES = 32
_TOP_SOURCE_CAP = 10  # bound the per-row source_ids list


def _ledgers_dir(root: Path) -> Path:
    return Path(root) / LEDGERS_DIR


def retrieval_ledger_path(root: Path, *, now: Optional[datetime] = None) -> Path:
    """The monthly-rotated ledger path for ``now`` (default: wall clock).

    ``retrieval_event-YYYYMM.jsonl`` -- a fresh file (and a fresh hash chain)
    each calendar month so no single ledger grows unbounded under the per-row
    LOCK_EX scan (P8S-8).
    """
    stamp = (now or datetime.now()).strftime("%Y%m")
    return _ledgers_dir(root) / f"retrieval_event-{stamp}.jsonl"


def _salt_path(root: Path) -> Path:
    return Path(root).joinpath(*_SALT_REL)


def _get_or_mint_salt(root: Path) -> bytes:
    """Return the per-root HMAC salt, minting it 0600 on first use (P8S-9).

    The salt is a random 32-byte secret. It is created atomically with
    ``O_CREAT | O_EXCL`` so two concurrent first-uses cannot both write; the
    loser simply re-reads the winner's salt. The file is mode 0600 -- it is a
    key, never world-readable. NEVER returned to callers / ledgered / exported.
    """
    path = _salt_path(root)
    try:
        return path.read_bytes()
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(_SALT_BYTES)
    try:
        # O_EXCL: exactly one writer wins the first-use race; 0600 from birth.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # safe_paths-internal: per-root secret salt, constant internal path
    except FileExistsError:
        # Another process minted it first; read theirs.
        return path.read_bytes()
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)
    return salt


def query_hmac(root: Path, query: str) -> str:
    """HMAC-SHA256 of ``query`` under the per-root salt (P8S-9).

    Deterministic for a given (root, query): the same query hashes to the same
    value within a root (so the scorecard could in principle bucket repeats),
    but the value is NOT ``sha256(query)`` -- without the secret salt it is not
    dictionary-reversible. Raises only if the salt cannot be minted/read; the
    caller (``log_search``) treats any raise as "skip the row".
    """
    salt = _get_or_mint_salt(root)
    return hmac.new(salt, str(query).encode("utf-8"), hashlib.sha256).hexdigest()


def log_search(
    root,
    *,
    query: str,
    k: int,
    engine: str,
    hybrid: bool,
    vector_coverage,
    result_count: int,
    top_source_ids,
    now: Optional[datetime] = None,
) -> bool:
    """Append one metadata-only retrieval_event row. BEST-EFFORT (P8S-8).

    Row shape: ``{ts, query_hmac, k, engine, hybrid, vector_coverage,
    result_count, top_source_ids, row_hash}`` -- no ``drop_id`` (ts + row_hash
    are identity), and NEVER the query text. ``vector_coverage`` is the active
    model's coverage float (or None when no model is active). ``top_source_ids``
    is capped to keep rows bounded.

    This NEVER raises: a read-only root, an import failure, a disk-full -- all
    are swallowed and reported as ``False``. The read path must keep working even
    when the write path cannot. Returns True iff the row was durably appended.
    """
    try:
        root = Path(root)
        qh = query_hmac(root, query)
        top = [str(s) for s in (top_source_ids or []) if str(s)][:_TOP_SOURCE_CAP]
        row = {
            "kind": "retrieval_event",
            "query_hmac": qh,
            "k": int(k),
            "engine": str(engine),
            "hybrid": bool(hybrid),
            "vector_coverage": vector_coverage,
            "result_count": int(result_count),
            "top_source_ids": top,
        }
        # NO_ID => mint no drop_id; ts + row_hash are identity. This skips the
        # id-collision full-file scan ledger.append would otherwise do (P8S-8),
        # keeping the per-search append off the quadratic path.
        ledger.append(retrieval_ledger_path(root, now=now), row, id_prefix=ledger.NO_ID)
        return True
    except Exception:
        # BEST-EFFORT: telemetry must never break or delay a search (P8S-8).
        return False
