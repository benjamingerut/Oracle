"""Quick-flow wizard (SPEC S8.1, new default) + the cli routing/rescue.

These drive ``wizard.run_quick`` (reached via ``wizard.run()``) with SCRIPTED
input. The contract under test:

  * a defaults-only stream (newlines) completes rc 0, spawns a root, registers
    instance "main", defaults the provider to anthropic + the claude model, and
    writes NO key when the key answer is blank;
  * the provider menu maps a number ("4" -> ollama, localhost base, empty
    key_env) and accepts a preset name ("openai");
  * a scripted key (via getpass_fn injection) lands in the profile .env;
  * the SUCCESS banner is printed and the full doctor report is NOT dumped on a
    warn-only result; the doctor report IS dumped when worst-is-fail;
  * ``run(advanced=True)`` still walks the old prompts (the advanced-only
    "Instance name" prompt appears) while the quick flow never prints it;
  * cli `oracle setup --advanced` routes to advanced; the non-tty no-instance
    error message is still raised (with the "(takes about a minute)" tail).

Spawn is slow (~1-2s); the defaults-only test does ONE real spawn under a tmp
ORACLE_HOME + tmp Path.home(). The banner/doctor-shape tests monkeypatch
doctor.run with a stub Report so they never spawn.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from oracle_agent import cli, config, doctor, wizard


class _Script:
    """A line-feeding stdin double; ``_ask`` and the secret fallback both call
    ``.readline()`` in order."""

    def __init__(self, lines):
        self._buf = io.StringIO("".join(
            l if l.endswith("\n") else l + "\n" for l in lines))

    def readline(self) -> str:
        return self._buf.readline()


def _home(tmp_path, monkeypatch):
    """Point Path.home() at a tmp dir so the defaulted ~/oracles/<inst> root
    lands under tmp (the profile is already isolated via the `profile` fixture's
    ORACLE_HOME)."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    return h


# --------------------------------------------------------------------------- #
# defaults-only real spawn
# --------------------------------------------------------------------------- #
def test_quick_defaults_only_spawns_and_registers(profile, tmp_path, monkeypatch):
    """A newline-only stream completes rc 0, spawns under ~/oracles/main,
    registers 'main', defaults provider to anthropic + claude, writes no key."""
    _home(tmp_path, monkeypatch)
    # non-tty so the blank key answer is read from the stream, not getpass.
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: False, raising=False)
    out = io.StringIO()
    # company, admin, provider, key  -> all blank (defaults).
    inp = _Script(["", "", "", ""])
    rc = wizard.run(stream_in=inp, stream_out=out,
                    getpass_fn=lambda _: "")
    text = out.getvalue()
    assert rc == 0, text

    cfg = config.load_config()
    assert "main" in (cfg.get("instances") or {})
    root = Path(cfg["instances"]["main"]["root"])
    assert (root / "oracle.yml").exists()
    assert root == (tmp_path / "home" / "oracles" / "main").resolve()

    prov = cfg["provider"]
    assert prov["name"] == "anthropic"
    assert prov["model"] == "claude-sonnet-4-6"
    assert prov["base_url"] == "https://api.anthropic.com/v1"

    # No key written (blank answer).
    assert config.resolve_secret(prov["api_key_env"]) is None
    assert "ready at" in text
    # The quick flow does NOT print the advanced instance-name prompt.
    assert "Instance name" not in text


# --------------------------------------------------------------------------- #
# provider menu mapping (no spawn: adopt a pre-spawned root)
# --------------------------------------------------------------------------- #
def _adopt(spawned_root, monkeypatch):
    """Register 'main' -> spawned_root and point Path.home() so the quick flow
    adopts the existing root (skips spawn)."""
    cfg = config.load_config()
    cfg = config.register_instance(cfg, "main", spawned_root)
    config.save_config(cfg)
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: False, raising=False)


def test_quick_provider_menu_number_maps_ollama(profile, spawned_root, monkeypatch):
    _adopt(spawned_root, monkeypatch)
    out = io.StringIO()
    # company, admin, provider="4" (ollama). Ollama has no key prompt.
    inp = _Script(["", "", "4"])
    rc = wizard.run(stream_in=inp, stream_out=out, getpass_fn=lambda _: "")
    assert rc in (0, 1), out.getvalue()  # doctor may warn-only(0) — never crashes
    cfg = config.load_config()
    prov = cfg["provider"]
    assert prov["name"] == "ollama"
    assert "localhost" in prov["base_url"]
    assert prov["api_key_env"] == ""
    assert "ollama.com" in out.getvalue()


def test_quick_provider_menu_name_maps_openai(profile, spawned_root, monkeypatch):
    _adopt(spawned_root, monkeypatch)
    out = io.StringIO()
    # provider="openai" by NAME, blank key.
    inp = _Script(["", "", "openai", ""])
    rc = wizard.run(stream_in=inp, stream_out=out, getpass_fn=lambda _: "")
    assert rc in (0, 1), out.getvalue()
    prov = config.load_config()["provider"]
    assert prov["name"] == "openai"
    assert prov["base_url"] == "https://api.openai.com/v1"
    assert prov["model"] == "gpt-4o"


