# Phase 3 — Forced Grounding

**Closes limit #2.** Today the authority footer makes *labeling* honest — a
model that skips the answer protocol gets tagged "conversational" — but nothing
*forces* it to consult the protocol before asserting a company fact. A
confident model can still emit an ungrounded claim in prose; only the footer
betrays it. This phase makes grounding structurally unavoidable for material
assertions, on every surface, enforced in code (I5).

Read first: `docs/roadmap/ROADMAP.md`, `docs/DESIGN.md` (D5), the kernel
`answer_protocol.py` (envelope shape, verdicts 0/2/3/4).

Depends on: Phase 1. Composes with Phase 2 (minimized answers carry envelopes).

## The core idea

Grounding cannot be a prompt instruction (the model can ignore it) and cannot
be a post-hoc footer (the claim already shipped). It must be a **gate between
the model's draft answer and the user**: a deterministic step that detects
material company claims in the draft, checks each against an answer-protocol
envelope obtained this turn, and forces a *repair loop* when a claim is
unbacked — the model is sent back with the specific objects it asserted without
grounding and must either ground them (call `oracle_answer`) or retract them.
Only a draft whose every material claim is backed (with the deterministic
footer carrying its verdict's label) is released.

This is the same philosophy as the kernel's `standing_deliverables` claim-gate
(which already gates briefings), lifted to interactive answers.

## Frozen interfaces

### `oracle_agent/agentloop/grounding.py` (new)
```python
@dataclass
class ClaimCheck:
    claims: list[Claim]            # material company claims found in the draft
    unbacked: list[Claim]          # claims with no covering envelope this turn
    mismatched: list[Claim]        # claims asserting on a refused-class or withheld envelope
@dataclass
class Claim:
    text: str                      # the asserting claim unit (sentence/list item/table row)
    object_guess: str | None       # best-effort business object it concerns
def known_objects(root: Path) -> list[str]    # truth-map object names, read server-side
def extract_claims(draft: str, *, objects_seen: list[str]) -> list[Claim]
def check_grounding(draft: str, envelopes: list[dict], *,
                    objects_seen: list[str]) -> ClaimCheck
def repair_prompt(check: ClaimCheck) -> str   # the message sent back to the model
```
- **Materiality predicate (pinned, P3S-17):** a claim unit is material iff it
  is a declarative unit AND (it references an object in `objects_seen` OR it
  asserts a figure/date/named entity as company fact). `extract_claims` is
  deterministic and conservative, tuned to favor *recall* (catch real claims)
  over precision; false positives cost an extra repair turn, not a leak.
- **Claim units, fail-closed (P3S-3, P3S-15):** the extraction unit is the
  sentence, list item, or table row. Quoted text, list items, table rows, and
  code-block content ARE extracted — exempting them would be a smuggling
  channel ("as the report says, '…'"). Hedge words ("I believe", "probably")
  do NOT exempt a unit that references a known object or carries figures/
  dates; hedging satisfies obligations only via a covering envelope. Accepted
  cost: a user who makes the model echo numbers buys repair turns, bounded by
  the repair budget and the gateway repair telemetry (P3-T4).
- **`objects_seen` sourcing (P3S-5):** `known_objects(root)` enumerates
  truth-map object names server-side — via the vendored `truth_map.load_rows`
  reader or a new `oracle truth objects --json` verb (pick at implementation;
  either way handle kernel version skew per STRESS A6). The list never enters
  model context, so STRESS H1 holds. `objects_seen` for both functions =
  truth-map objects ∪ envelope objects obtained this turn.
- **Coverage semantics (P3S-2):** a claim is covered ONLY by an envelope whose
  `business_object` equals the claim's `object_guess` under
  `truth_map.normalize_object` **equality** — never substring containment (an
  envelope for "company" must not cover "company revenue"). A claim with
  `object_guess is None` is ALWAYS unbacked (fail-closed); the repair prompt
  tells the model to name the object or retract.
- **Verdict obligations (P3S-6, decided):** grounded → plain assert ok;
  supported and caveated → satisfied by the deterministic authority footer
  (which already names verdict + object, SPEC S5/D5) — prose is NOT scanned
  for label strings; refused-class or **withheld** envelope with an assertion
  → `mismatched`. `mismatched` means exactly that, nothing subtler.
- **Governing envelope (P3S-13):** when one object has several envelopes this
  turn, the LATEST governs (matches the kernel's re-run-after-ingest upgrade
  semantics; the footer still lists all). Verdict is read from the envelope's
  `exit_code` field, falling back to the `verdict` string.
- **Withheld envelopes (P3S-1):** `verbtools._do_oracle_answer` is amended to
  set `"withheld": true` on any envelope whose payload was withheld above the
  ceiling. The checker treats withheld as refused-class: the model never saw
  the grounded payload, so a grounded `exit_code` must not certify whatever
  it asserts about the object.
- **Fail-closed gate (P3S-8):** any exception in extract/check/repair-prompt
  withholds the ENTIRE reply (generic notice + footer) — never releases the
  draft ungated. Extractor regexes must be linear-time (no nested
  quantifiers); drafts are attacker-influenced input.
- **Footer-lookalike stripping (P3S-18):** body lines matching the authority
  footer prefix (`— authority:` / `— conversational`) are stripped from the
  draft, so a model cannot spoof the deterministic footer in prose.

### `agentloop/loop.py` integration
```python
class AgentLoop:
    def __init__(self, ..., grounding: GroundingPolicy): ...   # REQUIRED, no default
```
- **Builder is the sole decision point (P3S-9):** `grounding` has NO default —
  `builder.build_loop` decides it for every caller. `surface == "gateway"` →
  ENFORCE hard-coded in the builder, ignoring config entirely; local → the
  single config key (below). A direct constructor caller cannot inherit a
  security-meaningful default by accident, and the mode is fixed at
  construction — no tool output, prompt injection, or config mutation can flip
  it mid-session (it is an instance attribute set once; no exposed verb writes
  config).
- After the model returns a content-only response, run `check_grounding` on
  the draft BEFORE the footer is appended. The gate changes prose only; the
  footer remains derived solely from the accumulated `envelopes` list
  (including envelopes obtained during repair turns) — gating never changes
  footer inputs (P3S-14).
- If `unbacked` or `mismatched` is non-empty and repair budget remains
  (`max_repair=2`), append `repair_prompt(check)` as a user turn (tools
  RE-ENABLED so it can call `oracle_answer`) and loop.
- **Shared per-turn budget (P3S-7):** repair turns SHARE the original turn's
  `max_iterations` budget — one global per-turn LLM-call ceiling, never a
  fresh allotment per repair. A per-turn wall-clock ceiling (gateway: 120s,
  aligned with `Dispatcher.timeout`) applies on top; hitting either ceiling →
  redact-and-notice immediately. This is the P1S-13-class interaction: the
  whole turn runs under the per-root `LOCK_EX`, so a repair storm must not
  stall other users of the root or the single-threaded serve loop.
- **Cap-exhausted turns (P3S-12):** the iteration-cap forced answer runs with
  tools disabled, so it cannot repair — it goes STRAIGHT to redact-and-notice,
  consuming no repair budget.
- **Redaction mechanics (P3S-14):** on exhausted budget, re-run extract+check
  on the FINAL draft and remove its unbacked/mismatched claim units whole
  (sentence / list item / table row — never partial, so markdown stays
  intact), then append the notice: "[N claim(s) withheld: not grounded — ask
  the operator to ingest evidence or promote authority]". The kernel
  `suggested_fix` lines appear once, in the footer (exit-4 envelopes already
  carry them there); the notice carries only the count. A fully-redacted
  reply ships notice + footer alone. The user never receives an unbacked
  material claim.
