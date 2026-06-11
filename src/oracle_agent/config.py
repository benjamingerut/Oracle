"""config.py -- shell profile, secrets, and instance registry (SPEC S1).

The profile dir (``~/.oracle`` or ``$ORACLE_HOME``) holds:

  config.json   -- all settings; NEVER secrets, only env-var *names* (guarded)
  .env          -- secrets only, 0o600, created atomically (never chmod-after)
  logs/         -- shell + gateway logs
  locks/        -- per-root + serve flocks

Secret discipline (STRESS M2/M3): ``set_env_secret`` writes via
``os.open(..., 0o600)`` under a ``0o077`` umask and an atomic same-dir rename --
the file is never briefly world-readable. ``save_config`` refuses any dict that
smuggles a literal secret (by key name, URL userinfo, or token shape) so a
credential can never land in the world-readable config.json.

Config versioning (P1-T3 / P1S-6 / P1S-14 / P1F-9):
  - ``CONFIG_VERSION`` is the current schema version (2).
  - ``"version"`` is NOT in DEFAULT_CONFIG; migration/detection run on raw JSON.
  - ``load_config`` migrates in memory only — never writes the file.
  - ``save_config`` stamps ``CONFIG_VERSION`` into the saved dict.
  - A missing ``"version"`` key is treated as v1.
  - A ``version > CONFIG_VERSION`` is rejected with guidance (fail closed).
  - After migration, every SECURITY_KEYS path present in the raw config must
    be present and unchanged in the migrated config (hard load error otherwise).

Stdlib only.
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable

# --------------------------------------------------------------------------- #
# config versioning
# --------------------------------------------------------------------------- #

#: The current config schema version.  Increment when a migration is added.
CONFIG_VERSION: int = 3

# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: dict = {
    "provider": {
        "name": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-6",
        "fallback_model": None,  # RESERVED, not wired in v1 (STRESS scope cut)
        "api_key_env": "ORACLE_LLM_API_KEY",
        "max_tokens": 4096,
        "local_is_confined": False,
        # Phase 8 (P8-T3): the embeddings endpoint for the optional vector
        # index. A SEPARATE one-purpose surface from chat (P8S-2). ``base_url``
        # and ``api_key_env`` default to the chat endpoint's when null/absent;
        # the embedding endpoint is then classified INDEPENDENTLY (and vetoed)
        # by the policy bridge (P8S-1). ``provider.embeddings.api_key_env`` and
        # ``provider.embeddings.base_url`` are SECURITY_KEYS (P8S-16) so a
        # migration can never silently drop or repoint the embedding egress.
        "embeddings": {
            "model": None,       # None => embedding/vector search disabled
            "base_url": None,    # None => inherit provider.base_url
            "api_key_env": None,  # None => inherit provider.api_key_env
        },
    },
    "chat": {
        "max_iterations": 20,
        "tool_result_max_chars": 20000,
        "history_max_chars": 400000,
        # Phase 3 (P3S-11): the LOCAL-chat forced-grounding default. The gateway
        # has NO grounding key -- its mode is hard-coded ENFORCE in the builder,
        # beyond the reach of config. Local default is "observe" until the P3-T7
        # budget gate passes, then flips to "enforce" (one change, here).
        "grounding_default": "observe",
        # Phase 3 (P3S-10): operator consent for the P3-T7 shadow-mode FP/latency
        # measurement. When true AND the local chat is in OBSERVE, each flagged
        # claim-unit is appended (claim text + verdict + timing) to a local-only
        # grounding_shadow.jsonl under profile_dir() for the budget evaluation.
        # Default OFF -- capture never happens without the operator opting in.
        # This is a TELEMETRY consent, NOT a security key: it is intentionally
        # absent from SECURITY_KEYS (a migration may freely toggle it off; the
        # default-off invariant is the safe direction). It can never reach the
        # gateway (no gateway grounding keys at all, P3S-11) and the capture
        # call site exists ONLY on the local-OBSERVE branch of the loop.
        "grounding_shadow": False,
    },
    "serve": {"tick_seconds": 300},
    "gateway": {
        "telegram": {
            "enabled": False,
            "token_env": "ORACLE_TELEGRAM_TOKEN",
            "allowlist": {},  # {"<tg_user_id>": {"role": "user", "instance": "<name>"}}
            "max_sensitivity": "internal",
            "per_user_writes_per_hour": 20,
            # Phase 3 (P3S-3): optional hourly cap on forced-grounding repair
            # round-trips per user. This is a throttle, NOT the grounding MODE
            # (which is hard-coded ENFORCE on the gateway, P3S-11). null = no cap;
            # the per-turn iteration + wall-clock budgets still bound every turn.
            "per_user_repairs_per_hour": None,
        },
        # Phase 4 (P4-T2): Slack surface (Option A, Socket Mode; optional dep).
        "slack": {
            "enabled": False,
            "token_env": "ORACLE_SLACK_TOKEN",
            "signing_secret_env": "ORACLE_SLACK_SIGNING_SECRET",
            "allowlist": {},  # {"<U…member id>": {"role": "user", "instance": "<name>"}}
            "max_sensitivity": "internal",
            "per_user_writes_per_hour": 20,
            "per_user_repairs_per_hour": None,
        },
        # Phase 4 (P4-T3): email surface (IMAP poll + SMTP send). Layered
        # fail-closed identity (P4S-10): ``max_sensitivity`` is HARD-CAPPED at
        # ``public`` by the adapter unless ``authserv_id`` is configured AND a
        # matching ``Authentication-Results`` header carries ``dmarc=pass`` --
        # so the config value above public is only an UPPER bound that the
        # per-message verification can unlock. A per-sender hourly turn cap is
        # ALWAYS on. Allowlist keys are the full address lowercased, matched
        # exactly (``user+tag@`` is a DIFFERENT key -> denied, fail closed).
        "email": {
            "enabled": False,
            "imap_host": "",
            "smtp_host": "",
            "user_env": "ORACLE_EMAIL_USER",
            "pass_env": "ORACLE_EMAIL_PASS",
            "allowlist": {},  # {"ceo@co.com": {"role": "user", "instance": "<name>"}}
            "max_sensitivity": "public",
            "authserv_id": None,
            "per_sender_turns_per_hour": 10,
            "per_user_writes_per_hour": 20,
            "per_user_repairs_per_hour": None,
            "poll_seconds": 60,
        },
        # Phase 4 (P4-T4): local HTTP/MCP surface. Loopback-bound, token-
        # authenticated single principal (the token IS the principal). The
        # ``bind`` must parse as a literal loopback IP (P4S-7); hostnames
        # (including ``"localhost"``) and ``0.0.0.0`` are refused at startup.
        "http": {
            "enabled": False,
            "bind": "127.0.0.1",
            "port": 8765,
            "token_env": "ORACLE_HTTP_TOKEN",
            "principal": "http-operator",
            "max_sensitivity": "internal",
            "per_user_writes_per_hour": 20,
            "per_user_repairs_per_hour": None,
        },
    },
    # Phase 4 (P4-T8): scheduled briefing delivery targets (P4S-15). Keyed by
    # instance name; each target must resolve to an ALREADY-allowlisted private
    # identity on its surface (deny-by-default: no target => no delivery).
    # Example:
    #   "<instance>": {"targets": [{"surface": "telegram", "user_id": "12345"},
    #                              {"surface": "email", "address": "ceo@co.com"}]}
    "briefings": {},
    "instances": {},  # {"<name>": {"root": "/abs/path"}}
    "ingest_roots": [],
    "default_instance": None,
}

_CONFIG_NAME = "config.json"
_ENV_NAME = ".env"

# --------------------------------------------------------------------------- #
# Security-key paths that migrations must never drop or alter silently.
# Each entry is a dotted path string; "providers.*.api_key_env" uses the
# wildcard "*" to match any single key at that level.
# --------------------------------------------------------------------------- #
SECURITY_KEYS: tuple[str, ...] = (
    "gateway.telegram.enabled",
    "gateway.telegram.allowlist",
    "gateway.telegram.max_sensitivity",
    "gateway.telegram.token_env",
    # Phase 4 (P4S-16): new per-surface security keys. A migration that drops
    # or repoints an enabled flag, allowlist, ceiling, credential env name,
    # IMAP/SMTP host, HTTP bind, or the email authserv-id is exfil-critical:
    # it could silently widen access, redirect confidential output to an
    # attacker's mailbox, or expose the loopback surface on a public address.
    "gateway.slack.enabled",
    "gateway.slack.allowlist",
    "gateway.slack.max_sensitivity",
    "gateway.slack.token_env",
    "gateway.slack.signing_secret_env",
    "gateway.email.enabled",
    "gateway.email.allowlist",
    "gateway.email.max_sensitivity",
    "gateway.email.user_env",
    "gateway.email.pass_env",
    "gateway.email.imap_host",
    "gateway.email.smtp_host",
    "gateway.email.authserv_id",
    "gateway.http.enabled",
    "gateway.http.max_sensitivity",
    "gateway.http.token_env",
    "gateway.http.bind",
    "gateway.http.port",
    # Briefing delivery targets are exfil-critical: a migration that rewrites a
    # target (surface/user_id/address) silently redirects confidential output.
    "briefings",
    "chat.grounding_default",
    # Phase 4 (P4S-16): the dead ``providers.*.api_key_env`` wildcard is FIXED.
    # The real config key is SINGULAR ``provider`` -- the plural wildcard
    # expanded against a nonexistent dict and protected nothing. The corrected
    # non-wildcard path walks the singular nesting; a migration that drops or
    # repoints ``provider.api_key_env`` (the chat egress credential) is now
    # refused. Regression: test_provider_api_key_env_drop_caught / _alter_caught.
    "provider.api_key_env",
    # Phase 8 (P8S-16): the embedding endpoint is content egress; a migration
    # must never silently drop or repoint it. These are SINGULAR ``provider``
    # dotted paths (same singular nesting as the corrected ``provider.api_key_env``
    # above); the non-wildcard ``_get_dotted`` walks the nesting, verified by
    # test_embeddings_security_key_drop_caught / _alter_caught.
    "provider.embeddings.api_key_env",
    "provider.embeddings.base_url",
    "ingest_roots",
    "default_instance",
    "default_provider",
)


def _get_dotted(d: dict, path: str):
    """Return the value at ``path`` (dotted) or ``_MISSING`` if absent."""
    parts = path.split(".")
    cur: object = d
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _get_dotted_wildcard(d: dict, path: str) -> list[tuple[str, object]]:
    """Expand a single ``*`` in ``path`` and return ``(resolved_path, value)`` pairs.

    Only the first ``*`` in the path is treated as a wildcard (matching any
    single mapping key).  All other segments are treated literally.
    """
    parts = path.split(".")
    try:
        star_idx = parts.index("*")
    except ValueError:
        # No wildcard — behave like a normal lookup.
        v = _get_dotted(d, path)
        if v is _MISSING:
            return []
        return [(path, v)]

    # Navigate to the dict that contains the wildcard level.
    before = parts[:star_idx]
    after = parts[star_idx + 1:]
    cur: object = d
    for part in before:
        if not isinstance(cur, dict) or part not in cur:
            return []
        cur = cur[part]
    if not isinstance(cur, dict):
        return []

    results = []
    for key in cur:
        sub_path = ".".join(before + [key] + after)
        v = _get_dotted(cur[key], ".".join(after)) if after else cur[key]
        if v is not _MISSING:
            results.append((sub_path, v))
    return results


class _Missing:
    """Sentinel for absent dotted-path lookups."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "<MISSING>"


