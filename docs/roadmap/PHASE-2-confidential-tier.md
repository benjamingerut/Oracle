# Phase 2 — Confidential Tier

**Closes limit #1.** Today the ceiling caps `local_agent` at `internal` because
`allow-minimized` sensitivity tiers have no minimizer — so the oracle is mute on
its most valuable knowledge. This phase builds a real, audited minimizer and a
verified local-confinement story so a *local* model can reason over
confidential material with redaction enforced in code and recorded in the
ledger. **No path in this phase ever raises the external-model ceiling above
`public`.** That line does not move, ever. (P2-T7 below *evaluates* — on
paper, default-off, fail-closed — a separate admin-attested `enterprise`
environment tier; it is a new matrix column behind an attestation ceremony,
not a change to `external`.)

> **Amended 2026-06-11** by the phase-opening stress pass (findings P2S-1…6,
> P2F-1…7, summarized at the end of this file). The headline finding (P2S-1/2):
> a loopback endpoint can *proxy* to a cloud — on the reference dev machine,
> Ollama on 127.0.0.1:11434 serves `:cloud` models whose manifests carry
> `remote_host: https://ollama.com:443`. Network locality is NOT processing
> locality. A v1 remediation landed ahead of phase start (egress veto,
> STRESS C2 extension); this phase builds the grant machinery on top of it.

The phase opens with a validation gate (P2-T0): minimized-answer usefulness is
*measured with real local models* before the full minimizer is built. If
minimization guts the answers, building P2-T1..T6 as specced would ship a
feature nobody can use — the gate forces that discovery to cost days, not the
phase.

Read first: `docs/roadmap/ROADMAP.md`, `STRESS.md` (H2 — why allow-minimized was not a
grant), the kernel's `_tools/policy.py` (`check_processing` returns
`allow|allow-minimized|deny`).

Depends on: Phase 1 (testkit for leak-assertions, SECURITY.md map) and the v1
pre-phase remediation (landed ahead of phase start — egress veto, STRESS C2
extension: `:cloud` model-name veto + `/api/tags` `remote_host` introspection
veto wired into `build_loop` and doctor; un-introspectable non-Ollama servers
keep `local_agent` with a doctor WARN).

## The core idea

The kernel already *decides* `allow-minimized` for confidential/restricted/
secret in `local_agent`. What was missing is the thing that *performs* the
minimization. The minimizer is a kernel-side, deterministic transform that
takes a chunk + its sensitivity + a target tier and returns a redacted view
plus a record of what it removed. Because it is kernel-side, it is sovereign,
testable, and shared by every surface — and it routes through the same
chokepoints (I2). The shell's job is to (a) request minimized retrieval, (b)
verify the returned content carries a minimization receipt, and (c) refuse if
it doesn't (I4/I5).

## Frozen interfaces

### Kernel (lands upstream, re-vendored via P1-T5): `_tools/minimizer.py`
```python
def minimize(text: str, *, sensitivity: str, target_tier: str,
             ontology: dict) -> Minimized
@dataclass
class Minimized:
    text: str                  # redacted content, safe at target_tier
    removed: list[Redaction]   # spans removed, by category (name/figure/...), NO raw values
    receipt_sha256: str        # hash over (text, source_sha, target_tier,
                               # rules_version) — bound to the EXACT released
                               # bytes; an unbound receipt is replayable onto
                               # different text (P2S-3)
    rules_version: str
    source_sensitivity: str    # the chunk's ORIGINAL tier, so the shell can
                               # enforce its own confidential cap (P2S-4)
@dataclass
class Redaction:
    category: str              # "person" | "money" | "account" | "email" | ...
    count: int                 # how many removed (never the values)
```
Deterministic, stdlib-only (regex + ontology entity lists from `oracle.yml`).
Categories and rules are declared in `oracle.yml` so each company tunes them.

