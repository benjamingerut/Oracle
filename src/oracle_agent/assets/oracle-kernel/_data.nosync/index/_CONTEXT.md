# _data.nosync/index

Home for the **rebuildable** knowledge retrieval index.

## What lives here

`knowledge.db` and any supporting files built by `_tools/knowledge_index.py` (`oracle index build|query|reindex`). The index uses sqlite FTS5 when the local sqlite3 build supports it, and falls back to a pure-python inverted index otherwise — query results are parity across both backends.

The same database also holds the `source_catalog` table (`_tools/source_catalog.py`): a self-healing cache of every Source note's parsed frontmatter plus precomputed match keys. It re-parses only new/changed notes (mtime/size drift or a `PARSE_VERSION` bump) and serves the search rerank, answer preflight, truth-map validation, and the Review Inbox without re-reading the notes. It is a lookup **signal, never a gate** — when it is unavailable, every consumer degrades to walking `Sources/` directly.

## Properties

- **Derived and rebuildable.** Nothing here is a source of truth. The authoritative content is the `Sources/` notes and the extracted text they reference; this index is a fast lookup over them and can be regenerated with `oracle index reindex` after any loss or corruption.
- **Provenance- and sensitivity-aware.** Each indexed row carries its source provenance and sensitivity label, so `query --max-sensitivity` can exclude over-ceiling rows. The index is queried by the answer protocol to pull evidence for a material answer.
- Because it is rebuildable, it lives under `_data.nosync/` (git-ignored) and is never backed up as primary state — the `Sources/` records are.
