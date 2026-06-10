# User-Models

One note per human the oracle serves. A user-model captures **how to be useful
to this specific person**: what they are trying to do, how they decide, what
they value in an answer, what they distrust, and how they like output shaped.

This is the hub the value-scorecard and the `user-feedback-learning` loop draw
on. When feedback says "you gave me too much hedging" or "I needed the number,
not the narrative", that learning is folded back into the relevant user-model.

## Behavioral type

`type: user_model`. Subtype is optional and usually omitted.

## What a good user-model captures

- **Role & mandate** — what they are accountable for.
- **Decision style** — fast/slow, data-first/intuition-first, risk posture.
- **What they value** — speed, certainty, optionality, defensibility, brevity.
- **Output preferences** — range vs point, prose vs table, depth they want.
- **Distrust triggers** — what makes them stop trusting the oracle.
- **Standing questions** — the recurring things they ask the oracle.

## Sensitivity

User-models model a real person and are usually `confidential`. Never include
anything the user would not want recorded about themselves.

## Discipline

Update the model from observed reality (feedback events, the shape of questions
asked), not from one offhand comment. A user-model is a living `model`-like note:
when it changes materially, note what changed and why in `## Change History`.
