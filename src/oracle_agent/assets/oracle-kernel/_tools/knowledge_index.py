#!/usr/bin/env python3
"""knowledge_index.py -- the retrieval index for the oracle kernel.

A small, dependency-free full-text retrieval index over indexed chunks of
ingested material. It prefers SQLite's FTS5 virtual-table engine when the
running interpreter's ``sqlite3`` was compiled with it, and otherwise falls
back to a PURE-PYTHON inverted index stored in ordinary SQLite tables. Either
way the public API is identical and results are comparable, so the kernel's
retrieval behaviour does not depend on a particular SQLite build.

The index database is a DERIVED artifact: it lives at the fixed internal path
``_data.nosync/index/knowledge.db`` under the oracle root. It is never a
user-supplied destination -- the only varying segment is the oracle root the
caller already trusts -- and it is created via ``sqlite3.connect`` (not
``open(...,'w')`` / ``shutil.*``), so it is outside the no-bypass guard's
remit. Nothing user-influenced is written through raw file I/O here.

REBUILDABLE WITH A COST (P8S-13): the lexical index is freely rebuildable from
the source corpus, but the OPTIONAL embedding vectors are NOT free to rebuild.
A ``reindex`` / ``_wipe`` / DB loss drops every ``chunk_vectors`` row, and
restoring coverage requires a FULL-CORPUS RE-EMBED through the shell's egress
endpoint -- a real cost (network, money) and a mass re-egress of content that
is ledgered (one ``embedding_event`` per batch) so the re-send is auditable.
"Derived, rebuildable" therefore holds for the lexical engine; for vectors it
means "rebuildable only by re-embedding, which the new sensitivity labels'
ceilings may rightly forbid". Doctor warns on the resulting coverage collapse.

Public API:
    KnowledgeIndex(root, *, force_fallback=False) -> instance
    .add(doc_id, text, *, source_id=None, sensitivity='internal',
         provenance='', chunk_index=0, start=0, end=0, title='') -> None
    .add_chunks(chunks) -> int          # bulk add list[dict]; upserts on (source_id, chunk_index)
    .delete_source(source_id) -> int    # remove all chunks for source_id; both engines
    .add_vectors(rows) -> int           # upsert chunk vectors (float32 BLOB); reject degenerate
    .pending_vectors(*, embedding_model, max_sensitivity=None, limit=None)
    .orphan_vectors() -> list[dict]     # doctor backstop: vectors with no chunk
    .prune_vectors(*, keep_model) -> int
    .search(query, *, k=10, max_sensitivity=None,
            query_vector=None, embedding_model=None) -> list[dict]
    .stats() -> dict
    .reindex(chunks) -> int             # wipe + rebuild from a fresh chunk set
    .engine -> 'fts5' | 'fallback'
    .close() -> None

The OPTIONAL embedding vector store (chunk_vectors) lives in the SAME SQLite DB
for BOTH engines (the lexical engine choice does not change where vectors are
stored -- the fallback engine stores vectors in exactly the same table as the
FTS5 engine, since vectors are an ordinary relational table independent of the
full-text engine). Vectors are keyed (source_id, chunk_index, embedding_model)
with NO sensitivity column -- sensitivity is ALWAYS join-read from ``chunks``
so a reclassified chunk's label is authoritative immediately (P8S-6). Vectors
are content-equivalent to their chunk and are removed in the SAME transaction
as the chunk-row mutation in delete_source / upsert / _wipe.

Module helpers:
    default_db_path(root) -> Path
    fts5_available() -> bool
    tokenize(text) -> list[str]

CLI:
    python3 knowledge_index.py --root R build  [--file chunks.json]
    python3 knowledge_index.py --root R add    --doc-id D --text T [...]
    python3 knowledge_index.py --root R query  --q "..." [--k N] [--max-sensitivity S]
                                               [--qvec-stdin]
    python3 knowledge_index.py --root R stats
    python3 knowledge_index.py --root R reindex --file chunks.json
    python3 knowledge_index.py --root R vectors-add     --file vectors.json
    python3 knowledge_index.py --root R vectors-pending --embedding-model M
                                               [--max-sensitivity S] [--limit N]
    python3 knowledge_index.py --root R vectors-prune   --keep-model M

The query vector travels on STDIN, never argv (size + ps-visibility): with
``--qvec-stdin`` the process reads ``{"embedding_model": M, "vector": [...]}``
from stdin, capped at 1 MiB / 8192 dims / finite floats, and the vector is
never echoed in an error. The ``vectors-*`` subcommands are NEVER exposed as a
model tool on any surface (P8S-10): ``vectors-pending`` emits chunk text and
``vectors-add`` injects vectors, so they are operator/shell-only.

Stdlib only (sqlite3, re, json, math, os, array, argparse, pathlib).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from array import array
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

__all__ = [
    "KnowledgeIndex",
    "default_db_path",
    "fts5_available",
    "index_chunks",
    "iter_chunks",
    "list_chunks",
    "tokenize",
    "SENSITIVITY_ORDER",
    "RRF_K",
    "VECTOR_CONTINGENCY_THRESHOLD",
]

# Reciprocal-rank-fusion constant, frozen by the spec: score(d) = Σ_r 1/(K + rank_r(d)).
# K=60 is the canonical RRF constant; it also CAPS any single ranking's
# contribution at 1/(K+1) per document, bounding token-stuffing influence.
RRF_K = 60

# Hybrid query-vector caps (P8S-3): the kernel never trusts the stdin payload.
_QVEC_MAX_BYTES = 1 << 20  # 1 MiB
_QVEC_MAX_DIMS = 8192

# Per-ranking candidate-pool depth for RRF fusion. The ceiling filter is applied
# IN each scan BEFORE this truncation (P8S-5), so it never alters which
# above-ceiling rows are excluded -- it only bounds how deep each ceiling-FILTERED
# list reaches before fusion, keeping the fused candidate set O(depth).
_HYBRID_CANDIDATE_DEPTH = 200

# Corpus-size contingency threshold (P8S-7). Brute-force cosine is measured on
# the FLOOR interpreter (Python 3.10, the pure-Python _dot fallback -- NOT the
# 3.12+ math.sumprod fast path), at the post-P7 design point of >= 100k chunks.
# A 1536-dim brute-force scan there is ~150M+ multiply-adds plus the per-query
# BLOB reads, which on the floor interpreter approaches the interactive budget.
# Past this many vectors for the ACTIVE model, doctor warns and the contingency
# ladder activates IN ORDER: (1) reduced dimensions first (provider `dimensions`
# param, e.g. 256-512 -- cuts storage and scan 3-6x for minor recall loss),
# (2) int8 quantization (x4 smaller, ~x2 faster). An ANN index is out of scope.
# This is a CORPUS-driven trigger, exposed for the shell's doctor to consume via
# stats()['vectors']; it is not a one-time laptop measurement.
VECTOR_CONTINGENCY_THRESHOLD = 100_000

# Least -> most sensitive. A query's ``max_sensitivity`` ceiling admits any row
# whose rank is <= the ceiling's rank; an unknown row sensitivity is treated as
# the strictest (so it is excluded unless the ceiling is also the strictest).
SENSITIVITY_ORDER = ["public", "internal", "confidential", "restricted", "secret"]
_SENS_RANK = {s: i for i, s in enumerate(SENSITIVITY_ORDER)}
_STRICTEST = len(SENSITIVITY_ORDER) - 1


def _sens_rank(label: Optional[str]) -> int:
    """Strictness rank of a sensitivity label; unknown/blank -> strictest."""
    if label is None:
        return _STRICTEST
    return _SENS_RANK.get(str(label).strip().lower(), _STRICTEST)


# --------------------------------------------------------------------------- #
# vector math (stdlib only; float32 BLOBs; unit-normalized so cosine == dot)
# --------------------------------------------------------------------------- #
def _dot(a, b) -> float:
    """Dot product of two equal-length float sequences.

    Uses ``math.sumprod`` (C-speed, Python 3.12+) when available, with a
    pure-Python fallback on 3.10/3.11. The Python FLOOR stays >=3.10 (P8S-7):
    installability is a product property and is not traded for the fast path.
    """
    sumprod = getattr(math, "sumprod", None)
    if sumprod is not None:
        return float(sumprod(a, b))
    total = 0.0
    for x, y in zip(a, b):
        total += x * y
    return float(total)


def _vec_norm(values) -> float:
    """L2 norm of a float sequence."""
    return math.sqrt(_dot(values, values))


def _normalize_vector(values) -> tuple[array, float]:
    """Return (unit-normalized float32 array, original L2 norm).

    Rejects zero-norm and non-finite vectors (P8S-11): a zero norm divides by
    zero at normalization, and a NaN/inf vector poisons every cosine it
    touches. The caller (add_vectors) raises ValueError on a returned norm of
    0.0 -- but we raise here so the rejection is single-sourced.
    """
    arr = array("f")
    for v in values:
        fv = float(v)
        if not math.isfinite(fv):
            raise ValueError("vector contains a non-finite component")
        arr.append(fv)
    norm = _vec_norm(arr)
    if not math.isfinite(norm) or norm <= 0.0:
        raise ValueError("vector has zero or non-finite norm")
    unit = array("f", (x / norm for x in arr))
    return unit, norm


def _blob_to_floats(blob: bytes) -> array:
    """Decode a little-endian float32 BLOB into an ``array('f')``."""
    arr = array("f")
    arr.frombytes(blob)
    if sys.byteorder != "little":  # pragma: no cover - little-endian dev/CI
        arr.byteswap()
    return arr


def _floats_to_blob(arr: array) -> bytes:
    """Encode an ``array('f')`` to a little-endian float32 BLOB."""
    if sys.byteorder != "little":  # pragma: no cover - little-endian dev/CI
        swapped = array("f", arr)
        swapped.byteswap()
        return swapped.tobytes()
    return arr.tobytes()


def _validate_query_vector(payload) -> tuple[str, array]:
    """Validate a stdin query-vector payload; never echo the vector on error.

    Returns (embedding_model, unit-normalized float32 array). Enforces the
    frozen caps (P8S-3): <= 8192 dims, finite floats only. The byte cap is
    enforced by the CLI read before JSON parse. The vector is NEVER included in
    any error message.
    """
    if not isinstance(payload, dict):
        raise ValueError("query-vector payload must be a JSON object")
    model = payload.get("embedding_model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("query-vector payload missing 'embedding_model'")
    raw = payload.get("vector")
    if not isinstance(raw, list) or not raw:
        raise ValueError("query-vector payload missing non-empty 'vector'")
    if len(raw) > _QVEC_MAX_DIMS:
        raise ValueError(
            f"query vector exceeds {_QVEC_MAX_DIMS} dims ({len(raw)} supplied)"
        )
    try:
        unit, _norm = _normalize_vector(raw)
    except ValueError:
        # Re-raise WITHOUT the vector contents.
        raise ValueError("query vector is degenerate (zero/non-finite)")
    return model.strip(), unit


# --------------------------------------------------------------------------- #
# tokenizer (porter-light: lowercase, split on non-word, fold a few suffixes)
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# A tiny stop list keeps the inverted index lean without hurting recall on the
# business-document vocabulary this oracle ingests.
_STOP = frozenset(
    """a an and are as at be by for from has have in is it its of on or that the
    to was were will with this these those then than""".split()
)


def _light_stem(tok: str) -> str:
    """A deliberately conservative suffix fold (NOT a full Porter stemmer).

    The single property that matters for retrieval is that a word's common
    inflections collapse to the SAME stem, so a singular query matches a plural
    document chunk and vice-versa. We fold, in order:

      * ``ies`` -> ``y``   (companies -> company)
      * ``sses`` -> ``ss`` (classes  -> class)
      * gerund/past ``ing``/``ed`` with de-doubling of a final repeated
        consonant (shipping -> ship, planned -> plan), so 'ship'/'shipping'
        co-locate;
      * plural ``s`` -> base (revenues -> revenue, customers -> customer).

    We deliberately do NOT strip a bare ``es`` (which over-stems revenues ->
    revenu); the plural ``s`` rule handles the common cases while keeping proper
    nouns and identifiers intact. Short tokens (<=3 chars) are left untouched.
    """
    if len(tok) <= 3:
        return tok

    if tok.endswith("ies") and len(tok) > 4:
        return tok[:-3] + "y"
    if tok.endswith("sses"):
        return tok[:-2]  # classes -> class

    for suf in ("ing", "ed"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            stem = tok[: len(tok) - len(suf)]
            # Undo a doubled final consonant (shipp -> ship, plann -> plan).
            if (
                len(stem) >= 2
                and stem[-1] == stem[-2]
                and stem[-1] not in "aeiou"
            ):
                stem = stem[:-1]
            return stem

    # Plural: strip a single trailing 's' (not 'ss'); revenues -> revenue.
    if tok.endswith("s") and not tok.endswith("ss") and len(tok) > 3:
        return tok[:-1]

    return tok


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, drop stopwords, light-stem.

    Used by the pure-python fallback for both indexing and querying so the two
    sides agree. The FTS5 path uses its own tokenizer for matching but reuses
    this for query-term extraction and for scoring overlap.
    """
    if not text:
        return []
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text.lower()):
        tok = m.group(0)
        if tok in _STOP:
            continue
        out.append(_light_stem(tok))
    return out


