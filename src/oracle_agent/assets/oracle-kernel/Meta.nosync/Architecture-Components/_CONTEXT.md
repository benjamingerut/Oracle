# Architecture-Components

One note per moving part of the oracle itself: a tool in `_tools/`, a workproduct
lane, a connector class, an enforcer, a ledger, a scheduler entrypoint. This is
the oracle's map of its own machinery — what each part does, what it depends on,
what guarantees it enforces, and how it can fail.

This hub feeds `architecture-retrospective` and `upgrade.py`: when the tool layer
changes, the affected component notes are the human-readable record of what moved
and why.

## Behavioral type

`type: architecture_component`. Subtype (optional) draws from the systems family
of the ontology enum, e.g. `application`, `process`, `workflow`.

## What a good component note captures

- **Responsibility** — the one job this component owns.
- **Chokepoint role** — if it is an enforcer (e.g. `safe_paths`, `policy`,
  `actions`, `answer_protocol`), state exactly what it makes impossible.
- **Dependencies** — what it imports / is imported by.
- **Guarantees** — what doctrine clause this component is the *enforcer* for
  (the Doctrine→Enforcer map lives here in human-readable form).
- **Failure modes** — how it degrades, and what the safe-default behavior is.
- **Tests** — which test file proves its guarantee.

## Discipline

A guarantee is only real if a component note names the tool that enforces it and
that tool is tested. If a component is the named enforcer for a SECURITY /
GOVERNANCE / PROCESSING-MATRIX clause, say so explicitly — `oracle_lint`
cross-checks the Doctrine→Enforcer map and fails the build on any unenforced
"must/required/denied" guarantee.