- **Repair turns and eviction (P3S-19):** repair user-turns carry a sentinel
  tag and the evictor treats a question + its repair chain as ONE turn group,
  so eviction can never drop the original question while keeping an orphaned
  repair fragment (extends the STRESS I1 group-eviction rule).
- `GroundingPolicy.OBSERVE` (footer-only, the v1 behavior) remains available
  for local-operator chat where the admin explicitly wants raw model output;
  `ENFORCE` is the only mode on the gateway. Which mode is the *default* per
  surface is governed by the budget gate below (gateway: ENFORCE always;
  local: per P3-T7).

### Surface defaults
- **Scope (P3S-16):** the gate covers `AgentLoop` surfaces only — local chat
  (REPL and `oracle chat -m` one-shots) and the gateway. Scheduled
  deliverables never pass through `AgentLoop`; they remain gated by the
  kernel's `standing_deliverables` claim-gate (no double gate). Relayed tool
  content (a model paraphrasing `oracle_brief` or `oracle_search` output)
  still requires `oracle_answer` envelopes per object — fail-closed; the
  brief's per-claim verdicts arrive as text, not envelopes, so the model must
  re-ground what it re-asserts. The repair cost of this is real and is
  measured by P3-T7.
- gateway / external: `ENFORCE`, no override — from day one. **Rollout is
  gateway-first**: the gateway is where unbacked claims reach other people, so
  it never waits on the budget gate below. There is NO gateway grounding key
  in the config schema (P3S-11): the mode is code (hard-coded in the builder),
  structurally beyond the reach of config migration, prompt injection, or
  tool output.
