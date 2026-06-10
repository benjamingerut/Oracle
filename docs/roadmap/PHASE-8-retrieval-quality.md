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
environment×sensitivity ceiling therefore applies to embedding requests exactly
as it applies to chat requests, enforced in code at the embedding dispatch
(I5), failing closed (I4). Chunks above the embedding endpoint's ceiling simply
stay lexical-only.

Read first: `docs/roadmap/ROADMAP.md` (invariants I1–I6),
`docs/remediation/SUB-5-roadmap.md` (decision 1), the kernel's
`_tools/knowledge_index.py` (engines, sensitivity ceiling, the
`(source_id, chunk_index)` upsert + `delete_source`), `_tools/chunker.py`
(offset-exact spans — citations must keep pointing at exact source spans),
`_tools/scorecard.py` (KPI structure), the shell's
`agentloop/policy_bridge.py` (`environment_for`, `max_sensitivity_for`) and
`llm/client.py` (STRESS C2 posture: no redirects, loopback per-request guard,
plaintext-key refusal).

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
  vectors via reciprocal-rank fusion, with the sensitivity ceiling applied to
  BOTH lists before fusion.
- **Shell:** an `embed()` method on the stdlib LLM client (same C2 posture as
  `chat()`), and an embedder module that (a) computes the embedding endpoint's
  own environment×sensitivity ceiling via the policy bridge, (b) refuses at
  dispatch to embed any chunk above that ceiling, and (c) applies the frozen
  query rule below. Vectors flow back into the kernel via `vectors add`.

Hybrid hits carry the same row shape as today (`start`/`end` char offsets from
`chunker.py`), so evidence citations remain offset-exact. The chunker is not
touched.

**Why pure-Python cosine is acceptable (justification, pinned):** vectors are
stored unit-normalized as little-endian float32 BLOBs (`array('f')`), so
similarity is a dot product. At the design corpus (≤ ~20k chunks × ≤ 1536
dims) a brute-force scan is ~30M multiply-adds — a bounded few seconds worst
case in CPython, and far less on the typical single-company corpus. That is
acceptable for an interactive oracle, I1 forbids numpy, and the sensitivity
ceiling prunes the scan for low-clearance surfaces. Int8 quantization (×4
smaller, ~×2 faster) is the documented contingency if P8-T2's measured budget
is exceeded; an ANN index is explicitly out of scope (see "what this phase
does NOT do").

## Frozen interfaces

### Kernel (upstream): `_tools/knowledge_index.py` additions
```python
# chunk_vectors(source_id TEXT, chunk_index INTEGER, embedding_model TEXT,
#               dim INTEGER, norm REAL, vector BLOB,
#               PRIMARY KEY (source_id, chunk_index, embedding_model))
.add_vectors(rows) -> int        # [{source_id, chunk_index, embedding_model,
                                 #   vector: list[float]}]; upsert; float32 BLOB
.pending_vectors(*, embedding_model, max_sensitivity=None, limit=None)
                                 # chunks lacking a vector for that model,
                                 # optionally ceiling-filtered (convenience —
                                 # the shell enforcer re-checks at dispatch)
.search(query, *, k=10, max_sensitivity=None,
        query_vector=None, embedding_model=None) -> list[dict]
                                 # query_vector present -> RRF fusion; absent
                                 # -> today's lexical path, byte-identical
.stats()                         # gains: vectors, vector_coverage,
                                 #        by_embedding_model
```
`delete_source` and `_wipe` remove `chunk_vectors` rows too (vectors are
content-equivalent — they must never outlive their chunk). RRF, frozen:
`score(d) = Σ_r 1/(60 + rank_r(d))` over the lexical and vector rankings, each
ceiling-filtered BEFORE fusion; the existing `_apply_rerank` authority/recency
boost multiplies the fused score, unchanged. Search compares only vectors
whose `embedding_model` matches the query vector's model (mixed-version
corpora degrade gracefully: uncovered chunks still compete lexically).

