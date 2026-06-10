"""cli.py -- the global ``oracle`` command (SPEC S8).

    oracle setup                         first-run wizard
    oracle spawn --root P --company-name N --admin-name A [--codename C]
    oracle instances [list|add NAME ROOT|remove NAME|default NAME]
    oracle chat [NAME] [-m MSG] [--max-sensitivity S]
    oracle serve [--once]
    oracle doctor [NAME]
    oracle model [show|set --provider P --model M --base-url U --key-env E]
    oracle kernel NAME -- <args...>      pass-through to the root's ./oracle
    oracle version

Instance resolution: explicit NAME > cwd inside a registered root >
default_instance > sole instance > error with guidance.

Stdlib only.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import config, doctor, spawn, wizard
from .agentloop import policy_bridge as pb


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, rest = args[0], args[1:]
    handler = {
        "setup": _cmd_setup, "spawn": _cmd_spawn, "instances": _cmd_instances,
        "chat": _cmd_chat, "serve": _cmd_serve, "doctor": _cmd_doctor,
        "model": _cmd_model, "kernel": _cmd_kernel, "version": _cmd_version,
    }.get(cmd)
    if handler is None:
        print(f"oracle: unknown command {cmd!r} (try `oracle help`)", file=sys.stderr)
        return 2
    return handler(rest)


# --------------------------------------------------------------------------- #
# instance resolution
# --------------------------------------------------------------------------- #
def resolve_instance(cfg: dict, name: str | None) -> tuple[str, Path]:
    roots = config.instance_roots(cfg)
    if name:
        if name not in roots:
            raise SystemExit(f"oracle: no instance named {name!r} "
                             f"(known: {', '.join(sorted(roots)) or 'none'})")
        return name, roots[name]
    cwd = Path.cwd().resolve()
    for n, r in roots.items():
        try:
            rr = Path(r).resolve()
        except OSError:
            continue
        if cwd == rr or rr in cwd.parents:
            return n, r
    default = cfg.get("default_instance")
    if default and default in roots:
        return default, roots[default]
    if len(roots) == 1:
        n = next(iter(roots))
        return n, roots[n]
    raise SystemExit("oracle: no instance specified and none resolvable; "
                     "run `oracle setup` or `oracle instances add NAME ROOT`")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def _cmd_setup(rest: list[str]) -> int:
    return wizard.run()


def _cmd_spawn(rest: list[str]) -> int:
    rc = spawn.main(rest)
    if rc == 0:
        # auto-register
        try:
            i = rest.index("--root")
            root = Path(rest[i + 1]).expanduser().resolve()
        except (ValueError, IndexError):
            return rc
        name = root.name.lower().replace(" ", "-")
        cfg = config.load_config()
        cfg = config.register_instance(cfg, name, root)
        config.save_config(cfg)
        print(f"registered instance '{name}' -> {root}")
    return rc


def _cmd_instances(rest: list[str]) -> int:
    cfg = config.load_config()
    sub = rest[0] if rest else "list"
    if sub == "list":
        roots = config.instance_roots(cfg)
        if not roots:
            print("(no instances; run `oracle setup`)")
            return 0
        default = cfg.get("default_instance")
        for n, r in sorted(roots.items()):
            star = "*" if n == default else " "
            print(f" {star} {n}  {r}")
        return 0
    if sub == "add" and len(rest) >= 3:
        root = Path(rest[2]).expanduser().resolve()
        if not (root / "oracle.yml").exists():
            print(f"oracle: {root} is not an oracle root (no oracle.yml)", file=sys.stderr)
            return 2
        cfg = config.register_instance(cfg, rest[1], root)
        config.save_config(cfg)
        print(f"registered '{rest[1]}' -> {root}")
        return 0
    if sub == "remove" and len(rest) >= 2:
        (cfg.get("instances") or {}).pop(rest[1], None)
        if cfg.get("default_instance") == rest[1]:
            cfg["default_instance"] = next(iter(cfg.get("instances") or {}), None)
        config.save_config(cfg)
        print(f"removed '{rest[1]}' (root left on disk)")
        return 0
    if sub == "default" and len(rest) >= 2:
        if rest[1] not in (cfg.get("instances") or {}):
            print(f"oracle: unknown instance {rest[1]!r}", file=sys.stderr)
            return 2
        cfg["default_instance"] = rest[1]
        config.save_config(cfg)
        return 0
    print("usage: oracle instances [list|add NAME ROOT|remove NAME|default NAME]",
          file=sys.stderr)
    return 2


def _cmd_chat(rest: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="oracle chat")
    ap.add_argument("name", nargs="?")
    ap.add_argument("-m", "--message", help="one-shot message (no REPL)")
    ap.add_argument("--max-sensitivity", help="LOWER the ceiling for this session")
    ns = ap.parse_args(rest)

    cfg = config.load_config()
    name, root = resolve_instance(cfg, ns.name)

    from .agentloop.builder import build_loop
    from .service.scheduler import root_lock

    loop = build_loop(cfg, root, surface="local",
                      ceiling_override=ns.max_sensitivity)
    disp = loop.dispatcher
    print(f"oracle chat — instance '{name}' | model {cfg['provider'].get('model')} "
          f"| env {disp.environment} | ceiling {disp.max_sensitivity}")

    def turn(text: str) -> int:
        with root_lock(name):
            try:
                result = loop.run_turn(text)
            except Exception as exc:
                print(f"[error: {type(exc).__name__}: {exc}]", file=sys.stderr)
                return 1
        print(result.text)
        return 0

    if ns.message:
        return turn(ns.message)

    print("(/quit to exit)")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            return 0
        turn(line)


def _cmd_serve(rest: list[str]) -> int:
    once = "--once" in rest
    cfg = config.load_config()
    from .service.serve import serve
    return serve(cfg, once=once)


def _cmd_doctor(rest: list[str]) -> int:
    return doctor.main(rest)


def _cmd_model(rest: list[str]) -> int:
    cfg = config.load_config()
    prov = cfg.get("provider") or {}
    sub = rest[0] if rest else "show"
    if sub == "show":
        env = pb.environment_for(prov.get("base_url", ""))
        print(f"provider : {prov.get('name')}")
        print(f"base_url : {prov.get('base_url')}  ({env})")
        print(f"model    : {prov.get('model')}")
        print(f"key env  : {prov.get('api_key_env')} "
              f"({'set' if config.resolve_secret(prov.get('api_key_env') or '') else 'UNSET'})")
        return 0
    if sub == "set":
        import argparse
        ap = argparse.ArgumentParser(prog="oracle model set")
        ap.add_argument("--provider")
        ap.add_argument("--model")
        ap.add_argument("--base-url")
        ap.add_argument("--key-env")
        ns = ap.parse_args(rest[1:])
        if ns.provider:
            prov["name"] = ns.provider
            preset = wizard.PRESETS.get(ns.provider)
            if preset and not ns.base_url:
                prov["base_url"] = preset[0]
            if preset and not ns.model:
                prov["model"] = preset[1]
        if ns.model:
            prov["model"] = ns.model
        if ns.base_url:
            prov["base_url"] = ns.base_url
        if ns.key_env:
            prov["api_key_env"] = ns.key_env
        cfg["provider"] = prov
        config.save_config(cfg)
        return _cmd_model(["show"])
    print("usage: oracle model [show|set --provider P --model M --base-url U --key-env E]",
          file=sys.stderr)
    return 2


def _cmd_kernel(rest: list[str]) -> int:
    """Pass-through: oracle kernel NAME -- <args...> (operator only)."""
    if not rest:
        print("usage: oracle kernel NAME -- <args...>", file=sys.stderr)
        return 2
    name = rest[0]
    tail = rest[1:]
    if tail and tail[0] == "--":
        tail = tail[1:]
    cfg = config.load_config()
    name, root = resolve_instance(cfg, name)
    import subprocess
    from .service.scheduler import root_lock
    with root_lock(name):
        proc = subprocess.run([sys.executable, str(root / "oracle"), *tail],
                              cwd=str(root))
    return proc.returncode


def _cmd_version(rest: list[str]) -> int:
    from . import __version__
    print(f"oracle-agent {__version__}")
    v = doctor._vendored_tools_version()
    print(f"packaged kernel tools_version: {v}")
    cfg = config.load_config()
    for n, r in sorted(config.instance_roots(cfg).items()):
        print(f"  instance '{n}': kernel {doctor._root_tools_version(r)}")
    return 0