- local chat: `ENFORCE` becomes the default **only after the P3-T7 budget gate
  passes**; until then local chat defaults to `OBSERVE` (the v1 footer-only
  behavior) with `oracle chat --grounding enforce` available as opt-up. Once
  the gate passes, the default flips to `ENFORCE` and
  `oracle chat --grounding observe` remains the logged operator opt-down.
- **Config plumbing (P3S-11):** the local default lives in ONE config key,
  `chat.grounding_default`, added to `SECURITY_KEYS` so a migration can never
  silently flip an operator's deliberate ENFORCE back to OBSERVE (P1-T3
  preservation check; migration test required). The `--grounding` flags log
  to a stderr banner line plus a metadata-only ledger row on the instance
  root (mode name only, never message bodies).

### Scope/budget note — when ENFORCE may become a default

ENFORCE costs more than the extractor's milliseconds: every repair turn is an
**extra model round-trip**, with its tokens and its seconds, and a
false-positive claim flag buys that cost for nothing. So ENFORCE may become a
default on any surface beyond the gateway only after both budgets are
*measured on real traffic* (not synthetic fixtures):

- **False-positive budget:** the rate at which `extract_claims` flags
  sentences that a human reviewer judges non-material (each one triggers a
  pointless repair turn).
- **Added-latency budget:** end-to-end added cost per turn, counting the
  repair loop's extra model round-trips in **tokens and wall-clock seconds**
  — not just the extractor/checker milliseconds that P3-T5 benchmarks.

**Budget numbers (set at the 2026-06-11 stress pass, P3S-10; P3-T7 measures
against them):**

- *False-positive budget:* ≤ 5% of flagged claim-units judged non-material by
  the operator, AND ≤ 10% of turns incur a repair round-trip caused solely by
  false positives.
- *Added-latency budget:* p50 added wall-clock ≤ 0.5s (no-repair path); p95
  added ≤ +1 model round-trip; mean added tokens per turn ≤ +20%.
- *Observation window:* ≥ 50 real turns across ≥ 7 days.

## Tasks

- **P3-T1 — claim extractor.** Implement `extract_claims` + `known_objects`
  (deterministic, stdlib, linear-time regexes). Build a labeled fixture corpus
  (drafts → expected claims) covering figures, dates, named systems, hedged vs
  asserted, and pure conversational text — where "purely conversational" is
  itself a labeled fixture class and materiality follows the pinned predicate
  above. The corpus MUST include named adversarial classes (P3S-3/15/17/18):
  *table-smuggle, list-smuggle, quote-smuggle, code-block-smuggle,
  hedge-smuggle, non-English clause, footer-lookalike*. Tune for recall.
  *Acceptance:* on the corpus, recall ≥ 0.95 on planted material claims
  overall AND per adversarial class (per-class recall reported), and zero
  claims flagged in the purely-conversational fixture class. Phase 6's
  independent eval re-measures. *Tests:* `test_grounding_extract.py` + corpus.
  *Deps:* P1.

- **P3-T2 — grounding checker.** Implement `check_grounding` mapping claims to
  envelopes under the pinned coverage semantics (normalize-equality, latest
  envelope per object, `exit_code` with `verdict` fallback) and the decided
  obligation model (footer satisfies supported/caveated). *Acceptance:* a
  grounded envelope backs a plain assertion about ITS object only; an
  assertion on a refused envelope → mismatched; an assertion on a
  `withheld: true` envelope → mismatched (P3S-1, with the matching
  `verbtools._do_oracle_answer` amendment + test); no envelope → unbacked;
  `object_guess is None` → unbacked; an envelope for "company" does not cover
  "company revenue" (equality, not containment); two envelopes for one object
  → the latest governs. *Tests:* `test_grounding_check.py`. *Deps:* P3-T1.

