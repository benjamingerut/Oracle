# _STANDING

Home for **standing deliverables** — recurring, cadenced artifacts the oracle regenerates on a loop rather than one-off documents produced on request.

## What lives here

Dated outputs from `_tools/standing_deliverables.py` (`oracle deliverables gen <kind>`), such as:

- `contradiction-digest` — open contradictions ranked by decision-relevance.
- `rec-scorecard` — recommendations adjudicated against observed Decisions and value_events.
- `freshness-report` — sources past their freshness budget.
- `value-scorecard` — the oracle's value contribution over the period.

## Discipline

- **Every claim routes through the answer protocol.** `standing_deliverables.py` calls `answer_protocol.preflight` for each claim; any claim that returns exit 4 (no authority) is dropped, not shipped. No uncited claim ships in a standing deliverable.
- Files are **dated** (`YYYY-MM-DD_<kind>.md`) and written through `artifact_io.emit` under the policy gate — they are exports and obey the same export rules as `_OUTPUT/`.
- Standing deliverables are **views**, not durable atomic memory. A claim worth keeping decomposes into `Memory.nosync/`; the deliverable is the periodic snapshot.
- A new run emits a new dated file; prior editions are kept for trend, never silently overwritten.
