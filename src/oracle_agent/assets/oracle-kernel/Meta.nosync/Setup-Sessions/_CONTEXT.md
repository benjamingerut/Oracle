# Setup-Sessions

One note per bootstrap / setup-interview session. A setup-session note records
what was decided during that session, what was deferred, and what the oracle
still needs from the admin before it is fully operational. It is the running
memory of the bootstrap process itself.

## What a setup-session note captures

- **Date & participants** — who ran the session.
- **Decided** — concrete configuration set this session (roles, connectors,
  processing matrix, backup policy, lanes).
- **Deferred** — what was explicitly postponed, and why, so "inert but safe" is
  never mistaken for "done".
- **Open asks** — what the oracle needs from the admin next.
- **Links** — to the ADR(s) and `Improvements/` notes the session produced.

## Discipline

The setup interview never loosens the security or governance baseline — defaults
stay strict until an admin deliberately changes them. Record each loosening as a
decision with a named approver. Cross-reference `BOOTSTRAP-STATUS.md`, which
holds the explicit maturity ladder; a setup-session note explains *why* the
oracle is where it is on that ladder.

## Type

`type: retrospective` (a setup session is a retrospective on the bootstrap).
Usually `internal`.