### Kernel CLI surface
```
oracle search vectors-add    --file vectors.json [--embedding-model M ...]
oracle search vectors-pending --embedding-model M [--max-sensitivity S] [--limit N]
oracle search query --q "..." [--qvec-stdin]   # stdin: {"embedding_model": M,
                                               #         "vector": [...]}
oracle search vectors-prune  --keep-model M    # drop superseded-model vectors
```
The query vector travels on stdin, never argv (size, ps-visibility).
`index_meta` records the ACTIVE `embedding_model`; changing it makes every
chunk "pending" for the new model without deleting old vectors until pruned.
Each `vectors-add` appends one `embedding_event` ledger row — **metadata
only**: source_id, chunk count, embedding_model, the caller-stamped endpoint
environment and applied ceiling; never text, never vectors.

### Kernel (upstream, flagged): `_tools/scorecard.py` + ledgers
New `RETRIEVAL_LEDGER = "Meta.nosync/ledgers/retrieval_event.jsonl"` — one
metadata row per search (ts, drop_id, query_sha256 — hash only, never query
text — k, engine, hybrid flag, vector_coverage, result_count, top source_ids).
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
    # chat(): _NoRedirect, plaintext-key refusal, _check_request_host for
    # local_agent clients, key never in errors; classify_error reused.
```
Config: a `provider.embeddings` block (`{model, base_url?, api_key_env?}`);
`base_url`/key default to the chat endpoint's. The embedding endpoint's
environment is classified **independently** via
`policy_bridge.environment_for(embed_base_url)` — a local chat model with an
external embedder must NOT inherit `local_agent`.

### Shell: `agentloop/embedder.py` (new)
```python
def embedding_ceiling(root, embed_base_url) -> str
    # max_sensitivity_for(root, environment_for(embed_base_url)); any error
    # -> "public" (fail closed, same as the bridge)
def embed_pending(client, root, *, ceiling, batch=64, limit=None) -> dict
    # pending -> ENFORCER: drop any chunk whose sensitivity exceeds ceiling
    # (re-checked here per chunk, regardless of what pending returned) ->
    # embed -> vectors-add. Idempotent + resumable by construction (the
    # pending set shrinks monotonically; keyed upsert).
def query_vector_allowed(retrieval_ceiling, embed_ceiling, order) -> bool
    # rank(retrieval_ceiling) <= rank(embed_ceiling)