The minimizer is a **NEW detector** keyed on the ontology entity lists +
category regexes (person/money/account/email/date). It is explicitly NOT
derived from `secret_scan.py` (P2F-5): that module detects *credentials* —
high-entropy unbroken runs; its `_is_wordy`/character-diversity guards
deliberately suppress word-like tokens, i.e. exactly the names minimization
must catch. It may share the stdlib/regex discipline and the
redaction-excerpt helper, never the detection heuristics. Fail-closed
posture: an unrecognized-but-suspicious span (a capitalized multi-token run,
an unusual number format adjacent to a category hit) defaults to redaction;
when the rules cannot decide, the chunk is withheld — never released raw.

### Kernel CLI surface
```
oracle search query --q=... --max-sensitivity confidential --minimize-to internal
oracle answer --object X --minimize-to internal --format json
```
Real entry points (P2F-4): `--minimize-to` lands on `knowledge_index.py
query` (which already takes `--q/--k/--max-sensitivity`) and on the `answer`
implementation in `answer_protocol.py` — there is no `answer.py`, and today's
answer envelope carries only a document-level `sensitivity_ceiling`, so P2-T1
adds a per-released-chunk `minimization` block to that envelope.

When `--minimize-to T` is present and a chunk's sensitivity exceeds `T`, the
kernel returns the minimized view + a `minimization` block
(`{receipt_sha256, rules_version, source_sensitivity,
removed:[{category,count}]}`) instead of the raw chunk. Without the flag,
behavior is unchanged (raw or denied per matrix). The block carries the
SOURCE sensitivity so the shell can enforce its own cap (P2S-4).

### Shell: policy_bridge.py changes
```python
def ceiling_for(root, environment, cfg) -> Ceiling
@dataclass
class Ceiling:
    plain: str                 # highest exactly-"allow" tier — today's
                               # max_sensitivity_for logic, retained as the
                               # plain implementation (P2F-2)
    minimized: str             # capped at "confidential" for local_agent, IFF
                               # confinement verified. NOT "highest allow-minimized tier"
                               # (which would reach "secret" per the matrix) — capping at
                               # confidential is a deliberate conservative bound that avoids
                               # re-opening STRESS H2 for restricted/secret material.
def confinement_verified(cfg, root) -> bool
```
`confinement_verified` is **network locality AND processing locality**
(P2S-1/2/6) — ALL of the following, fail-closed on any error or ambiguity:

1. **Endpoint loopback** (`environment == local_agent`, literal-loopback
   only — unchanged).
2. **Egress veto** (landed ahead of phase start — egress veto, STRESS C2
   extension): the resolved `provider.model` must not be a proxy-to-cloud
   model — `:cloud` model-name veto + `/api/tags` `remote_host` introspection
   veto. The introspection source is pinned to `/api/tags` (verified:
   `/api/show` omits `remote_host` on current Ollama builds; `/v1/models`
   shows no distinguishing field at all). Introspection can only DENY, never
   grant — a malicious local proxy can lie about itself.
3. **`provider.confined_models` allowlist:** the model id must appear on an
   explicit operator allowlist; **empty ⇒ deny** (the STRESS-I2
   `ingest_roots` pattern).
4. **Per-model admin attestation — the GRANT:** an explicit, ledgered admin
   act attesting that the named model id processes on-box. Checkbox config
   is NOT sufficient; the attestation row is what unlocks `minimized`.
   Layers 1–3 (and 5–6) can only deny.
5. **Operator opt-in:** `provider.local_is_confined` is true. The flag is
   opt-in, never the grant (P2S-6).
6. **A minimizer is present in the root.**

The shell may release content above `plain` only as minimized output, only up
to `minimized`, only when `confinement_verified` is true. External endpoints:
`minimized == plain == public`, always (the function returns early).

