# Phase 8 — Retrieval Quality

**Added by the SUB-5 roadmap amendment (decision 1).** The oracle's value is
grounded answers, and grounded answers begin with retrieval. Today retrieval is
lexical-only: FTS5 (or the pure-Python inverted-index fallback) with a light
stemmer, plus the authority/recency rerank. That misses paraphrase ("headcount"
never matches "number of employees") and cross-vocabulary questions — exactly
the questions a non-technical admin asks. This phase adds an **optional
embedding index alongside FTS5**, fused by reciprocal-rank fusion, with the
lexical engine remaining the always-available fallback (I1: zero new required
dependencies — embeddings come from the already-configured `/v1` endpoint's
`embeddings` API over `urllib`, or a local model server; no numpy).

**The security pin (non-negotiable): an embedding call IS content egress.**
Sending a chunk's text to an embeddings endpoint is indistinguishable, for
policy purposes, from sending it to a chat endpoint. The policy bridge's
environment×sensitivity ceiling — INCLUDING the egress veto
(`policy_bridge.egress_veto`, the STRESS C2/P2S-2 follow-up that reclassifies
a loopback Ollama listener serving `*:cloud` proxied models as external;
landed after this spec's first draft, folded in by P8S-1) — therefore applies
to embedding requests exactly as it applies to chat requests, enforced in
code at the embedding dispatch (I5), failing closed (I4). Chunks above the
embedding endpoint's post-veto ceiling simply stay lexical-only.

Read first: `docs/roadmap/ROADMAP.md` (invariants I1–I6),
`docs/remediation/SUB-5-roadmap.md` (decision 1), the kernel's
`_tools/knowledge_index.py` (engines, sensitivity ceiling, the
`(source_id, chunk_index)` upsert + `delete_source`), `_tools/chunker.py`
(offset-exact spans — citations must keep pointing at exact source spans),
`_tools/scorecard.py` (KPI structure), `_tools/oracle_cli.py`
(`_translate`'s search-group routing — P8S-4), the shell's
`agentloop/policy_bridge.py` (`environment_for`, `max_sensitivity_for`, and
`egress_veto`) and `llm/client.py` (STRESS C2 posture: no redirects, loopback
per-request guard, plaintext-key refusal).

Depends on: Phase 1 (P1-T5 kernel re-vendor plumbing for all upstream work,
P1-T2 testkit, P1-T1 SECURITY.md map). Benefits from Phase 7's connector
corpus — tune ranking against real content, not toy fixtures. Independent of
P2/P3 and can run in parallel with them.

## The core idea

The kernel stays sovereign and never dials out (I3): `knowledge_index.py`
gains a vector store and a hybrid ranker, but it only ever *stores and
compares* vectors handed to it through its CLI. All embedding computation —
the network egress — happens in the shell, where the policy bridge lives and
where the egress ceiling can be enforced at dispatch. The split:

- **Kernel (upstream, re-vendored via P1-T5):** a `chunk_vectors` table in the
  SAME SQLite DB (`_data.nosync/index/knowledge.db`), keyed
  `(source_id, chunk_index, embedding_model)` so the existing upsert,
  `delete_source`, and `reindex`/`_wipe` semantics extend naturally; hybrid
  search that fuses the lexical ranking with brute-force cosine over the
  vectors via reciprocal-rank fusion, with the sensitivity ceiling applied IN
  each scan, to BOTH lists, before any ranking or candidate truncation —
  ranks are dense positions within the ceiling-filtered lists (P8S-5).
- **Shell:** an `embed()` method on the stdlib LLM client (same C2 posture as
  `chat()`, on a SEPARATE one-purpose client instance — P8S-2), and an
  embedder module that (a) computes the embedding endpoint's own POST-VETO
  environment×sensitivity ceiling via the policy bridge (`environment_for`
  then `egress_veto`), (b) refuses at dispatch to embed any chunk above that
  ceiling, and (c) applies the frozen query rule below. Vectors flow back
  into the kernel via `vectors add`.

Hybrid hits carry the same row shape as today (`start`/`end` char offsets from
`chunker.py`), so evidence citations remain offset-exact. The chunker is not
touched.

**Why pure-Python cosine is acceptable (justification, re-pinned by P8S-7):**
vectors are stored unit-normalized as little-endian float32 BLOBs
(`array('f')`), so similarity is a dot product. The dot product uses
`math.sumprod` (C-speed, Python 3.12+) when available, with a pure-Python
fallback on 3.10/3.11 — the Python floor STAYS `>=3.10`; installability is a
product property and is not traded for a fast path. The latency budget is
therefore measured on the FLOOR interpreter (3.10, fallback path), not the
developer's interpreter, and the design corpus is restated honestly: the
original ≤ ~20k figure predates Phase 7 — five-plus connectors × years of
mail/Drive/Notion put the design point at **≥ 100k chunks**, where a
1536-dim brute-force scan is ~150M+ multiply-adds and the per-query BLOB
read is no longer trivial. That is still acceptable for an interactive
oracle because I1 forbids numpy, the sensitivity ceiling prunes the scan for
low-clearance surfaces, and a contingency ladder is pinned IN ORDER:
(1) **reduced dimensions first** (the provider `dimensions` param, e.g.
256–512 — cuts storage and scan 3–6× for minor recall loss), (2) int8
quantization (×4 smaller, ~×2 faster). The contingency trigger is
CORPUS-DRIVEN, not a one-time laptop measurement: P8-T2 derives a named
chunk-count threshold from the measured budget, doctor warns when the corpus
crosses it, and the ladder activates there. An ANN index is explicitly out
of scope (see "what this phase does NOT do").

## Frozen interfaces

### Kernel (upstream): `_tools/knowledge_index.py` additions
```python
# chunk_vectors(source_id TEXT, chunk_index INTEGER, embedding_model TEXT,
#               dim INTEGER, norm REAL, vector BLOB,
#               PRIMARY KEY (source_id, chunk_index, embedding_model))
# NO sensitivity column — sensitivity is ALWAYS read by JOIN to chunks on
# (source_id, chunk_index), so a reclassified chunk's label is authoritative
# the instant its chunk row changes; a copied label could go stale (P8S-6).
.add_vectors(rows) -> int        # [{source_id, chunk_index, embedding_model,
                                 #   vector: list[float]}]; upsert; float32
                                 # BLOB; zero-norm and non-finite vectors
                                 # REJECTED (P8S-11)
.pending_vectors(*, embedding_model, max_sensitivity=None, limit=None)
                                 # chunks lacking a vector for that model,
                                 # optionally ceiling-filtered (convenience —
                                 # the shell enforcer re-checks at dispatch)
.search(query, *, k=10, max_sensitivity=None,
        query_vector=None, embedding_model=None) -> list[dict]
                                 # query_vector present -> RRF fusion; absent
                                 # -> today's lexical path, byte-identical
.stats()                         # gains: vectors, vector_coverage,
                                 #        by_embedding_model, dim_mismatches
```
`delete_source`, `_wipe`, and the `(source_id, chunk_index)` upsert remove
`chunk_vectors` rows **in the SAME SQLite transaction as the chunk-row
mutation** — one commit, so a crash can never leave a vector outliving its
chunk (P8S-6; vectors are content-equivalent — they must never outlive their
chunk). Doctor gains a cheap orphan-vector query (vector rows with no chunk)
as the crash-tolerance backstop. RRF, frozen:
`score(d) = Σ_r 1/(60 + rank_r(d))` over the lexical and vector rankings,
where the ceiling filter is applied IN each scan — before any candidate
truncation — and `rank_r` is the DENSE position within the ceiling-FILTERED
list. An above-ceiling chunk must not perturb visible ranks or scores, or its
existence leaks through rank shifts (P8S-5); acceptance below makes this
byte-testable. Named accepted residual: corpus-global statistics (FTS5 bm25
IDF; the fallback's idf over all chunks) include above-ceiling rows — a weak
aggregate signal that exists in today's lexical path too. The existing
`_apply_rerank` authority/recency boost multiplies the fused score, unchanged
(note: RRF actually CAPS token-stuffing influence relative to raw bm25 — a
single ranking contributes at most 1/61 per document). Search compares only
vectors whose `embedding_model` matches the query vector's model AND whose
`dim` equals the query vector's length — same name + different dim is
skipped and counted in `stats().dim_mismatches`, never fused (P8S-11;
mixed-version corpora degrade gracefully: uncovered chunks still compete
lexically).

### Kernel CLI surface
```
oracle search vectors-add    --file vectors.json [--embedding-model M ...]
oracle search vectors-pending --embedding-model M [--max-sensitivity S] [--limit N]
oracle search query --q "..." [--qvec-stdin]   # stdin: {"embedding_model": M,
                                               #         "vector": [...]}
oracle search vectors-prune  --keep-model M    # drop superseded-model vectors
```
The query vector travels on stdin, never argv (size, ps-visibility). The
kernel caps the stdin read (1 MiB, ≤ 8192 dims, finite floats only) and never
echoes the vector in an error message (P8S-3).

Routing reality (P8S-4): `oracle_cli._translate`'s search-group passthrough
allowlist is today `("query", "build", "add", "stats", "reindex")` — unfixed,
`oracle search vectors-add ...` would be silently rewritten into a TEXT QUERY
for the literal string "vectors-add" and exit 0, no-opping the whole vector
pipeline while looking green. The allowlist is EXTENDED to `vectors-add`,
`vectors-pending`, `vectors-prune` (with `--qvec-stdin` passing through),
backed by a routing test; belt-and-braces, the shell validates the
`vectors-add` response SHAPE (`{"added": N}`) rather than trusting rc 0.

The `vectors.json` handoff file is written under the ROOT at `tmp.nosync/`,
created `0o600` via `os.open`, and deleted after `vectors-add` returns —
vectors are content-equivalent to their chunks and must not transit a
world-readable tmp path (P8S-10). The `vectors-*` subcommands are NEVER added
to the verbtools tool schemas on any surface — the model must not gain a
bulk corpus-text export (`vectors-pending` emits chunk text) or a
vector-injection tool — enforced structurally, SH-005-style (P8S-10).

`index_meta` records the ACTIVE `embedding_model`; changing it makes every
chunk "pending" for the new model without deleting old vectors until pruned.
Each `vectors-add` appends one `embedding_event` ledger row — **metadata
only**: source_id, chunk count, embedding_model, the caller-stamped endpoint
environment and applied ceiling; never text, never vectors. The
environment/ceiling fields are ATTESTATION — the kernel records what the
shell claims and cannot verify shell egress; the enforced guarantees point at
the shell enforcer tests (P8S-15, see P8-T9).

### Kernel (upstream, flagged): `_tools/scorecard.py` + ledgers
New retrieval ledger, MONTHLY-ROTATED:
`Meta.nosync/ledgers/retrieval_event-YYYYMM.jsonl`. Rotation is load-bearing
(P8S-8): `ledger.append` scans the whole file under LOCK_EX per row, so an
unbounded per-search ledger is a quadratic hot path riding inside search
latency; the hash chain restarts per file, which `ledger.verify` already
tolerates as a legacy prefix. One metadata row per search: ts, **`query_hmac`
— HMAC-SHA256 of the query text under a per-root random salt minted on first
use and stored under `_data.nosync/` (never exported, never ledgered,
excluded from sync); a bare sha256 of short natural-language queries is
dictionary-reversible (P8S-9)** — k, engine, hybrid flag, vector_coverage,
result_count, top source_ids. No `drop_id` minting (`id_prefix=None`; ts +
row_hash are identity — this removes one of the two full-file scans per
append). The append is BEST-EFFORT: it never fails, blocks, or measurably
delays a search — the read path must not become write-dependent, and a
read-only root still searches. Named accepted residual: top source_ids per
search correlate queries to sources — metadata-tier, accepted.
`answer_event` rows gain an additive `source_ids` field (cited sources).
`compute_kpis` gains a `retrieval` section:

  * searches, non_empty_rate, hybrid_share, vector_coverage;
  * **retrieval_hit_rate** — share of window searches whose top source_ids
    intersect the source_ids cited by an exit-0 `answer_event` in the window;
  * **time_to_first_grounded_answer** — median days from a source's ingest to
    the first exit-0 answer citing it.

This is all **upstream kernel work** (scorecard, ledgers, and the
answer-protocol field land in the Oracle Spawn kit and are re-vendored via
P1-T5; the shell never edits vendored kernel files, I3). Precision@k against
ground truth does NOT live in the scorecard — it lives in the eval harness,
where gold labels exist (P8-T8 → Phase 6).

### Shell: `llm/client.py`
```python
class LLMClient:
    def embed(self, texts: list[str], *, model: str) -> list[list[float]]
    # POST {base_url}/embeddings; same construction + per-request guards as
    # chat(): _NoRedirect, plaintext-key refusal, _check_request_host called
    # on the /embeddings URL before send, key never in errors; classify_error
    # reused.
```
Config: a `provider.embeddings` block (`{model, base_url?, api_key_env?}`);
`base_url`/key default to the chat endpoint's.
`provider.embeddings.api_key_env` and `provider.embeddings.base_url` join
`config.SECURITY_KEYS` so no migration can silently drop or alter the
embedding endpoint (P8S-16).

The embeddings client is a SEPARATE, one-purpose `LLMClient` instance
constructed with the embeddings base_url/key and its own POST-VETO
environment — never the chat client with a swapped path — so the
construction-time plaintext-key refusal and the `local_agent` per-request
guard key to the EMBEDDING endpoint; the chat client never calls
`/embeddings` and the embed client never calls `/chat/completions` (P8S-2).
STRESS scope-cut reconciliation: the v1 "one client per session" cut existed
because a second client with a different environment could RAISE a ceiling
computed for the primary; the embeddings client is exactly such a second
client, and the frozen query rule below — a ceiling COMPARISON, fail-closed
both ways — is the mechanism that prevents it from raising anything. This
supersedes the cut for embeddings only; the chat fallback chain stays cut.

The embedding endpoint's environment is classified **independently** via
`policy_bridge.environment_for(embed_base_url)` **and then vetoed via
`policy_bridge.egress_veto(embed_base_url, embed_model)`** — a local chat
model with an external embedder must NOT inherit `local_agent`, and a
loopback Ollama listener serving a `*:cloud` EMBEDDING model is the same
breach as the chat case (P8S-1).

### Shell: `agentloop/embedder.py` (new)
```python
def embedding_ceiling(root, embed_base_url, embed_model) -> str
    # environment_for(embed_base_url), then egress_veto(embed_base_url,
    # embed_model) — any veto reclassifies to "external" BEFORE
    # max_sensitivity_for (P8S-1); any error -> "public" (fail closed, same
    # as the bridge). Computed at build_loop AND re-computed at the start of
    # every embed_pending run / backfill batch (an `ollama pull *:cloud`
    # between ticks is the TOCTOU window); NEVER per-search — the veto's 3 s
    # probe must not ride the search path. Per-search reuses the build-time
    # value; the client's per-request loopback guard covers a host swap.
def embed_pending(client, root, *, ceiling, batch=64, limit=None) -> dict
    # pending -> ENFORCER: re-read each chunk's CURRENT sensitivity at
    # dispatch (one indexed query per chunk — zero reclassification window,
    # P8S-14) and drop any chunk whose sensitivity exceeds ceiling ->
    # embed -> vectors-add (response shape validated, P8S-4). Idempotent +
    # resumable by construction (the pending set shrinks monotonically;
    # keyed upsert).
def query_vector_allowed(retrieval_ceiling, embed_ceiling, order) -> bool
    # rank(retrieval_ceiling) <= rank(embed_ceiling); embed_ceiling is the
    # POST-VETO ceiling.
```

The query-path seam (P8S-3 — `verbtools.Dispatcher` stays egress-free, by
design): `build_loop` injects an optional PURE callable
`query_embedder: Callable[[str], dict | None]` into the Dispatcher (`None` ⇒
lexical exactly as today). The callable closes over the embed client, the
post-veto ceiling, and the `query_vector_allowed` decision, so verbtools
itself holds no client and no ceiling logic. `run_verb` gains an
`input: str | None` parameter for the stdin channel; the stdin payload is
composed EXCLUSIVELY by shell code — `{"embedding_model": M, "vector":
[...]}`, a config-sourced model string plus computed floats; the
model-supplied `terms` never enter stdin, so the channel is not
model-influencable and the argv chokepoint discipline (I2) is preserved. The
query-path embed call runs under a **10 s timeout**, and ANY transport
failure (network error, timeout, malformed response) degrades silently to
lexical — the same silent-but-correct degradation as every policy refusal.

### The frozen query rule (security pin, spelled out)

A retrieval query may itself contain internal content (the user quotes a
figure, names a customer). The query text is therefore conservatively
classified **at the surface's retrieval ceiling** — the `max_sensitivity` the
search runs with in `verbtools._do_oracle_search`. Frozen rule: **the query is
embedded — and vector search participates at all — iff
`rank(retrieval_ceiling) <= rank(embedding endpoint's POST-VETO ceiling)`.**
An external-classified OR vetoed embedder (ceiling `public`) therefore
disables vector search entirely for every internal-and-above surface, and
retrieval falls back to lexical, silently and correctly. No truncation, no "embed a sanitized
query" cleverness — the rule is a comparison of two labels and an early
return. Symmetric consequences, both fail closed:

| | external or vetoed embedder (ceiling `public`) | local_agent, veto-clean embedder (ceiling e.g. `internal`) |
|---|---|---|
| chunk embedding | PUBLIC chunks only | chunks at/below `internal`; confidential+ stay lexical-only |
| query embedding | only surfaces whose retrieval ceiling is `public` | surfaces at/below `internal` |

## Tasks

- **P8-T1 — kernel vector store (upstream).** `chunk_vectors` table (NO
  sensitivity column — the label is always join-read from `chunks`, P8S-6) +
  `add_vectors`/`pending_vectors`/stats coverage + removal wired into
  `delete_source`, `_wipe`, and the `(source_id, chunk_index)` upsert **in
  the same SQLite transaction as the chunk mutation** (P8S-6; a re-ingested
  chunk's stale vector is deleted with the old row — a reclassified chunk
  must not keep a vector minted under its old sensitivity; since the OLD
  egress already happened and cannot be retracted, the drop forces a re-embed
  that the new label's ceiling may rightly forbid, and the chunk then
  degrades to lexical-only). float32 BLOB via `array('f')`, norm stored,
  zero-norm/non-finite vectors rejected (P8S-11), stdlib only, both engines.
  Also in this task: the `oracle_cli._translate` allowlist extension for the
  `vectors-*` subcommands with a routing test (P8S-4); the doctor
  orphan-vector query (P8S-6); and the knowledge_index docstring's "derived,
  rebuildable" claim amended — a rebuild now implies a full-corpus re-embed
  through the egress endpoint (cost + ledgered, auditable re-egress; P8S-13).
  *Acceptance:* add/delete/reindex round-trips; `delete_source` leaves zero
  vector rows, including when killed mid-call (single transaction); upserting
  a chunk drops its old vector; a connector re-sync through
  `ingest_pipeline._remove_superseded_chunks` leaves zero vectors for the
  superseded source_id (end-to-end, P8S-6); `oracle search vectors-add`
  routes to vectors-add, never to a text query (P8S-4); stats reports
  per-model coverage. *Tests (kernel):* `test_knowledge_vectors.py`. *Deps:*
  P1-T5.

- **P8-T2 — kernel hybrid search + RRF (upstream).** `search(...,
  query_vector=...)`: brute-force dot product over same-`(model, dim)`
  vectors (`math.sumprod` fast path, pure-Python fallback on 3.10/3.11 — the
  floor stays 3.10, P8S-7), ceiling filter applied IN both scans before any
  ranking or candidate truncation, RRF(k=60) over dense post-filter ranks,
  `_apply_rerank` post-fusion; `--qvec-stdin` on `query` (stdin capped: 1 MiB,
  ≤ 8192 dims, finite floats; vector never echoed in errors). Measure and
  record the latency budget ON THE FLOOR INTERPRETER (3.10, fallback path) at
  the **≥ 100k-chunk** post-P7 design point (P8S-7), and from the measurement
  pin the named chunk-count threshold at which doctor warns and the
  contingency ladder activates — reduced dimensions first, int8 second.
  *Acceptance:* without a query vector the output is byte-identical to today;
  with one, a paraphrase fixture query ranks the semantically-matching chunk
  first; an over-ceiling chunk never appears even when it is the best cosine
  hit; **adding or removing an above-ceiling chunk leaves the below-ceiling
  result list and scores byte-identical** (the rank-perturbation existence
  leak, P8S-5); a same-model different-dim vector is skipped and counted,
  never fused (P8S-11); hits keep exact `start`/`end` offsets.
  *Tests (kernel):* `test_hybrid_search.py`. *Deps:* P8-T1.

- **P8-T3 — shell embeddings client.** `LLMClient.embed()` +
  `provider.embeddings` config block (with `provider.embeddings.{api_key_env,
  base_url}` added to `config.SECURITY_KEYS`, P8S-16) + independent POST-VETO
  environment classification of the embedding base_url. The embeddings client
  is a separate one-purpose instance (P8S-2). Full C2 posture inherited and
  tested ON THE `/embeddings` PATH (redirect refused, local_agent per-request
  loopback guard at send time, plaintext-key refusal at construction against
  the EMBEDDINGS base_url, key absent from all errors). *Acceptance:* against
  a fake `/v1/embeddings` server, batches round-trip; a 302 raises; a
  `local_agent`-classified embed client refuses a non-loopback URL at send
  time; the chat client never issues an `/embeddings` request and the embed
  client never issues `/chat/completions`. *Tests:* `test_embed_client.py`.
  *Deps:* P1 (none hard; parallel with T1/T2).

- **P8-T4 — embedding egress enforcer (the security pin).**
  `agentloop/embedder.py`: `embedding_ceiling` (`environment_for` +
  `egress_veto`, P8S-1), the per-chunk dispatch check (current-sensitivity
  re-read, P8S-14), and `query_vector_allowed`; the `query_embedder` callable
  injected into the Dispatcher by `build_loop` (verbtools stays egress-free,
  P8S-3) computes the query vector only when the rule allows and passes it
  via `--qvec-stdin` through `run_verb(input=...)`, else runs lexical exactly
  as today. Every refusal path AND every transport failure (10 s embed
  timeout) is silent-but-correct degradation, never an error surfaced to the
  model. Testkit gains a `FakeEmbedClient` recording request payloads plus an
  `assert_no_content_above`-style assertion over embedding requests (additive
  to the frozen P1-T2 interface, P8S-16). *Acceptance:* with the fake embed
  client — an internal chunk is NEVER in any request when the embedder
  ceiling is public; an internal-surface query is never embedded by a
  public-ceiling embedder (vector search disabled, lexical results still
  returned); a loopback endpoint serving a `*:cloud` embedding model is
  classified external and embeds nothing above public (P8S-1);
  ceiling-computation error → zero embedding requests; embed transport
  failure → lexical results, no error surfaced. *Tests:*
  `test_embedder_enforcer.py` (the named enforcer tests:
  `test_embed_dispatch_blocks_over_ceiling_chunks`,
  `test_external_embedder_disables_vector_search_above_public`,
  `test_embed_ceiling_applies_egress_veto`,
  `test_ceiling_error_fails_closed_no_egress`). *Deps:* P8-T3.

- **P8-T5 — incremental embedding + backfill loop.** After a shell-initiated
  ingest, run a bounded `embed_pending` pass for the touched source; a
  scheduler-tick backfill drains the rest of the corpus in batches, gated by
  `scheduler.autonomy_enabled(root)` and resumable across ticks (it is just
  `embed_pending` with a limit). The post-veto ceiling is RE-COMPUTED at the
  start of every run/batch — never cached from an earlier build (P8S-1).
  Vectors hand off via the 0600 in-root `tmp.nosync/` file, deleted after
  `vectors-add` returns (P8S-10), and the `vectors-add` response shape is
  validated (P8S-4). Every batch lands one `embedding_event` row.
  *Acceptance:* a fresh corpus reaches full coverage across N ticks; killing
  mid-backfill loses at most one batch and resumes; autonomy off → zero
  embedding requests; a model renamed to `*:cloud` between ticks → zero
  requests on the next tick (P8S-1); ledger rows are metadata-only; the
  handoff file is 0600 while alive and gone afterwards. *Tests:*
  `test_embed_backfill.py`. *Deps:* P8-T1, P8-T3, P8-T4.

- **P8-T6 — model-change re-embed + mixed-version degradation.** Switching
  `provider.embeddings.model` updates `index_meta`, re-pends every chunk,
  backfill re-embeds under the new model; `vectors-prune` drops the old;
  search never fuses across models OR dims (P8S-11). Doctor + dashboard show
  embedding environment (POST-VETO, naming the veto reason when one fired,
  P8S-1), ceiling, model, and coverage; doctor warns on a coverage collapse
  relative to the active model — the post-reindex signature, since a
  `reindex`/`_wipe`/DB loss now implies a full-corpus re-embed through the
  egress endpoint (cost + auditable re-egress via `embedding_event`, P8S-13)
  — and at the P8-T2 corpus threshold (P8S-7). *Acceptance:* mid-migration
  (50% coverage) hybrid search uses new-model vectors only and still returns
  lexical hits for uncovered chunks; a dimension change under the same model
  name never fuses (P8S-11); doctor on an external embedder config states
  plainly that internal+ surfaces are lexical-only and why; doctor on a
  vetoed loopback config names the proxied remote host. *Tests:* extend
  `test_knowledge_vectors.py`, doctor tests. *Deps:* P8-T1, P8-T5.

- **P8-T7 — retrieval KPIs (upstream, flagged).** Monthly-rotated
  `retrieval_event-YYYYMM.jsonl` (P8S-8), `answer_event.source_ids`, and the
  scorecard `retrieval` section (retrieval_hit_rate,
  time_to_first_grounded_answer, coverage/share numbers) per the frozen
  interface above; composite formula untouched (KPI addition, not
  re-weighting). The per-search append is best-effort — it never fails or
  blocks a search — mints no drop_id, and stores `query_hmac` under the
  per-root salt in `_data.nosync/` (P8S-9). *Acceptance:* synthetic ledgers
  yield the expected hit-rate and median; query text never appears in any
  row, and the stored hash is NOT a bare sha256 of the query (dictionary
  test: `sha256(query)` does not match the stored value); a read-only root
  still searches (append failure swallowed); a month rollover starts a fresh
  chain that `ledger.verify` accepts; old scorecards without the section
  still parse. *Tests (kernel):* `test_scorecard_retrieval.py`,
  `test_retrieval_ledger.py`. *Deps:* P8-T2, P1-T5.

- **P8-T8 — gold retrieval fixtures + eval wiring.** A small in-repo fixture
  set `eval/fixtures/retrieval_gold.json`: a synthetic public-only corpus
  (secret-scan clean) + ~25 query→expected-source_id pairs, including
  paraphrase cases lexical search demonstrably misses AND a
  **lexical-anchor subset** (exact identifiers, codes, figures — the queries
  embeddings notoriously lose; P8S-12). CI has no embedding endpoint, so the
  fixture VENDORS precomputed real vectors (corpus chunks + queries) under a
  pinned `embedding_model`, with a documented regeneration script and a
  schema check — fake embeddings cannot honestly evaluate paraphrase recall
  (P8S-12). One stuffed-document case demonstrates RRF's bounded-influence
  property. **Hold-out, frozen now (P8S-12): every 5th query id is reserved
  for Phase 6 and excluded from ALL P8 tuning.** A harness scenario computes
  hit@k and MRR for lexical-only vs hybrid on the non-held-out remainder and
  feeds Phase 6 (P6-T1/P6-T5 consume it as a behavior catalog). *Acceptance:*
  lexical-only baseline recorded; hybrid beats it on the paraphrase subset;
  **hybrid hit@k ≥ lexical-only hit@k on the lexical-anchor subset**
  (paraphrase recall is not paid for with exact-match regression); held-out
  ids untouched by any tuning; fixture file is schema-checked. *Tests:*
  `test_retrieval_gold.py`. *Deps:* P8-T2, P1-T2.

- **P8-T9 — SECURITY.md guarantees.** Add: "an embedding request never
  carries content above the embedding endpoint's POST-VETO environment
  ceiling (the egress veto applies to embedding endpoints exactly as to chat
  endpoints)" (P8S-1), "an external embedding endpoint never receives a
  non-public query or chunk", "vectors never outlive or out-clear their
  chunk", "the `vectors-*` CLI surface is never exposed as a model tool on
  any surface" (structural exclusion, SH-005-style enforcer; P8S-10), "every
  embedding batch is ledgered (metadata only)". Wire each to the P8-T4/T1/T5
  tests — the egress guarantees point at the SHELL enforcer tests
  (`test_embedder_enforcer.py`); the `embedding_event` environment/ceiling
  stamp is ATTESTATION (the kernel records what the shell claims), so that
  guarantee is either scoped to "every batch is ledgered, metadata-only"
  (kernel-testable) or marked advisory for the stamp fields (P8S-15, per I6).
  *Acceptance:* `verify_enforcers()` still empty. *Tests:* extend
  `test_security_map.py`. *Deps:* P8-T4, P8-T5, P1-T1.

## Security invariants for this phase

- **Embedding = egress, veto included.** The chunk/query ceiling check
  happens at the dispatch in `embedder.py` on every request (I5), not at
  configuration time, against the chunk's CURRENT sensitivity (re-read at
  dispatch, P8S-14), under the POST-VETO ceiling (P8S-1); any error computing
  the ceiling means no request leaves (I4).
- A vector is content-equivalent to its chunk (embedding inversion is real):
  same sensitivity (always join-read from the chunk row, never copied), same
  DB, removed with the chunk in the same transaction, never exported below
  the chunk's tier (the shell handoff file is 0600 in-root and deleted),
  never included in any ledger row.
- The kernel never makes a network call; the shell never writes the index DB
  directly — vectors enter only through the `vectors-add` chokepoint, the
  query vector only through `--qvec-stdin`, and the stdin payload is composed
  exclusively by shell code — model-supplied text never reaches it (I2/I3).
- The `vectors-*` subcommands are structurally absent from every tool schema
  on every surface (P8S-10) — the model never gains bulk corpus-text export
  or vector injection.
- Minimized tiers are out of scope: minimized text (Phase 2) is NOT embedded
  in this phase under any configuration — confidential+ stays lexical-only
  even on a confined local endpoint.
- Lexical-only is not a degraded mode; it is the guaranteed floor. Every
  refusal path — policy AND transport — in this phase ends in today's exact
  lexical behavior.

## What this phase does NOT do

No reranker or cross-encoder model; no ANN/IVF index (brute force, with the
reduced-dimensions-then-int8 ladder as the only contingencies, P8S-7); no
Python-floor raise (3.10 stays); no query rewriting, HyDE, or
multi-query expansion; no change to the chunker or chunk sizes (offsets stay
the citation contract); no external vector database; no embedding of
minimized or confidential+ content; no relevance feedback loops. If hybrid
RRF + the gold fixtures show recall is still the bottleneck after P7's corpus
lands, a reranker becomes a *new* phase proposal with its own egress analysis.

## Stress pass (done 2026-06-11 — before coding, as required)

An adversarial review (security + implementation-feasibility lenses, P8S-*)
ran against the original draft AND the current post-P1/P3/P7 code; all 16
findings were adjudicated ACCEPTED and folded into the interfaces/tasks
above. The headline (P8S-1): this spec predated the egress veto
(`policy_bridge.egress_veto`, the STRESS C2/P2S-2 follow-up) — as drafted,
embedding endpoints received a strictly WEAKER classification than chat
endpoints, inverting the spec's own "embedding = egress" pin. Adjudicated
pins where the review offered options: the Python floor STAYS 3.10
(P8S-7 — installability is a product property); the zero-window branch of
P8S-14 (re-read sensitivity at dispatch); 0600 in-root handoff under
`tmp.nosync/` (P8S-10); `query_hmac` under a per-root salt in `_data.nosync/`
(P8S-9); the every-5th-query-id hold-out frozen now (P8S-12). The original
seed question "can a crafted document stuff tokens to win RRF fusion?" was
answered by design: RRF caps any single ranking's contribution at 1/(60+1)
per document — strictly less stuffing influence than today's raw bm25; a
stuffed-doc fixture case lands in P8-T8. Summary of findings and where each
landed:

| ID | Sev | Finding (one line) | Resolution |
|----|-----|--------------------|------------|
| P8S-1 | CRIT | Spec predates the egress veto: a loopback Ollama `*:cloud` EMBEDDING model classifies `local_agent` → internal chunks/queries egress to ollama.com; frozen query rule compared a pre-veto ceiling | `embedding_ceiling(root, url, model)` applies `egress_veto` after `environment_for`; recomputed per backfill run, never per-search; the query rule compares the POST-VETO ceiling; named enforcer test; doctor shows post-veto env + veto reason (frozen interfaces, P8-T4/T5/T6/T9) |
| P8S-2 | HIGH | Second-client hazard unpinned: the embed client needs its own env/guards/endpoint; contradicts the STRESS "one client per session" scope cut | separate one-purpose embed client; `_check_request_host` on the `/embeddings` URL; plaintext-key refusal against the embeddings base_url; scope-cut reconciliation note (frozen interface, P8-T3) |
| P8S-3 | HIGH | The `_do_oracle_search` wiring seam does not exist: Dispatcher is deliberately egress-free; `run_verb` has no stdin; transport failures unspecced | injected pure `query_embedder` callable; `run_verb(input=)`; stdin caps (1 MiB, ≤ 8192 dims, finite floats, never echoed); 10 s embed timeout; silent lexical on ANY transport failure (frozen interface, P8-T4) |
| P8S-4 | HIGH | `oracle_cli._translate` silently rewrites `oracle search vectors-add` into a TEXT QUERY for "vectors-add", rc 0 — the backfill no-ops forever while looking green | `_translate` allowlist extended + routing test; shell validates the `vectors-add` response shape (CLI surface, P8-T1/T5) |
| P8S-5 | HIGH | RRF rank-positional scoring leaks above-ceiling EXISTENCE via rank perturbation unless ranks are assigned post-filter | dense ranks within ceiling-FILTERED lists; ceiling applied in-scan before candidate truncation; byte-identical add/remove acceptance; corpus-global IDF residual named accepted (frozen RRF, P8-T2) |
| P8S-6 | HIGH | Vector lifecycle: deletes not pinned transactional (crash orphans); sensitivity could be copied stale into chunk_vectors; the P7 supersession path untested | single-transaction delete/upsert/`_wipe`; NO sensitivity column (join-read only); end-to-end `_remove_superseded_chunks` acceptance; doctor orphan-vector check (frozen interface, P8-T1) |
| P8S-7 | HIGH | Cosine budget unproven on the 3.10 floor (`math.sumprod` is 3.12+); the post-P7 corpus is 5–25× the 20k design point | floor STAYS 3.10; sumprod fast path + pure-Python ≤3.11 fallback; budget measured on the floor interpreter at ≥ 100k chunks; corpus-driven doctor-warned threshold; reduced dims FIRST, int8 second (justification re-pinned, P8-T2/T6) |
| P8S-8 | MED | Per-search `ledger.append` scans the whole file twice under LOCK_EX + fsync — quadratic; the read path becomes write-dependent | monthly rotation `retrieval_event-YYYYMM.jsonl`; no drop_id minting; best-effort append never fails/blocks a search (frozen ledger interface, P8-T7) |
| P8S-9 | MED | `query_sha256` of short natural-language queries is dictionary-reversible | renamed `query_hmac`: HMAC-SHA256 under a per-root salt minted on first use in `_data.nosync/` (never exported/ledgered); source_ids correlation residual named accepted (frozen ledger interface, P8-T7) |
| P8S-10 | MED | `vectors.json` via world-readable tmp leaks content-equivalent vectors; `vectors-pending` is a bulk corpus-text channel | 0600 in-root handoff under `tmp.nosync/`, deleted after vectors-add; `vectors-*` NEVER in tool schemas, SH-005-style structural enforcer (CLI surface, P8-T5/T9) |
| P8S-11 | MED | Same model name + different dim → garbage cosine or crash; zero-norm vectors divide by zero at normalization | search matches `(embedding_model, dim)`; mismatches skipped + counted in `stats().dim_mismatches`; degenerate vectors rejected at `add_vectors` (frozen interface, P8-T1/T2/T6) |
| P8S-12 | MED | Gold fixtures can't run honestly in CI (no endpoint; fake vectors can't judge paraphrase); acceptance was one-sided toward paraphrase; fixtures authored by the ranker's author | vendored precomputed pinned-model vectors + regeneration script; lexical-anchor non-regression subset; hold-out frozen: every 5th query id reserved for P6, excluded from all P8 tuning (P8-T8) |
| P8S-13 | LOW | The "derived, rebuildable" DB claim now hides a full-corpus re-egress (cost + mass re-send) on reindex/`_wipe`/DB loss | doctor coverage-collapse warning; restore/reindex ⇒ re-embed documented; kernel docstring amended; `embedding_event` makes the re-egress auditable (P8-T1/T6) |
| P8S-14 | LOW | TOCTOU: a chunk reclassified between the `pending_vectors` fetch and the embed dispatch still embeds under its old label | zero-window branch adjudicated: the enforcer re-reads each chunk's CURRENT sensitivity at dispatch (one indexed query per chunk) (frozen interface, P8-T4) |
| P8S-15 | LOW | `embedding_event`'s environment/ceiling stamp is shell ATTESTATION, not kernel enforcement — an I6 honesty gap if a guarantee cites it | egress guarantees point at the shell enforcer tests; the ledger-stamp guarantee scoped to "ledgered, metadata-only" / stamp fields advisory (P8-T9) |
| P8S-16 | LOW | Testkit has no embeddings fake; `SECURITY_KEYS` misses `provider.embeddings.*` | `FakeEmbedClient` + embedding-payload assertion as an additive P8-T4 testkit deliverable; `provider.embeddings.{api_key_env, base_url}` added to `SECURITY_KEYS` (P8-T3/T4) |

## Definition of done

- [x] Kernel vector store + hybrid RRF search (filter-before-rank dense
      ranks, `(model, dim)` matching, single-transaction vector lifecycle) +
      `--qvec-stdin` + `vectors-*` CLI incl. the `_translate` routing fix
      (upstream, re-vendored); lexical path byte-identical when no vector is
      supplied; above-ceiling chunks provably invisible AND rank-inert.
- [x] Shell `embed()` on a separate one-purpose client with full C2 posture
      on the `/embeddings` path; independent POST-VETO environment
      classification of the embedding endpoint; `provider.embeddings.*` in
      `SECURITY_KEYS`.
- [x] Egress enforcer proven: over-ceiling chunks and queries never leave,
      and the egress veto applies to embedding endpoints (named enforcer
      tests green); all refusals AND transport failures degrade to lexical.
- [x] Incremental + autonomy-gated resumable backfill with per-run post-veto
      ceiling recompute; `embedding_event` ledgered; delete/supersede removes
      vectors atomically (incl. the P7 connector supersession path); model
      change re-embeds with graceful mixed-version search.
- [x] Scorecard `retrieval` section (hit-rate, time-to-first-grounded-answer)
      live, upstream, metadata-only; salted `query_hmac`; monthly-rotated,
      never-blocking retrieval ledger.
- [x] `eval/fixtures/retrieval_gold.json` in repo with vendored pinned-model
      vectors; hybrid beats the recorded lexical baseline on the paraphrase
      subset and does not regress the lexical-anchor subset; every-5th-id
      hold-out untouched, wired for Phase 6.
- [x] SECURITY.md guarantees added and backed (attestation honestly scoped,
      P8S-15); `make check` green incl. new kernel + shell tests; CI green.

**Phase 8 code-complete 2026-06-11.** All nine tasks (T1 kernel vector store +
hybrid RRF, T2 `vectors-*` CLI + `_translate` fix, T3 shell `embed()` client,
T4 egress veto over the embedding endpoint, T5 resumable backfill, T6
delete/supersede + re-embed, T7 scorecard `retrieval` section, T8 gold
fixtures, T9 SECURITY.md) shipped and backed; `make check` green incl. the new
kernel + shell tests. One honest caveat: **the vendored gold vectors are
`synthetic-hash-v1`**, the documented seeded concept-projection from
`eval/fixtures/regen_retrieval_gold.py` — NOT a real embedding model. CI has no
embedding endpoint, so the fixture's purpose is harness wiring + lexical-anchor
non-regression, not a measurement of real paraphrase recall (the file header
says so). Real-model vectors REPLACE these the moment an embedding endpoint is
first configured: re-run the regen script against the live `/embeddings` API and
re-pin the model id. Phase 6 consumes these same synthetic vectors for its
fixture-scoped `gold_hit_at_k`/`gold_mrr` under the same caveat.
