# Sources

Immutable snapshots of evidence: connector pulls, documents, testimony, meetings, schema snapshots, artifacts, web captures, and manual observations. Every material claim the oracle makes should trace back to a Source.

## What belongs here

One note per piece of evidence, as it existed at a point in time. Use `type: source`. A load-bearing source records provenance, raw location (in `_data.nosync/` or `Workproduct.nosync/_INPUT/`), a `content_sha256`, sensitivity, an as-of date, and a **grain card** (what one row/record means, the unit, the time basis, and known gaps). Most Source notes are created by `source_record.py` during ingestion, which registers the content hash in the ledger.

## Mutability

Immutable. If the underlying input changes, **write a new source** and supersede the old one (`supersedes:` / `superseded_by:`); do not edit old evidence. The only permitted edit is a legal/security **redaction**, which must go through `policy.record_redaction` and leave a redaction_event. `oracle_lint` FAILS on any on-disk/ledger hash mismatch for a source, so silent edits are caught mechanically.

## Sensitivity

A Source inherits the sensitivity of the data it captured — set `sensitivity:` at log time using the intake classifier's stricter-row-wins result, and never below the source material's true tier. Raw secrets must never be captured into a Source body; if evidence contains a secret, redact it and record the redaction. Sensitivity here gates whether the evidence may be processed externally or exported, via `policy.py`.
