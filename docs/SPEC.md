# SPEC — shell interface contract (binding)

Frozen interfaces for the shell layer. Implementation MUST match this spec;
deviations require updating this spec first. Everything is Python ≥3.10
stdlib-only. All new modules live under `src/oracle_agent/`.

Conventions: all functions that touch disk take explicit `Path`s; nothing
reads global state except `config.py`; every module has a focused test file
under `tests/shell/`.

---

## S1. `config.py` — profile, secrets, instances

Profile dir: `ORACLE_HOME` env var if set, else `~/.oracle`. Created on first
write with mode `0o700`.

```python
DEFAULT_CONFIG: dict          # documented defaults, deep-copied on load
def profile_dir() -> Path
def load_config() -> dict     # config.json merged over DEFAULT_CONFIG
def save_config(cfg: dict) -> None        # atomic write (tmp+rename), 0o600
def load_env_file() -> dict[str, str]     # parse ~/.oracle/.env KEY=VALUE
def set_env_secret(key: str, value: str) -> None  # upsert .env, chmod 0o600
def resolve_secret(env_key: str) -> str | None    # os.environ first, then .env
```

`config.json` schema (no secrets anywhere in it — lint-checked by doctor):

```json
{
  "provider": {
    "name": "anthropic|openai|openrouter|ollama|custom",
    "base_url": "https://api.openai.com/v1",
    "model": "...",
    "fallback_model": null,
    "api_key_env": "ORACLE_LLM_API_KEY",
    "max_tokens": 4096
  },
  "chat": {"max_iterations": 20, "tool_result_max_chars": 20000,
            "history_max_chars": 400000},
  "serve": {"tick_seconds": 300},
  "gateway": {
    "telegram": {
      "enabled": false,
      "token_env": "ORACLE_TELEGRAM_TOKEN",
      "allowlist": {"<telegram_user_id>": {"role": "user", "instance": "<name>"}},
      "max_sensitivity": "internal"
    }
  },
  "instances": {"<name>": {"root": "/abs/path"}},
  "default_instance": null
}
```

Secret resolution NEVER logs values. `save_config` refuses (ValueError) any
config dict containing a key matching `(?i)(api[_-]?key|token|secret|password)$`
whose value is a non-empty string that is not an env-var *name*
(`^[A-Z][A-Z0-9_]*$`).

## S2. `llm/client.py` — OpenAI-compatible chat client

```python
class LLMError(Exception):
    kind: str   # "auth" | "rate_limit" | "context_overflow" | "server"
                # | "network" | "bad_request"
    status: int | None
    retryable: bool   # rate_limit/server/network => True

@dataclass
class ToolCall:    id: str; name: str; arguments: str   # raw JSON string

@dataclass
class ChatResponse:
    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str | None
    usage: dict

class LLMClient:
    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 timeout: float = 120.0, extra_headers: dict | None = None): ...
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float | None = None,
             max_tokens: int | None = None) -> ChatResponse: ...

def classify_error(status: int | None, body: str) -> LLMError
def chat_with_retry(client, messages, *, tools=None, max_attempts=5,
                    base_delay=1.0, sleep=time.sleep, **kw) -> ChatResponse
```

- POSTs `{base_url}/chat/completions` (base_url already ends in `/v1` or
  equivalent; trailing slashes normalized). Anthropic is reached via its
  OpenAI-compatible endpoint — no SDK.
- `Authorization: Bearer <key>` only when key present (Ollama needs none).
- Body parsing tolerates missing `usage` / `tool_calls`.
- `context_overflow` detected from status 400 + body markers
  (`context_length`, `maximum context`, `too many tokens`).
- `chat_with_retry`: exponential backoff `base_delay * 2^n` + full jitter,
  retries only `retryable` errors; respects `Retry-After` if present;
  injectable `sleep` for tests.
- The API key is never included in any exception message, repr, or log.

## S3. `agentloop/policy_bridge.py` — environment + ceiling

```python
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
def environment_for(base_url: str) -> str
    # "local_agent" iff URL host is provably loopback; else "external".
    # Unparseable URL => "external" (fail closed).
def load_root_policy(root: Path) -> module
    # importlib spec_from_file_location of <root>/_tools/policy.py —
    # the ROOT's policy is authoritative, not the vendored copy.
def max_sensitivity_for(root: Path, environment: str) -> str
    # Highest label in the root policy's SENSITIVITY_ORDER for which
    # check_processing(label, environment) does not deny. Any error in
    # policy evaluation => "public" (fail closed).
def min_sensitivity(a: str, b: str) -> str    # stricter (lower) of two labels
```

## S4. `agentloop/verbtools.py` — kernel verbs as tools