_MISSING = _Missing()


def _check_security_keys(raw: dict, migrated: dict) -> None:
    """Verify that every SECURITY_KEYS path present in ``raw`` is present and
    unchanged in ``migrated``.  Raises ``ValueError`` on the first violation.
    """
    for path in SECURITY_KEYS:
        if "*" in path:
            pairs = _get_dotted_wildcard(raw, path)
            for resolved, raw_val in pairs:
                # Build the resolved path for lookup in migrated.
                mig_val = _get_dotted(migrated, resolved)
                if mig_val is _MISSING:
                    raise ValueError(
                        f"Migration dropped security key {resolved!r} "
                        f"(was: {raw_val!r}); refusing to load."
                    )
                if mig_val != raw_val:
                    raise ValueError(
                        f"Migration altered security key {resolved!r}: "
                        f"{raw_val!r} -> {mig_val!r}; refusing to load."
                    )
        else:
            raw_val = _get_dotted(raw, path)
            if raw_val is _MISSING:
                continue  # Key not present in raw; nothing to preserve.
            mig_val = _get_dotted(migrated, path)
            if mig_val is _MISSING:
                raise ValueError(
                    f"Migration dropped security key {path!r} "
                    f"(was: {raw_val!r}); refusing to load."
                )
            if mig_val != raw_val:
                raise ValueError(
                    f"Migration altered security key {path!r}: "
                    f"{raw_val!r} -> {mig_val!r}; refusing to load."
                )


