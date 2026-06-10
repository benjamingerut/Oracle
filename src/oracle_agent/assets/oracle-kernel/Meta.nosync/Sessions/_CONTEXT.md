# Sessions

`Sessions/` stores episodic records of material interactions with the oracle:
the request, work performed, business objects discussed, sources/tools/skills
used, expensive retrieval steps, and structured claims/questions/conflicts the
agent observed.

This folder is **Meta memory**, not the canonical home for business truth. A
session record can preserve that a user said or an agent observed something, but
durable business information must be matriculated into the existing behavioral
stores:

- `Memory.nosync/Sources/` for evidence snapshots.
- `Memory.nosync/Findings/` for atomic learned claims.
- `Memory.nosync/Questions/` for unresolved questions.
- `Memory.nosync/Contradictions/` for conflicts.
- `Memory.nosync/Models/`, `Metrics/`, `Queries/`, and mutable hubs where the
  information belongs.

Run `oracle session-memory dream` or the `memory-matriculation` loop to
decompose pending session records. Derived MemPalace/Graphify artifacts created
from sessions are rebuildable access layers only; they are never answer
authority.
