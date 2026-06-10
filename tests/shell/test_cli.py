"""Tests for cli.py + doctor.py (SPEC S8 / S10)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from oracle_agent import cli, config, doctor


def _register(root: Path, name="main") -> dict:
    cfg = config.load_config()
    cfg = config.register_instance(cfg, name, root)
    config.save_config(cfg)
    return config.load_config()


# --------------------------------------------------------------------------- #
# instance resolution
# --------------------------------------------------------------------------- #
def test_resolve_explicit_name(profile, spawned_root):
    cfg = _register(spawned_root, "alpha")
    name, root = cli.resolve_instance(cfg, "alpha")
    assert name == "alpha" and Path(root) == Path(spawned_root).resolve()


def test_resolve_unknown_name_exits(profile, spawned_root):
    cfg = _register(spawned_root, "alpha")
    with pytest.raises(SystemExit):
        cli.resolve_instance(cfg, "nope")


def test_resolve_default(profile, spawned_root, tmp_path):
    cfg = _register(spawned_root, "alpha")
    other = tmp_path / "other"
    other.mkdir()
    cfg = config.register_instance(cfg, "beta", other)
    cfg["default_instance"] = "alpha"
    name, _ = cli.resolve_instance(cfg, None)
    assert name == "alpha"


def test_resolve_sole_instance(profile, spawned_root):
    cfg = _register(spawned_root, "only")
    cfg["default_instance"] = None
    name, _ = cli.resolve_instance(cfg, None)
    assert name == "only"


def test_resolve_cwd_inside_root(profile, spawned_root, monkeypatch):
    cfg = _register(spawned_root, "alpha")
    cfg["default_instance"] = None
    monkeypatch.chdir(spawned_root / "Memory.nosync")
    name, _ = cli.resolve_instance(cfg, None)
    assert name == "alpha"


def test_resolve_nothing_exits(profile):
    with pytest.raises(SystemExit):
        cli.resolve_instance(config.load_config(), None)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def test_instances_add_requires_oracle_yml(profile, tmp_path, capsys):
    rc = cli.main(["instances", "add", "x", str(tmp_path)])
    assert rc == 2


def test_instances_add_and_list(profile, spawned_root, capsys):
    assert cli.main(["instances", "add", "alpha", str(spawned_root)]) == 0
    assert cli.main(["instances", "list"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out


def test_instances_remove(profile, spawned_root, capsys):
    cli.main(["instances", "add", "alpha", str(spawned_root)])
    assert cli.main(["instances", "remove", "alpha"]) == 0
    cfg = config.load_config()
    assert "alpha" not in (cfg.get("instances") or {})


def test_model_show_and_set(profile, capsys):
    assert cli.main(["model", "set", "--provider", "ollama"]) == 0
    out = capsys.readouterr().out
    assert "localhost:11434" in out
    assert "local_agent" in out


def test_unknown_command(profile, capsys):
    assert cli.main(["frobnicate"]) == 2


def test_version_runs(profile, capsys):
    assert cli.main(["version"]) == 0
    out = capsys.readouterr().out
    assert "oracle-agent" in out


def test_chat_oneshot_with_stubbed_loop(profile, spawned_root, monkeypatch, capsys):
    _register(spawned_root, "main")

    class FakeLoop:
        class dispatcher:
            environment = "local_agent"
            max_sensitivity = "internal"

        def run_turn(self, text):
            from oracle_agent.agentloop.loop import TurnResult
            return TurnResult(text=f"echo:{text}", envelopes=[], iterations=1)

    import oracle_agent.agentloop.builder as builder
    monkeypatch.setattr(builder, "build_loop", lambda *a, **k: FakeLoop())
    rc = cli.main(["chat", "main", "-m", "hello"])
    assert rc == 0
    assert "echo:hello" in capsys.readouterr().out


def test_chat_ceiling_can_only_lower(profile, spawned_root, monkeypatch):
    """--max-sensitivity is passed as ceiling_override; builder min()s it."""
    _register(spawned_root, "main")
    captured = {}

    import oracle_agent.agentloop.builder as builder
    real_build = builder.build_loop

    def spy(cfg, root, **kw):
        captured.update(kw)
        # don't actually build an LLM loop in tests

        class L:
            class dispatcher:
                environment = "external"
                max_sensitivity = "public"

            def run_turn(self, text):
                from oracle_agent.agentloop.loop import TurnResult
                return TurnResult(text="ok", envelopes=[], iterations=1)
        return L()

    monkeypatch.setattr(builder, "build_loop", spy)
    cli.main(["chat", "main", "-m", "x", "--max-sensitivity", "secret"])
    assert captured["ceiling_override"] == "secret"
    # builder.min_sensitivity guarantees override can only lower; verify directly:
    from oracle_agent.agentloop import policy_bridge as pb
    assert pb.min_sensitivity("public", "secret") == "public"


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def test_doctor_healthy_spawn_no_fail(profile, spawned_root, capsys):
    _register(spawned_root, "main")
    rep = doctor.run("main")
    text = rep.render()
    assert not rep.worst_is_fail(), text
    assert "instance 'main'" in text


def test_doctor_detects_missing_root(profile, tmp_path):
    cfg = config.load_config()
    cfg["instances"]["ghost"] = {"root": str(tmp_path / "gone")}
    config.save_config(cfg)
    rep = doctor.run()
    assert rep.worst_is_fail()


def test_doctor_flags_bad_env_perms(profile, monkeypatch):
    config.set_env_secret("ORACLE_LLM_API_KEY", "sk-x-1234567890abcdef")
    import os
    os.chmod(config.env_path(), 0o644)
    rep = doctor.run()
    assert rep.worst_is_fail()
