# Systems

The software and process machinery the company runs on: source systems, applications, databases, repos, workflows, and processes.

## What belongs here

One note per system. Use `type: system` with `subtype:` one of: `source_system`, `application`, `database`, `repo`, `workflow`, `process`. A system note describes what the thing *is* and how the oracle reasons about it; it is the human-memory counterpart to a `Connectors/` manifest (the machine-readable runtime config). When a system is wired as a data source, link its connector manifest.

Use this for the CRM, the ERP, the data warehouse, the billing system, a key git repo, an onboarding workflow, a monthly-close process. Do not store credentials here — those are `.env.nosync` and connector `auth.vars`.

## Mutability

Mutable hub. Systems evolve (versions, owners, schemas) — update in place and bump `updated:`. Point-in-time schema snapshots are immutable `Sources/` notes; link them rather than freezing schema detail here.

## Sensitivity

Most system notes are `internal`; raise to `confidential` when topology, schema, or access detail would aid an attacker or reveal sensitive data shapes. Never include connection strings, tokens, or keys in the body — reference the env var name only.
