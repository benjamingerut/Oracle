---
id: adr-{{DATE}}-initial-oracle-bootstrap
type: architecture_decision
title: Initial oracle bootstrap
created: "{{DATE}}"
updated: "{{DATE}}"
sensitivity: internal
status: active
actor: "{{ADMIN_NAME}}"
role: admin
tags:
  - meta
  - architecture
  - bootstrap
---

## Decision

Bootstrap a self-contained, local-sovereign oracle filesystem for
{{COMPANY_NAME}} (codename {{CODENAME}}) from the Oracle Spawn v2 seed kernel, on
{{DATE}}, under the authority of {{ADMIN_NAME}} (admin).

The oracle is a three-tier stack:

- **Tier 1 — Floor (security + reliability).** A single path-containment
  chokepoint (`safe_paths.py`) every writer imports; a durable append-only
  ledger (`ledger.py`); a schema-validating linter (`oracle_lint.py`); an
  entropy-scored secret scanner (`secret_scan.py`); and a real policy gate
  (`policy.py`). These land first and are test-green before anything is built on
  them.
- **Tier 2 — Engine (knowledge + accuracy).** A deterministic ingestion pipeline
  and an answer protocol (`answer_protocol.py` + `truth_map.py`) that refuses a
  material answer when no source authority exists.
- **Tier 3 — Execution + self-improvement.** A loop runner (`loops.py`), capture
  of value/feedback/failure events, standing deliverables, and **scoped autonomy
  that is OFF by default** (`actions.py` + `Autonomy/autonomy.yml`).

## Rationale

The oracle serves as {{COMPANY_NAME}}'s company memory, evidence system,
contradiction resolver, workproduct engine, and self-improving partner. v2 is
built on one governing meta-rule: **every security / policy / accuracy guarantee
in doctrine names its enforcing tool, or is explicitly stamped "advisory".**
`oracle_lint` cross-checks that map and fails the build on any unenforced
"must / required / denied" guarantee — so the doctrine is binding, not decorative.

Autonomy ships last and OFF because a path-traversal flaw plus headless execution
would otherwise be an automated exfiltration engine. The floor is proven before
any loop can run unattended.

## Consequences

- The oracle is usable interactively from spawn; it does nothing unattended until
  an admin opts into autonomy via `Autonomy/autonomy.yml`.
- Local sovereignty is preserved: only the executable tool layer is ever
  upgraded (hash-verified), never doctrine, Memory, Meta, or business config.
- Maturity is "scaffolded": inert-but-safe is explicitly NOT "done" — see
  `BOOTSTRAP-STATUS.md`.

## Open Follow-Up

- Confirm the user / admin roster and write a `User-Models/` note per user.
- Confirm the connector roster and register manifests.
- Confirm the backup policy and run a real `verify-restore` round-trip.
- Confirm the processing matrix against {{COMPANY_NAME}}'s actual sensitivity
  tiers.
- Ingest or connectorize the seed material so the truth-map and knowledge index
  are populated (until then the answer protocol refuses with
  `no-authority-bootstrap`).
