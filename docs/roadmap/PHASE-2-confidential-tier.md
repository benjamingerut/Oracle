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

The phase opens with a validation gate (P2-T0): minimized-answer usefulness is
*measured with real local models* before the full minimizer is built. If
minimization guts the answers, building P2-T1..T6 as specced would ship a
feature nobody can use — the gate forces that discovery to cost days, not the
phase.

Read first: `docs/roadmap/ROADMAP.md`, `STRESS.md` (H2 — why allow-minimized was not a
grant), the kernel's `_tools/policy.py` (`check_processing` returns
`allow|allow-minimized|deny`).

Depends on: Phase 1 (testkit for leak-assertions, SECURITY.md map).

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
    receipt_sha256: str        # hash over (source_sha, target_tier, rules_version)
    rules_version: str
@dataclass
class Redaction:
    category: str              # "person" | "money" | "account" | "email" | ...
    count: int                 # how many removed (never the values)
```
Deterministic, stdlib-only (regex + ontology entity lists from `oracle.yml`).
Categories and rules are declared in `oracle.yml` so each company tunes them.

### Kernel CLI surface
```
oracle search query --q=... --max-sensitivity confidential --minimize-to internal
oracle answer --object X --minimize-to internal --format json
```
When `--minimize-to T` is present and a chunk's sensitivity exceeds `T`, the
kernel returns the minimized view + a `minimization` block
(`{receipt_sha256, rules_version, removed:[{category,count}]}`) instead of the
raw chunk. Without the flag, behavior is unchanged (raw or denied per matrix).

### Shell: policy_bridge.py changes
```python
def ceiling_for(root, environment, local_is_confined) -> Ceiling
@dataclass
class Ceiling:
    plain: str                 # highest exactly-"allow" tier (unchanged logic)
    minimized: str             # capped at "confidential" for local_agent+confined, IFF
                               # confinement verified. NOT "highest allow-minimized tier"
                               # (which would reach "secret" per the matrix) — capping at
                               # confidential is a deliberate conservative bound that avoids
                               # re-opening STRESS H2 for restricted/secret material.
def confinement_verified(cfg, root) -> bool   # local_is_confined AND endpoint loopback
                                              # AND a minimizer is present in the root
