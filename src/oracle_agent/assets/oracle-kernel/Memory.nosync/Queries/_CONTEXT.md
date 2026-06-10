# Queries

Saved, reusable analytical queries — the parameterized questions the oracle asks of its data sources often enough to be worth pinning down.

## What belongs here

One note per reusable query. Use `type: query`. Capture the intent, the exact query text (SQL, retrieval query, API call shape), the system it runs against, its parameters, and what a correct result looks like. A query note makes an analysis repeatable and auditable, and lets a `Metric` definition point to its canonical calculation.

Do not paste large result sets here — results are evidence and belong in `Sources/` or `_data.nosync/`. This is the *question*, not the answer.

## Mutability

Mutable hub. Queries get tuned as schemas change or definitions sharpen — keep the query text current and bump `updated:`. If a query's results contradict another source, open a `Contradiction`.

## Sensitivity

Query *text* is usually `internal`. Raise to `confidential` when the query embeds sensitive filters, identifiers, or reveals schema/strategy. Never embed credentials or connection strings — reference the `Systems/` note and the env var name. Results inherit the data's sensitivity, classified on the linked Source/Finding.
