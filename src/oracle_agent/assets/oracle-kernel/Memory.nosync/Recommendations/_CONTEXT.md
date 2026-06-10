# Recommendations

Accountable oracle advice. A recommendation is a bet the oracle makes, scored later against what actually happened — not against whether a human approved it.

## What belongs here

One note per recommendation. Use `type: recommendation`. The original `action`, `rationale`, `evidence`, `baseline`, and `expected_signal` are **immutable** — they are the bet as placed, and the schema requires every one. A separate, mutable `adjudication:` block records the verdict over time: the observed signal, the observed Decisions and value_events, and when it was adjudicated.

Recommendations are adjudicated against **observed reality** (Decisions taken, value_events captured), never by asking a human to approve. Do not ask "do you accept this recommendation?" — instead watch what the organization does and let `recommendation.py` score it.

## Mutability

Split mutability. The five original fields never change; to revise the advice itself, write a **new** recommendation and supersede the old one. Only the `adjudication:` block and `status:` (`open` -> `supported` | `contradicted` | `superseded` | `retired`) update in place. The content hash covers the immutable portion; lint catches tampering with the original bet.

## Sensitivity

Set `sensitivity:` to the strictest tier of the evidence and the action's subject matter — strategic recommendations are usually `confidential` or `restricted`. A recommendation that would reveal an unannounced move should be `restricted`. Classify up when in doubt; no secrets in the body.
