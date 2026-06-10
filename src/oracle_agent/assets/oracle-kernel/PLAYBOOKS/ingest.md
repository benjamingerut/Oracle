# Playbook: ingesting material

Use this whenever material arrives: files, folders, exports, decks, contracts,
meeting notes, pastes, transcripts, connector pulls. The pipeline is
non-destructive — originals are never moved or altered.

## The one command

```
./oracle ingest <files and/or folders...> \
  [--business-object "<object>"] [--source-system "<authority label>"] \
  [--sensitivity <floor>] [--connector <id>] [--actor <who>] [--role <role>]
```

What happens per file: stage (outside paths are copied into
`Workproduct.nosync/_INPUT` with hash verification) → extract → chunk →
classify sensitivity (stricter-row-wins; your `--sensitivity` is a floor) →
index → immutable Source record → **draft truth-map row proposal** when a
business object is named → contradiction-candidate check.

## Make evidence count: name the object and the system

`--business-object` + `--source-system` are what turn a file into *answerable
evidence*: they wire the Source to the truth map, so
`./oracle answer --object ...` upgrades from refused to supported in the same
session. Ingesting without them still preserves and indexes the material, but
nothing can cite it as authority until an admin wires it later.

- Paste/transcript with no file? Write it to a temp file first, then ingest.
  Record provenance in the body (who said it, when).
- Non-admin role with authority metadata: the Source persists as an
  `authority-candidate` (evidence, not authority) and surfaces in the Review
  Inbox for admin wiring. This is the designed path, not an error.

## Scanned PDFs and images: you are the OCR engine

The stdlib kernel cannot read pixels. When extraction yields no text the
Source is tagged `needs-ocr` and lands in the Review Inbox. The operating
agent (you, if multimodal) closes it:

1. Open and read the ORIGINAL staged file with your own vision/reading.
2. Transcribe faithfully — content, tables, dates, signatures noted as seen.
3. Write the transcript to a file and ingest it with provenance:
   ```
   ./oracle ingest <transcript.md> --business-object "..." \
     --source-system "<original system>" --actor <you>
   ```
   In the transcript body state: `derivation: agent-ocr of <original source id>`.
4. The original Source stays as the immutable raw evidence; the transcript is
   the searchable derivation citing it.

## Live systems: connectors

A connector manifest (`Connectors/`) represents an external system. Two pull
styles, declared by the manifest's `access_mode`:

- `folder`/`file_drop` — the deterministic `localfolder` reference connector
  pulls files from a configured location through the same pipeline.
- `api`/`mcp`/`cli`/`manual` — the operating agent executes the pull (MCP
  tools, APIs, exports) following the manifest's documented steps, then hands
  files to `./oracle ingest --connector <id> --source-system <id>`. The
  kernel still enforces containment, classification, dedup, and ledgers.

Installing or authorizing a connector is admin work (`PLAYBOOKS/admin-setup.md`).

## After a batch

- Check the result line per file; `NEEDS-OCR` items go to the Review Inbox.
- `./oracle review` — competing authority claims and proposed rows appear here.
- `./oracle admin truth validate` — see which objects are now promotable.
- Large or sensitive batches: confirm the classifier's sensitivity labels match
  reality; when in doubt, classify up (`DOCTRINE.md` §2).