# --------------------------------------------------------------------------- #
# Migrations: key n migrates version n -> n+1.
# Each migration must be pure (no side effects) and idempotent.
# --------------------------------------------------------------------------- #

def _migrate_v1_to_v2(raw: dict) -> dict:
    """Migrate a v1 config dict to v2.

    v1 had no ``"version"`` field.  v2 adds it.  No other structural change
    is required for the base schema; the stamp is added here and ``load_config``
    sets it, but the canonical stamp is written by ``save_config``.
    """
    out = copy.deepcopy(raw)
    out["version"] = 2
    return out


def _migrate_v2_to_v3(raw: dict) -> dict:
    """Migrate a v2 config dict to v3 (Phase 4, P4S-16).

    v3 adds the per-surface gateway blocks (slack/email/http) and the
    ``briefings`` delivery-target block.  These are all backfilled by the
    ``_deep_merge`` with ``DEFAULT_CONFIG`` AFTER migration, so the migration
    itself is a NO-OP structural bump: it only stamps the version.

    The bump is deliberate (not skipped): the security-key preservation check
    only runs when a migration actually fires, so a real version bump makes the
    check exercise EVERY new SECURITY_KEYS path against existing configs --
    catching any future migration that would drop or repoint one (P4S-16).
    """
    out = copy.deepcopy(raw)
    out["version"] = 3
    return out