# --------------------------------------------------------------------------- #
# key path -> profile .env
# --------------------------------------------------------------------------- #
def test_quick_key_lands_in_profile_env(profile, spawned_root, monkeypatch):
    _adopt(spawned_root, monkeypatch)
    out = io.StringIO()
    # company, admin, provider=anthropic(default "1"), key (read from stream
    # because non-tty; getpass_fn unused).
    inp = _Script(["", "", "1", "sk-ant-quicktest-0123456789"])
    rc = wizard.run(stream_in=inp, stream_out=out, getpass_fn=lambda _: "")
    assert rc in (0, 1), out.getvalue()
    cfg = config.load_config()
    env_key = cfg["provider"]["api_key_env"]
    assert config.resolve_secret(env_key) == "sk-ant-quicktest-0123456789"
    # config.json never holds the literal secret.
    cfg_text = config.config_path().read_text()
    assert "sk-ant-quicktest" not in cfg_text


# --------------------------------------------------------------------------- #
# banner vs full doctor report (stubbed doctor, no spawn)
# --------------------------------------------------------------------------- #
def _stub_report(monkeypatch, *, fail: bool):
    rep = doctor.Report()
    rep.add(doctor.OK, "python 3.x")
    rep.add(doctor.WARN, "no ingested sources (oracle knows nothing yet)", "ingest")
    if fail:
        rep.add(doctor.FAIL, "something is broken", "fix it")
    monkeypatch.setattr(doctor, "run", lambda name=None: rep)


def test_quick_banner_no_full_report_on_warn_only(profile, spawned_root, monkeypatch):
    _adopt(spawned_root, monkeypatch)
    _stub_report(monkeypatch, fail=False)
    out = io.StringIO()
    rc = wizard.run(stream_in=_Script(["", "", "1", ""]), stream_out=out,
                    getpass_fn=lambda _: "")
    text = out.getvalue()
    assert rc == 0
    assert "Your oracle is ready" in text
    # The warn line's text must NOT be dumped (no full report on warn-only).
    assert "knows nothing yet" not in text
    # The warn COUNT is surfaced in the banner instead.
    assert "1 optional item pending" in text


def test_quick_prints_report_on_fail(profile, spawned_root, monkeypatch):
    _adopt(spawned_root, monkeypatch)
    _stub_report(monkeypatch, fail=True)
    out = io.StringIO()
    rc = wizard.run(stream_in=_Script(["", "", "1", ""]), stream_out=out,
                    getpass_fn=lambda _: "")
    text = out.getvalue()
    assert rc == 1
    # On fail the report IS printed and the banner is NOT.
    assert "something is broken" in text
    assert "Your oracle is ready" not in text


# --------------------------------------------------------------------------- #
# advanced flow still walks the old prompts
# --------------------------------------------------------------------------- #
def test_advanced_walks_old_prompts(profile, spawned_root, monkeypatch):
    """run(advanced=True) prints the advanced-only 'Instance name' prompt."""
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: False, raising=False)
    out = io.StringIO()
    # Adopt the existing spawned root: instance name, root path, provider,
    # model, key, ingest roots, telegram-no.
    inp = _Script([
        "main", str(spawned_root), "anthropic", "claude-sonnet-4-6",
        "", "", "N",
    ])
    rc = wizard.run(advanced=True, stream_in=inp, stream_out=out,
                    getpass_fn=lambda _: "")
    text = out.getvalue()
    assert "Instance name" in text
    assert rc in (0, 1), text


# --------------------------------------------------------------------------- #
# cli routing + non-tty rescue message
# --------------------------------------------------------------------------- #
def test_cli_setup_advanced_routes_to_advanced(profile, monkeypatch):
    captured = {}

    def fake_run(advanced=False, **kw):
        captured["advanced"] = advanced
        return 0

    monkeypatch.setattr(wizard, "run", fake_run)
    assert cli.main(["setup", "--advanced"]) == 0
    assert captured["advanced"] is True


def test_cli_setup_default_routes_to_quick(profile, monkeypatch):
    captured = {}

    def fake_run(advanced=False, **kw):
        captured["advanced"] = advanced
        return 0

    monkeypatch.setattr(wizard, "run", fake_run)
    assert cli.main(["setup"]) == 0
    assert captured["advanced"] is False


def test_cli_non_tty_no_instance_error(profile, monkeypatch):
    """When not a tty, the no-instance path raises the guidance SystemExit with
    the new '(takes about a minute)' tail -- it never invokes the wizard."""
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit) as ei:
        cli.resolve_instance(config.load_config(), None)
    assert "takes about a minute" in str(ei.value)
