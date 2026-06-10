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

Stdlib only.
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path

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
    },
    "chat": {
        "max_iterations": 20,
        "tool_result_max_chars": 20000,
        "history_max_chars": 400000,
    },
    "serve": {"tick_seconds": 300},
    "gateway": {
        "telegram": {
            "enabled": False,
            "token_env": "ORACLE_TELEGRAM_TOKEN",
            "allowlist": {},  # {"<tg_user_id>": {"role": "user", "instance": "<name>"}}
            "max_sensitivity": "internal",
            "per_user_writes_per_hour": 20,
        }
    },
    "instances": {},  # {"<name>": {"root": "/abs/path"}}
    "ingest_roots": [],
    "default_instance": None,
}

_CONFIG_NAME = "config.json"
_ENV_NAME = ".env"

# Secret-guard patterns (STRESS M3).
_SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|cookie|bearer)$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_USERINFO_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_BEARER_RE = re.compile(r"\bBearer\s+\S")
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9]{16,}")


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
    """Load config.json merged over DEFAULT_CONFIG (defaults fill gaps)."""
    p = config_path()
    if not p.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"config.json is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("config.json must contain a JSON object")
    return _deep_merge(DEFAULT_CONFIG, data)


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
    return None


def save_config(cfg: dict) -> None:
    """Atomically write config.json (0o600), refusing any smuggled secret."""
    reason = _scan_secret_leak(cfg)
    if reason:
        raise ValueError(f"refusing to save config with a secret: {reason}")
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(cfg, indent=2, sort_keys=True) + "\n"
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