MIGRATIONS: dict[int, Callable[[dict], dict]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
}

# Secret-guard patterns (STRESS M3).
_SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|cookie|bearer)$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_USERINFO_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_BEARER_RE = re.compile(r"\bBearer\s+\S")
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9]{16,}")
# Hyphenated Anthropic-style keys: sk-ant-api03-... (hyphens break the plain sk- run)
_SK_ANT_RE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}")
# Telegram bot tokens: 123456:AABBccDDee... (6+ digits colon 30+ alphanumeric/special)
_TG_BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}")


# --------------------------------------------------------------------------- #
# profile location
# --------------------------------------------------------------------------- #
def profile_dir() -> Path:
    """Profile dir: ``$ORACLE_HOME`` if set, else ``~/.oracle``. Ensured 0o700."""
    env = os.environ.get("ORACLE_HOME")
    p = Path(env).expanduser() if env else (Path.home() / ".oracle")
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
        os.chmod(p, 0o700)
    return p


def locks_dir() -> Path:
    d = profile_dir() / "locks"
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    return d


def logs_dir() -> Path:
    d = profile_dir() / "logs"
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    return d


# --------------------------------------------------------------------------- #
# config.json
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge ``over`` onto a deep copy of ``base``."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def config_path() -> Path:
    return profile_dir() / _CONFIG_NAME


