---
id: "dir-{{DATE}}-initial-admin-authority"
type: directive
title: Initial admin authority for oracle bootstrap
created: "{{DATE}}"
updated: "{{DATE}}"
sensitivity: internal
status: active
actor: "{{ADMIN_NAME}}"
role: admin
tags:
  - governance
  - bootstrap
---

## Directive

{{ADMIN_NAME}} is recorded as the initial admin for the {{COMPANY_NAME}} oracle ({{CODENAME}}), with the admin capabilities defined in `DOCTRINE.md` and `oracle.yml` (`governance.roles.admin`).

This is the root governance fact the oracle relies on for authority, approvals, autonomy grants, and exports. Confirm or supersede this directive if the governance model changes.

## Lifecycle

Directives are immutable. Do not edit this text. If authority changes, write a new directive note and link the two with `superseded_by:` here and `supersedes:` there. Lifecycle is expressed through `status:` (active | fulfilled | superseded | expired | revoked) and the supersession links — never by rewriting the original instruction.