- **P3-T3 — repair loop in AgentLoop.** Wire `GroundingPolicy` (required
  constructor arg, builder decides), the repair turn (tools re-enabled), the
  `max_repair` budget SHARED with `max_iterations` plus the wall-clock
  ceiling, and the final redact-and-notice fallback (claim-unit redaction on
  the final draft; fully-redacted reply ships notice + footer alone).
  Preserve the v1 message-pairing invariant (STRESS I1: every tool-call
  message must remain paired with its tool-reply; repair turns are appended
  as new user/assistant groups, never spliced mid-pair) and the P3S-19
  eviction rule (repair user-turns tagged; question + repair chain evicted as
  one group). Cap-exhausted forced answers go straight to redaction (P3S-12).
  Any gate exception withholds the whole reply (P3S-8). Testkit needs
  repair-aware scripting: a `ScriptedResponse` entry is consumed per repair
  turn so a fake model can script assert → repair → ground sequences.
  *Acceptance:* a scripted model that asserts ungrounded → repair turn → calls
  `oracle_answer` → grounded release; a stubborn model that never grounds →
  offending claim units redacted, notice + fix shown, no unbacked claim in
  the output; a model that burns the full iteration budget then asserts →
  straight redaction, no repair; a raising extractor → whole reply withheld;
  total LLM calls per turn never exceed `max_iterations`. *Tests:*
  `test_grounding_loop.py` via testkit. *Deps:* P3-T2, P1-T2.

- **P3-T4 — surface wiring + override.** Gateway forces `ENFORCE` hard-coded
  in `builder.build_loop` (no override path, no config key) — gateway-first
  rollout. Local chat: default per the budget gate (`OBSERVE` until P3-T7
  passes, then `ENFORCE`) read from `chat.grounding_default`, which is added
  to `SECURITY_KEYS` with a migration-preservation test (P3S-11);
  `--grounding enforce` / `--grounding observe` flags on local only, both
  logged (stderr banner + metadata-only ledger row). The gateway ledger row
  gains repair telemetry: `repairs: n` and added seconds per turn (P3S-3/7),
  plus an optional `gateway.per_user_repairs_per_hour` cap. *Acceptance:*
  building via the builder with `surface="gateway"` and a config attempting
  observe still yields ENFORCE (carried as a `security_map` test-enforced
  guarantee); local `--grounding observe` produces the v1 footer-only
  behavior; the local default is a single config point that P3-T7's outcome
  flips; a migration that drops or alters `chat.grounding_default` is
  refused. *Tests:* extend `test_telegram.py`, `test_cli.py`,
  `test_config.py`. *Deps:* P3-T3.

- **P3-T5 — performance guard.** The extractor + checker run on every turn;
  ensure they add negligible latency (pure-Python, no model call). Add a
  micro-benchmark test on a typical draft AND on adversarial pathological
  inputs (one very long sentence, a 10k-row table, deeply nested markdown) —
  the regexes must stay linear-time under attacker-shaped drafts (P3S-8).
  Bound is generous for shared CI runners: assert < 50ms, not "a few ms"
  (timing tests flake otherwise). *Acceptance:* benchmark within bound on
  both typical and pathological inputs; no network/model calls in the
  grounding path. *Tests:* `test_grounding_perf.py`. *Deps:* P3-T2.

- **P3-T6 — SECURITY.md guarantee.** "No material company claim is released
  to any user without a covering answer-protocol envelope **for its business
  object** whose obligations the text honors (gateway: no override)." Wire to
  P3 tests. The wording is deliberate (P3S-4): the gate forces protocol
  invocation and verdict-obligation compliance per object — it does not
  verify the asserted proposition (see invariants). *Acceptance:*
  `verify_enforcers()` empty. *Deps:* P3-T3, P1-T1.

