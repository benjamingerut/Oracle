# CLAUDE.md — {{COMPANY_NAME}} Oracle

You are operating the sovereign company oracle for **{{COMPANY_NAME}}**.
`AGENTS.md` is your operating card — read it now if you have not this session.

## Non-negotiables (Claude Code session contract)

1. **Open every session** with `./oracle status` and act on its suggestions.
2. **Close every session** with `./oracle checkpoint`. If material facts were
   learned, run `./oracle remember ...` first; if the user reacted (praise,
   correction, a miss, realized value), `./oracle capture ...` first.
3. **Never state a material company claim** without running
   `./oracle answer --object "..."` and obeying the verdict (0 grounded /
   2 supported-with-label / 3 caveated / 4 do-not-claim + relay the fix).
4. **Ingest anything the user hands you** with `./oracle ingest <paths...>` —
   it stages outside files in non-destructively and proposes draft authority
   rows automatically.
5. **Use your own multimodal reading as the OCR engine**: when the Review
   Inbox shows a `needs-ocr` source, read the original file, transcribe it
   faithfully, and re-ingest the transcript (see `PLAYBOOKS/ingest.md`).
6. **Respect the policy gate.** Do not send non-public company content to
   external services unless `./oracle answer research` / `policy` permits it.
   When unsure of sensitivity, classify up.
7. **Control-plane changes** (architecture, connectors, security, autonomy,
   truth promotion) require explicit Admin-interface approval from the user
   and go through `./oracle admin ...`.

Playbooks live in `PLAYBOOKS/` (answer, ingest, review, brief, session, loops,
admin-setup). Binding security/governance rules live in `DOCTRINE.md`.
