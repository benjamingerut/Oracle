# DESIGN — Oracle: governed knowledge kernel + installable system agent

**Status: adopted.** This document records what the combined system is, what it
takes from each parent (Oracle Spawn kernel, Hermes Agent), and the load-bearing
design decisions. The binding interface contract lives in `SPEC.md`; the
adversarial review record lives in `STRESS.md`.

## 1. What this is

A **sovereign company oracle you can install like a product**. Two layers:

| Layer | Origin | Role |
|---|---|---|
| **Kernel** (`src/oracle_agent/assets/oracle-kernel`) | Oracle Spawn (vendored verbatim) | The deterministic, stdlib-only epistemic substrate: graduated answer authority, truth map, immutable source records + ledgers, sensitivity×environment policy matrix, review inbox, loops, earned autonomy, doctrine→enforcer lint. 40 tool modules (38 CLI groups via `oracle_cli.py`), 594 kernel tests. |
| **Shell** (`src/oracle_agent/*`) | New code, Hermes-patterned | The installable system-agent layer: global `oracle` command, setup wizard, model-agnostic LLM agent loop whose *only tools are kernel verbs*, scheduler daemon, Telegram gateway, doctor, installer. |

The kernel is never modified by the shell. Spawned oracle roots remain fully
self-contained (each carries its own `_tools/` + `./oracle` wrapper) and keep
working if this package is uninstalled — sovereignty survives distribution.

## 2. What we took from each parent

**From Oracle Spawn (kept, untouched):** the entire kernel; the spawn flow
(tool-layer-only `--force` merges, manifest stamping, post-spawn audit+lint
gate); the doctrine→enforcer discipline; graduated authority (exit 0/2/3/4);
autonomy ladder with kill-switch-first; ledgers and immutability.

**From Hermes Agent (patterns, reimplemented stdlib-only):**
- One-command install + profile directory (`~/.oracle`) + `setup` wizard +
  `doctor` + self-update path.
- A provider-agnostic OpenAI-compatible chat loop with native tool calling,
  error classification, jittered retry, and a fallback model.
- Prompt-caching discipline: byte-stable system prompt for the whole session;
  memory snapshots frozen at session start (Hermes AGENTS.md: "per-conversation
  prompt caching is sacred").
- A durable scheduler daemon (Hermes cron) — here it ticks the kernel's own
  `harness.py`, so the autonomy gate stays in front of every headless action.
- Messaging-gateway architecture with per-user allowlists (Hermes gateway) —
  here Telegram first, with chat user IDs doubling as real actor identity.
- The tool-registry restraint lesson: Hermes ships 65 tool schemas per call and
  documents the bloat as a problem. Our loop exposes **at most 10** verbs.

**Deliberately NOT taken from Hermes:** its dependency stack (uv, Node TUI,
ripgrep, ffmpeg, provider SDKs), 20-platform gateway breadth, skills-hub,
plugins, voice. Zero runtime dependencies is a kernel guarantee we extend to
the whole product.

## 3. Load-bearing decisions

**D1 — Everything is stdlib.** LLM client = `urllib.request` against
`/v1/chat/completions`; gateway = Telegram long-polling via `urllib`; state =
`json` + `sqlite3` + the kernel's own ledgers. `pip install oracle-agent` pulls
nothing. CI can prove it the same way the kernel does.

**D2 — The model only acts through kernel chokepoints.** The agent loop's tool
surface is a fixed allowlist of kernel verbs executed as argv subprocesses of
the *root's own* `./oracle` wrapper (never `shell=True`, never the vendored
kernel — the root's kernel version is authoritative for that root). Every
governance property the kernel enforces (containment, immutability, policy,
review-gating) therefore survives the new runtime unmodified.

**D3 — The model picker is policy-gated.** The shell classifies the provider
endpoint as `local_agent` (provably loopback — literal `localhost` or an IPv4/
IPv6 address whose `is_loopback` is true; DNS is NOT consulted; `0.0.0.0` is
external) or `external` (everything else, fail-closed) and asks the **root's
own** policy gate (via its CLI, never by importing root code) for the
sensitivity ceiling: the highest label whose verdict is exactly `allow` —
`allow-minimized` is not a grant. The ceiling is enforced in code on the
*output of every tool and on the system prompt*, not just on search input. Net
effect: an external API model sees `public` only; a local model sees up to
`internal`; confidential+ stays out of every model context until a real
minimizer exists (see STRESS H2). Confidential-tier unlock is roadmap Phase 2
work. This is the matrix Oracle always had, now wired to model selection —
something Hermes cannot offer.

**D4 — Surfaces have different blast radii.** Local `oracle chat` (operator's
own machine) exposes the full user-role verb set. Gateway sessions (remote
chat users) get a *reduced* read-and-capture tool surface (no local-path
ingest), a stricter default retrieval ceiling (`internal`), and every turn is
ledgered. Control-plane (admin) verbs are exposed to **no** model on any
surface; the loop relays the kernel's `suggested_fix` commands for the human
to run.

**D5 — Authority labeling is enforced by the loop, not requested of the
model.** The loop tracks the answer-protocol envelopes obtained during a turn
and appends a deterministic authority footer (grounded/supported/caveated, or
"conversational — no authority claimed"). A model that skips the protocol
can't fake the label.

**D6 — Headless = kernel harness, nothing else.** `oracle serve` never invents
its own background actions; it ticks `harness.py --once` per instance under
the per-root flock, so the kill-switch, autonomy level, allowlist and blast
caps decide everything. Every gateway turn also holds the per-root flock,
serializing chat and serve writes across processes (STRESS A4). Autonomy ships
OFF; `serve` with autonomy off is a safe no-op plus gateway.

**D7 — Identity.** The kernel's `--actor/--role` flags are advisory-by-design.
The gateway upgrades this: Telegram user IDs are verified by the platform, the
allowlist maps them to roles, and the mapping can only be edited on the host
(never via chat — the prompt-injection refusal is structural).

## 4. Shape of the product

```
~/.oracle/                    # profile (shell-owned)
  config.json                 # provider, instances, gateway (NO secrets)
  .env                        # secrets only, chmod 600
  logs/                       # shell + gateway logs
  serve.lock                  # single-daemon lock
<any path>/MyCompany/         # each oracle root (kernel-owned, sovereign)
  oracle  oracle.yml  _tools/  Memory.nosync/  Meta.nosync/  ...
```

CLI: `oracle setup` (wizard) · `oracle spawn` · `oracle chat [-m msg]` ·
`oracle serve` · `oracle doctor` · `oracle model` · `oracle instances` ·
`oracle kernel <name> -- <verb...>` (pass-through to a root's own CLI).

## 5. Honest limits (v1)

- The agent loop's *use* of the answer protocol for free-form prose remains
  advisory (as in the kernel); D5 makes the labeling honest, not the prose.
- Gateway v1 is Telegram only; the adapter interface is the extension point.
- No TUI; plain stdin/stdout REPL with streaming-free turns.
- Context management is truncation-based (per-tool-result caps + oldest-turn
  eviction), not summarization.