def load_config() -> dict:
    """Load config.json merged over DEFAULT_CONFIG (defaults fill gaps).

    Version detection, future-version rejection, and migrations all run on
    the **raw parsed JSON** before ``_deep_merge`` with ``DEFAULT_CONFIG``
    (P1S-6, P1F-9).  The file is NEVER written by this function; migration
    happens in memory only (P1S-14).  A corrupt or unparseable file raises
    ``ValueError`` and is never overwritten (fail closed, INV-I4).
    """
    p = config_path()
    if not p.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        raw_text = p.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(
            f"config.json is corrupt or unreadable: {exc}\n"
            f"Fix or remove {p} — it will never be auto-repaired."
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            "config.json must contain a JSON object at the top level."
        )

    # --- Version detection (on raw data, before merge) ---
    raw_version: int = data.get("version", 1)  # missing "version" => v1
    if not isinstance(raw_version, int) or raw_version < 1:
        raise ValueError(
            f"config.json has an invalid 'version' value: {raw_version!r}. "
            f"Expected a positive integer."
        )
    if raw_version > CONFIG_VERSION:
        raise ValueError(
            f"config.json was written by a newer Oracle shell "
            f"(version {raw_version}); this shell understands up to "
            f"version {CONFIG_VERSION}. "
            f"Upgrade the oracle-agent package to load this config."
        )

    # --- Apply migrations in sequence on a copy of the raw dict ---
    migrated = copy.deepcopy(data)
    current = raw_version
    while current < CONFIG_VERSION:
        migrator = MIGRATIONS.get(current)
        if migrator is None:
            raise ValueError(
                f"No migration path from config version {current} to "
                f"{current + 1}.  Cannot load config."
            )
        migrated = migrator(migrated)
        current += 1

    # --- Security-key preservation check (P1S-6) ---
    if raw_version < CONFIG_VERSION:
        # Only run the check when migration actually happened.
        _check_security_keys(data, migrated)

    # --- Merge migrated raw dict over defaults ---
    return _deep_merge(DEFAULT_CONFIG, migrated)


def _scan_secret_leak(value, key: str = "") -> str | None:
    """Return a human reason iff ``value`` (recursively) smuggles a secret."""
    if isinstance(value, dict):
        for k, v in value.items():
            reason = _scan_secret_leak(v, str(k))
            if reason:
                return reason
        return None
    if isinstance(value, list):
        for item in value:
            reason = _scan_secret_leak(item, key)
            if reason:
                return reason
        return None
    if not isinstance(value, str):
        return None
    if value and _SECRET_KEY_RE.search(key) and not _ENV_NAME_RE.match(value):
        return (
            f"config key {key!r} holds a literal value; store only the NAME of an "
            f"env var (e.g. ORACLE_LLM_API_KEY) and put the secret in .env"
        )
    if _USERINFO_RE.search(value):
        return f"config value for {key!r} embeds URL userinfo credentials"
    if _BEARER_RE.search(value) or _SK_TOKEN_RE.search(value):
        return f"config value for {key!r} looks like a literal token"
    if _SK_ANT_RE.search(value):
        return f"config value for {key!r} looks like a literal Anthropic API key (sk-ant-…)"
    if _TG_BOT_TOKEN_RE.search(value):
        return f"config value for {key!r} looks like a literal Telegram bot token"
    return None


def save_config(cfg: dict) -> None:
    """Atomically write config.json (0o600), refusing any smuggled secret.

    Stamps ``CONFIG_VERSION`` into the saved dict so that future loads can
    detect the schema version.  The original ``cfg`` dict is not mutated.
    """
    reason = _scan_secret_leak(cfg)
    if reason:
        raise ValueError(f"refusing to save config with a secret: {reason}")
    # Stamp version into the copy that gets written; do NOT mutate the caller's dict.
    to_write = copy.deepcopy(cfg)
    to_write["version"] = CONFIG_VERSION
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(to_write, indent=2, sort_keys=True) + "\n"
    _atomic_write(p, text, mode=0o600)


