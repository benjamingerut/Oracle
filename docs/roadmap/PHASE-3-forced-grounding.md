# Phase 3 — Forced Grounding

**Closes limit #2.** Today the authority footer makes *labeling* honest — a
model that skips the answer protocol gets tagged "conversational" — but nothing
*forces* it to consult the protocol before asserting a company fact. A
confident model can still emit an ungrounded claim in prose; only the footer
betrays it. This phase makes grounding structurally unavoidable for material
assertions, on every surface, enforced in code (I5).

Read first: `docs/ROADMAP.md`, `docs/DESIGN.md` (D5), the kernel
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
Only a draft whose every material claim is backed (or explicitly hedged to
match its verdict) is released.

This is the same philosophy as the kernel's `standing_deliverables` claim-gate
(which already gates briefings), lifted to interactive answers.

## Frozen interfaces

### `oracle_agent/agentloop/grounding.py` (new)
```python
@dataclass
class ClaimCheck:
    claims: list[Claim]            # material company claims found in the draft
    unbacked: list[Claim]          # claims with no covering envelope this turn
    mismatched: list[Claim]        # claim asserts stronger than its verdict allows
@dataclass
class Claim:
    text: str                      # the asserting sentence
    object_guess: str | None       # best-effort business object it concerns
def extract_claims(draft: str, *, objects_seen: list[str]) -> list[Claim]
def check_grounding(draft: str, envelopes: list[dict]) -> ClaimCheck
def repair_prompt(check: ClaimCheck) -> str   # the message sent back to the model
```
- `extract_claims` is deterministic and conservative: it flags declarative
  sentences that reference a known business object or a truth-map row, plus
  sentences containing figures/dates/named entities asserted as fact. It is
  tuned to favor *recall* (catch real claims) over precision; false positives
  cost an extra repair turn, not a leak.
- A claim is "backed" iff an envelope obtained this turn covers its object with
  a verdict whose obligations the draft honors (grounded→plain assert ok;
  supported→must be labeled; caveated→must carry the caveat; refused→must not
  assert).

### `agentloop/loop.py` integration
```python
class AgentLoop:
    def __init__(self, ..., grounding: GroundingPolicy = GroundingPolicy.ENFORCE): ...
```
- After the model returns a content-only response, run `check_grounding`.
- If `unbacked` or `mismatched` is non-empty and repair budget remains
  (`max_repair=2`), append `repair_prompt(check)` as a user turn (tools
  RE-ENABLED so it can call `oracle_answer`) and loop.
- If the budget is exhausted and claims remain unbacked, **the loop redacts the
  offending sentences** and appends a notice: "[N claim(s) withheld: not
  grounded — ask the operator to ingest evidence or promote authority]" plus
  the kernel `suggested_fix`. The user never receives an unbacked material
  claim.
- `GroundingPolicy.OBSERVE` (footer-only, the v1 behavior) remains available
  for local-operator chat where the admin explicitly wants raw model output;
  `ENFORCE` is the default and the only mode on the gateway.

### Surface defaults
- gateway / external: `ENFORCE`, no override.
- local chat: `ENFORCE` by default; `oracle chat --grounding observe` lets the
  operator opt down (logged).

## Tasks

- **P3-T1 — claim extractor.** Implement `extract_claims` (deterministic,
  stdlib). Build a labeled fixture corpus (drafts → expected claims) covering
  figures, dates, named systems, hedged vs asserted, and pure conversational
  text. Tune for recall. *Acceptance:* on the corpus, recall ≥ 0.95 on planted
  material claims, and zero claims flagged in purely conversational replies.
  *Tests:* `test_grounding_extract.py` + corpus. *Deps:* P1.

- **P3-T2 — grounding checker.** Implement `check_grounding` mapping claims to
  envelopes and verdict-obligation matching. *Acceptance:* a grounded envelope
  backs a plain assertion; a supported envelope without a label → mismatched; a
  refused envelope with an assertion → unbacked; no envelope → unbacked.
  *Tests:* `test_grounding_check.py`. *Deps:* P3-T1.

- **P3-T3 — repair loop in AgentLoop.** Wire `GroundingPolicy`, the repair
  turn (tools re-enabled), the `max_repair` budget, and the final
  redact-and-notice fallback. Preserve message-pairing invariants from P1.
  *Acceptance:* a scripted model that asserts ungrounded → repair turn → calls
  `oracle_answer` → grounded release; a stubborn model that never grounds →
  offending sentences redacted, notice + fix shown, no unbacked claim in the
  output. *Tests:* `test_grounding_loop.py` via testkit. *Deps:* P3-T2, P1-T2.

- **P3-T4 — surface wiring + override.** Default `ENFORCE`; gateway forces it
  (no override path); `oracle chat --grounding observe` opt-down on local only,
  logged. *Acceptance:* gateway loop ignores any attempt to set OBSERVE; local
  `--grounding observe` produces the v1 footer-only behavior. *Tests:* extend
  `test_telegram.py`, `test_cli.py`. *Deps:* P3-T3.

- **P3-T5 — performance guard.** The extractor + checker run on every turn;
  ensure they add negligible latency (pure-Python, no model call). Add a
  micro-benchmark test asserting < a few ms on a typical draft. *Acceptance:*
  benchmark within budget; no network/model calls in the grounding path.
  *Tests:* `test_grounding_perf.py`. *Deps:* P3-T2.

- **P3-T6 — SECURITY.md guarantee.** "No material company claim is released to
  any user without a covering answer-protocol envelope whose obligations the
  text honors (gateway: no override)." Wire to P3 tests. *Acceptance:*
  `verify_enforcers()` empty. *Deps:* P3-T3, P1-T1.

## Security / correctness invariants

- The grounding gate is deterministic and runs server-side (in the shell), not
  in the model — the model cannot disable it (I5).
- Fallback is redaction, not release: on exhausted repair budget the unbacked
  text is removed, never shipped with a disclaimer-and-hope.
- The extractor errs toward flagging; a missed claim is the failure mode to
  drive toward zero, measured by Phase 6's eval.
- `OBSERVE` mode is local-operator-only, logged, and can never be reached from
  the gateway or any external surface.

## Stress pass (before coding)

Can the model smuggle a claim past the extractor (claim embedded in a list, a
code block, a quote, a non-English clause)? Can it satisfy the checker with an
envelope for a *different* object than the one it asserts? Does the repair loop
terminate (interaction with the iteration cap)? Append findings.

## Definition of done

- [ ] Deterministic extractor + checker with a labeled corpus (recall ≥ 0.95).
- [ ] Repair loop with redaction fallback; no unbacked material claim ever
      released; pairing invariants preserved.
- [ ] ENFORCE default; gateway non-overridable; local OBSERVE opt-down logged.
- [ ] Negligible added latency (benchmarked).
- [ ] SECURITY.md guarantee added and backed.
- [ ] `make check` green; CI green.