# --------------------------------------------------------------------------- #
# db location + FTS5 probe
# --------------------------------------------------------------------------- #
def default_db_path(root) -> Path:
    """Fixed internal location of the rebuildable index DB under ``root``."""
    return Path(root) / "_data.nosync" / "index" / "knowledge.db"


def fts5_available() -> bool:
    """True iff this interpreter's sqlite3 can create an FTS5 virtual table.

    Probed by actually attempting ``CREATE VIRTUAL TABLE ... USING fts5`` on an
    in-memory database (the only reliable test across SQLite builds).
    """
    try:
        con = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return False
    try:
        con.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
        return True
    except sqlite3.Error:
        return False
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# the index
# --------------------------------------------------------------------------- #
class KnowledgeIndex:
    """A retrieval index with an FTS5 fast-path and a pure-python fallback.

    The two engines store the same logical rows (id, doc_id, source_id,
    sensitivity, provenance, title, chunk_index, start, end, text). Search
    returns the same shape from both; scores are not directly comparable across
    engines but ranking within an engine is stable.
    """

    def __init__(self, root, *, db_path=None, force_fallback: bool = False) -> None:
        self.root = Path(root)
        self.db_path = Path(db_path) if db_path is not None else default_db_path(root)
        # Allow forcing the fallback for testing parity, or via env for ops.
        env_force = os.environ.get("ORACLE_INDEX_FORCE_FALLBACK", "").strip().lower()
        forced = force_fallback or env_force in ("1", "true", "yes", "on")
        self.engine = "fts5" if (not forced and fts5_available()) else "fallback"
        # Count of same-model/different-dim vectors skipped by the LAST vector
        # scan; surfaced through stats().dim_mismatches (P8S-11).
        self._last_dim_mismatches = 0
        # Constant, non-user-influenced internal path; created via sqlite3.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self.db_path))
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    # -- schema -------------------------------------------------------------- #
    def _ensure_schema(self) -> None:
        cur = self._con
        # A meta table records which engine wrote this DB, so a later open with a
        # different SQLite build does not silently mix engines.
        cur.execute("CREATE TABLE IF NOT EXISTS index_meta (k TEXT PRIMARY KEY, v TEXT)")
        stored = self._meta_get("engine")
        if stored and stored != self.engine:
            # The DB was built by a different engine. Honor what is on disk so
            # search works against existing rows; rebuild to switch engines.
            self.engine = stored
        else:
            self._meta_set("engine", self.engine)

        if self.engine == "fts5":
            cur.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
                "doc_id, source_id, sensitivity, provenance, title, "
                "chunk_index UNINDEXED, start_off UNINDEXED, end_off UNINDEXED, "
                "body, tokenize='unicode61')"
            )
            # FTS5 virtual tables do not support UNIQUE constraints natively.
            # We maintain a shadow table that maps (source_id, chunk_index) to
            # the FTS5 rowid, enforcing uniqueness so upsert can delete-then-
            # insert without leaving orphaned FTS5 rows.
            cur.execute(
                "CREATE TABLE IF NOT EXISTS chunks_key ("
                "source_id TEXT NOT NULL, chunk_index INTEGER NOT NULL, "
                "fts_rowid INTEGER NOT NULL, "
                "PRIMARY KEY (source_id, chunk_index))"
            )
        else:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS chunks ("
                "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
                "doc_id TEXT, source_id TEXT, sensitivity TEXT, provenance TEXT, "
                "title TEXT, chunk_index INTEGER, start_off INTEGER, end_off INTEGER, "
                "body TEXT, UNIQUE(source_id, chunk_index))"
            )
            # Inverted index: one row per (term, chunk) with term frequency.
            cur.execute(
                "CREATE TABLE IF NOT EXISTS postings ("
                "term TEXT, chunk_rowid INTEGER, tf INTEGER)"
            )
            cur.execute("CREATE INDEX IF NOT EXISTS ix_postings_term ON postings(term)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_postings_chunk ON postings(chunk_rowid)"
            )
        # The optional vector store lives in the SAME DB for BOTH engines: it is
        # an ordinary relational table, independent of the full-text engine, so
        # the fallback and FTS5 builds store vectors identically (the spec's
        # "same DB" pin). Keyed (source_id, chunk_index, embedding_model); NO
        # sensitivity column -- sensitivity is always join-read from chunks.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS chunk_vectors ("
            "source_id TEXT NOT NULL, chunk_index INTEGER NOT NULL, "
            "embedding_model TEXT NOT NULL, dim INTEGER NOT NULL, "
            "norm REAL NOT NULL, vector BLOB NOT NULL, "
            "PRIMARY KEY (source_id, chunk_index, embedding_model))"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_chunk_vectors_model "
            "ON chunk_vectors(embedding_model)"
        )
        cur.commit()

    def _meta_get(self, k: str) -> Optional[str]:
        row = self._con.execute("SELECT v FROM index_meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def _meta_set(self, k: str, v: str) -> None:
        self._con.execute(
            "INSERT INTO index_meta(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )
        self._con.commit()

    def active_embedding_model(self) -> Optional[str]:
        """The ACTIVE embedding model recorded in index_meta, or None.

        Changing this makes every chunk "pending" for the new model without
        deleting old vectors until ``prune_vectors`` is called.
        """
        return self._meta_get("embedding_model")

    def set_active_embedding_model(self, model: str) -> None:
        self._meta_set("embedding_model", str(model).strip())

    # -- add ---------------------------------------------------------------- #
    def add(
        self,
        doc_id: str,
        text: str,
        *,
        source_id: Optional[str] = None,
        sensitivity: str = "internal",
        provenance: str = "",
        chunk_index: int = 0,
        start: int = 0,
        end: int = 0,
        title: str = "",
    ) -> None:
        """Index one chunk of text. ``doc_id`` groups chunks of one document."""
        body = text or ""
        sid = source_id or ""
        sens = (sensitivity or "internal").strip().lower() or "internal"
        cidx = int(chunk_index)
        if self.engine == "fts5":
            # Upsert: if a row with the same (source_id, chunk_index) already
            # exists, delete the FTS5 row first (via the shadow key table), then
            # insert the new one. The chunk's vectors (content-equivalent, minted
            # under the OLD sensitivity label) are dropped IN THE SAME
            # TRANSACTION (P8S-6): a re-ingested/reclassified chunk must not keep
            # a vector minted under its prior label. The drop forces a re-embed
            # that the new label's ceiling may rightly forbid -> lexical-only.
            existing = self._con.execute(
                "SELECT fts_rowid FROM chunks_key WHERE source_id=? AND chunk_index=?",
                (sid, cidx),
            ).fetchone()
            if existing is not None:
                self._con.execute(
                    "DELETE FROM chunks WHERE rowid=?", (existing[0],)
                )
                self._con.execute(
                    "DELETE FROM chunk_vectors WHERE source_id=? AND chunk_index=?",
                    (sid, cidx),
                )
            cur = self._con.execute(
                "INSERT INTO chunks("
                "doc_id, source_id, sensitivity, provenance, title, "
                "chunk_index, start_off, end_off, body) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (doc_id, sid, sens, provenance, title, cidx,
                 int(start), int(end), body),
            )
            fts_rowid = cur.lastrowid
            self._con.execute(
                "INSERT OR REPLACE INTO chunks_key(source_id, chunk_index, fts_rowid) "
                "VALUES(?,?,?)",
                (sid, cidx, fts_rowid),
            )
        else:
            # Fallback: UNIQUE(source_id, chunk_index) constraint allows us to
            # DELETE the old postings before replacing the chunk row.
            old = self._con.execute(
                "SELECT rowid FROM chunks WHERE source_id=? AND chunk_index=?",
                (sid, cidx),
            ).fetchone()
            if old is not None:
                self._con.execute(
                    "DELETE FROM postings WHERE chunk_rowid=?", (old[0],)
                )
                # Same-transaction vector drop (P8S-6) -- see fts5 branch.
                self._con.execute(
                    "DELETE FROM chunk_vectors WHERE source_id=? AND chunk_index=?",
                    (sid, cidx),
                )
            cur = self._con.execute(
                "INSERT OR REPLACE INTO chunks("
                "doc_id, source_id, sensitivity, provenance, title, "
                "chunk_index, start_off, end_off, body) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (doc_id, sid, sens, provenance, title, cidx,
                 int(start), int(end), body),
            )
            rowid = cur.lastrowid
            tf = Counter(tokenize(body))
            if tf:
                self._con.executemany(
                    "INSERT INTO postings(term, chunk_rowid, tf) VALUES(?,?,?)",
                    [(term, rowid, count) for term, count in tf.items()],
                )
        self._con.commit()

    def add_chunks(self, chunks: Iterable[dict]) -> int:
        """Bulk-add chunk dicts. Each dict may carry: doc_id, text/body,
        source_id, sensitivity, provenance, chunk_index, start, end, title.
        Returns the number added.
        """
        n = 0
        for c in chunks:
            self.add(
                c.get("doc_id") or c.get("id") or f"doc-{n}",
                c.get("text", c.get("body", "")),
                source_id=c.get("source_id"),
                sensitivity=c.get("sensitivity", "internal"),
                provenance=c.get("provenance", ""),
                chunk_index=c.get("chunk_index", c.get("index", 0)),
                start=c.get("start", 0),
                end=c.get("end", 0),
                title=c.get("title", ""),
            )
            n += 1
        return n

    def delete_source(self, source_id: str) -> int:
        """Remove all indexed chunks for ``source_id`` from both engines.

        Returns the number of chunk rows deleted.  Safe to call when the
        source has no chunks (returns 0).

        The source's ``chunk_vectors`` rows are removed IN THE SAME SQLite
        TRANSACTION as the chunk-row deletion (P8S-6): vectors are
        content-equivalent and must never outlive their chunk. A single commit
        covers both mutations, so a crash can never leave a vector orphaned
        behind a deleted source. We always sweep the vector rows (even when the
        source has no chunk rows) so a prior crash that orphaned vectors is
        also healed here.
        """
        sid = str(source_id)
        if self.engine == "fts5":
            # Collect all FTS5 rowids for this source so we can delete them
            # individually (FTS5 does not support DELETE … WHERE on columns
            # that are not the rowid).
            rows = self._con.execute(
                "SELECT fts_rowid FROM chunks_key WHERE source_id=?", (sid,)
            ).fetchall()
            count = len(rows)
            if count:
                for row in rows:
                    self._con.execute(
                        "DELETE FROM chunks WHERE rowid=?", (row[0],)
                    )
                self._con.execute(
                    "DELETE FROM chunks_key WHERE source_id=?", (sid,)
                )
        else:
            # Collect rowids first so we can clean the postings table.
            rows = self._con.execute(
                "SELECT rowid FROM chunks WHERE source_id=?", (sid,)
            ).fetchall()
            count = len(rows)
            if count:
                rowids = [r[0] for r in rows]
                placeholders = ",".join("?" * len(rowids))
                self._con.execute(
                    f"DELETE FROM postings WHERE chunk_rowid IN ({placeholders})",
                    rowids,
                )
                self._con.execute("DELETE FROM chunks WHERE source_id=?", (sid,))
        # Drop vectors for this source in the SAME (uncommitted) transaction.
        vec_cur = self._con.execute(
            "DELETE FROM chunk_vectors WHERE source_id=?", (sid,)
        )
        vec_deleted = vec_cur.rowcount or 0
        if count or vec_deleted:
            self._con.commit()
        return count

    # -- vectors ------------------------------------------------------------ #
    def add_vectors(self, rows: Iterable[dict]) -> int:
        """Upsert chunk vectors. Returns the number of rows written.

        Each row: ``{source_id, chunk_index, embedding_model, vector:
        list[float]}``. Vectors are stored unit-normalized as little-endian
        float32 BLOBs (``array('f')``) with the original L2 ``norm`` recorded,
        so cosine similarity is a plain dot product at search time. Zero-norm
        and non-finite vectors are REJECTED (P8S-11) -- they divide by zero at
        normalization or poison every cosine they touch. The key
        (source_id, chunk_index, embedding_model) upserts, so re-embedding a
        chunk under the same model replaces its vector. The vector is NEVER
        echoed in a rejection error.
        """
        n = 0
        to_write: list[tuple] = []
        for r in rows:
            sid = str(r.get("source_id") or "")
            cidx = int(r.get("chunk_index", 0))
            model = str(r.get("embedding_model") or "").strip()
            if not model:
                raise ValueError("vector row missing 'embedding_model'")
            raw = r.get("vector")
            if not isinstance(raw, (list, tuple)) or not raw:
                raise ValueError(
                    f"vector row for ({sid!r},{cidx}) missing non-empty 'vector'"
                )
            try:
                unit, norm = _normalize_vector(raw)
            except ValueError:
                # Reject WITHOUT echoing the vector contents.
                raise ValueError(
                    f"degenerate vector for ({sid!r},{cidx},{model!r}) rejected "
                    "(zero or non-finite norm)"
                )
            to_write.append((sid, cidx, model, len(unit), float(norm),
                             _floats_to_blob(unit)))
        for tpl in to_write:
            self._con.execute(
                "INSERT INTO chunk_vectors("
                "source_id, chunk_index, embedding_model, dim, norm, vector) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(source_id, chunk_index, embedding_model) DO UPDATE SET "
                "dim=excluded.dim, norm=excluded.norm, vector=excluded.vector",
                tpl,
            )
            n += 1
        if n:
            self._con.commit()
        return n

    def pending_vectors(
        self,
        *,
        embedding_model: str,
        max_sensitivity: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Chunks lacking a vector for ``embedding_model``.

        Returns chunk dicts (including ``text``) for chunks with no
        ``chunk_vectors`` row under the given model, optionally ceiling-filtered.
        The ceiling here is a CONVENIENCE pre-filter -- the shell enforcer
        re-reads each chunk's CURRENT sensitivity at dispatch (P8S-14), so this
        is not the security boundary.
        """
        model = str(embedding_model or "").strip()
        sql = (
            "SELECT c.doc_id, c.source_id, c.sensitivity, c.provenance, c.title, "
            "c.chunk_index, c.start_off, c.end_off, c.body "
            "FROM chunks c "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM chunk_vectors v "
            "  WHERE v.source_id=c.source_id AND v.chunk_index=c.chunk_index "
            "    AND v.embedding_model=?) "
            "ORDER BY c.source_id, c.chunk_index"
        )
        rows = self._con.execute(sql, (model,)).fetchall()
        out: list[dict] = []
        ceiling = _sens_rank(max_sensitivity) if max_sensitivity is not None else None
        for row in rows:
            if ceiling is not None and _sens_rank(row["sensitivity"]) > ceiling:
                continue
            out.append(self._row_to_chunk(row))
            if limit is not None and len(out) >= max(0, int(limit)):
                break
        return out

    def orphan_vectors(self) -> list[dict]:
        """Doctor backstop (P8S-6): vector rows with no matching chunk row.

        A single-transaction lifecycle means this should ALWAYS be empty; a
        non-empty result is the crash-tolerance signal that a vector outlived
        its chunk. Cheap: a NOT EXISTS anti-join keyed on the chunk PK.
        """
        rows = self._con.execute(
            "SELECT v.source_id, v.chunk_index, v.embedding_model "
            "FROM chunk_vectors v "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM chunks c "
            "  WHERE c.source_id=v.source_id AND c.chunk_index=v.chunk_index) "
            "ORDER BY v.source_id, v.chunk_index, v.embedding_model"
        ).fetchall()
        return [
            {
                "source_id": r["source_id"],
                "chunk_index": r["chunk_index"],
                "embedding_model": r["embedding_model"],
            }
            for r in rows
        ]

    def prune_vectors(self, *, keep_model: str) -> int:
        """Drop every vector whose embedding_model != ``keep_model``.

        Used after a model migration to reclaim superseded-model vectors once
        the new model's backfill has covered the corpus. Returns the count
        dropped.
        """
        keep = str(keep_model or "").strip()
        cur = self._con.execute(
            "DELETE FROM chunk_vectors WHERE embedding_model != ?", (keep,)
        )
        dropped = cur.rowcount or 0
        if dropped:
            self._con.commit()
        return dropped

    # -- search ------------------------------------------------------------- #
    def search(
        self,
        query: str,
        *,
        k: int = 10,
        max_sensitivity: Optional[str] = None,
        query_vector=None,
        embedding_model: Optional[str] = None,
    ) -> list[dict]:
        """Return up to ``k`` ranked hits for ``query``.

        ``max_sensitivity`` is a ceiling: a row whose sensitivity is STRICTER
        than the ceiling is excluded (the retrieval layer never surfaces
        over-ceiling material to a query operating at a lower clearance). An
        unknown row sensitivity is treated as the strictest, so it is excluded
        unless the ceiling is also the strictest. Each hit is a dict with:
        doc_id, source_id, sensitivity, provenance, title, chunk_index, start,
        end, text, score.

        HYBRID PATH (P8-T2): when ``query_vector`` is supplied (a unit-normalized
        float sequence) together with its ``embedding_model``, the lexical
        ranking is FUSED with a brute-force cosine ranking over same-(model, dim)
        vectors via reciprocal-rank fusion (RRF, k=60). The sensitivity ceiling
        is applied IN each scan -- BEFORE any ranking or candidate truncation --
        and RRF ranks are the DENSE positions within the ceiling-FILTERED lists,
        so an above-ceiling chunk can never perturb the visible ranks or scores
        (P8S-5; existence leak closed). When ``query_vector`` is None the output
        is BYTE-IDENTICAL to the lexical-only path (the early-return guard
        above stands and nothing below changes).
        """
        terms = tokenize(query)
        if not terms:
            return []
        ceiling = _sens_rank(max_sensitivity) if max_sensitivity is not None else None

        # -- lexical scan (ceiling applied in-scan, before truncation) ------- #
        if self.engine == "fts5":
            lexical = self._search_fts5(query, terms)
        else:
            lexical = self._search_fallback(terms)
        if ceiling is not None:
            lexical = [h for h in lexical if _sens_rank(h["sensitivity"]) <= ceiling]

        if query_vector is None:
            # Lexical-only path -- byte-identical to today.
            hits = lexical
            self._apply_rerank(hits)
            hits.sort(key=lambda h: (-h["score"], h["doc_id"], h["chunk_index"]))
            return hits[: max(0, int(k))]

        # -- hybrid path: fuse lexical + dense via RRF ----------------------- #
        dense = self._search_vectors(
            query_vector,
            embedding_model=embedding_model,
            ceiling=ceiling,
        )
        # Candidate truncation happens AFTER the in-scan ceiling filter (P8S-5):
        # each ceiling-filtered list is capped to a fixed depth before fusion so
        # the fused candidate set stays bounded. The cap NEVER changes which
        # above-ceiling rows are excluded (that already happened in-scan); it
        # only limits how deep each visible list reaches.
        lexical_pool = lexical[:_HYBRID_CANDIDATE_DEPTH]
        dense_pool = dense[:_HYBRID_CANDIDATE_DEPTH]
        hits = self._fuse_rrf(lexical_pool, dense_pool)
        self._apply_rerank(hits)
        hits.sort(key=lambda h: (-h["score"], h["doc_id"], h["chunk_index"]))
        return hits[: max(0, int(k))]

    # -- dense (vector) scan + RRF fusion ----------------------------------- #
    def _search_vectors(
        self,
        query_vector,
        *,
        embedding_model: Optional[str],
        ceiling: Optional[int],
    ) -> list[dict]:
        """Brute-force cosine ranking over same-(model, dim) chunk vectors.

        Sensitivity is JOIN-READ from ``chunks`` (never a copied label) so a
        reclassified chunk's vector is filtered by its CURRENT label. The
        ceiling is applied IN this scan, before any truncation, so above-ceiling
        chunks are invisible AND rank-inert (P8S-5). Returns hits sorted by
        descending cosine, carrying the same row shape as lexical hits (with the
        offset-exact start/end the chunker minted).
        """
        if not embedding_model:
            return []
        qmodel = str(embedding_model).strip()
        qarr = query_vector if isinstance(query_vector, array) else array(
            "f", (float(x) for x in query_vector)
        )
        qdim = len(qarr)
        if qdim == 0:
            return []
        # Only vectors with the SAME model name AND the SAME dim are eligible;
        # a same-name-different-dim vector is skipped + counted (P8S-11). Join to
        # chunks for the authoritative sensitivity label and the row payload.
        rows = self._con.execute(
            "SELECT c.doc_id, c.source_id, c.sensitivity, c.provenance, c.title, "
            "c.chunk_index, c.start_off, c.end_off, c.body, "
            "v.dim AS vdim, v.vector AS vblob "
            "FROM chunk_vectors v "
            "JOIN chunks c ON c.source_id=v.source_id "
            "  AND c.chunk_index=v.chunk_index "
            "WHERE v.embedding_model=?",
            (qmodel,),
        ).fetchall()
        scored: list[dict] = []
        mismatches = 0
        for row in rows:
            if int(row["vdim"]) != qdim:
                mismatches += 1
                continue
            # Ceiling filter IN the scan -- before any ranking/truncation.
            if ceiling is not None and _sens_rank(row["sensitivity"]) > ceiling:
                continue
            cand = _blob_to_floats(row["vblob"])
            cos = _dot(qarr, cand)  # both unit-normalized -> dot == cosine
            hit = self._row_to_hit(row, cos)
            scored.append(hit)
        self._last_dim_mismatches = mismatches
        scored.sort(
            key=lambda h: (-h["score"], h["doc_id"], h["chunk_index"])
        )
        return scored

    def _fuse_rrf(self, lexical: list[dict], dense: list[dict]) -> list[dict]:
        """Reciprocal-rank fusion of two ranked, ceiling-FILTERED hit lists.

        ``score(d) = Σ_r 1/(RRF_K + rank_r(d))`` where ``rank_r`` is the DENSE
        (1-based) position of d within ranking r's ceiling-filtered list. A
        document missing from a ranking simply contributes nothing from that
        ranking. Identity is (source_id, chunk_index). The fused ``score``
        replaces the per-engine score (which is not cross-comparable); the
        original lexical/cosine signals are preserved as ``lexical_score`` and
        ``cosine`` for transparency.
        """
        def _key(h: dict) -> tuple:
            return (str(h.get("source_id", "")), int(h.get("chunk_index", 0)))

        fused: dict[tuple, dict] = {}

        def _accumulate(ranked: list[dict], signal_field: str) -> None:
            for rank, h in enumerate(ranked, start=1):
                key = _key(h)
                entry = fused.get(key)
                if entry is None:
                    entry = dict(h)
                    entry["score"] = 0.0
                    fused[key] = entry
                entry["score"] = float(entry["score"]) + 1.0 / (RRF_K + rank)
                entry[signal_field] = float(h["score"])

        _accumulate(lexical, "lexical_score")
        _accumulate(dense, "cosine")
        return list(fused.values())

    # -- authority/recency rerank (v2) -------------------------------------- #
    def _apply_rerank(self, hits: list[dict]) -> None:
        """Boost text-match scores by source authority and recency.

        A best text match from a stale, unwired note should not outrank a
        nearly-as-good match from the confirmed authority of record. Boosts are
        multiplicative on the engine score and recorded per hit as ``rerank``
        so callers can see why ordering changed. Graceful: any failure leaves
        the engine ranking untouched.
        """
        if not hits:
            return
        try:
            boosts = self._source_boosts()
        except Exception:
            return
        if not boosts:
            return
        for h in hits:
            factor = boosts.get(str(h.get("source_id", "")), 1.0)
            if factor != 1.0:
                h["score"] = float(h["score"]) * factor
                h["rerank"] = round(factor, 3)

    def _source_boosts(self) -> dict:
        """Map source_id -> multiplier from truth-map authority + as_of recency."""
        try:
            import answer_protocol as _ap
            import truth_map as _tm
        except Exception:  # pragma: no cover - package fallback
            from . import answer_protocol as _ap  # type: ignore
            from . import truth_map as _tm  # type: ignore
        from datetime import datetime, timezone

        # Source frontmatter comes from the self-healing catalog when it is
        # available (O(changed notes), not O(notes) re-parses per query); any
        # catalog failure degrades to the direct folder walk. Either way the
        # boosts stay a SIGNAL on the engine ranking, never a gate.
        snap = None
        try:
            import source_catalog as _sc  # type: ignore
        except Exception:  # pragma: no cover - package fallback
            try:
                from . import source_catalog as _sc  # type: ignore
            except Exception:
                _sc = None
        if _sc is not None:
            try:
                snap = _sc.snapshot(self.root)
            except Exception:
                snap = None

        # The boost map only changes when the sources change (new snapshot),
        # the truth map changes (mtime), or the recency buckets roll (date) --
        # cache it on the snapshot so repeated searches pay dict lookups only.
        now = datetime.now(timezone.utc)
        cache_key = None
        if snap is not None:
            try:
                tm_mtime = (self.root / "TRUTH-MAP.md").stat().st_mtime_ns
            except OSError:
                tm_mtime = 0
            cache_key = ("source_boosts", tm_mtime, now.date().isoformat())
            cached = snap.derived.get(cache_key)
            if cached is not None:
                return cached

        confirmed: set[str] = set()
        drafted: set[str] = set()
        for row in _tm.load_rows(self.root):
            primary = str(row.get("primary source", "")).strip().lower()
            if not _tm.primary_source_is_authoritative(primary):
                continue
            if str(row.get("status", "")).strip().lower() == "confirmed":
                confirmed.add(primary)
            else:
                drafted.add(primary)

        boosts: dict[str, float] = {}
        if snap is not None:
            frontmatters = [e["fm"] for e in snap.entries]
        else:
            folder = self.root / "Memory.nosync" / "Sources"
            if not folder.is_dir():
                return boosts
            frontmatters = [
                _ap.read_frontmatter(p)
                for p in sorted(folder.glob("*.md"))
                if not p.name.startswith("_")
            ]
        for fm in frontmatters:
            if not fm:
                continue
            # The index keys chunks by the captured content hash's first 12
            # hex chars; the note carries the full hash.
            full = str(fm.get("captured_sha256") or fm.get("sha256") or "").strip()
            sid = (
                full[:12]
                if full
                else str(fm.get("sha256_12") or fm.get("source_id") or fm.get("id") or "").strip()
            )
            if not sid:
                continue
            factor = 1.0
            label = str(fm.get("authority_id") or fm.get("source_system") or "").strip().lower()
            if label and label in confirmed:
                factor *= 1.25
            elif label and label in drafted:
                factor *= 1.10
            as_of = _ap._parse_as_of(str(fm.get("as_of", fm.get("created", ""))))
            if as_of is not None:
                age_days = (now - as_of).total_seconds() / 86400.0
                if age_days <= 30:
                    factor *= 1.15
                elif age_days <= 365:
                    factor *= 1.05
            if factor != 1.0:
                boosts[sid] = factor
        if snap is not None and cache_key is not None:
            snap.derived[cache_key] = boosts
        return boosts

    def _row_to_hit(self, row, score: float) -> dict:
        return {
            "doc_id": row["doc_id"],
            "source_id": row["source_id"],
            "sensitivity": row["sensitivity"],
            "provenance": row["provenance"],
            "title": row["title"],
            "chunk_index": row["chunk_index"],
            "start": row["start_off"],
            "end": row["end_off"],
            "text": row["body"],
            "score": float(score),
        }

    def _row_to_chunk(self, row) -> dict:
        """Return the stable public chunk shape for exports/sidecars."""
        return {
            "doc_id": row["doc_id"],
            "source_id": row["source_id"],
            "sensitivity": row["sensitivity"],
            "provenance": row["provenance"],
            "title": row["title"],
            "chunk_index": row["chunk_index"],
            "start": row["start_off"],
            "end": row["end_off"],
            "text": row["body"],
        }

    def list_chunks(
        self,
        *,
        source_id: Optional[str] = None,
        max_sensitivity: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Return indexed chunks in stable order.

        This is the public read API for derived-memory exports and tests.
        Callers should not inspect the SQLite schema directly. Unknown
        sensitivity labels are treated as strictest by the same ceiling logic
        used by ``search``.
        """
        sql = (
            "SELECT rowid, doc_id, source_id, sensitivity, provenance, title, "
            "chunk_index, start_off, end_off, body FROM chunks"
        )
        params: list = []
        where: list[str] = []
        if source_id:
            where.append("source_id=?")
            params.append(source_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY doc_id, chunk_index, rowid"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = self._con.execute(sql, params).fetchall()
        chunks = [self._row_to_chunk(row) for row in rows]
        if max_sensitivity is not None:
            ceiling = _sens_rank(max_sensitivity)
            chunks = [c for c in chunks if _sens_rank(c["sensitivity"]) <= ceiling]
        return chunks

    def _search_fts5(self, raw_query: str, terms: list[str]) -> list[dict]:
        # Build a forgiving MATCH expression from the extracted terms (OR-of-
        # prefix-terms) so the FTS5 path has recall comparable to the fallback's
        # token-overlap scoring. We quote each term to neutralise FTS5 operators.
        match_terms = []
        for t in terms:
            safe = t.replace('"', "")
            if safe:
                match_terms.append(f'"{safe}"*')
        if not match_terms:
            return []
        match_expr = " OR ".join(match_terms)
        try:
            rows = self._con.execute(
                "SELECT doc_id, source_id, sensitivity, provenance, title, "
                "chunk_index, start_off, end_off, body, "
                "bm25(chunks) AS rank FROM chunks WHERE chunks MATCH ? "
                "ORDER BY rank",
                (match_expr,),
            ).fetchall()
        except sqlite3.Error:
            # If bm25 or MATCH choke on the expression, degrade to overlap.
            return self._search_overlap(terms)
        hits: list[dict] = []
        for row in rows:
            # bm25 returns LOWER == better; convert to a positive score where
            # HIGHER == better so the common sort key applies across engines.
            score = -float(row["rank"]) if row["rank"] is not None else 0.0
            hits.append(self._row_to_hit(row, score))
        return hits

    def _search_fallback(self, terms: list[str]) -> list[dict]:
        """TF-IDF-ish scoring over the pure-python inverted index."""
        total_docs_row = self._con.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
        n_docs = int(total_docs_row["n"]) if total_docs_row else 0
        if n_docs == 0:
            return []
        # Accumulate a score per chunk_rowid across query terms.
        scores: dict[int, float] = {}
        for term in set(terms):
            rows = self._con.execute(
                "SELECT chunk_rowid, tf FROM postings WHERE term=?", (term,)
            ).fetchall()
            df = len(rows)
            if df == 0:
                continue
            idf = math.log(1.0 + (n_docs / df))
            for r in rows:
                rowid = int(r["chunk_rowid"])
                tf = int(r["tf"])
                scores[rowid] = scores.get(rowid, 0.0) + (1.0 + math.log(tf)) * idf
        if not scores:
            return []
        # Fetch the chunk rows for the matched rowids.
        ids = list(scores.keys())
        placeholders = ",".join("?" * len(ids))
        rows = self._con.execute(
            "SELECT rowid, doc_id, source_id, sensitivity, provenance, title, "
            "chunk_index, start_off, end_off, body FROM chunks "
            f"WHERE rowid IN ({placeholders})",
            ids,
        ).fetchall()
        hits = [self._row_to_hit(row, scores[int(row["rowid"])]) for row in rows]
        return hits

    def _search_overlap(self, terms: list[str]) -> list[dict]:
        """Engine-agnostic last-resort: score by raw token overlap.

        Used only if the FTS5 MATCH path errors. Reads bodies and counts the
        tokenized overlap with the query terms.
        """
        rows = self._con.execute(
            "SELECT doc_id, source_id, sensitivity, provenance, title, "
            "chunk_index, start_off, end_off, body FROM chunks"
        ).fetchall()
        want = set(terms)
        hits: list[dict] = []
        for row in rows:
            body_terms = Counter(tokenize(row["body"]))
            overlap = sum(body_terms[t] for t in want if t in body_terms)
            if overlap > 0:
                hits.append(self._row_to_hit(row, float(overlap)))
        return hits

    # -- maintenance -------------------------------------------------------- #
    def stats(self) -> dict:
        n = int(self._con.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])
        by_sens_rows = self._con.execute(
            "SELECT sensitivity, COUNT(*) AS c FROM chunks GROUP BY sensitivity"
        ).fetchall()
        by_sens = {r["sensitivity"]: int(r["c"]) for r in by_sens_rows}
        docs = int(
            self._con.execute(
                "SELECT COUNT(DISTINCT doc_id) AS d FROM chunks"
            ).fetchone()["d"]
        )
        # -- vector coverage (P8-T1) ---------------------------------------- #
        vec_total = int(
            self._con.execute(
                "SELECT COUNT(*) AS n FROM chunk_vectors"
            ).fetchone()["n"]
        )
        by_model_rows = self._con.execute(
            "SELECT embedding_model, COUNT(*) AS c FROM chunk_vectors "
            "GROUP BY embedding_model"
        ).fetchall()
        by_model = {r["embedding_model"]: int(r["c"]) for r in by_model_rows}
        # vector_coverage: per-model share of CHUNKS that carry a vector for
        # that model (a chunk may be covered by one model and pending another).
        coverage: dict[str, float] = {}
        for model, _count in by_model.items():
            covered = int(
                self._con.execute(
                    "SELECT COUNT(*) AS c FROM ("
                    "  SELECT DISTINCT v.source_id, v.chunk_index "
                    "  FROM chunk_vectors v "
                    "  JOIN chunks c ON c.source_id=v.source_id "
                    "    AND c.chunk_index=v.chunk_index "
                    "  WHERE v.embedding_model=?)",
                    (model,),
                ).fetchone()["c"]
            )
            coverage[model] = (covered / n) if n else 0.0
        return {
            "engine": self.engine,
            "db_path": str(self.db_path),
            "chunks": n,
            "documents": docs,
            "by_sensitivity": by_sens,
            "vectors": vec_total,
            "vector_coverage": coverage,
            "by_embedding_model": by_model,
            "dim_mismatches": int(self._last_dim_mismatches),
        }

    def _wipe(self) -> None:
        self._con.execute("DELETE FROM chunks")
        if self.engine == "fts5":
            self._con.execute("DELETE FROM chunks_key")
        else:
            self._con.execute("DELETE FROM postings")
        # Vectors go with the chunks, in the SAME transaction (P8S-6). A wipe
        # therefore implies a full-corpus re-embed to restore coverage (P8S-13).
        self._con.execute("DELETE FROM chunk_vectors")
        self._con.commit()

    def reindex(self, chunks: Iterable[dict]) -> int:
        """Wipe the index and rebuild it from a fresh chunk set."""
        self._wipe()
        return self.add_chunks(chunks)

    def close(self) -> None:
        try:
            self._con.commit()
        except sqlite3.Error:
            pass
        self._con.close()

    def __enter__(self) -> "KnowledgeIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load_chunks_file(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "chunks" in data:
        data = data["chunks"]
    if not isinstance(data, list):
        raise ValueError("chunks file must be a JSON list (or {chunks: [...]})")
    return data


def _load_vectors_file(path: Path) -> list[dict]:
    """Load the vectors handoff file: a JSON list (or {vectors: [...]}).

    Each entry: ``{source_id, chunk_index, embedding_model, vector: [...]}``.
    The file is the 0600 in-root handoff the shell writes and deletes (P8S-10);
    this function only parses it.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "vectors" in data:
        data = data["vectors"]
    if not isinstance(data, list):
        raise ValueError("vectors file must be a JSON list (or {vectors: [...]})")
    return data


def _read_query_vector_stdin(stream=None) -> tuple[str, list]:
    """Read + validate the query-vector stdin payload (P8S-3).

    Caps the read at 1 MiB BEFORE JSON parse, validates the shape and dims, and
    NEVER echoes the vector contents in an error. Returns (embedding_model,
    unit-normalized float list) ready to hand to ``search``.
    """
    src = stream if stream is not None else sys.stdin
    buf = src.buffer if hasattr(src, "buffer") else src
    raw = buf.read(_QVEC_MAX_BYTES + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > _QVEC_MAX_BYTES:
        raise ValueError(f"query-vector stdin exceeds {_QVEC_MAX_BYTES} bytes")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("query-vector stdin is not valid JSON")
    model, unit = _validate_query_vector(payload)
    return model, list(unit)


def _ledger_embedding_batch(root, rows, *, environment, ceiling) -> None:
    """Append metadata-only embedding_event rows for a vectors-add batch (P8S-15).

    Best-effort: a ledger failure must not fail the vectors-add (the vectors are
    already committed). The model is read from the batch rows (they share one
    active model on the shell's backfill path).
    """
    try:
        import embedding_ledger as _el  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import embedding_ledger as _el  # type: ignore
        except Exception:
            return
    try:
        model = ""
        for r in rows:
            m = str(r.get("embedding_model") or "").strip()
            if m:
                model = m
                break
        _el.append_batch_events(
            root, rows,
            embedding_model=model,
            environment=environment,
            ceiling=ceiling,
        )
    except Exception:  # pragma: no cover - best-effort audit
        return


def _log_retrieval_event(root, *, query, k, idx, hybrid, hits) -> None:
    """Append one best-effort retrieval_event row for this search (P8-T7).

    Reads the active-model vector_coverage from ``idx.stats()`` and the surfaced
    ``top_source_ids`` from the hits, then hands them to the monthly-rotated
    retrieval ledger. The helper is itself wrapped so a missing retrieval_ledger
    module (or any other failure) can NEVER break or delay the search (P8S-8):
    the write path is strictly subordinate to the read path.
    """
    try:
        import retrieval_ledger as _rl  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import retrieval_ledger as _rl  # type: ignore
        except Exception:
            return
    try:
        stats = idx.stats()
        active = idx.active_embedding_model()
        cov = stats.get("vector_coverage", {})
        coverage = cov.get(active) if active else None
        top = []
        for h in hits:
            sid = str(h.get("source_id", "")).strip()
            if sid and sid not in top:
                top.append(sid)
        _rl.log_search(
            root,
            query=query,
            k=int(k),
            engine=idx.engine,
            hybrid=bool(hybrid),
            vector_coverage=coverage,
            result_count=len(hits),
            top_source_ids=top,
        )
    except Exception:  # pragma: no cover - best-effort telemetry
        return


def _jsonish(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _chunk_to_dict(chunk) -> dict:
    if hasattr(chunk, "to_dict"):
        return chunk.to_dict()
    if isinstance(chunk, dict):
        return dict(chunk)
    return {"text": str(chunk)}


def index_chunks(
    root,
    source_id: str,
    chunks,
    sensitivity: str,
    provenance,
) -> int:
    """Index pipeline chunks under one source id.

    ``ingest_pipeline`` owns extraction/chunking and passes its chunk objects
    here. This helper normalizes their shape into the public KnowledgeIndex row
    contract and returns the count inserted.
    """
    sid = str(source_id or "")
    prov = _jsonish(provenance)
    rows: list[dict] = []
    for i, chunk in enumerate(chunks or []):
        c = _chunk_to_dict(chunk)
        chunk_index = c.get("chunk_index", c.get("index", i))
        rows.append({
            "doc_id": c.get("doc_id") or sid or f"doc-{i}",
            "source_id": c.get("source_id") or sid,
            "sensitivity": c.get("sensitivity") or sensitivity or "internal",
            "provenance": c.get("provenance") or prov,
            "title": c.get("title", ""),
            "chunk_index": chunk_index,
            "start": c.get("start", 0),
            "end": c.get("end", 0),
            "text": c.get("text", c.get("body", "")),
        })
    with KnowledgeIndex(root) as idx:
        return idx.add_chunks(rows)


def list_chunks(
    root,
    *,
    source_id: Optional[str] = None,
    max_sensitivity: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Module-level convenience wrapper around ``KnowledgeIndex.list_chunks``."""
    with KnowledgeIndex(root) as idx:
        return idx.list_chunks(
            source_id=source_id,
            max_sensitivity=max_sensitivity,
            limit=limit,
        )


def iter_chunks(
    root,
    *,
    source_id: Optional[str] = None,
    max_sensitivity: Optional[str] = None,
    limit: Optional[int] = None,
):
    """Yield indexed chunks. Kept as a generator for adapter code."""
    for chunk in list_chunks(
        root,
        source_id=source_id,
        max_sensitivity=max_sensitivity,
        limit=limit,
    ):
        yield chunk


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Oracle knowledge retrieval index")
    ap.add_argument("--root", default=".", help="oracle root")
    ap.add_argument(
        "--force-fallback", action="store_true",
        help="force the pure-python inverted-index engine (testing/ops)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="add chunks from a JSON file")
    p_build.add_argument("--file", required=True)

    p_add = sub.add_parser("add", help="add a single chunk")
    p_add.add_argument("--doc-id", required=True)
    p_add.add_argument("--text", required=True)
    p_add.add_argument("--source-id", default=None)
    p_add.add_argument("--sensitivity", default="internal")
    p_add.add_argument("--provenance", default="")
    p_add.add_argument("--title", default="")
    p_add.add_argument("--chunk-index", type=int, default=0)
    p_add.add_argument("--start", type=int, default=0)
    p_add.add_argument("--end", type=int, default=0)

    p_query = sub.add_parser("query", help="search the index")
    p_query.add_argument("--q", required=True)
    p_query.add_argument("--k", type=int, default=10)
    p_query.add_argument("--max-sensitivity", default=None)
    p_query.add_argument(
        "--qvec-stdin", action="store_true",
        help="read {embedding_model, vector} from stdin and run hybrid search",
    )

    sub.add_parser("stats", help="index statistics")

    p_re = sub.add_parser("reindex", help="wipe + rebuild from a JSON file")
    p_re.add_argument("--file", required=True)

    # -- vector store subcommands (operator/shell-only; never a model tool) --- #
    p_vadd = sub.add_parser("vectors-add", help="upsert chunk vectors from a JSON file")
    p_vadd.add_argument("--file", required=True)
    # ATTESTATION fields the SHELL stamps (P8S-15): when EITHER is supplied, the
    # batch is ledgered (one embedding_event row per source_id, metadata-only).
    # The kernel records but cannot verify these (it makes no network call).
    p_vadd.add_argument("--embedding-environment", default=None)
    p_vadd.add_argument("--embedding-ceiling", default=None)

    p_vpend = sub.add_parser(
        "vectors-pending", help="chunks lacking a vector for the given model"
    )
    p_vpend.add_argument("--embedding-model", required=True)
    p_vpend.add_argument("--max-sensitivity", default=None)
    p_vpend.add_argument("--limit", type=int, default=None)

    p_vprune = sub.add_parser(
        "vectors-prune", help="drop vectors for every model except --keep-model"
    )
    p_vprune.add_argument("--keep-model", required=True)

    args = ap.parse_args(argv)
    idx = KnowledgeIndex(args.root, force_fallback=args.force_fallback)
    try:
        if args.cmd == "build":
            n = idx.add_chunks(_load_chunks_file(Path(args.file)))
            print(json.dumps({"added": n, "engine": idx.engine}, indent=2))
            return 0
        if args.cmd == "add":
            idx.add(
                args.doc_id, args.text,
                source_id=args.source_id, sensitivity=args.sensitivity,
                provenance=args.provenance, title=args.title,
                chunk_index=args.chunk_index, start=args.start, end=args.end,
            )
            print(json.dumps({"added": 1, "engine": idx.engine}, indent=2))
            return 0
        if args.cmd == "query":
            qvec = None
            qmodel = None
            if getattr(args, "qvec_stdin", False):
                try:
                    qmodel, qvec = _read_query_vector_stdin()
                except ValueError as exc:
                    # The error never carries the vector (P8S-3). Degrade to a
                    # lexical query rather than failing the search outright.
                    sys.stderr.write(f"knowledge_index: {exc}; lexical only\n")
                    qvec = None
                    qmodel = None
            hits = idx.search(
                args.q, k=args.k, max_sensitivity=args.max_sensitivity,
                query_vector=qvec, embedding_model=qmodel,
            )
            # Best-effort retrieval telemetry (P8-T7): one metadata-only row per
            # search, monthly-rotated, salted query_hmac, never the query text.
            # This NEVER fails or delays the search -- the helper swallows every
            # error and a read-only root still returns hits below (P8S-8).
            _log_retrieval_event(
                args.root, query=args.q, k=args.k, idx=idx,
                hybrid=qvec is not None, hits=hits,
            )
            print(json.dumps(hits, indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "stats":
            print(json.dumps(idx.stats(), indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "reindex":
            n = idx.reindex(_load_chunks_file(Path(args.file)))
            print(json.dumps({"reindexed": n, "engine": idx.engine}, indent=2))
            return 0
        if args.cmd == "vectors-add":
            rows = _load_vectors_file(Path(args.file))
            n = idx.add_vectors(rows)
            # Each vectors-add appends one embedding_event row per source_id --
            # metadata only (P8S-15). Gated on a supplied attestation field so a
            # bare operator vectors-add (no env/ceiling) leaves the ledger and
            # the {"added": N} response shape untouched. The shell ALWAYS stamps
            # these, so every real backfill batch is audited.
            env_attest = getattr(args, "embedding_environment", None)
            ceil_attest = getattr(args, "embedding_ceiling", None)
            if env_attest is not None or ceil_attest is not None:
                _ledger_embedding_batch(
                    args.root, rows,
                    environment=env_attest, ceiling=ceil_attest,
                )
            # Frozen response shape {"added": N} (P8S-4): the shell validates
            # THIS shape rather than trusting rc 0.
            print(json.dumps({"added": n}, indent=2))
            return 0
        if args.cmd == "vectors-pending":
            pend = idx.pending_vectors(
                embedding_model=args.embedding_model,
                max_sensitivity=args.max_sensitivity,
                limit=args.limit,
            )
            print(json.dumps(pend, indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "vectors-prune":
            dropped = idx.prune_vectors(keep_model=args.keep_model)
            print(json.dumps({"pruned": dropped}, indent=2))
            return 0
    finally:
        idx.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
