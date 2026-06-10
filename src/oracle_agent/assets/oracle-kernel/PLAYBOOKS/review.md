# Playbook: working the Review Inbox

The Review Inbox is the oracle's single closure mechanism: every state that
needs a decision flows into one ranked queue. Nothing rots silently — if it's
pending, it's here. Work it top-down; the queue is self-cleaning (doing the
action removes the item on the next build).

```
./oracle review            # ranked queue (top 15)
./oracle review --all      # everything
./oracle review summary    # counts by kind
```

## Item kinds, in priority order

1. **contradiction** (must_resolve first) — open conflicts in memory, plus
   *authority-conflict candidates* (two systems claiming the same object).
   Adjudicate: read both claims and their sources; decide which authority
   governs, or record/keep a Contradiction note with the residual; update the
   note's status and `contradiction_class` (`must_resolve` blocks clean
   answers until resolved). Never silently average the values.
2. **promotable-row** — a draft truth-map row whose authority has resolving
   evidence. If the source and join keys check out:
   `./oracle admin truth promote --object "<obj>" --actor <admin>` (admin
   capability; flips answers from supported to grounded).
3. **authority-candidate** — a Source whose authority claim was captured for
   admin review. Read its "Authority candidate" section; if legitimate, wire
   it (`admin truth propose --source ...`, then promote when evidenced).
4. **needs-ocr** — transcribe with your own multimodal reading and re-ingest
   (`PLAYBOOKS/ingest.md`).
5. **needs-review-finding** — a derived claim awaiting review. Check the claim
   against its cited source: confirm (`status: confirmed`), correct it (write
   a superseding finding), or retire it. Overdue items are flagged.
6. **needs-review-query** — a session-derived retrieval strategy parked by the
   dreaming pass. If it is worth reusing, fill in the exact query text and set
   `status: active`; otherwise retire it. Overdue items are flagged.
7. **stale-question** — an open question past its budget. Check whether
   ingested evidence now answers it; answer and close, plan research, or
   escalate to the admin in the next brief.
8. **stale-model** — a Model past its staleness budget. Re-validate against
   current findings (run `./oracle loops run insight-synthesis` for the
   worklist), update or supersede, stamp `last_validated`.
9. **unconsumed-events** — feedback/value/failure events waiting for a
   learning loop: `./oracle loops run <loop-id>`, then complete with
   `--consume-all` once actually processed.

## Cadence

- `./oracle status` shows the inbox count at session start; the top item is
  always in "Do next".
- Every leadership brief lists the top pending decisions, so an unworked inbox
  becomes visible to the leader rather than silently stale.
- Budgets (question/model staleness, finding warn age) are configurable in
  `oracle.yml` under `review:`; defaults are 14/60/7 days.

## Discipline

- Confirming a finding is a real review, not a rubber stamp: open the cited
  source and check the claim against it.
- Resolution writes go to the canonical notes (Findings/Contradictions/...),
  not to ad-hoc files; immutable types are superseded, never edited
  (`DOCTRINE.md` §1).
- If an item cannot be resolved without the admin or the leader, say so in the
  session summary and leave it — it will keep surfacing by design.