**builder.py integration (P2F-2):** `max_sensitivity_for` is retained as the
`plain` implementation, wrapped by `ceiling_for` (the current seam in
`builder.py` passes a bare string to the Dispatcher — the rewiring is part of
the frozen interface, not an afterthought). The Dispatcher receives BOTH
fields: `plain` drives today's forced `--max-sensitivity` path when no
minimized grant exists; when `minimized > plain`, dispatch forces
`--max-sensitivity <minimized>` (the hard first layer, P2S-4) plus
`--minimize-to <plain>`. `ceiling_override` lowers **both** fields via
`min_sensitivity`. The system prompt states `plain` (what the model may see
raw) and notes minimized availability.

### Shell: verbtools.py dispatch
- `_do_oracle_search` / `_do_oracle_answer` pass `--minimize-to <plain>` when
  the ceiling allows a minimized tier above plain; in that mode the forced
  `--max-sensitivity` is `<minimized>` (= confidential) — the **hard first
  layer** (P2S-4): restricted/secret rows are never even returned. Without a
  minimized grant, `--max-sensitivity <plain>` exactly as today (the M5
  single-flag forcing is unchanged in both modes).
- **Receipt check (I5) — the second layer:** any returned chunk/envelope
  whose declared sensitivity exceeds `plain` MUST carry a `minimization`
  block whose `receipt_sha256` the shell **recomputes over the actual
  returned text** (`sha256(text ‖ source_sha ‖ target_tier ‖ rules_version)`)
  — presence alone is not verification (P2S-3) — AND whose
  `source_sensitivity` rank is ≤ `minimized` (P2S-4): a valid receipt on
  restricted/secret material is still dropped. Anything failing either check
  is dropped and substituted with the withheld stub. A model can never
  receive above-plain content lacking a verifiable, content-bound receipt.
- Every minimized release appends a `minimization_event` ledger row (kind,
  categories + bucketed counts, receipt, surface, environment) — metadata
  only.

## Tasks

- **P2-T0 — minimized-usefulness validation (phase-opening gate).** Before any
  minimizer code is built, measure whether minimized answers are *useful*:
  assemble a representative confidential Q&A fixture set (synthetic but
  realistic — names, figures, accounts, dates woven through the way real
  company documents weave them), hand-minimize it per a **frozen provisional
  category set recorded in this spec before T0 runs** (P2-T1 implements that
  same set and may not silently diverge — this breaks the T0↔T1 circularity,
  P2F-3), and run real local models against the minimized views. *Pinned
  reference model:* `qwen3.6-32k` (the class the audience will actually run
  on loopback). Results are valid **only for its 32k context regime** —
  document-length fixtures that exceed it are flagged, never silently
  truncated (a short-fixture-only set biases the verdict optimistic).
  Score answer adequacy with a written rubric (does the redacted view still
  let the model answer the question correctly, or does redaction gut the
  answer?). **Conclusion-level leakage is a rubric category, not a success
  mode (P2S-5):** a fixture where the minimized view still lets the model
  assert the confidential *conclusion* scores as leakage — entity-span
  removal alone is not the bar.
  *Judging (P2F-3):* correctness is adjudicated by ≥ 2 human raters with a
  written tie-break rule — OR by a model-judge that sees ground truth only
  under the same confinement rules as any other confidential processing,
  with a mandatory human spot-check of a sample. Never a single author
  scoring their own fixtures; fixture authorship is independent of rubric
  authorship.
  *Go/no-go criteria (explicit, recorded in this spec before coding):*
  **go** = on the fixture set, ≥ 70% (an arbitrary threshold, stamped as
  such — revisit once data exists) of questions remain answerable-correctly
  from the minimized view AND no category of question is uniformly gutted
  (operationalized: minimum N = 10 questions per category; a category is
  "gutted" if fewer than 30% of its questions survive);
  **no-go** = below threshold, in which case P2-T1..T6 do NOT proceed as
  specced — the phase pivots to re-scoping (coarser categories, alternative
  redaction strategies, or elevating the P2-T7 design decision from
  "evaluate" to "decide now"). *Acceptance:* fixture set + rubric + measured
  results checked in under `docs/eval/`; a written go/no-go verdict appended
  to this spec. *Tests:* none (measurement task); artifacts are the
  deliverable. *Deps:* P1. **Gates: P2-T1 through P2-T6.**

