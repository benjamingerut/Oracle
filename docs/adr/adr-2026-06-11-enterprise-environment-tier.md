# ADR — The `enterprise` Environment Tier (P2-T7)

**Status: PROPOSED — decision document only; deliberately NOT built.**
Per the PHASE-2 spec (P2-T7), this ADR specifies the policy-matrix column and
the attestation ceremony so the decision is made once, in the open, with its
risks named — and so nobody "just wires it up" casually. Zero code ships with
this ADR.

## Context

The policy matrix recognizes two environments: `local_agent` (genuinely-local
model, ceiling `internal` today) and `external` (any other endpoint, ceiling
`public`). The P2-T0 usefulness gate returned a provisional NO-GO on
local-model minimized-confidential answering (55% vs the 70% bar,
`docs/eval/p2-t0/REPORT.md`): on current local models, the confidential tier
stays mute. Frontier models would clear the usefulness bar — but they are
`external`, and `external` sees `public` only, ever. That invariant is
correct as a default and wrong as a permanent ceiling for every business:
some operators have contractual zero-data-retention agreements with a model
provider and would knowingly choose that trade.

The egress veto (STRESS C2 extension) sharpened the lesson behind this tier:
*network locality is not processing locality, and classification must follow
where content actually goes.* The `enterprise` tier is the honest inverse —
content knowingly goes to a contracted external processor, and the system
records exactly that, instead of pretending the endpoint is something it
isn't.

## Decision (what the tier IS, if and when an operator enables it)

A third environment value, `enterprise`, with this policy-matrix column:

| sensitivity | external | enterprise | local_agent |
|---|---|---|---|
| public | allow | allow | allow |
| internal | deny | **allow** | allow |
| confidential | deny | **allow-minimized*** | allow-minimized |
| restricted | deny | deny | allow-minimized |
| secret | deny | deny | allow-minimized |

\* `allow-minimized` for enterprise remains inert until a minimizer exists
(the same P2 gate governs); in practice the tier's v1 value is **internal**
content on frontier models. Confidential never flows unminimized to ANY
environment, enterprise included.

## The attestation ceremony (the part that is not a config checkbox)

`enterprise` classification is granted per (endpoint, model) pair and ONLY by
all of the following together, each fail-closed:

1. **Admin attestation record** — a signed-intent file the admin creates via
   an interactive control-plane command (never reachable from chat, gateway,
   serve, or any model surface), naming: the provider, the endpoint base_url,
   the exact model id(s), the contract reference (e.g. "MSA §x zero-retention
   addendum, dated …"), and the admin's name. The command refuses non-TTY.
2. **Ledgered grant** — the attestation is appended to the instance's
   hash-chained ledger (auditable, tamper-evident) and the doctor displays it
   prominently on every run ("enterprise tier ACTIVE for <model> @ <host>:
   attested by <admin> on <date>, contract <ref>").
3. **Config pin under SECURITY_KEYS** — `provider.enterprise.{base_url,
   model, attestation_id}`; a migration that drops or alters any of it is a
   hard load error. Absent or inconsistent (attestation_id not found in the
   ledger, base_url mismatch) ⇒ the endpoint classifies plain `external`
   (public only). Every ambiguity resolves down, never up (INV-I4).
4. **No transitive grant** — the attestation covers exactly the named model
   ids at the named endpoint. A renamed model, a different model on the same
   endpoint, or the same model elsewhere is `external`.

Revocation is one command (marks the attestation revoked in the ledger;
config cleanup suggested by doctor) and takes effect on the next loop build.

## Consequences

- **Accepted risk, named:** content up to `internal` (and minimized
  confidential, later) leaves the box to a third party. The protection is
  contractual + audit-trail, not technical. The README/SECURITY.md must say
  this in exactly those words when the tier ships.
- The footer/provenance honesty rules apply unchanged — answers state their
  environment; nothing about `enterprise` is hidden from the person reading
  the answer.
- The gateway's per-surface ceilings still cap below the environment ceiling;
  enterprise does not widen any gateway surface by itself.
- The tier competes with re-running the P2-T0 gate on better local models —
  whichever clears first unlocks confidential value. They are not exclusive.

## What it will take to build (sizing, for the future phase that picks it up)

Kernel: the matrix column + attestation ledger verbs + doctor display
(upstream, re-vendored). Shell: `environment_for` learns the attested-pair
check (after the egress veto — the veto still applies; an attested endpoint
that proxies elsewhere is still vetoed), config keys + SECURITY_KEYS, the
interactive attestation/revocation CLI, enforcer tests for every fail-closed
branch above, SECURITY.md guarantees. Estimated one focused task group, gated
by its own stress pass.

## Alternatives considered

- **Do nothing** (local-only forever): leaves confidential value gated on
  local-model quality indefinitely; rejected as a permanent answer, kept as
  the default posture.
- **Silent allow-by-config** (a `trusted: true` flag on a provider): rejected
  — exactly the casual wiring this ADR exists to prevent; no ceremony, no
  audit trail, invisible in review.
- **Per-request consent prompts**: rejected — consent fatigue makes it
  theater; the decision is institutional, not per-message.
