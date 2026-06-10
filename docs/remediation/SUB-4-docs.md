# SUB-4 — Docs Reconciliation (Phase 4)

Runs after code phases 1–3 so stamps reflect the post-fix reality. Two
file-disjoint tasks in parallel.

- D1 owns: `README.md`, `docs/DESIGN.md`, `docs/SPEC.md`, `docs/STRESS.md`
- D2 owns: `docs/roadmap/PHASE-1…6*.md` (defect corrections only — the
  structural roadmap amendment is Phase 5 / SUB-5, do not do it here),
  `docs/roadmap/ROADMAP.md` (only the two line-item fixes listed)

Both tasks: verify every stamp/claim against the post-remediation code before
writing it (grep/read; cite file:line in the doc where the doc style already
does). Never stamp "✅/resolved" anything the code does not enforce; the I6
discipline (every guarantee names its enforcer or is stamped advisory)
governs.

---

## D1 — Core docs

Re-verify and correct, in light of Phases 1–3 fixes:

1. **STRESS.md re-stamps:**
   - C1/H1 (ceiling on every tool output): now true via external schema drop
     of checkpoint/loops_due (S4). Update the mechanism description.
   - A4 (flock around every run_verb incl. gateway): now true (S2). Update.
   - M5 (sensitivity-token stripping): describe the real two-layer mechanism
     (`--q=` packing + strip), name the now-existing test.
   - M4 (gateway-sourced memory "excluded from authority-bearing retrieval"):
     code provides `--actor gateway_user:<id>` attribution + kernel review
     gating only — re-stamp accurately (advisory/partial, name what enforces
     what).
2. **SPEC.md corrections:**
   - S4 brief line-scan: describe what S4 (Phase 2) actually implemented; if
     the implementation reported a BLOCKER (no kernel markers), stamp the
     line-scan advisory/not-implemented and describe the availability gate
     instead.
   - S4: checkpoint/loops_due external-drop documented.
   - S3: fix `local_deterministic` ceiling row to match the kernel matrix;
     replace the fictitious `LOOPBACK = {…,"0.0.0.0"}` constant with the real
     post-S1 literal-loopback rule (and note `0.0.0.0` is external).
   - S5 `minimized_status` return type (dict, not str).
   - S6: remove SIGHUP claim (restart-only) or stamp future.
   - S7: ledger rows are written directly by the shell process (metadata-only)
     — fix the "via ledger.py subprocess discipline" claim; fix cache wording
     (now true LRU per S2; idle eviction only if S2 added it — verify).
   - S8.2: doctor claims — instance arg now honored (S3); remove/implement
     "serve lock freshness" claim accurately.
   - S10: the enforcer-test table lists only tests that exist (the Phase 2
     additions close most; verify each name against the test files).
3. **README.md:** fix "22 spec findings" → the actual STRESS ID count; add
   the kernel-fix highlights only if README already enumerates such detail
   (don't bloat); ensure the Honest Limits section still matches reality
   post-fixes (e.g. eviction-based context still true; "self-improving"
   phrasing must be honest: the machinery exists, the unattended actuator is
   roadmap work — one clause suffices).
4. **DESIGN.md:** fix the "39 tools" count to a defined, correct number
   (state the counting rule); any D-section claims invalidated by S1–S4
   (loopback classification description, gateway locking) updated.
5. Update test counts wherever stated (README/ROADMAP mention 626 — count
   post-remediation and update; D2 owns ROADMAP's two line items, so D1
   coordinates by leaving ROADMAP counts alone).

**Acceptance:** every changed stamp/claim cites or matches a real enforcer;
no stale counts; no claim contradicts the code as of this phase.

---

## D2 — Phase-spec defect corrections (roadmap files)

1. **PHASE-2:** pin `Ceiling.minimized` — define it as capped at
   `confidential` for `local_agent`+confined (NOT "highest allow-minimized
   tier", which computes to `secret` and re-opens STRESS H2). Make the frozen
   interface, the matrix reference, and P2-T3's acceptance criteria agree.
   Fix the `policy.py` path reference to `_tools/policy.py`.
2. **PHASE-1:**
   - P1-T6 (restore): the kernel `backup.py` CLI has only `run` and
     `verify-restore`; there is no restore-from-archive. Re-scope the task as
     upstream-kernel work (mark like P2-T1's upstream pattern) or an explicit
     shell-side restore implementation; remove the false "wraps backup.py
     restore" framing.
   - P1-T4 (upgrade): specify where `--from-kernel <dir>` comes from (the
     shell package's own vendored `assets/oracle-kernel` of the *newer*
     installed package version; spell out the source-of-truth and the flow).
3. **PHASE-5:** P5-T2 — no kernel write verb accepts `--role` today; mark the
   kernel CLI change as upstream work with its own task line, same pattern as
   other upstream-flagged tasks.
4. **PHASE-4:** P4-T2 (Slack) — replace the unimplementable "Events API over
   the local HTTP server / urllib ws-less long-poll" framing with an honest
   feasibility note and two viable options: (a) Socket Mode via an OPTIONAL
   websocket dependency (I1 graceful-degradation clause: adapter disabled
   when lib absent), or (b) documented reverse-proxy/tunnel requirement for
   Events API. Mark the decision as a phase-opening checkpoint.
5. **All six phase specs:** fix "Read first: `docs/ROADMAP.md`" → 
   `docs/roadmap/ROADMAP.md`.
6. **PHASE-3:** P3-T3 "pairing invariants from P1" → cite v1 STRESS I1
   correctly.
7. **ROADMAP.md (two line items only):** P1 row "eval-harness skeleton" →
   what P1 actually delivers (`testkit.py`); P4 row "streaming" → "optional
   typing indicators". (Anything structural waits for SUB-5.)

**Acceptance:** a team reading any phase spec finds no reference to a
nonexistent CLI/flag/file; PHASE-2's ceiling definition is self-consistent;
all path references resolve.