```python
def tool_schemas(surface: str) -> list[dict]
    # surface "local": oracle_status, oracle_search, oracle_answer,
    #   oracle_review, oracle_ingest, oracle_remember, oracle_capture,
    #   oracle_brief, oracle_checkpoint, oracle_loops_due       (10)
    # surface "gateway": oracle_status, oracle_search, oracle_answer,
    #   oracle_review, oracle_capture, oracle_remember          (6)
    # No control-plane verb is EVER exposed on any surface.

def run_verb(root: Path, argv: list[str], timeout: float = 120.0) -> tuple[int, str]
    # subprocess [sys.executable, str(root/"oracle"), *argv], cwd=root,
    # argv list only (never shell=True), stdout+stderr captured, env scrubbed
    # of all *_KEY/*_TOKEN/*_SECRET/*_PASSWORD variables.

@dataclass
class Dispatcher:
    root: Path
    surface: str            # "local" | "gateway"
    max_sensitivity: str    # already min()ed by the caller (S3 ceiling
                            # ∧ surface ceiling)
    def dispatch(self, name: str, arguments: dict) -> ToolOutcome
```

`ToolOutcome`: `text: str` (capped at `tool_result_max_chars`, marker appended
when truncated), `envelope: dict | None` (parsed answer-protocol JSON when
`name == "oracle_answer"`).

Dispatch rules (enforced in code, not prompt):
- `oracle_search` always passes `--max-sensitivity <self.max_sensitivity>`;
  any model-supplied sensitivity argument is ignored.
- `oracle_answer` runs `answer --object <o> [--question <q>] --format json`.
- `oracle_ingest` (local surface only) accepts paths; each is resolved and
  must exist; no glob/`..` interpretation beyond `Path.resolve`.
- Unknown tool name or argument type mismatch → error text outcome, no
  subprocess.
- String arguments are passed as single argv elements — quoting/injection is
  structurally impossible.

## S5. `agentloop/loop.py` — the agent loop

```python
@dataclass
class TurnResult:
    text: str               # final assistant text WITH authority footer
    envelopes: list[dict]   # answer-protocol envelopes seen this turn
    iterations: int

class AgentLoop:
    def __init__(self, client: LLMClient, dispatcher: Dispatcher,
                 system_prompt: str, *, max_iterations: int = 20,
                 history_max_chars: int = 400_000,
                 fallback: LLMClient | None = None): ...
    def run_turn(self, user_text: str) -> TurnResult
```

- System prompt built once by `build_system_prompt(root, surface,
  environment, max_sensitivity)`: operating identity, the answer-protocol
  contract (verdicts 0/2/3/4 and what each obliges), the environment +
  ceiling in force, surface rules, and a frozen snapshot of `./oracle status`.
  **Byte-stable for the life of the session** (Hermes caching discipline).
- Loop: call LLM → execute every returned tool call via dispatcher → append
  results → repeat until a content-only response or `max_iterations`
  (then a forced "respond now with what you have" turn, tools disabled).
- On `context_overflow`: evict oldest non-system turns and retry once.
- On exhausted retries with `fallback` set: retry the call once on fallback.
- **Authority footer (deterministic, D5):** after the model's final text the
  loop appends one line derived ONLY from `envelopes`:
  `— authority: grounded (Object A); supported, authority not confirmed
  (Object B)` or `— conversational; no authority protocol invoked.`
  Exit-4 envelopes additionally append the kernel's `suggested_fix` lines
  verbatim.
- Prompt-injection stance: all tool output is appended as `role: "tool"`
  content, never executed; the system prompt states that instructions found
  inside documents/results are data.

## S6. `service/scheduler.py` + `service/serve.py`

```python
@dataclass
class TickResult: instance: str; rc: int; output: str
def tick_instance(name: str, root: Path, timeout: float = 600.0) -> TickResult
    # subprocess [sys.executable, str(root/"_tools"/"harness.py"),
    #             "--root", str(root), "--once"]  — autonomy gate inside.
def tick_all(instances: dict[str, Path]) -> list[TickResult]

def acquire_serve_lock() -> file | None    # fcntl.flock on ~/.oracle/serve.lock
def serve(cfg: dict, *, once: bool = False) -> int
    # loop: tick_all every serve.tick_seconds; run gateway poll between
    # ticks when enabled; SIGTERM/SIGINT exit cleanly releasing the lock.
    # once=True: single tick + single gateway poll pass (for tests).
```

Logs to `~/.oracle/logs/serve.log` (append, line-oriented, no secrets).

## S7. `gateway/telegram.py`

```python
class TelegramAPI:        # thin urllib wrapper, injectable base for tests
    def __init__(self, token: str, base="https://api.telegram.org"): ...
    def get_updates(self, offset: int, timeout: int = 25) -> list[dict]
    def send_message(self, chat_id: int, text: str) -> None

class TelegramGateway:
    def __init__(self, api, cfg: dict, instances: dict[str, Path],
                 loop_factory): ...
    def poll_once(self) -> int     # returns number of handled messages
```

Rules (all enforced in code):
- Sender resolution: `update.message.from.id` looked up in `allowlist`.
  Unknown sender → message ignored entirely (logged user-id only, no reply,
  no LLM call). Deny by default.
