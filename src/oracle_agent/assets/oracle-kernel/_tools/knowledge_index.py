#!/usr/bin/env python3
"""knowledge_index.py -- the retrieval index for the oracle kernel.

A small, dependency-free full-text retrieval index over indexed chunks of
ingested material. It prefers SQLite's FTS5 virtual-table engine when the
running interpreter's ``sqlite3`` was compiled with it, and otherwise falls
back to a PURE-PYTHON inverted index stored in ordinary SQLite tables. Either
way the public API is identical and results are comparable, so the kernel's
retrieval behaviour does not depend on a particular SQLite build.

The index database is a DERIVED, REBUILDABLE artifact: it lives at the fixed
internal path ``_data.nosync/index/knowledge.db`` under the oracle root. It is
never a user-supplied destination -- the only varying segment is the oracle
root the caller already trusts -- and it is created via ``sqlite3.connect``
(not ``open(...,'w')`` / ``shutil.*``), so it is outside the no-bypass guard's
remit. Nothing user-influenced is written through raw file I/O here.

Public API:
    KnowledgeIndex(root, *, force_fallback=False) -> instance
    .add(doc_id, text, *, source_id=None, sensitivity='internal',
         provenance='', chunk_index=0, start=0, end=0, title='') -> None
    .add_chunks(chunks) -> int          # bulk add list[dict]
    .search(query, *, k=10, max_sensitivity=None) -> list[dict]
    .stats() -> dict
    .reindex(chunks) -> int             # wipe + rebuild from a fresh chunk set
    .engine -> 'fts5' | 'fallback'
    .close() -> None

Module helpers:
    default_db_path(root) -> Path
    fts5_available() -> bool
    tokenize(text) -> list[str]

CLI:
    python3 knowledge_index.py --root R build  [--file chunks.json]
    python3 knowledge_index.py --root R add    --doc-id D --text T [...]
    python3 knowledge_index.py --root R query  --q "..." [--k N] [--max-sensitivity S]
    python3 knowledge_index.py --root R stats
    python3 knowledge_index.py --root R reindex --file chunks.json

Stdlib only (sqlite3, re, json, math, os, argparse, pathlib).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
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
]

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
        else:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS chunks ("
                "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
                "doc_id TEXT, source_id TEXT, sensitivity TEXT, provenance TEXT, "
                "title TEXT, chunk_index INTEGER, start_off INTEGER, end_off INTEGER, "
                "body TEXT)"
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
        if self.engine == "fts5":
            self._con.execute(
                "INSERT INTO chunks("
                "doc_id, source_id, sensitivity, provenance, title, "
                "chunk_index, start_off, end_off, body) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (doc_id, sid, sens, provenance, title, int(chunk_index),
                 int(start), int(end), body),
            )
        else:
            cur = self._con.execute(
                "INSERT INTO chunks("
                "doc_id, source_id, sensitivity, provenance, title, "
                "chunk_index, start_off, end_off, body) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (doc_id, sid, sens, provenance, title, int(chunk_index),
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

    # -- search ------------------------------------------------------------- #
    def search(
        self,
        query: str,
        *,
        k: int = 10,
        max_sensitivity: Optional[str] = None,
    ) -> list[dict]:
        """Return up to ``k`` ranked hits for ``query``.

        ``max_sensitivity`` is a ceiling: a row whose sensitivity is STRICTER
        than the ceiling is excluded (the retrieval layer never surfaces
        over-ceiling material to a query operating at a lower clearance). An
        unknown row sensitivity is treated as the strictest, so it is excluded
        unless the ceiling is also the strictest. Each hit is a dict with:
        doc_id, source_id, sensitivity, provenance, title, chunk_index, start,
        end, text, score.
        """
        terms = tokenize(query)
        if not terms:
            return []
        ceiling = _sens_rank(max_sensitivity) if max_sensitivity is not None else None
        if self.engine == "fts5":
            hits = self._search_fts5(query, terms)
        else:
            hits = self._search_fallback(terms)
        if ceiling is not None:
            hits = [h for h in hits if _sens_rank(h["sensitivity"]) <= ceiling]
        self._apply_rerank(hits)
        hits.sort(key=lambda h: (-h["score"], h["doc_id"], h["chunk_index"]))
        return hits[: max(0, int(k))]

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
        return {
            "engine": self.engine,
            "db_path": str(self.db_path),
            "chunks": n,
            "documents": docs,
            "by_sensitivity": by_sens,
        }

    def _wipe(self) -> None:
        self._con.execute("DELETE FROM chunks")
        if self.engine == "fallback":
            self._con.execute("DELETE FROM postings")
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

    sub.add_parser("stats", help="index statistics")

    p_re = sub.add_parser("reindex", help="wipe + rebuild from a JSON file")
    p_re.add_argument("--file", required=True)

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
            hits = idx.search(args.q, k=args.k, max_sensitivity=args.max_sensitivity)
            print(json.dumps(hits, indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "stats":
            print(json.dumps(idx.stats(), indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "reindex":
            n = idx.reindex(_load_chunks_file(Path(args.file)))
            print(json.dumps({"reindexed": n, "engine": idx.engine}, indent=2))
            return 0
    finally:
        idx.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
