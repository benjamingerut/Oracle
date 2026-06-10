# Entities

Mutable company nouns and instruments that do not fit a more specific behavioral hub (people, groups, assets, systems, metrics, queries).

## What belongs here

One note per canonical noun. Use the `subtype:` frontmatter field — never a per-noun folder — to say *what kind* of entity it is. Valid entity subtypes (from the ontology enum, validated by lint): `organization`, `customer`, `vendor`, `product`, `facility`, `competitor`, `legal_entity`, `term`, `custom`.

If a noun is genuinely a person, group, physical/financial/digital/contractual/strategic asset, source system / application / database / repo / workflow / process, a measured metric, or a saved query, file it in the matching hub instead. Entities is the home for organizations, products, customers, vendors, competitors, legal entities, and defined business terms.

## Mutability

Mutable hub. Update in place as understanding changes. Prefer provenance-linked change notes (link the `Source` or `Finding` that drove the update) over silent edits, and bump `updated:`. Material claims about the entity still belong in `Findings/`, with this note linking to them.

## Sensitivity

Set `sensitivity:` to the strictest tier any fact in the note warrants. A customer or vendor identity is often `confidential`; internal-only terms are usually `internal`. When unsure, classify up. Never place secrets (keys, tokens, credentials) in the body.
