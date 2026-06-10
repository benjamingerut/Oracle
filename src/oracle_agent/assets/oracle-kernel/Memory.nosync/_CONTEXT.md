# Memory.nosync

Company memory lives here. It is the source of truth for what the oracle knows or believes about **the company** — distinct from `Meta.nosync/` (the oracle's memory about *itself*).

## How this tree is organized

Folders partition by **behavioral type** — how a note *behaves* over its life, not what company noun it names. The company noun is a `subtype:` field in frontmatter, validated against the ontology subtype enum by `oracle_lint`. This is the type-system reconciliation: one `Customers/` company-noun folder is wrong; a `person` note with `subtype: customer` is right.

### Mutable hubs (updated in place; provenance-linked change notes)

- `Entities/` — company nouns and instruments (`subtype:` organization, customer, vendor, product, facility, competitor, legal_entity, term, custom)
- `People/` — individual humans
- `Groups/` — teams, departments, segments, cohorts, boards, committees, user groups
- `Assets/` — physical, financial, digital, contractual, strategic instruments
- `Systems/` — source systems, applications, databases, repos, workflows, processes
- `Metrics/` — measured quantities and their definitions
- `Queries/` — saved reusable analytical queries
- `Models/` — explanatory compressions of how the company works
- `Questions/` — open research agenda and ignorance ledger
- `Contradictions/` — first-class unresolved conflicts (mutable investigation objects)

### Immutable / mostly-immutable statements (supersede; never silently edit)

- `Sources/` — immutable evidence snapshots (provenance, hash, grain card)
- `Findings/` — atomic claims at a point in time
- `Recommendations/` — accountable advice (original immutable; adjudication block mutates)
- `Decisions/` — observed organizational actions
- `Directives/` — authorized instructions from governance-authorized humans

## Mutability rule (mechanical, not conventional)

Immutable types (`source`, `finding`, `decision`, `directive`) record a `content_sha256` in their ledger when registered. `oracle_lint` FAILS on any on-disk/ledger hash mismatch. To change an immutable claim you **write a new note and supersede the old one** (`supersedes:` / `superseded_by:`), never edit the bytes. Mutable hubs and the recommendation adjudication block update in place.

## Sensitivity

Every note carries a required `sensitivity:` field — one of `public | internal | confidential | restricted | secret`. Set it at log time using the stricter-row-wins discipline (when in doubt, classify up). Secrets never live in note bodies; they belong in `.env.nosync` only. Sensitivity gates processing and export through `policy.py`.

## What does NOT belong here

This is not a scratchpad or artifact store. Whole files live in `Workproduct.nosync/`; raw data lives in `_data.nosync/`; workbench files live in `Analysis.nosync/`. Material claims need evidence, source authority, confidence, claim tier, disconfirmer, resolving source, and an as-of date.

Each type folder ships a `_CONTEXT.md` (what belongs, mutability, sensitivity guidance) and a `_template.md` (copy it; rename `_template.md` -> a real slug; fill the frontmatter; it lints clean as written).