```

### The frozen query rule (security pin, spelled out)

A retrieval query may itself contain internal content (the user quotes a
figure, names a customer). The query text is therefore conservatively
classified **at the surface's retrieval ceiling** — the `max_sensitivity` the
search runs with in `verbtools._do_oracle_search`. Frozen rule: **the query is
embedded — and vector search participates at all — iff
`rank(retrieval_ceiling) <= rank(embedding endpoint's ceiling)`.** An
external-classified embedder (ceiling `public`) therefore disables vector
search entirely for every internal-and-above surface, and retrieval falls back
to lexical, silently and correctly. No truncation, no "embed a sanitized
query" cleverness — the rule is a comparison of two labels and an early
return. Symmetric consequences, both fail closed:

| | external embedder (ceiling `public`) | local_agent embedder (ceiling e.g. `internal`) |
|---|---|---|
| chunk embedding | PUBLIC chunks only | chunks at/below `internal`; confidential+ stay lexical-only |
| query embedding | only surfaces whose retrieval ceiling is `public` | surfaces at/below `internal` |

## Tasks

- **P8-T1 — kernel vector store (upstream).** `chunk_vectors` table +
  `add_vectors`/`pending_vectors`/stats coverage + removal wired into
  `delete_source`, `_wipe`, and the `(source_id, chunk_index)` upsert (a
  re-ingested chunk's stale vector is deleted with the old row — a reclassified
  chunk must not keep a vector minted under its old sensitivity). float32 BLOB
  via `array('f')`, norm stored, stdlib only, both engines. *Acceptance:*
  add/delete/reindex round-trips; `delete_source` leaves zero vector rows;
  upserting a chunk drops its old vector; stats reports per-model coverage.
  *Tests (kernel):* `test_knowledge_vectors.py`. *Deps:* P1-T5.

- **P8-T2 — kernel hybrid search + RRF (upstream).** `search(...,
  query_vector=...)`: brute-force cosine over same-model vectors, RRF(k=60)
  fusion with the lexical list, ceiling filter on both lists pre-fusion,
  `_apply_rerank` post-fusion; `--qvec-stdin` on `query`. Measure and record a
  latency budget (≤ 2 s at 20k×1024 on a laptop; if missed, implement the int8
  contingency in this task). *Acceptance:* without a query vector the output is
  byte-identical to today; with one, a paraphrase fixture query ranks the
  semantically-matching chunk first; an over-ceiling chunk never appears even
  when it is the best cosine hit; hits keep exact `start`/`end` offsets.
  *Tests (kernel):* `test_hybrid_search.py`. *Deps:* P8-T1.

- **P8-T3 — shell embeddings client.** `LLMClient.embed()` +
  `provider.embeddings` config block + independent environment classification
  of the embedding base_url. Full C2 posture inherited and tested (redirect
  refused, local_agent per-request loopback guard, plaintext-key refusal, key
  absent from all errors). *Acceptance:* against a fake `/v1/embeddings`
  server, batches round-trip; a 302 raises; a `local_agent`-classified client
  refuses a non-loopback URL at send time. *Tests:* `test_embed_client.py`.
  *Deps:* P1 (none hard; parallel with T1/T2).

- **P8-T4 — embedding egress enforcer (the security pin).**
  `agentloop/embedder.py`: `embedding_ceiling`, the per-chunk dispatch check,
  and `query_vector_allowed`; wire `_do_oracle_search` to compute the query
  vector only when the rule allows and pass it via `--qvec-stdin`, else run
  lexical exactly as today. Every refusal path is silent-but-correct
  degradation, never an error surfaced to the model. *Acceptance:* with a fake
  embed client recording requests — an internal chunk is NEVER in any request
  when the embedder ceiling is public; an internal-surface query is never
  embedded by a public-ceiling embedder (vector search disabled, lexical
  results still returned); ceiling-computation error → zero embedding requests.
  *Tests:* `test_embedder_enforcer.py` (the named enforcer tests:
  `test_embed_dispatch_blocks_over_ceiling_chunks`,
  `test_external_embedder_disables_vector_search_above_public`,
  `test_ceiling_error_fails_closed_no_egress`). *Deps:* P8-T3.

- **P8-T5 — incremental embedding + backfill loop.** After a shell-initiated
  ingest, run a bounded `embed_pending` pass for the touched source; a
  scheduler-tick backfill drains the rest of the corpus in batches, gated by
  `scheduler.autonomy_enabled(root)` and resumable across ticks (it is just
  `embed_pending` with a limit). Every batch lands one `embedding_event` row.
  *Acceptance:* a fresh corpus reaches full coverage across N ticks; killing
  mid-backfill loses at most one batch and resumes; autonomy off → zero
  embedding requests; ledger rows are metadata-only. *Tests:*
  `test_embed_backfill.py`. *Deps:* P8-T1, P8-T3, P8-T4.

- **P8-T6 — model-change re-embed + mixed-version degradation.** Switching
  `provider.embeddings.model` updates `index_meta`, re-pends every chunk,
  backfill re-embeds under the new model; `vectors-prune` drops the old;
  search never fuses across models. Doctor + dashboard show embedding
  environment, ceiling, model, and coverage. *Acceptance:* mid-migration (50%
  coverage) hybrid search uses new-model vectors only and still returns
  lexical hits for uncovered chunks; doctor on an external embedder config
  states plainly that internal+ surfaces are lexical-only and why. *Tests:*
  extend `test_knowledge_vectors.py`, doctor tests. *Deps:* P8-T1, P8-T5.

- **P8-T7 — retrieval KPIs (upstream, flagged).** `retrieval_event` ledger,
  `answer_event.source_ids`, and the scorecard `retrieval` section
  (retrieval_hit_rate, time_to_first_grounded_answer, coverage/share numbers)
  per the frozen interface above; composite formula untouched (KPI addition,
  not re-weighting). *Acceptance:* synthetic ledgers yield the expected
  hit-rate and median; query text never appears in any row (hash only); old
  scorecards without the section still parse. *Tests (kernel):*
  `test_scorecard_retrieval.py`, `test_retrieval_ledger.py`. *Deps:* P8-T2,
  P1-T5.

- **P8-T8 — gold retrieval fixtures + eval wiring.** A small in-repo fixture
  set `eval/fixtures/retrieval_gold.json`: a synthetic public-only corpus
  (secret-scan clean) + ~25 query→expected-source_id pairs, including
  paraphrase cases lexical search demonstrably misses. A harness scenario
  computes hit@k and MRR for lexical-only vs hybrid and feeds Phase 6
  (P6-T1/P6-T5 consume it as a behavior catalog). *Acceptance:* lexical-only
  baseline recorded; hybrid beats it on the paraphrase subset; fixture file is
  schema-checked. *Tests:* `test_retrieval_gold.py`. *Deps:* P8-T2, P1-T2.

- **P8-T9 — SECURITY.md guarantees.** Add: "an embedding request never carries
  content above the embedding endpoint's environment ceiling", "an external
  embedding endpoint never receives a non-public query or chunk", "vectors
  never outlive or out-clear their chunk", "every embedding batch is
  ledgered (metadata only)". Wire each to the P8-T4/T1/T5 tests.
  *Acceptance:* `verify_enforcers()` still empty. *Tests:* extend
  `test_security_map.py`. *Deps:* P8-T4, P8-T5, P1-T1.

## Security invariants for this phase

- **Embedding = egress.** The chunk/query ceiling check happens at the
  dispatch in `embedder.py` on every request (I5), not at configuration time;
  any error computing the ceiling means no request leaves (I4).
- A vector is content-equivalent to its chunk (embedding inversion is real):
  same sensitivity, same DB, removed with the chunk, never exported below the
  chunk's tier, never included in any ledger row.
- The kernel never makes a network call; the shell never writes the index DB
  directly — vectors enter only through the `vectors-add` chokepoint (I2/I3).
- Minimized tiers are out of scope: minimized text (Phase 2) is NOT embedded
  in this phase under any configuration — confidential+ stays lexical-only
  even on a confined local endpoint.
- Lexical-only is not a degraded mode; it is the guaranteed floor. Every
  refusal path in this phase ends in today's exact lexical behavior.

## What this phase does NOT do

No reranker or cross-encoder model; no ANN/IVF index (brute force, with int8
quantization as the only contingency); no query rewriting, HyDE, or
multi-query expansion; no change to the chunker or chunk sizes (offsets stay
the citation contract); no external vector database; no embedding of
minimized or confidential+ content; no relevance feedback loops. If hybrid
RRF + the gold fixtures show recall is still the bottleneck after P7's corpus
lands, a reranker becomes a *new* phase proposal with its own egress analysis.

## Stress pass (before coding)

Run the STRESS.md discipline against this design first; append findings here.
Seed questions: can a chunk re-ingested at a *higher* sensitivity retain a
vector minted under the old label (upsert/delete coverage)? Can the embedding
endpoint be swapped to a non-loopback host between classification and a
backfill batch (per-request guard parity with chat)? Can a crafted document
stuff tokens to win RRF fusion and displace authoritative sources past the
`_apply_rerank` boost? Does `query_sha256` in `retrieval_event` leak anything
under a dictionary attack on short queries (consider salting per root)? Can
the gold fixture set be gamed by tuning to it (hold-out split for P6)? Does a
half-pruned mixed-model index ever fuse incomparable cosine spaces?

## Definition of done

- [ ] Kernel vector store + hybrid RRF search + `--qvec-stdin` +
      `vectors-*` CLI (upstream, re-vendored); lexical path byte-identical
      when no vector is supplied.
- [ ] Shell `embed()` with full C2 posture; independent environment
      classification of the embedding endpoint.
- [ ] Egress enforcer proven: over-ceiling chunks and queries never leave
      (named enforcer tests green); all refusals degrade to lexical.
- [ ] Incremental + autonomy-gated resumable backfill; `embedding_event`
      ledgered; delete/supersede removes vectors; model change re-embeds with
      graceful mixed-version search.
- [ ] Scorecard `retrieval` section (hit-rate, time-to-first-grounded-answer)
      live, upstream, metadata-only.
- [ ] `eval/fixtures/retrieval_gold.json` in repo; hybrid beats the recorded
      lexical baseline on the paraphrase subset; wired for Phase 6.
- [ ] SECURITY.md guarantees added and backed; `make check` green incl. new
      kernel + shell tests; CI green.