- **P2-T1 — kernel minimizer (upstream).** Implement `_tools/minimizer.py` —
  a NEW ontology/regex detector per the frozen interface (explicitly not
  derived from `secret_scan.py` heuristics; fail-closed on
  unrecognized-suspicious spans — P2F-5) + `oracle.yml` `minimization:`
  config (categories per the T0 frozen provisional set, rules_version) + the
  `--minimize-to` flag on `knowledge_index.py query` and the
  `answer_protocol.py` answer envelope's new `minimization` block (P2F-4).
  Deterministic, stdlib-only. Lands in the Oracle Spawn kit; re-vendored via
  P1-T5. *Acceptance:* given a confidential chunk with names/figures and
  `--minimize-to internal`, output contains no name/figure and a
  content-bound receipt carrying `source_sensitivity`; `--minimize-to`
  absent → unchanged. *Tests (kernel):* `test_minimizer.py`,
  `test_minimize_cli.py`. *Deps:* P1-T5.

- **P2-T2 — minimization ledger + lint (upstream).** A `minimization_event`
  ledger (metadata only, like `export_event`); per-category counts are
  **bucketed (1, 2-5, 6+)** — exact counts over repeated queries fingerprint
  which confidential object was touched (P2F-7); the residual side channel
  (category-sequence + surface + timestamp) is named in doctrine as an
  accepted limit. `oracle_lint` gains a doctrine→enforcer row asserting
  minimized releases are logged. *Acceptance:* a minimized query writes
  exactly one metadata row, no raw values present, counts bucketed.
  *Tests (kernel):* `test_minimization_ledger.py`. *Deps:* P2-T1.

- **P2-T3 — shell ceiling split.** Implement `Ceiling`/`ceiling_for`/
  `confinement_verified` per the frozen interface (all six confinement
  conditions: loopback, egress veto, allowlist, attestation grant, opt-in,
  minimizer presence); `external` early-returns `public` for both fields;
  `local_agent` returns `minimized` only when confinement is verified. Wire
  the builder integration (P2F-2): the Dispatcher receives BOTH `plain` and
  `minimized`; `ceiling_override` lowers BOTH. *Acceptance:* table tests —
  external→(public,public); local+all-six-verified→(internal,confidential);
  local+not-confined→(internal,internal); a `:cloud` model name OR an
  `/api/tags` `remote_host` hit→(internal,internal) regardless of every flag
  (P2S-1); empty `confined_models`→(internal,internal); missing
  attestation→(internal,internal); introspection error/timeout→
  (internal,internal). Fail-closed on any error. Builder-level test: both
  fields reach the Dispatcher; an override lowers both. *Tests:* extend
  `test_policy_bridge.py`; builder integration test. *Deps:* P2-T1 (presence
  check), P1, the pre-phase egress veto (landed).

- **P2-T4 — shell minimized dispatch + receipt enforcement.** Pass
  `--minimize-to <plain>` with `--max-sensitivity <minimized>` (the hard
  first layer, P2S-4) in search/answer when `minimized > plain`; on every
  above-plain item RECOMPUTE the receipt hash over the returned text (P2S-3)
  and enforce `source_sensitivity ≤ minimized`; drop+stub anything failing
  either check; write the shell-side awareness into the authority footer
  with honest wording — "answered from minimized confidential evidence
  (redaction removes identifiers, not inferences)" — the footer must not
  imply the answer is safe to redistribute (P2S-5). *Acceptance:* with a
  fake kernel returning above-plain content WITHOUT a receipt, the shell
  withholds it (leak-assert from P1-T2 passes); WITH a content-bound receipt
  at/under `minimized`, it is released; a valid receipt replayed onto
  DIFFERENT text is dropped (P2S-3); a valid receipt on `restricted`/`secret`
  material is dropped (P2S-4); a composition scenario — minimized-confidential
  + raw-internal in the same context — runs as a `testkit` leak scenario and
  the footer wording is asserted (P2S-5). *Tests:* extend
  `test_verbtools.py`; `testkit` leak scenarios. *Deps:* P2-T1, P2-T3, P1-T2.