# --------------------------------------------------------------------------- #
# atomic / mode-safe writes
# --------------------------------------------------------------------------- #
def _atomic_write(path: Path, text: str, *, mode: int) -> None:
    """Write ``text`` to ``path`` atomically with exact ``mode`` (no chmod race).

    A temp file is created in the same dir with ``os.open(..., mode)`` under a
    ``0o077`` umask, written, fsynced, then ``os.replace``d over the target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix="~")
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            os.chmod(path, mode)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    finally:
        os.umask(old_umask)


# --------------------------------------------------------------------------- #
# .env secrets
# --------------------------------------------------------------------------- #
def env_path() -> Path:
    return profile_dir() / _ENV_NAME


def load_env_file() -> dict[str, str]:
    """Parse ``~/.oracle/.env`` (``KEY=VALUE`` lines) into a dict."""
    p = env_path()
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def set_env_secret(key: str, value: str) -> None:
    """Upsert ``KEY=VALUE`` into ``.env`` atomically at 0o600 (STRESS M2).

    The key must be a valid env-var name. The value is written verbatim; it is
    never logged. The whole file is rewritten via the no-chmod-race path.
    """
    if not _ENV_NAME_RE.match(key):
        raise ValueError(f"invalid env var name: {key!r}")
    if "\n" in value or "\r" in value:
        raise ValueError("secret value must not contain newlines")
    existing = load_env_file()
    existing[key] = value
    lines = [
        "# Oracle secrets -- 0600, never committed, never logged.",
    ]
    for k in sorted(existing):
        lines.append(f"{k}={existing[k]}")
    _atomic_write(env_path(), "\n".join(lines) + "\n", mode=0o600)


def write_root_env_secret(root, key: str, value: str) -> None:
    """Upsert ``KEY=VALUE`` into ``<root>/.env.nosync`` atomically at 0o600 (P7S-4).

    Connector credentials must live in the ORACLE ROOT's own ``.env.nosync``, not
    the profile ``.env``: the shell scrubs ``*_KEY``/``_TOKEN``/``_SECRET``/
    ``_PASSWORD`` vars from every kernel subprocess env
    (``verbtools._scrubbed_env``), so only the root's own file is visible to a
    scheduled kernel pull. ``set_env_secret`` targets the profile ``.env`` and is
    WRONG for connector creds.

    Uses the same ``_atomic_write`` (0o600 under a 0o077 umask, temp+rename, no
    chmod race) as the profile secret writer. The value is written verbatim and
    is never logged. The whole root ``.env.nosync`` is rewritten via the
    no-chmod-race path.
    """
    if not _ENV_NAME_RE.match(key):
        raise ValueError(f"invalid env var name: {key!r}")
    if "\n" in value or "\r" in value:
        raise ValueError("secret value must not contain newlines")
    env_file = Path(root).expanduser() / ".env.nosync"
    existing: dict[str, str] = {}
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k:
                existing[k] = v
    existing[key] = value
    lines = ["# Oracle connector secrets -- 0600, never committed, never logged."]
    for k in sorted(existing):
        lines.append(f"{k}={existing[k]}")
    _atomic_write(env_file, "\n".join(lines) + "\n", mode=0o600)


def resolve_secret(env_key: str) -> str | None:
    """Resolve a secret by env-var name: ``os.environ`` first, then ``.env``."""
    if not env_key:
        return None
    val = os.environ.get(env_key)
    if val:
        return val
    return load_env_file().get(env_key)


# --------------------------------------------------------------------------- #
# instance registry
# --------------------------------------------------------------------------- #
def register_instance(cfg: dict, name: str, root: Path) -> dict:
    """Return a new cfg with instance ``name`` -> resolved ``root`` registered."""
    cfg = copy.deepcopy(cfg)
    cfg.setdefault("instances", {})[name] = {"root": str(Path(root).expanduser().resolve())}
    if cfg.get("default_instance") is None:
        cfg["default_instance"] = name
    return cfg


def instance_roots(cfg: dict) -> dict[str, Path]:
    """Map of instance name -> root Path from the registry."""
    out: dict[str, Path] = {}
    for name, meta in (cfg.get("instances") or {}).items():
        root = (meta or {}).get("root")
        if root:
            out[name] = Path(root)
    return out
