# Oracle

**A sovereign company oracle you install like a product.** Oracle combines a
governed, deterministic knowledge kernel (graduated answer authority, truth
map, immutable ledgers, sensitivity policy, earned autonomy) with an
installable system-agent shell (a global `oracle` command, a model-agnostic
LLM chat loop, a scheduler daemon, and a Telegram gateway).

Zero runtime dependencies. The kernel **and** the shell are pure Python
stdlib — `pip install` pulls nothing, a fresh Python 3.10+ box runs it
immediately, and every spawned oracle root remains self-contained even if
this package is uninstalled.

## Why this exists

LLM agents remember things; an Oracle *knows* things, with stated authority.
Every material claim runs through a graduated answer protocol:

| verdict | meaning | obligation |
|---|---|---|
| **grounded** (0) | confirmed authority + fresh evidence | state it plainly |
| **supported** (2) | evidence under draft authority | label "authority not confirmed" |
| **caveated** (3) | stale evidence / open contradiction | answer only with the caveat |
| **refused** (4) | no authority, no evidence | do not claim; relay the fix |

The chat loop appends that verdict as a deterministic footer computed from the
protocol envelopes — a model that skips the protocol gets labeled
"conversational"; it cannot fabricate a grounded answer.

**The model picker is policy-gated.** The shell classifies your LLM endpoint:
provably-loopback (Ollama etc.) → `local_agent`, anything else → `external`
(fail-closed). The oracle's own policy matrix then caps what retrieval may
enter the model's context: an external API model sees `public` only; a local
model sees up to `internal`; confidential+ never enters any model context in
v1. Enforced in code at dispatch, not requested in a prompt.

## Install

```sh
sh installer/install.sh --from-dir /path/to/this/repo   # or --git-url URL
oracle setup        # wizard: spawn an oracle, pick a provider, store keys
oracle doctor       # verify everything
```

Layout: source + venv under `~/.oracle/`, command symlinked into
`~/.local/bin`, secrets in `~/.oracle/.env` (0600), settings in
`~/.oracle/config.json` (never holds a secret — structurally refused).

## Use

```sh
oracle chat                       # REPL against your default instance
oracle chat acme -m "What's our support SLA?"
oracle serve                      # daemon: scheduled loops + Telegram gateway
oracle kernel acme -- ingest batch ~/Documents/handbook.pdf
oracle kernel acme -- review     # the kernel CLI, passed through verbatim
oracle model set --provider ollama --model llama3.1   # swap models anytime
```

The agent's **only** capabilities are ten kernel verbs run as argv
subprocesses of the root's own `./oracle` CLI — status, search, answer,
review, ingest, remember, capture, brief, checkpoint, loops-due. No shell, no
filesystem, no control plane. Admin operations (truth promotion, autonomy,
connectors, upgrades) are never exposed to any model on any surface; the loop
relays the kernel's suggested commands for *you* to run.

### Telegram

```jsonc
// ~/.oracle/config.json
"gateway": {"telegram": {
  "enabled": true,
  "token_env": "ORACLE_TELEGRAM_TOKEN",
  "allowlist": {"123456789": {"role": "user", "instance": "acme"}}
}}
```

`oracle serve` then answers allowlisted users in **private chats only**
(group leaks are structurally impossible), with a reduced tool surface, an
`internal` ceiling, per-user write rate limits, and a metadata-only ledger row
per turn. Access changes happen only here, on this machine — there is no tool
a chat message could invoke to change them.

### Autonomy

Ships **off**. `oracle serve` ticks each instance's own `harness.py`, so the
kernel's chain — kill-switch first, then autonomy level, allowlist, blast-radius
caps — decides everything headless. Turning it on is a deliberate admin act in
the root's `Meta.nosync/Autonomy/autonomy.yml`.

## Architecture

```
┌─ shell (this package, stdlib-only) ─────────────────────────────┐
│  oracle CLI · setup wizard · doctor · installer                 │
│  LLM client (any /v1/chat/completions; redirects blocked)       │
│  agent loop (byte-stable prompt · authority footer · eviction)  │
│  policy bridge (endpoint → environment → sensitivity ceiling)   │
│  scheduler daemon (per-root locks) · Telegram gateway           │
└──────────────── every action = argv subprocess ─────────────────┘
┌─ kernel (vendored, spawned per company, stdlib-only) ───────────┐
│  answer protocol · truth map · knowledge index (FTS5)           │
│  immutable source records + ledgers · review inbox · loops      │
│  policy matrix · roles · autonomy gate · lint (doctrine→enforcer)│
└─────────────────────────────────────────────────────────────────┘
```

Design rationale: `docs/DESIGN.md`. Binding interfaces: `docs/SPEC.md`.
Adversarial review record (23 spec findings + 4 implementation findings, all
resolved or accepted-and-documented): `docs/STRESS.md`.

## Roadmap

The forward arc from v1.0 to the final best state — confidential-tier
minimization, forced grounding, a multi-surface gateway, fleet operations, and
continuous evaluation — is in `docs/roadmap/`. `ROADMAP.md` is the index and
rationale; each `PHASE-N-*.md` is a standalone spec (frozen interfaces, task
breakdown with IDs, acceptance criteria, test plans, definition of done)
written to drive agentic team development.

## Verify

```sh
make check    # manifest → spawn → audit → lint → secret scans → 753 tests
```

## Honest limits (v1)

- The model's *use* of the answer protocol in free prose is advisory (as in
  the kernel); the footer makes the labeling honest, not the prose.
- `allow-minimized` sensitivity tiers are not auto-released — no minimizer
  exists yet, so confidential+ stays out of model context entirely.
- Telegram is the only gateway; the adapter interface is the extension seam.
- POSIX-only daemon (fcntl locks). Context management is eviction-based.

## Credits

The kernel is Oracle Spawn. The shell's install/UX/loop patterns are informed
by [Nous Research's Hermes Agent](https://github.com/NousResearch/hermes-agent)
(MIT) — patterns reimplemented stdlib-only, no code vendored.