- **P2-T5 — confinement doctrine + doctor.** Make `provider.local_is_confined`
  a first-class, documented, doctor-checked config setting. (Language
  corrected by the stress pass, P2F-1: the field was NOT removed — it still
  ships in `DEFAULT_CONFIG`; the S1 remediation removed only the *parameter*
  that read it. This task **wires up the existing dead field** with real
  semantics backed by the minimizer; it does not re-add it.) The flag is
  operator **opt-in, never the grant** (P2S-6): it can only deny; the grant
  is the ledgered per-model admin attestation, with introspection as the
  veto. Add the attestation ceremony (an explicit Admin-interface act
  attesting a named model id; ledgered; revocable). Doctor explains what
  confinement means, what it does/doesn't guarantee (loopback ≠ no
  forwarding — STRESS C2 / P2S-1), shows the resolved model's egress status
  from `/api/tags` and the pre-phase egress-veto verdict (not just the flag
  value), and shows the resulting plain/minimized ceilings. **Config (P2F-1,
  dep P1-T3):** add `provider.local_is_confined` and
  `provider.confined_models` to `config.SECURITY_KEYS` so migrations must
  preserve them (test plants a migration that drops one and asserts the hard
  load error). *Acceptance:* doctor on a fully-verified confined-local
  config shows `confidential` minimized ceiling; on a `:cloud`/proxy model
  shows the veto and `internal`; on external shows public/public with an
  explanation. *Tests:* extend `test_cli.py`/doctor tests +
  `test_config.py`. *Deps:* P2-T3, P1-T3.

- **P2-T6 — SECURITY.md guarantees.** Add guarantees: "external models never
  receive above-public content (incl. minimized)", "a loopback endpoint that
  proxies to a cloud never unlocks the minimized tier", "above-plain content
  reaches a model only with a verified, content-bound minimization receipt
  at/under the confidential cap", "every minimized release is ledgered".
  Wire to the P2 tests. *Acceptance:* `verify_enforcers()` still empty.
  *Tests:* `test_security_map.py`. *Deps:* P2-T2, P2-T3, P2-T4, P1-T1.

- **P2-T7 — `enterprise` environment tier (design decision, NOT a build).**
  Evaluate a third environment value alongside `local_agent`/`external`:
  `enterprise` — an admin-attested tier for external endpoints under a
  contractual zero-retention agreement (e.g. an enterprise API agreement with
  a frontier-model provider). Deliverable is an **ADR + a policy-matrix
  column spec**, explicitly NOT an implementation: the matrix gains an
  `enterprise` column on paper, with default-off / fail-closed semantics
  (absent attestation ⇒ the environment resolves to `external`, I4), and the
  ADR records the decision to build it, defer it, or reject it. The ADR must
  cover: the attestation ceremony (an explicit, ledgered admin act through
  the Admin interface — checkbox-style config is NOT sufficient; the admin
  attests to a named contract, and the attestation is what unlocks the
  column), what the tier may see (at most `internal`; the confidential line
  is out of scope for this decision), revocation, and doctor visibility.
  This task exists because it is the **only path by which frontier-quality
  models ever touch internal data** — the model-quality/confinement tradeoff
  must be confronted on paper, not smuggled in by code. **This does not move
  the external line:** `external` stays capped at `public` forever;
  `enterprise` is a *distinct* environment value that exists only after an
  explicit admin attestation ceremony, never by default and never silently.
  The ADR makes its **own** model-quality-gap argument (P2F-6: P2-T0
  measures local models on minimized views — a different model class on a
  different data regime, so its data is context, not the decision input);
  comparative frontier-model data is optional and, if gathered, uses
  synthetic fixtures ONLY (gathering it sends the fixtures off-box).
  *Acceptance:* ADR merged under `docs/adr/`; matrix-column spec with
  default-off/fail-closed semantics; zero code changes in this task.
  *Deps:* P1.

