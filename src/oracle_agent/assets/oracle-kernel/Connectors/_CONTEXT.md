# Connectors

One connector per external system, repo, database, folder, SaaS tool, or data
source the oracle reads from. Each connector is a folder `Connectors/<id>/`
holding a machine-readable `<id>.manifest.yaml` (the binding record the runtime
loads) and, optionally, a human narrative `<id>.md`.

A connector describes capability, auth **by variable name only**, query method,
source authority, freshness, schema/grain, gotchas, forbidden uses, health
checks, and how much of the source we copy. It never holds the bytes of a
secret — auth is referenced by environment-variable name, and credentials live
only in the (git-ignored) `.env`. Do not store secrets here.

## Files in this folder

- `connector-template.md` — narrative template; copy to `<id>.md` per source.
- `connector-template.manifest.yaml` — machine template; copy to
  `<id>/<id>.manifest.yaml` and fill in. Block-style YAML only (see below).
- `localfolder/localfolder.manifest.yaml` — the shipped reference connector:
  a read-only pull from one admin-approved local folder into `_INPUT/`.

## locality x capture_tier

`locality` and `capture_tier` are one orthogonal pair, each 3-valued, with a
shared decode table. Set both deliberately:

| `locality` (where the bytes physically live)            | `capture_tier` (how much we copy locally)              |
| ------------------------------------------------------- | ------------------------------------------------------ |
| `external_only` — bytes stay in the source system; we   | `manifest_only` — we copy nothing; the manifest is the |
| only hold the manifest + query method.                  | only local artifact (a pure pointer/connector).        |
| `snapshot_local` — we copy a point-in-time snapshot of  | `snapshot` — we copy a bounded point-in-time subset    |
| selected bytes into the oracle (e.g. a pulled export).  | into `_INPUT/` for ingestion; not kept in sync.        |
| `mirror_local` — we maintain a continuously-refreshed   | `mirror` — we keep a continuously-refreshed local copy |
| local copy that tracks the source.                      | tracking the source (highest blast radius).            |

The two axes are independent: e.g. `external_only` pairs with `manifest_only`
(a pure connector that never copies); `snapshot_local` pairs with `snapshot`
(the common ingest case). `oracle_lint` cross-checks the pair against the
shared decode table. The default for new connectors is the most conservative
pair: `external_only` / `manifest_only`.

## Runtime contract

A working connector is more than a manifest. The runtime
(`_tools/connectors/`) loads the manifest through the safe-subset YAML loader,
validates it against `_tools/schemas/connector.schema.json`, and exposes four
methods (see `references/connector-manifests.md` for the full contract):

- `pull(ctx)` — non-destructive read of new bytes into `_INPUT/<id>/`, through
  `safe_paths` + the policy gate + the intake sensitivity classifier. Read-only
  connectors refuse to run if their manifest declares `permissions: read_write`.
- `probe(ctx)` — inspect the source (e.g. a file-type histogram) without copying.
- `freshness(ctx)` — verdict (`fresh` / `stale` / `unknown`) vs the manifest SLA.
- `health(ctx)` — `healthy` / `degraded` / `broken`.

## YAML rules (binding)

Manifests are loaded by a strict safe-subset loader. Write **block style only**:
one `- item` per line for lists; never inline flow like `[a, b]` or `{k: v}`.
Write an empty list or map as a bare `key:` (which parses to nothing), never
`[]` or `{}`. No anchors `&`, aliases `*`, tags `!`, or multi-document `---`.

## Truth-map linkage

`authoritative_for` / `corroborates` / `cannot_prove` are the connector's claim
to source authority; they feed `TRUTH-MAP.md` and the answer protocol. Before a
connector is relied on for a material answer, its authoritative objects should
appear (or be added) as rows in the truth map.
