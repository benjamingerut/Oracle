# SUB-1 — Kernel Core Fixes (Phase 1)

All paths relative to `src/oracle_agent/assets/oracle-kernel/`. Tasks K1–K4
are file-disjoint and run in parallel. Each task adds tests under `tests/`
in this kernel tree and must keep `make check` green.

---

## K1 — Truth map: pipe-injection + atomic locked writes

**Files:** `_tools/truth_map.py`, new/extended tests in `tests/` (truth-map test file).

**Findings (verified):**
- `propose_row`/`promote_row` (`truth_map.py:367,391`) compose rows as
  `"| " + " | ".join(cells) + " |"` with no `|` escaping. A cell value
  containing `|` (reachable via ordinary ingest: `ingest_pipeline._propose_truth_rows`
  auto-proposes rows from ingested-document metadata) silently shifts columns
  and rewrites the authority table.
- `_write_truth_map` (`truth_map.py:282-289`) is a bare `path.write_text` —
  no temp+`os.replace`, no lock. Crash mid-write or concurrent writers
  corrupt TRUTH-MAP.md.

**Required behavior:**
1. Escape `|` → `\|` in every cell at composition time (the parser already
   restores `\|`). Reject (raise/refuse with clear error) any cell containing
   a newline.
2. Round-trip property: any cell value without newlines survives
   propose → parse unchanged, including values with `|`, `\|`, leading/
   trailing spaces.
3. `_write_truth_map` writes via temp file in the same directory +
   `os.replace`, under an `fcntl` lock, mirroring the discipline of
   `ledger.rewrite_atomic`. All writers route through it.

**Tests (acceptance):**
- Pipe-in-metadata proposal round-trips with cells intact (the previous
  corruption case now preserved/escaped).
- Newline in a cell is refused.
- Write is atomic: simulate by checking temp+replace usage (no partial file
  on injected failure between write and replace).

---

## K2 — Ledger: tamper-evident hash chain + quarantine dedupe

**Files:** `_tools/ledger.py`, ledger tests in `tests/`.

**Findings (verified):**
- "Immutable/append-only" is convention only: `verify()` (`ledger.py:281-332`)
  checks JSON validity, duplicate `drop_id`s, missing keys. In-place edits of
  historical rows are undetectable.
- `load()` (`ledger.py:133-168`) re-appends each malformed line to
  `.quarantine` on every read and never heals the source — quarantine grows
  unboundedly from a single bad line.

**Required behavior:**
1. Each appended row gains `row_hash` = sha256 over a canonical serialization
   (sorted keys, separators=(",",":")) of the row minus `row_hash`, plus the
   previous row's `row_hash` (`prev_hash` concept; genesis uses `""`).
   Written under the existing append lock.
2. `verify()` walks the chain: legacy unhashed prefix rows are tolerated
   (reported as `legacy`), but from the first hashed row onward any edit,
   deletion, or reordering breaks the chain and is reported with line number.
3. Quarantine dedupe: a malformed line is quarantined at most once (key by
   sha256 of the raw line); repeat reads do not grow `.quarantine` or re-warn
   beyond a single notice.
4. `rewrite_atomic` (used by `repair()`) re-chains hashes for the surviving
   rows and records that a rewrite happened (a rewrite-marker row appended
   with actor + reason), so repairs are themselves auditable.

**Tests (acceptance):**
- Append N rows, edit row k in place → `verify()` reports break at k.
- Delete / reorder a row → break detected.
- Legacy file (no hashes) + new appends → verify passes with legacy prefix
  noted; edits in the hashed suffix detected.
- Malformed line read 3× → exactly one quarantine entry.
- Repair re-chains and appends rewrite marker.

Keep verify output shape backward-compatible where tests rely on it; extend,
don't break.

---

## K3 — Knowledge index: dedup, upsert, source deletion

**Files:** `_tools/knowledge_index.py`, `_tools/ingest_pipeline.py` (only its
chunk-registration path), a migration `_tools/migrations/0003_*.py`, tests.

**Findings (verified):**
- `add`/`add_chunks` (`knowledge_index.py:263-326`) always INSERT; no
  uniqueness on `(source_id, chunk_index)`, no `delete_by_source`.
  Re-adding the same document 3× yields 3 duplicate hits.
- Re-ingesting an updated file (new hash → new source_id) leaves prior
  chunks orphaned forever; only full `reindex()` cleans up.

**Required behavior:**
1. Unique key `(source_id, chunk_index)` enforced in both the FTS5 backend
   and the pure-Python fallback; `add_chunks` upserts (replace on conflict).
2. New `delete_source(source_id)` removing all chunks for a source, in both
   backends.
3. Migration `0003_…` (follow the existing `NNNN_` idempotent pattern in
   `_tools/migrations/`): dedupe existing rows (keep newest per key) and
   install the uniqueness constraint/index.
4. Ingest pipeline: when ingesting a file whose logical origin (per
   `source_catalog`) already has a previous source record, remove the
   superseded source's chunks from the index after the new source's chunks
   are registered. Inspect `source_catalog.py` for the supersession linkage;
   if no linkage exists, key on the catalog's origin path. Fail closed: if
   supersession cannot be determined, leave old chunks but emit a review-
   queue-visible warning rather than silently duplicating.

**Tests (acceptance):**
- Re-add same doc 3× → exactly one set of chunks; search returns no dupes.
- Updated file re-ingest → old chunks gone, new chunks present.
- Migration on a pre-existing duplicated DB dedupes and is idempotent.
- Both backends (FTS5 + fallback) covered.

---

## K4 — Secret scan: coverage broadening

**Files:** `_tools/secret_scan.py`, `tests/test_secret_scan.py`.

**Findings (verified misses):** Anthropic `sk-ant-…` (generic entropy only),
Azure `AccountKey=…` connection strings (missed), `npm_…` tokens (incidental),
GCP service-account JSON private keys, `password = <wordlike-no-digit>`
(missed — heuristic requires a digit), Telegram bot tokens `123456:AA…`,
GitHub `ghp_`/`github_pat_`, Slack `xox[abprs]-`.

**Required behavior:**
1. Named patterns for each of the above (named = stable finding type, not
   entropy-net incidental).
2. Assignment heuristic tightened: keys matching
   `password|passwd|secret|token|api_key|apikey` with a value ≥8 chars flag
   even without digits, EXCEPT obvious placeholders (case-insensitive:
   `example`, `changeme`, `placeholder`, `redacted`, `your[-_]`, `xxx+`,
   `<...>` angle-bracket templates, `${...}`/`{{...}}` interpolations, and
   values referencing env lookups). False-positive discipline matters: the
   scan gates `make check`; run it over the repo and kernel asset tree and
   ensure zero new false positives (adjust the placeholder allowlist if a
   legitimate fixture trips — fixtures may also be annotated with the scan's
   existing inline-ignore mechanism if one exists; check before inventing).
3. Each new pattern has a positive and a negative test.

**Acceptance:** all listed token shapes detected by name; placeholders not
flagged; `make check` (which runs the scan over shipped content) stays green.