## Security invariants for this phase

- External endpoint ⇒ `minimized == plain == public`. This is checked by an
  explicit test that no `--minimize-to` above public is ever emitted on an
  external environment.
- **Confinement is processing locality, not network locality (P2S-1/2):** a
  loopback endpoint that proxies to a cloud (`:cloud` model names,
  `remote_host` manifests) never unlocks `minimized` — introspection vetoes,
  the allowlist filters, attestation grants, and anything ambiguous denies.
  The v1 egress veto (landed ahead of phase start, STRESS C2 extension)
  protects the `internal` ceiling on the same evidence.
- The minimizer is the ONLY producer of above-plain-but-releasable content; the
  shell trusts content above `plain` ONLY with a receipt whose
  `rules_version` it recognizes (unknown rules_version → withhold, I4), whose
  hash RECOMPUTES over the released bytes (P2S-3), and whose
  `source_sensitivity` is ≤ `minimized` (P2S-4).
- Redaction records and ledger rows carry categories + bucketed counts
  (1, 2-5, 6+), NEVER the removed raw values; the count/category-sequence
  side channel is a named, accepted residual (P2F-7).
- Minimization is best-effort by nature (regex/ontology); doctrine states
  plainly that it reduces but does not *prove* zero leakage — it removes
  identifiers, not inferences (P2S-5) — and therefore it is gated behind
  explicit operator opt-in (`local_is_confined`) + loopback + egress veto +
  allowlist + per-model attestation + minimizer presence — never default-on.

## Stress pass (done 2026-06-11 — before coding, as required)

Two adversarial reviews (security lens P2S-*, feasibility lens P2F-*) ran
against the original draft; all findings were adjudicated and folded into the
interfaces/tasks above. Every machine claim was verified live, not by
reading: the reference dev host's Ollama really does serve `:cloud` proxy
models from 127.0.0.1:11434 (`/api/tags` shows
`remote_host: https://ollama.com:443`); the OpenAI-compatible `/v1/models`
surface shows **no** distinguishing field; `/api/show` omits `remote_host` —
which is why the introspection source is pinned to `/api/tags`.

The draft's own stress questions are answered: a crafted chunk CAN evade a
regex/ontology minimizer (hence the fail-closed posture + doctrine honesty);
a stale receipt COULD have been replayed on different text under the original
receipt definition (hence content binding, P2S-3); an external provider can
never see `--minimize-to` raised (invariant + explicit test) — but the
dangerous variant the question missed was the *loopback proxy*, P2S-1.