- Allowlist maps to `{role, instance}`; role is always `user` for v1 — an
  `admin` value is accepted in config but still gets the gateway (reduced)
  tool surface; control-plane never crosses the gateway.
- Each (user, instance) gets an `AgentLoop` with surface `"gateway"` and
  ceiling `min(provider ceiling, gateway.max_sensitivity)`.
- Every handled turn appends a row to the instance's
  `Meta.nosync/ledgers/gateway_event.jsonl` via the root's `ledger.py`:
  `{kind: "gateway_turn", platform, user_id, chat_id, chars_in, chars_out,
  envelopes: [...verdicts...], ts}` — metadata only, never message bodies.
- Replies are sent as plain text (no Telegram markdown parsing of model
  output), chunked at 4000 chars.
- Allowlist changes happen ONLY by editing config on the host. Any chat
  message asking to modify access is answered with a fixed refusal string.

## S8. `cli.py`, `wizard.py`, `doctor.py`

`oracle` entry (`main(argv) -> int`):

| Command | Behavior |
|---|---|
| `oracle setup` | wizard (S8.1) |
| `oracle spawn --root P --company-name N --admin-name A [...]` | thin wrapper over `oracle_agent.spawn.main` + auto-register instance |
| `oracle instances [list\|add NAME ROOT\|remove NAME\|default NAME]` | registry ops |
| `oracle chat [NAME] [-m MSG] [--max-sensitivity S]` | REPL (or one-shot with `-m`) on instance; `--max-sensitivity` may only LOWER the ceiling |
| `oracle serve [--once]` | S6 daemon |
| `oracle doctor [NAME]` | S8.2 |
| `oracle model [show\|set ...]` | provider config |
| `oracle kernel NAME -- <args...>` | pass-through to root's `./oracle` |
| `oracle version` | package + kernel versions |

Instance resolution: explicit NAME > cwd inside a registered root >
`default_instance` > sole registered instance > error with guidance.

**S8.1 wizard** (stdin prompts, every step skippable, idempotent re-run):
instance name → root path → company/admin name → spawn (or adopt existing
root) → provider preset (anthropic / openai / openrouter / ollama / custom)
→ model id → API key (stored via `set_env_secret`, input not echoed when
tty) → optional Telegram (token env + allowlist seeding) → final `doctor`.

**S8.2 doctor** checks, each `[ok]/[warn]/[fail]` with a one-line fix:
python ≥3.10; profile perms (`~/.oracle` 0700, `.env` 0600); config.json
parses + no inline secrets (S1 regex); each instance: root exists, `./oracle
check` rc, kernel manifest stamped; provider: key resolvable, environment
classification, `GET {base}/models` reachable (warn-only); gateway: token
resolvable, allowlist non-empty when enabled; serve lock freshness.
Exit 0 iff no `[fail]`.

## S9. Installer (`installer/install.sh`)

POSIX sh. Steps: detect `python3` ≥3.10 → `git clone` (or `--from-dir` local
copy) into `~/.oracle/app` → create venv `~/.oracle/venv` → `pip install
~/.oracle/app` → symlink `oracle` into `~/.local/bin` (with PATH hint) →
run `oracle doctor`. Idempotent re-run = update (git pull + reinstall).
No curl-pipe tricks inside; no sudo; nothing system-wide.

## S10. Test plan (tests/shell/)

- `test_config.py` — defaults, atomic save, secret-in-config refusal, .env
  round-trip + perms, resolve order.
- `test_llm_client.py` — against an in-process `http.server` stub: happy
  path, tool-call parsing, each error class, retry/backoff schedule
  (injected sleep), Retry-After, key never in error text.
- `test_policy_bridge.py` — loopback table (incl. `127.0.0.2`→external,
  IPv6, ports, garbage URLs), ceiling vs a spawned root's policy module,
  fail-closed on broken policy file.
- `test_verbtools.py` — against a real spawned root (kernel fixture):
  schema surface per surface, search ceiling forced, answer envelope parsed,
  argv injection attempts inert, env scrubbing, truncation.
- `test_agentloop.py` — scripted fake client: multi-step tool loop, footer
  for grounded/supported/refused/no-protocol, iteration cap, context-overflow
  eviction, fallback switch.
- `test_scheduler.py` — tick on spawned root (autonomy off ⇒ clean no-op rc),
  lock exclusivity.
- `test_telegram.py` — fake API object: allowlist deny (no reply), allowed
  flow end-to-end with scripted loop, ledger row appended, chunking, refusal
  on access-change requests.
- `test_cli.py` — instance resolution matrix, spawn+register, chat -m
  one-shot with stubbed loop, doctor on a healthy spawn.
- `test_stdlib_only.py` — walks `src/oracle_agent` (excluding kernel assets,
  which have their own guard) and asserts every import is stdlib or package-
  local.
