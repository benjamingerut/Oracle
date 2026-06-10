# Assets

Instruments of value the company holds, owes, or relies on: physical, financial, digital, contractual, and strategic assets.

## What belongs here

One note per asset. Use `type: asset` with `subtype:` one of: `physical`, `financial`, `digital`, `contractual`, `strategic`. Examples: a piece of equipment or property (physical), a bank/brokerage position or receivable (financial), a dataset/domain/codebase-as-asset (digital), an MSA/lease/IP-license (contractual), a moat or brand (strategic).

Distinguish from `Systems/`: a database *as a running thing the oracle reads* is a `system`; the dataset *as owned value* can be an `asset`. Use judgment; do not duplicate — link instead.

## Mutability

Mutable hub. Value, status, and terms change — update in place and bump `updated:`. Point-in-time valuations or measured balances belong in `Metrics/` or `Findings/` and are linked from here, so the asset note stays a stable hub.

## Sensitivity

Financial and contractual assets are usually `confidential` or `restricted`. Strategic assets (moats, unannounced plans) can be `restricted`. Classify up when terms are non-public. Never embed credentials, account numbers, or contract secrets verbatim — reference where they live and keep secrets in `.env.nosync`.