| ID | Sev | Finding (one line) | Resolution |
|----|-----|--------------------|------------|
| P2S-1 | CRIT | Loopback Ollama serving `:cloud` models proxies minimized-confidential to ollama.com; the draft's `confinement_verified` could not detect it | confinement = network AND processing locality: `/api/tags` introspection veto + `confined_models` allowlist (empty ⇒ deny) + ledgered per-model admin attestation as the grant; fail-closed (frozen interface, P2-T3/T5) |
| P2S-2 | CRIT | The same proxy already breaches the v1 `internal` ceiling, unminimized | v1 remediation landed ahead of phase start (egress veto, STRESS C2 extension): `:cloud` name veto + `remote_host` introspection veto in `build_loop` + doctor; P2 builds the grant machinery on top |
| P2S-3 | HIGH | `receipt_sha256` didn't bind to the released text → replay onto different content | receipt hashes the output text; shell recomputes and compares (frozen interface, P2-T4) |
| P2S-4 | HIGH | Receipt presence-check would release restricted/secret as "minimized" past the confidential cap | two layers: `--max-sensitivity <minimized>` hard filter + `source_sensitivity ≤ minimized` at the receipt check (dispatch, P2-T4) |
| P2S-5 | HIGH | Minimization removes identifiers, not inferences; provenance inflation; composition/paraphrase restatement | conclusion-level leakage is a no-go rubric category (P2-T0); composition-attack test + honest footer wording (P2-T4); doctrine limit stated (invariants) |
| P2S-6 | MED | `local_is_confined` is operator self-attestation, not verification | flag demoted to opt-in (deny-only); the grant is the per-model attestation; doctor shows resolved egress (P2-T5) |
| P2F-1 | HIGH | "removed/reintroduce" language wrong — the field still ships in `DEFAULT_CONFIG`; absent from `SECURITY_KEYS`, so a migration could silently drop it | language corrected to "wire up the existing dead field"; `local_is_confined` + `confined_models` added to `SECURITY_KEYS` with P1-T3 dep (P2-T5) |
| P2F-2 | HIGH | `Ceiling` dataclass didn't match the `max_sensitivity_for` string seam in `builder.py`; which field feeds the dispatcher was unspecified | builder integration pinned: both fields flow to the Dispatcher; `ceiling_override` lowers both; `max_sensitivity_for` retained as the `plain` impl (frozen interface, P2-T3) |
| P2F-3 | HIGH | T0 judge unspecified (model-judging-model circularity); fixture/threshold honesty; 32k context bias; T0↔T1 category circularity | ≥ 2 human raters + tie-break (or confined model-judge + human spot-check); independent fixture authorship; `qwen3.6-32k` pinned with context-regime validity note; 70% stamped arbitrary; min N per category; frozen provisional category set (P2-T0) |
| P2F-4 | MED | Spec CLI names didn't match the kernel (`search query` vs `knowledge_index query`; no `answer.py`) | real entry points pinned: `knowledge_index.py query` + `answer_protocol.py`; `minimization` block rides the answer envelope (frozen interface, P2-T1) |
| P2F-5 | MED | `secret_scan.py` heuristics (credential/entropy, `_is_wordy`) suppress exactly what minimization must catch | minimizer is a NEW ontology/regex detector, not derived from secret_scan; fail-closed on unrecognized-suspicious spans (frozen interface, P2-T1) |
| P2F-6 | MED | P2-T7 cited T0 (local models / minimized views) as the input for a frontier-model / unminimized-data question | claim dropped; the ADR makes its own quality-gap argument; comparative data optional, synthetic-fixture-only (P2-T7) |
| P2F-7 | LOW | Exact category+count ledger sequences fingerprint which confidential object was touched | counts bucketed (1, 2-5, 6+) in P2-T2; residual named in doctrine (invariants) |

## Definition of done

- [ ] P2-T0 usefulness validation run with real local models; go/no-go verdict
      recorded in this spec (it gates everything below).
- [ ] Kernel minimizer + `--minimize-to` + ledger + lint (upstream, re-vendored).
- [ ] Shell ceiling split; external stays public/public under all inputs; a
      loopback proxy-to-cloud model never unlocks `minimized` (veto +
      allowlist + attestation proven by table tests).
- [ ] Receipt enforcement: above-plain without a valid content-bound receipt
      at/under the confidential cap is withheld (leak-assert + replay +
      source-rank tests proven).
- [ ] Doctor explains confinement + shows resolved-model egress and ceilings;
      opt-in only; `SECURITY_KEYS` extended (migrations preserve the
      confinement fields).
- [ ] SECURITY.md guarantees added and backed.
- [ ] P2-T7 `enterprise`-tier ADR + matrix-column spec merged (decision only,
      default-off/fail-closed; zero code).
- [ ] `make check` green incl. new kernel + shell tests; CI green.