- **P3-T7 — ENFORCE-default budget measurement (gates the default flip).**
  Run ENFORCE in shadow/logged mode on real local-operator traffic for the
  pinned observation window; measure (a) the false-positive rate of
  `extract_claims` against human judgment of flagged claim-units, and (b) the
  added cost per turn including repair-loop model round-trips in tokens and
  wall-clock seconds. **Shadow capture (P3S-10):** FP labeling needs the
  flagged claim text, which the metadata-only ledgers must never carry — so
  shadow samples land in a dedicated local-only, operator-consented file
  under `profile_dir()` (`grounding_shadow.jsonl`: flagged claim text +
  verdict), explicitly excluded from backups (P1-T6 G5) and never written on
  any gateway path; OBSERVE-mode local traffic only. *Acceptance:* both
  measurements within the budgets in the scope note above → local default
  flips to ENFORCE (one config change, P3-T4); either budget exceeded →
  default stays OBSERVE and the extractor is retuned before re-measuring.
  The measurement report is checked in under `docs/eval/`. The gateway is
  NOT gated by this task — it runs ENFORCE regardless. *Tests:* none
  (measurement task); the report is the deliverable. *Deps:* P3-T4, P3-T5.

## Security / correctness invariants

- The grounding gate is deterministic and runs server-side (in the shell), not
  in the model — the model cannot disable it (I5). The mode is fixed at loop
  construction by the builder; no tool output, prompt injection, or config
  read mid-session can flip it (P3S-9/11).
- Fallback is redaction, not release: on exhausted repair budget the unbacked
  text is removed, never shipped with a disclaimer-and-hope. A gate exception
  withholds the entire reply — fail closed, never fail open (P3S-8).
- **Honest limit (P3S-4):** the gate forces protocol invocation and
  verdict-obligation compliance — it is object-level, not proposition-level.
  A model can still misstate a grounded object's value; the grounded payload
  is in its context, but nothing diffs the assertion against it.
  Proposition-level verification is Phase 6/8 eval territory.
- A withheld envelope never backs an assertion: `withheld: true` is
  refused-class (P3S-1).
- The extractor errs toward flagging; a missed claim is the failure mode to
  drive toward zero, measured by Phase 6's eval.
- `OBSERVE` mode is local-operator-only, logged, and can never be reached from
  the gateway or any external surface. The local default key is
  SECURITY_KEYS-protected; the gateway has no grounding key at all.

## Stress pass (done 2026-06-11 — before coding, as required)

One adversarial review (security + feasibility lenses, P3S-*) ran against the
original draft and the real shipped code (`loop.py`, `verbtools.py`, the
kernel `answer_protocol.py` / `standing_deliverables.py`, `telegram.py`,
`config.py`, `builder.py`); all findings were adjudicated and folded into the
interfaces/tasks above. Summary of accepted findings and where each landed:

