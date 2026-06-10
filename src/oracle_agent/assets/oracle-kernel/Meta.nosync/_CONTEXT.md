# Meta.nosync — the oracle's self-memory

This tree is the oracle's memory **about itself**: how it is run, where it
helps, where it fails, what it has decided about its own architecture, and the
loops that keep it improving. Company facts live in `Memory.nosync/`; this tree
holds only the oracle's operational self-knowledge.

Keep the two cleanly separated. If a self-improvement record happens to assert a
company fact (e.g. a retrospective notices a real metric), create the company
fact in `Memory.nosync/` and link to it from here — do not let company facts
accrete inside Meta.

## What lives here

| Folder | Holds | Behavioral type |
| --- | --- | --- |
| `User-Models/` | Models of each user the oracle serves — goals, decision style, what they value, how they read output. | `user_model` |
| `Architecture-Components/` | One note per moving part of the oracle (a tool, a lane, a connector class, an enforcer). | `architecture_component` |
| `Value-Scorecards/` | Periodic scorecards tracking whether the oracle helped the user understand, decide, act, avoid risk, or discover opportunity. | `value_scorecard` |
| `Sessions/` | Episodic records of material sessions: request, answer/work summary, business objects, sources/tools/skills used, and structured memory signals. | `session` |
| `Loops/` | The recurring improvement processes. Active loops are real runnable records (`runner`, `last_run`, `next_review`). | `loop` |
| `Autonomy/` | The scoped-autonomy allowlist (`autonomy.yml`, OFF by default) and the `KILL-SWITCH` sentinel. | config |
| `Architecture-Decisions/` | ADRs — durable decisions about the oracle's own shape. | `architecture_decision` |
| `Feedback/` | Narrative feedback notes that contextualize raw `feedback_event` ledger rows. | `feedback_event` |
| `Value-Events/` | Narrative value notes that contextualize raw `value_event` ledger rows. | `value_event` |
| `Failure-Events/` | Narrative failure notes that contextualize raw `failure_event` ledger rows. | `failure_event` |
| `Improvements/` | Concrete improvements proposed or applied to the oracle. | `improvement` |
| `Retrospectives/` | Periodic reviews of whether the architecture itself should change. | `retrospective` |
| `Security-Events/` | Narrative security notes that contextualize raw `export_event` / `redaction_event` / `action_event` ledger rows. | various |
| `Setup-Sessions/` | A note per bootstrap / setup-interview session: what was decided and what is still deferred. | `retrospective` |
| `ledgers/` | The durable append-only registries (`*.jsonl`). **Tracked in git, metadata-only.** | — |

## Events: ledger vs note

The append-only **ledger** rows in `ledgers/` are the durable, machine-readable
record (written via `_tools/ledger.py`, never edited by hand). A **note** in the
matching folder is optional narrative context that a human or the agent writes
when an event deserves a story — it links back to the ledger `drop_id`. The
ledger is the source of truth; the note is the gloss.

Session records are intake for `memory-matriculation`, not a permanent home for
company facts. Learned business information from sessions should be decomposed
into `Memory.nosync/Findings/`, `Questions/`, `Contradictions/`, `Queries/`, or
the right mutable hub.

## Sensitivity

Most Meta notes are `internal`. `User-Models/` may be `confidential` (they model
a person). `Security-Events/` may be `confidential` or higher. Never paste a
secret value into any note here — secrets live only in `.env.nosync` and are
referenced by variable name.

## Reliability note

The ledgers under `ledgers/` are the spine of self-improvement and autonomy.
They are append-only and corruption-tolerant: `ledger.load()` quarantines a bad
line rather than bricking the file. Do not hand-edit them; use the `ledger.py`
CLI (`verify` / `repair` / `render`) if a row looks wrong.
