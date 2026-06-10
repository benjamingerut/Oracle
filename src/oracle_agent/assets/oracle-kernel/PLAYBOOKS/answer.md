# Playbook: answering a question

Use this whenever a response would contain a **material claim** — a fact,
number, conclusion, or recommendation someone could act on.

## The flow

1. **Name the business object(s).** "What's our runway?" touches `Cash / bank`
   (and maybe `Revenue / invoices`). A multi-object question runs the preflight
   once per object; the strictest verdict governs the overall answer framing.
2. **Decompose broad questions** into sub-questions with one object each, and
   gather evidence per sub-question:
   ```
   ./oracle search "<terms>" [--k 10] [--max-sensitivity <label>]
   ```
   Hits are reranked by source authority and recency; each hit carries its
   `source_id` for citation. Read the underlying Source notes for grain and
   caveats before relying on a chunk.
3. **Run the preflight** for each object:
   ```
   ./oracle answer --object "<business object>" [--question "..."]
   ```
4. **Obey the verdict:**

| Exit | Verdict | Obligation |
|---|---|---|
| 0 | grounded | Answer. Cite the authority source. Confidence as a range. |
| 2 | supported | Answer with the literal label "supported — authority not confirmed", cite the evidence, and include the envelope's upgrade command so the user can see the path to grounded. |
| 3 | caveated | Answer only with the caveat stated first (stale evidence, open must_resolve contradiction, or authority with no ingested evidence yet). |
| 4 | refused | Do not make the claim. Tell the user what is missing and relay the envelope's `suggested_fix` commands verbatim — running them upgrades the verdict, usually in the same session. |

5. **Compose the answer**: claims + citations + confidence + surfaced caveats
   + disconfirmers when decision-relevant. Note what the evidence *cannot*
   prove (the truth-map row's "Cannot prove" column is binding).
6. **Capture**: if the exchange taught you a durable fact or opened a question,
   `./oracle remember ...` before checkpoint.

## Exploratory public research (different path)

For public-topic research that needs no private company context externally:

```
./oracle answer research --question "<public research question>"
```

If company context would be included in an external prompt, add
`--includes-company-context --context-sensitivity <label>` — the processing
matrix decides, and a denial is final (exit 4). A research pass authorizes a
workflow, not an authoritative answer: cite public sources, label the output
exploratory, and convert durable conclusions into Sources + truth-map
authority before using them as material claims.

## Failure modes

- **Refused on a fresh oracle** is correct, not broken: ingest evidence, and
  the same object answers at exit 2 immediately (`PLAYBOOKS/ingest.md`).
- **Object name doesn't resolve**: check `./oracle admin truth rows` for the
  canonical names (matching is case/slash/whitespace-tolerant, not fuzzy).
- **Verdict seems wrong**: `./oracle admin truth validate` shows the per-row
  diagnosis (authority, evidence count, freshness) and the exact next step.