| ID | Sev | Finding (one line) | Resolution |
|----|-----|--------------------|------------|
| P3S-1 | CRIT | Withheld-above-ceiling envelope (grounded rc, payload withheld) certifies fabricated answers | `withheld: true` marking in `_do_oracle_answer`; checker treats as refused-class; security_map guarantee (interfaces, P3-T2) |
| P3S-2 | CRIT | `object_guess=None` coverage + match predicate unpinned → any-envelope bypass or undefined FP behavior | `None` → always unbacked (fail-closed); coverage = `normalize_object` equality, never containment (interfaces, P3-T2) |
| P3S-3 | CRIT | Quotes/lists/tables/code: exempt = smuggling channel, extract = echo-DoS — undecided | Extract fail-closed everywhere; repair telemetry in gateway ledger + optional per-user repair cap; smuggle fixture classes (interfaces, P3-T1/T4) |
| P3S-4 | HIGH | Object-level envelope ≠ proposition truth; P3-T6 guarantee overclaimed | Honest limit stated in invariants; guarantee reworded "for its business object" (P3-T6) |
| P3S-5 | HIGH | `objects_seen` had no data source; qualitative object claims dodge extraction | `known_objects(root)` server-side truth-map enumeration (H1-safe, A6 skew noted); `check_grounding` signature threaded (interfaces, P3-T1) |
| P3S-6 | HIGH | "Caveat the envelope gives" unimplementable — `Envelope` has no caveat text field | Decided (a): supported/caveated obligations satisfied by the deterministic footer; `mismatched` = asserts on refused-class/withheld only (interfaces, P3-T2) |
| P3S-7 | HIGH | Repair loop under per-root `LOCK_EX`; per-repair iterations unbounded (≤60 LLM calls/turn) | Repairs share the turn's `max_iterations` budget + wall-clock ceiling (gateway 120s); ledger telemetry (loop integration, P3-T3/T4) |
| P3S-8 | HIGH | Gate exception/ReDoS on attacker-influenced drafts could fail open or hang the serve loop | Fail-closed withhold-all; linear-time regexes; pathological perf fixtures; loose CI bound <50ms (interfaces, P3-T3/T5) |
| P3S-9 | HIGH | Constructor default ENFORCE breaks all existing tests and duplicates the config point | `grounding` required arg; builder is sole decision point; gateway hard-coded; testkit repair-aware scripts (loop integration, P3-T3/T4) |
| P3S-10 | HIGH | Shadow-mode FP labels need claim text — conflicts with metadata-only ledgers; budgets were unset | Local-only consented `grounding_shadow.jsonl` under profile dir, backup-excluded, never on gateway; budgets recorded in scope note (FP ≤5% / ≤10% turns, p50 ≤0.5s, p95 ≤+1 RT, tokens ≤+20%, ≥50 turns/≥7d) (P3-T7) |
| P3S-11 | MED | New grounding key not in `SECURITY_KEYS`; gateway mode must not be config; flag-log sink unspecified | `chat.grounding_default` added to SECURITY_KEYS + migration test; no gateway key exists; stderr banner + metadata-only ledger row (surface defaults, P3-T4) |
| P3S-12 | MED | Iteration-cap forced answer runs tools-disabled — cannot repair | Straight to redact-and-notice, no repair budget consumed (loop integration, P3-T3) |
| P3S-13 | MED | Conflicting envelopes per object unpinned; verdict field read unpinned | Latest-per-object governs (kernel re-run semantics); `exit_code` with `verdict` fallback; footer lists all (interfaces, P3-T2) |
| P3S-14 | MED | Redaction mechanics: markdown breakage, empty-reply case, footer interaction | Whole-claim-unit redaction on final draft; notice+footer-only floor; footer inputs untouched; `suggested_fix` once, in footer (loop integration, P3-T3) |
| P3S-15 | MED | Hedge words exempting claims = extraction-dodge channel | Hedges don't exempt known-object/figure units; hedge-smuggle fixture class (interfaces, P3-T1) |
| P3S-16 | MED | Relayed kernel-gated content (brief/search) trips the gate; "every surface" scope ambiguous | AgentLoop-only scope pinned; `standing_deliverables` not double-gated; relayed content requires envelopes fail-closed, cost measured by P3-T7 (surface defaults) |
| P3S-17 | MED | Recall-tuned vs zero-conversational-flags contradictory; corpus self-graded | Materiality predicate pinned; named adversarial fixture classes with per-class recall; Phase 6 re-measures independently (interfaces, P3-T1) |
| P3S-18 | LOW | Model can spoof the authority footer inside prose | Footer-lookalike body lines stripped by the gate; fixture (interfaces, P3-T1) |
| P3S-19 | LOW | Repair user-turns create eviction groups → orphaned repair fragments | Repair turns tagged; question + repair chain evicted as one group (loop integration, P3-T3) |

## Definition of done

- [x] Deterministic extractor + checker with a labeled corpus (recall = 1.00
      overall AND per adversarial fixture class); withheld envelopes are
      refused-class; coverage is normalize-equality only.
- [x] Repair loop with redaction fallback; no unbacked material claim ever
      released; repairs share the per-turn iteration budget + wall-clock
      ceiling; gate exceptions fail closed; STRESS I1 message-pairing AND
      P3S-19 repair-group eviction invariants preserved.
- [x] Gateway ENFORCE non-overridable, hard-coded in the builder
      (gateway-first); local default governed by the P3-T7 budget gate via
      `chat.grounding_default` (SECURITY_KEYS-protected); grounding-mode
      flags logged; repair telemetry in the gateway ledger.
- [x] Negligible extractor/checker latency incl. pathological inputs
      (benchmarked, P3-T5: typical 0.01ms, 10k-row table 39ms, all <50ms).
- [ ] **PENDING — the one open item:** real-traffic budgets measured against
      the pinned numbers (P3-T7 requires ≥50 real local turns over ≥7 days
      with operator labels; the capture machinery + `oracle grounding-report`
      ship now, the measurement runs on real use). The local
      `chat.grounding_default` stays OBSERVE until that report says GO.
- [x] SECURITY.md guarantees SH-059..SH-063 added and backed (object-level
      wording per P3S-4).
- [x] `make check` green locally; CI on next push.

**Phase 3 code-complete 2026-06-11.** Gateway runs ENFORCE from day one; the
local default flip awaits the P3-T7 real-traffic measurement.
