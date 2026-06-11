"""doctor.py -- diagnose the install, profile, instances, and provider (SPEC S8.2).

Each check prints ``[ok]/[warn]/[fail]`` with a one-line fix. Exit 0 iff no
``[fail]``. Read-only: doctor never mutates state.

Stdlib only.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config
from .agentloop import policy_bridge as pb

OK, WARN, FAIL = "ok", "warn", "fail"


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, msg: str, fix: str = "") -> None:
        self.rows.append((level, msg, fix))

    def worst_is_fail(self) -> bool:
        return any(level == FAIL for level, _, _ in self.rows)

    def render(self) -> str:
        out = []
        for level, msg, fix in self.rows:
            line = f"[{level}] {msg}"
            if fix and level != OK:
                line += f"\n        fix: {fix}"
            out.append(line)
        return "\n".join(out)


def _vendored_tools_version() -> str | None:
    manifest = (Path(__file__).resolve().parent / "assets" / "oracle-kernel"
                / ".kernel-manifest.json")
    try:
        return json.loads(manifest.read_text()).get("tools_version")
    except (OSError, json.JSONDecodeError):
        return None


def _root_tools_version(root: Path) -> str | None:
    try:
        return json.loads((root / ".kernel-manifest.json").read_text()).get("tools_version")
    except (OSError, json.JSONDecodeError):
        return None


def _is_non_loopback_http(base_url: str) -> bool:
    """Return True iff ``base_url`` is an ``http://`` URL whose host is NOT loopback.

    Self-contained: no DNS; checks literal host string only (matching S1's
    client refusal rule).  Loopback = 127.0.0.0/8, ::1, or the literal
    hostname ``localhost``.
    """
    if not base_url:
        return False
    try:
        parsed = urllib.parse.urlparse(base_url)
    except Exception:
        return False
    if (parsed.scheme or "").lower() != "http":
        return False
    host = parsed.hostname or ""
    if host.lower() == "localhost":
        return False
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_loopback:
            return False
    except ValueError:
        pass  # not a bare IP — treat as non-loopback
    return True


def _count_real_sources(root: Path) -> int:
    """Count non-template markdown files in ``Memory.nosync/Sources/``.

    Template/context sentinel files start with ``_``; everything else is a
    real source record.
    """
    sources_dir = root / "Memory.nosync" / "Sources"
    if not sources_dir.is_dir():
        return 0
    return sum(
        1 for p in sources_dir.iterdir()
        if p.suffix.lower() == ".md" and not p.name.startswith("_")
    )


def run(instance: str | None = None) -> Report:
    rep = Report()

    # python
    if sys.version_info >= (3, 10):
        rep.add(OK, f"python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        rep.add(FAIL, f"python {sys.version_info.major}.{sys.version_info.minor} < 3.10",
                "install Python 3.10+")

    # profile perms
    pdir = config.profile_dir()
    pmode = stat.S_IMODE(os.stat(pdir).st_mode)
    rep.add(OK if pmode == 0o700 else WARN, f"profile dir {pdir} mode {oct(pmode)}",
            f"chmod 700 {pdir}")
    env_file = config.env_path()
    if env_file.exists():
        emode = stat.S_IMODE(os.stat(env_file).st_mode)
        rep.add(OK if emode == 0o600 else FAIL, f".env mode {oct(emode)}",
                f"chmod 600 {env_file}")

    # config parse + secret guard
    try:
        cfg = config.load_config()
        config._scan_secret_leak(cfg) and rep.add(
            FAIL, "config.json contains a literal secret",
            "move secrets to .env; store only env-var names in config.json")
        if not config._scan_secret_leak(cfg):
            rep.add(OK, "config.json parses and holds no inline secrets")
    except ValueError as exc:
        rep.add(FAIL, f"config.json error: {exc}", "fix or delete config.json")
        return rep

    # ingest_roots — global config-level check (instance-independent)
    ingest_roots = cfg.get("ingest_roots") or []
    if not ingest_roots:
        rep.add(WARN, "ingest_roots is empty — your oracle cannot ingest from chat",
                "add directories to config.json ingest_roots")

    # instances
    roots = config.instance_roots(cfg)
    if instance is not None:
        # filter to the named instance only
        if instance in roots:
            roots = {instance: roots[instance]}
        else:
            rep.add(FAIL, f"no instance named {instance!r} "
                          f"(known: {', '.join(sorted(config.instance_roots(cfg))) or 'none'})",
                    "run `oracle instances list` to see registered instances")
            return rep
    if not roots:
        rep.add(WARN, "no instances registered", "run `oracle setup` or `oracle spawn`")
    vendored = _vendored_tools_version()
    for name, root in roots.items():
        if not (root / "oracle.yml").exists():
            rep.add(FAIL, f"instance '{name}': root missing oracle.yml ({root})",
                    "re-spawn or fix the path with `oracle instances add`")
            continue
        rc = _check_rc(root)
        rep.add(OK if rc == 0 else WARN, f"instance '{name}': oracle check rc={rc}",
                "run `oracle kernel {name} -- check` for details".format(name=name))
        rtv = _root_tools_version(root)
        if rtv is None:
            rep.add(WARN, f"instance '{name}': kernel manifest not stamped",
                    "re-spawn to stamp the manifest")
        elif vendored and rtv != vendored:
            _kernel_src = str(Path(__file__).resolve().parent / "assets" / "oracle-kernel")
            rep.add(WARN, f"instance '{name}': kernel {rtv} != packaged {vendored}",
                    f"run `oracle upgrade kernel {name}` "
                    f"(or: oracle kernel {name} -- admin upgrade apply "
                    f"--from-kernel {_kernel_src})")
        else:
            rep.add(OK, f"instance '{name}': kernel {rtv}")
        # zero-sources check
        n_sources = _count_real_sources(root)
        if n_sources == 0:
            rep.add(WARN, f"instance '{name}': no ingested sources (oracle knows nothing yet)",
                    f"oracle kernel {name} -- ingest batch <path>")
        else:
            rep.add(OK, f"instance '{name}': {n_sources} source(s) ingested")

    # provider
    prov = cfg.get("provider") or {}
    env_key = prov.get("api_key_env") or ""
    base_url = prov.get("base_url", "")
    environment = pb.environment_for(base_url)
    rep.add(OK, f"provider env: {environment} ({base_url})")
    if environment == "local_agent":
        rep.add(OK, "local model: ceiling up to internal")
    else:
        rep.add(OK, "external model: ceiling public (confidential+ withheld)")
    # non-https non-loopback endpoint is a hard FAIL
    if _is_non_loopback_http(base_url):
        rep.add(FAIL,
                "LLM endpoint is plain http:// to a non-loopback host — "
                "API key would be sent in cleartext",
                "set a https:// base_url (oracle model set --base-url ...)")
    key = config.resolve_secret(env_key) if env_key else None
    if env_key and not key:
        rep.add(WARN, f"provider API key env '{env_key}' is unset",
                f"oracle model set --key-env {env_key}, then add it to .env")
    elif env_key:
        rep.add(OK, f"provider API key resolvable via {env_key}")
    _probe_models(rep, base_url)

    # gateway
    tg = ((cfg.get("gateway") or {}).get("telegram") or {})
    if tg.get("enabled"):
        if not config.resolve_secret(tg.get("token_env") or ""):
            rep.add(FAIL, "telegram enabled but token unresolved",
                    f"add {tg.get('token_env')} to .env")
        elif not (tg.get("allowlist") or {}):
            rep.add(WARN, "telegram enabled but allowlist empty (no one can use it)",
                    "add user IDs to gateway.telegram.allowlist in config.json")
        else:
            rep.add(OK, f"telegram enabled, {len(tg['allowlist'])} allowed user(s)")

    return rep


def _check_rc(root: Path) -> int:
    import subprocess
    try:
        proc = subprocess.run([sys.executable, str(root / "oracle"), "check"],
                              cwd=str(root), capture_output=True, text=True, timeout=120)
        return proc.returncode
    except Exception:
        return 1


def _probe_models(rep: Report, base_url: str) -> None:
    if not base_url:
        return
    url = base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=5):
            rep.add(OK, f"provider reachable: GET {url}")
    except (urllib.error.URLError, socket.timeout, OSError):
        rep.add(WARN, f"provider /models not reachable ({url})",
                "expected for some providers; verify base_url + network")


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="oracle doctor")
    ap.add_argument("instance", nargs="?")
    args = ap.parse_args(argv)
    rep = run(args.instance)
    print(rep.render())
    return 1 if rep.worst_is_fail() else 0


if __name__ == "__main__":
    raise SystemExit(main())
