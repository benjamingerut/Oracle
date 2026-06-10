# Connector Template

Copy this file to `<connector-id>/<connector-id>.md` (and the companion
`connector-template.manifest.yaml` to `<connector-id>/<connector-id>.manifest.yaml`)
when a source system is identified. The `.md` is the human narrative; the
`.manifest.yaml` is the binding record the runtime loads.

## Capability

What this source can answer, and the business questions it unlocks.

## Auth

Reference variable names only (e.g. `FOO_API_TOKEN`). Never write secret
values here. Credentials live only in the git-ignored `.env`.

## How To Query

CLI / API / MCP / database / file-drop / folder instructions and any helper
scripts. For a runtime connector, name the module under `_tools/connectors/`
that implements `pull` / `probe` / `freshness` / `health`.

## Source Authority

What business objects this source is **primary** for (`authoritative_for`),
what it only `corroborates`, and what it `cannot_prove`. These feed the
truth map.

## Grain / Schema

Row/object grain, keys, time basis, schema map location, and join keys.

## Locality / Capture Tier

State both axes explicitly (see `_CONTEXT.md` decode table):

- `locality` — where the bytes physically live: `external_only` /
  `snapshot_local` / `mirror_local`.
- `capture_tier` — how much we copy locally: `manifest_only` / `snapshot` /
  `mirror`.

Default to the most conservative pair (`external_only` / `manifest_only`) and
escalate only when a copy is genuinely needed.

## Freshness

Expected refresh cadence, last verified date, decay risk, and the SLA the
runtime checks `freshness(ctx)` against.

## Biases / Forbidden Uses

Known blind spots, incentive distortions, and forbidden shortcuts
(`forbidden_uses`).

## Health / Schema Refresh

How to check access, permissions, minimal reads, schema drift, and freshness —
the `checks` the runtime's `health(ctx)` performs and the `schema_refresh`
cadence.
