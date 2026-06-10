# Truth Map

No source is globally authoritative. Authority is **by business object**.

This file is doctrine **and** data: the table below is machine-parsed by
`_tools/truth_map.py` and consumed by `_tools/answer_protocol.py` before any
material answer. It is normally edited THROUGH the tools — `./oracle admin
truth propose|promote|validate` — and ingest auto-proposes draft rows when
evidence names a business object. Keep it a single, well-formed GitHub-style
markdown table — one header row, one separator row, then one row per business
object. Do not split it, reorder the columns, or add a second table to this
file.

## How it is read (parser contract)

`truth_map.py` finds the **first** markdown table whose header row contains, at
minimum, the columns `Business object`, `Primary source`, `Freshness budget`, and
`Status`. Column matching is case-insensitive and trims surrounding whitespace, so
the human-readable headers below are also the machine keys. Every column is parsed;
the four named ones are load-bearing:

- **Business object** — the resolution key. `resolve(object)` matches an answer's
  named business object against this column (case-insensitive, slash- and
  whitespace-tolerant) and returns the row, or `None` when nothing claims authority.
- **Primary source** — the authority. An empty value or a literal `TBD` means *no
  source yet claims authority*; the answer protocol treats that as missing authority.
- **Freshness budget** — how stale the primary source may be before an answer must
  be caveated. Expressed as a duration (`30d`, `24h`, `7d`), `review on change`, or
  `document-specific`. `answer_protocol.py` compares the source's `as_of` against
  this budget to produce `fresh | stale | unknown`.
- **Status** — `draft` until the row has been confirmed against the live source;
  `confirmed` once the primary source and join keys are verified. On the graduated
  ladder, fresh evidence under a `draft` row yields **supported (exit 2)** answers
  with damped confidence; only a `confirmed` row grounds at exit 0. Promotion is
  an explicit admin act: `./oracle admin truth promote` (requires
  `change_truth_authority` and resolving evidence).

## The map

| Business object | Primary source | Corroborates | Join keys | Cannot prove | Freshness budget | First invariant | Status |
|---|---|---|---|---|---|---|---|
| Company identity / ownership | Admin directive + legal/company docs | finance/legal systems | legal entity ids | current operations alone | review on change | names/entities reconcile | draft |
| Customers / accounts | TBD connector | contracts, billing, product usage | customer/account ids | cash or actual usage alone | 7d | join coverage | draft |
| Revenue / invoices | TBD accounting/ERP | bank, CRM, contracts | invoice/customer ids | product engagement alone | 7d | totals reconcile | draft |
| Cash / bank | TBD finance/bank | accounting, board docs | account/date ids | accrual revenue | 24h | cash in - cash out = balance delta | draft |
| Product / service delivery | TBD ops/product systems | tickets, docs, customer feedback | product/customer ids | revenue without join | 24h | event/state totals | draft |
| People / org | TBD HR/admin docs | Slack/email/org docs | email/person ids | productivity/quality alone | 30d | active roster reconcile | draft |
| Legal / contracts | contracts repository | CRM/accounting | legal entity ids | actual performance | document-specific | executed version exists | draft |
| Strategy / plans | owner/admin docs | observed decisions, workproduct | doc ids | execution | review on change | plan vs observed action | draft |

## Discipline

- **Before a material answer, run the preflight.** With no row AND no ingested
  evidence the claim is refused (exit 4) and the envelope's `suggested_fix`
  names the commands that change that. With evidence but unconfirmed authority
  the answer ships labeled **supported (exit 2)** — honest, useful, upgradeable.
- A row's `Primary source` is the *authority of record*; `Corroborates` sources may
  agree but never substitute for the primary, and source echo is not independent
  confirmation (`DOCTRINE.md` §6).
- `Cannot prove` is binding: it records the questions this object's authority is
  structurally unable to answer. Do not let a convenient source answer them.
- When the primary source changes or a connector is wired:
  `./oracle admin truth propose --object ... --source ...` sets the source on a
  TBD row; `./oracle admin truth promote` confirms it once join keys are
  verified against live data. `./oracle admin truth validate` shows every
  row's authority, evidence count, freshness, and next step.
- `./oracle lint` parses this table; a malformed table (missing a load-bearing column,
  broken separator row, duplicate `Business object`) fails the lint gate.
