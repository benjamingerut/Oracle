---
id: "sys-{{DATE}}-rename-me"
type: system
title: <system name>
created: "{{DATE}}"
updated: "{{DATE}}"
sensitivity: internal
status: active
subtype: application
tags:
  - system
---

## Summary

<What this system is and what role it plays for {{COMPANY_NAME}}.>

## Subtype

`subtype:` must be one of: source_system, application, database, repo, workflow, process.

## Particulars

- Owner: <link to People/ or Groups/>
- Access mode: <api, mcp, cli, database, repo, folder, manual — and link the Connectors/ manifest if wired>
- Auth: <reference the .env var NAME only; never the value>

## What it is authoritative for

- <Which business objects this system is the source of truth for — keep aligned with TRUTH-MAP.md.>

## Schema and snapshots

- <Link immutable schema snapshots in Sources/ rather than freezing detail here.>

## Change log

- {{DATE}} — created at bootstrap.
