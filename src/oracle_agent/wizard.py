"""wizard.py -- interactive first-run setup (SPEC S8.1).

Walks the operator through: instance + root, spawn (or adopt), provider preset,
model, API key (stored to .env, never echoed), optional Telegram. Idempotent:
re-running updates rather than duplicates. Every prompt is skippable.

Stdlib only.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

from . import config, doctor, spawn

PRESETS = {
    "anthropic": ("https://api.anthropic.com/v1", "claude-sonnet-4-6", "ORACLE_LLM_API_KEY"),
    "openai": ("https://api.openai.com/v1", "gpt-4o", "ORACLE_LLM_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "anthropic/claude-sonnet-4-6", "ORACLE_LLM_API_KEY"),
    "ollama": ("http://localhost:11434/v1", "llama3.1", ""),  # local, no key
    "custom": ("", "", "ORACLE_LLM_API_KEY"),
}


def _ask(prompt: str, default: str = "", *, stream_in=None, stream_out=None) -> str:
    out = stream_out or sys.stdout
    inp = stream_in or sys.stdin
    suffix = f" [{default}]" if default else ""
    out.write(f"{prompt}{suffix}: ")
    out.flush()
    line = inp.readline()
    if not line:
        return default
    line = line.strip()
    return line or default


def run(*, stream_in=None, stream_out=None, getpass_fn=getpass.getpass) -> int:
    out = stream_out or sys.stdout
    out.write("Oracle setup\n============\n")
    cfg = config.load_config()

    name = _ask("Instance name", cfg.get("default_instance") or "main",
                stream_in=stream_in, stream_out=stream_out)
    default_root = str(Path.home() / "oracles" / name)
    existing = (cfg.get("instances") or {}).get(name, {}).get("root", default_root)
    root_str = _ask("Oracle root path", existing, stream_in=stream_in, stream_out=stream_out)
    root = Path(root_str).expanduser().resolve()

    if (root / "oracle.yml").exists():
        out.write(f"Adopting existing oracle at {root}\n")
    else:
        company = _ask("Company name", "My Company", stream_in=stream_in, stream_out=stream_out)
        admin = _ask("Admin (your) name", "Admin", stream_in=stream_in, stream_out=stream_out)
        out.write(f"Spawning oracle at {root} ...\n")
        rc = spawn.main(["--root", str(root), "--company-name", company,
                         "--admin-name", admin])
        if rc != 0:
            out.write("Spawn failed; aborting setup.\n")
            return rc
    cfg = config.register_instance(cfg, name, root)

    # provider
    preset = _ask("Provider preset (anthropic/openai/openrouter/ollama/custom)",
                  cfg.get("provider", {}).get("name", "anthropic"),
                  stream_in=stream_in, stream_out=stream_out)
    base, model, key_env = PRESETS.get(preset, PRESETS["custom"])
    if preset == "custom" or not base:
        base = _ask("Base URL (…/v1)", cfg.get("provider", {}).get("base_url", ""),
                    stream_in=stream_in, stream_out=stream_out)
    model = _ask("Model id", model or cfg.get("provider", {}).get("model", ""),
                 stream_in=stream_in, stream_out=stream_out)
    cfg["provider"].update({"name": preset, "base_url": base, "model": model,
                            "api_key_env": key_env})

    if key_env:
        out.write(f"API key (stored to .env as {key_env}; leave blank to skip): ")
        out.flush()
        try:
            secret = getpass_fn("") if sys.stdin.isatty() else (stream_in.readline().strip() if stream_in else "")
        except Exception:
            secret = ""
        if secret:
            config.set_env_secret(key_env, secret)
            out.write("  key saved.\n")

    # optional telegram
    want_tg = _ask("Enable Telegram gateway? (y/N)", "N",
                   stream_in=stream_in, stream_out=stream_out).lower()
    if want_tg.startswith("y"):
        token_env = cfg["gateway"]["telegram"].get("token_env", "ORACLE_TELEGRAM_TOKEN")
        out.write(f"Telegram bot token (stored as {token_env}; blank to skip): ")
        out.flush()
        try:
            tok = getpass_fn("") if sys.stdin.isatty() else (stream_in.readline().strip() if stream_in else "")
        except Exception:
            tok = ""
        if tok:
            config.set_env_secret(token_env, tok)
        uid = _ask("Your Telegram numeric user ID (to allowlist)", "",
                   stream_in=stream_in, stream_out=stream_out)
        cfg["gateway"]["telegram"]["enabled"] = True
        if uid:
            cfg["gateway"]["telegram"].setdefault("allowlist", {})[uid] = {
                "role": "user", "instance": name}

    config.save_config(cfg)
    out.write("\nConfig saved. Running doctor ...\n\n")
    rep = doctor.run(name)
    out.write(rep.render() + "\n")
    return 1 if rep.worst_is_fail() else 0