```
The shell may release content above `plain` only as minimized output, only up
to `minimized`, only when `confinement_verified` is true. External endpoints:
`minimized == plain == public`, always (the function returns early).

### Shell: verbtools.py dispatch
- `_do_oracle_search` / `_do_oracle_answer` pass `--minimize-to <plain>` when
  the ceiling allows a minimized tier above plain.
- **Receipt check (I5):** any returned chunk/envelope whose declared
  sensitivity exceeds `plain` MUST carry a `minimization.receipt_sha256`; if it
  doesn't, the shell drops it and substitutes the withheld stub. A model can
  never receive above-plain content lacking a verifiable receipt.
- Every minimized release appends a `minimization_event` ledger row (kind,
  categories+counts, receipt, surface, environment) — metadata only.

## Tasks

- **P2-T0 — minimized-usefulness validation (phase-opening gate).** Before any
  minimizer code is built, measure whether minimized answers are *useful*:
  assemble a representative confidential Q&A fixture set (synthetic but
  realistic — names, figures, accounts, dates woven through the way real
  company documents weave them), hand-minimize it per the planned category
  rules, and run real local models (the same class the audience will actually
  run on loopback) against the minimized views. Score answer adequacy with a
  written rubric (does the redacted view still let the model answer the
  question correctly, or does redaction gut the answer?).
  *Go/no-go criteria (explicit, recorded in this spec before coding):*
  **go** = on the fixture set, ≥ 70% of questions remain answerable-correctly
  from the minimized view AND no category of question is uniformly gutted;
  **no-go** = below threshold, in which case P2-T1..T6 do NOT proceed as
  specced — the phase pivots to re-scoping (coarser categories, alternative
  redaction strategies, or elevating the P2-T7 design decision from
  "evaluate" to "decide now"). *Acceptance:* fixture set + rubric + measured
  results checked in under `docs/eval/`; a written go/no-go verdict appended
  to this spec. *Tests:* none (measurement task); artifacts are the
  deliverable. *Deps:* P1. **Gates: P2-T1 through P2-T6.**

- **P2-T1 — kernel minimizer (upstream).** Implement `_tools/minimizer.py` +
  `oracle.yml` `minimization:` config (categories, rules_version) + the
  `--minimize-to` flag on `knowledge_index query` and `answer`. Deterministic,
  stdlib-only. Lands in the Oracle Spawn kit; re-vendored via P1-T5.
  *Acceptance:* given a confidential chunk with names/figures and
  `--minimize-to internal`, output contains no name/figure and a receipt;
  `--minimize-to` absent → unchanged. *Tests (kernel):* `test_minimizer.py`,
  `test_minimize_cli.py`. *Deps:* P1-T5.

- **P2-T2 — minimization ledger + lint (upstream).** A `minimization_event`
  ledger (metadata only, like `export_event`); `oracle_lint` gains a
  doctrine→enforcer row asserting minimized releases are logged. *Acceptance:*
  a minimized query writes exactly one metadata row, no raw values present.
  *Tests (kernel):* `test_minimization_ledger.py`. *Deps:* P2-T1.

- **P2-T3 — shell ceiling split.** Implement `Ceiling`/`ceiling_for`/
  `confinement_verified`; `external` early-returns `public` for both fields;
  `local_agent` returns `minimized` only when confinement verified AND a
  minimizer exists in the root. *Acceptance:* table tests — external→
  (public,public); local+confined+minimizer→(internal,confidential);
  local+not-confined→(internal,internal). Fail-closed on any error. *Tests:*
  extend `test_policy_bridge.py`. *Deps:* P2-T1 (presence check), P1.

- **P2-T4 — shell minimized dispatch + receipt enforcement.** Pass
  `--minimize-to plain` in search/answer when `minimized > plain`; verify
  receipts on every above-plain item; drop+stub anything lacking one; write the
  shell-side awareness into the authority footer ("answered from minimized
  confidential evidence"). *Acceptance:* with a fake kernel returning above-plain
  content WITHOUT a receipt, the shell withholds it (leak-assert from P1-T2
  passes); WITH a receipt at/under `minimized`, it is released. *Tests:* extend
  `test_verbtools.py`; a `testkit` leak scenario. *Deps:* P2-T1, P2-T3, P1-T2.

- **P2-T5 — confinement doctrine + doctor.** Add `provider.local_is_confined`
  as a new, first-class, documented, doctor-checked config setting (it was
  removed in S1 remediation as a dead security knob; this task reintroduces it
  with real semantics backed by the minimizer): doctor explains what confinement
  means, what it does/doesn't guarantee (loopback ≠ no forwarding — STRESS C2),
  and shows the resulting plain/minimized ceilings.
  *Acceptance:* doctor on a confined-local config shows `confidential`
  minimized ceiling; on external shows public/public with an explanation.
  *Tests:* extend `test_cli.py`/doctor tests. *Deps:* P2-T3.

- **P2-T6 — SECURITY.md guarantees.** Add guarantees: "external models never
  receive above-public content (incl. minimized)", "above-plain content
  reaches a model only with a verified minimization receipt", "every minimized
  release is ledgered". Wire to the P2 tests. *Acceptance:* `verify_enforcers()`
  still empty. *Tests:* `test_security_map.py`. *Deps:* P2-T2, P2-T4, P1-T1.

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
  *Acceptance:* ADR merged under `docs/adr/`; matrix-column spec with
  default-off/fail-closed semantics; zero code changes in this task.
  *Deps:* P2-T0 (its usefulness data is the decision's main input), P1.

## Security invariants for this phase

- External endpoint ⇒ `minimized == plain == public`. This is checked by an
  explicit test that no `--minimize-to` above public is ever emitted on an
  external environment.
- The minimizer is the ONLY producer of above-plain-but-releasable content; the
  shell trusts content above `plain` ONLY with a receipt whose
  `rules_version` it recognizes (unknown rules_version → withhold, I4).
- Redaction records and ledger rows carry categories + counts, NEVER the
  removed raw values.
- Minimization is best-effort by nature (regex/ontology); doctrine states
  plainly that it reduces but does not *prove* zero leakage, and therefore it is
  gated behind explicit operator opt-in (`local_is_confined`) + loopback +
  presence — never default-on.

## Stress pass (before coding)

Can a crafted chunk evade the minimizer (entity not in ontology, unusual
number format, name in an image alt-text)? Does the receipt actually bind to
the content, or can a stale receipt be replayed on different text? Can an
external provider ever see `--minimize-to` raised? Append findings.

## Definition of done

- [ ] P2-T0 usefulness validation run with real local models; go/no-go verdict
      recorded in this spec (it gates everything below).
- [ ] Kernel minimizer + `--minimize-to` + ledger + lint (upstream, re-vendored).
- [ ] Shell ceiling split; external stays public/public under all inputs.
- [ ] Receipt enforcement: above-plain without a valid receipt is withheld
      (leak-assert proven).
- [ ] Doctor explains confinement + shows ceilings; opt-in only.
- [ ] SECURITY.md guarantees added and backed.
- [ ] P2-T7 `enterprise`-tier ADR + matrix-column spec merged (decision only,
      default-off/fail-closed; zero code).
- [ ] `make check` green incl. new kernel + shell tests; CI green.
