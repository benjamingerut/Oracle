# People

Individual humans relevant to {{COMPANY_NAME}}: the admin, leaders, employees, key customers, vendors' contacts, advisors, counterparties.

## What belongs here

One note per real person. Use `type: person`. There is no per-person subtype enum value beyond the default; if a person's *role* relative to the company matters, capture it in the body and `tags:`, and link to the `Group` or `Entity` they belong to.

Do not file teams, departments, or cohorts here — those are `Groups/`. Do not invent placeholder people; create a note only for a person the oracle actually needs to reason about.

## Mutability

Mutable hub. Update in place as the relationship, role, or contact facts change, and bump `updated:`. Material claims about a person (e.g. a decision they made, a commitment) belong in `Findings/` or `Decisions/` and are linked from here.

## Sensitivity

People notes are PII by default. Set `sensitivity:` to at least `confidential`; use `restricted` for sensitive personal, legal, or compensation facts. Never store personal secrets, credentials, or government identifiers in the body. When in doubt, classify up — `policy.py` enforces processing and export ceilings off this field.
