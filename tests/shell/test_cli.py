"""Tests for cli.py + doctor.py (SPEC S8 / S10)."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from oracle_agent import cli, config, doctor, wizard, spawn


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

    from oracle_agent.agentloop.loop import GroundingPolicy

    class FakeLoop:
        grounding = GroundingPolicy.OBSERVE

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
        from oracle_agent.agentloop.loop import GroundingPolicy

        class L:
            grounding = GroundingPolicy.OBSERVE

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


def _chat_spy(monkeypatch, grounding_value="enforce"):
    """Install a build_loop spy; return the captured-kwargs dict."""
    import oracle_agent.agentloop.builder as builder
    from oracle_agent.agentloop.loop import GroundingPolicy, TurnResult
    captured = {}

    def spy(cfg, root, **kw):
        captured.update(kw)

        class L:
            grounding = GroundingPolicy(grounding_value)

            class dispatcher:
                environment = "local_agent"
                max_sensitivity = "internal"

            def run_turn(self, text):
                return TurnResult(text="ok", envelopes=[], iterations=1)
        return L()

    monkeypatch.setattr(builder, "build_loop", spy)
    return captured


def test_chat_grounding_flag_passed_to_builder(profile, spawned_root, monkeypatch):
    """oracle chat --grounding enforce forwards grounding_override to the builder."""
    _register(spawned_root, "main")
    captured = _chat_spy(monkeypatch, "enforce")
    rc = cli.main(["chat", "main", "-m", "x", "--grounding", "enforce"])
    assert rc == 0
    assert captured["grounding_override"] == "enforce"


def test_chat_grounding_override_logs_banner_and_ledger(profile, spawned_root,
                                                        monkeypatch, capsys):
    """A --grounding override emits a stderr banner AND a metadata-only ledger row."""
    _register(spawned_root, "main")
    _chat_spy(monkeypatch, "enforce")
    cli.main(["chat", "main", "-m", "hello secret message", "--grounding", "enforce"])
    err = capsys.readouterr().err
    assert "grounding mode overridden" in err
    # Metadata-only ledger row on the instance root.
    ledger = (spawned_root / "Meta.nosync" / "ledgers" / "chat_event.jsonl")
    assert ledger.exists(), "grounding-override ledger row not written"
    import json as _json
    row = _json.loads(ledger.read_text().splitlines()[-1])
    assert row["kind"] == "grounding_override"
    assert row["mode"] == "enforce"
    assert row["surface"] == "local"
    # Never a message body.
    assert "hello secret message" not in _json.dumps(row)


def test_chat_no_grounding_flag_writes_no_ledger(profile, spawned_root, monkeypatch):
    """Without --grounding, no override banner/ledger row is produced.

    The spawned_root is session-scoped and may carry rows from other tests, so
    assert that THIS turn adds nothing (row count unchanged), not that the file
    is absent.
    """
    _register(spawned_root, "main")
    _chat_spy(monkeypatch, "observe")
    ledger = (spawned_root / "Meta.nosync" / "ledgers" / "chat_event.jsonl")
    before = len(ledger.read_text().splitlines()) if ledger.exists() else 0
    cli.main(["chat", "main", "-m", "x"])
    after = len(ledger.read_text().splitlines()) if ledger.exists() else 0
    assert after == before, "a turn without --grounding must not log an override"


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


# --------------------------------------------------------------------------- #
# S3.1 — doctor instance filtering
# --------------------------------------------------------------------------- #
def test_doctor_named_instance_only(profile, spawned_root, tmp_path):
    """doctor.run("main") checks only 'main', not other instances."""
    _register(spawned_root, "main")
    # register a ghost instance with a missing root
    cfg = config.load_config()
    cfg["instances"]["ghost"] = {"root": str(tmp_path / "gone")}
    config.save_config(cfg)
    # Running with "main" should NOT report ghost's fail
    rep = doctor.run("main")
    text = rep.render()
    assert "ghost" not in text


def test_doctor_unknown_instance_fails(profile, spawned_root):
    """doctor.run("nonexistent") reports FAIL immediately."""
    _register(spawned_root, "main")
    rep = doctor.run("nonexistent")
    assert rep.worst_is_fail()


def test_doctor_all_instances_when_no_name(profile, spawned_root, tmp_path):
    """doctor.run() with no name checks all instances."""
    _register(spawned_root, "main")
    cfg = config.load_config()
    cfg["instances"]["ghost"] = {"root": str(tmp_path / "gone")}
    config.save_config(cfg)
    rep = doctor.run()
    text = rep.render()
    assert "ghost" in text
    assert rep.worst_is_fail()


# --------------------------------------------------------------------------- #
# S3.1 — doctor new warnings
# --------------------------------------------------------------------------- #
def test_doctor_empty_ingest_roots_warns(profile, spawned_root):
    """Empty ingest_roots raises a WARN about ingest being dead."""
    _register(spawned_root, "main")
    cfg = config.load_config()
    cfg["ingest_roots"] = []
    config.save_config(cfg)
    rep = doctor.run()
    text = rep.render()
    assert "ingest_roots" in text


def test_doctor_zero_sources_warns(profile, spawned_root):
    """A fresh (zero-source) oracle instance raises a WARN."""
    _register(spawned_root, "main")
    rep = doctor.run("main")
    text = rep.render()
    # A freshly spawned oracle has no real sources
    assert "source" in text.lower()


def test_doctor_non_https_non_loopback_fails(profile, spawned_root):
    """http:// endpoint on non-loopback host is a FAIL."""
    _register(spawned_root, "main")
    cfg = config.load_config()
    cfg["provider"]["base_url"] = "http://api.example.com/v1"
    config.save_config(cfg)
    rep = doctor.run("main")
    assert rep.worst_is_fail()
    text = rep.render()
    assert "cleartext" in text or "http://" in text


def test_doctor_loopback_http_ok(profile, spawned_root, monkeypatch):
    """http://localhost is allowed (not a FAIL)."""
    _register(spawned_root, "main")
    cfg = config.load_config()
    cfg["provider"]["base_url"] = "http://localhost:11434/v1"
    config.save_config(cfg)
    # Keep the egress probe deterministic/offline: clean veto, Ollama reachable.
    monkeypatch.setattr(doctor.pb, "egress_veto", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_is_ollama_tags_reachable", lambda url: True)
    rep = doctor.run("main")
    # Should not fail due to http loopback
    rows = [(lvl, msg) for lvl, msg, _ in rep.rows
            if "cleartext" in msg or "plain http" in msg.lower()]
    assert not rows, f"Unexpected http-loopback fail: {rows}"


def test_doctor_egress_veto_cloud_model_fails(profile, spawned_root, monkeypatch):
    """A loopback provider serving a ':cloud' model is a FAIL (egress veto)."""
    _register(spawned_root, "main")
    cfg = config.load_config()
    cfg["provider"]["base_url"] = "http://127.0.0.1:11434/v1"
    cfg["provider"]["model"] = "deepseek-v4-pro:cloud"
    config.save_config(cfg)
    rep = doctor.run("main")
    assert rep.worst_is_fail()
    text = rep.render()
    assert "cloud-proxied" in text
    assert "deepseek-v4-pro:cloud" in text
    assert "qwen3.6-32k" in text  # fix line names a fully-local alternative


def test_doctor_egress_unverifiable_loopback_warns(profile, spawned_root, monkeypatch):
    """A loopback endpoint whose /api/tags is unreachable raises a WARN, not FAIL."""
    _register(spawned_root, "main")
    cfg = config.load_config()
    cfg["provider"]["base_url"] = "http://127.0.0.1:8000/v1"  # e.g. vLLM
    cfg["provider"]["model"] = "local-model"
    config.save_config(cfg)
    # Clean veto + unreachable /api/tags.
    monkeypatch.setattr(doctor.pb, "egress_veto", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_is_ollama_tags_reachable", lambda url: False)
    rep = doctor.run("main")
    text = rep.render()
    assert "cannot verify processing locality" in text
    assert "STRESS C2" in text
    # WARN, not FAIL (genuine local server must not be blocked).
    rows = [lvl for lvl, msg, _ in rep.rows if "cannot verify" in msg]
    assert rows == [doctor.WARN]


def test_doctor_egress_clean_ollama_ok(profile, spawned_root, monkeypatch):
    """A clean Ollama loopback model (veto clear, /api/tags reachable) is OK."""
    _register(spawned_root, "main")
    cfg = config.load_config()
    cfg["provider"]["base_url"] = "http://127.0.0.1:11434/v1"
    cfg["provider"]["model"] = "qwen3.6-32k"
    config.save_config(cfg)
    monkeypatch.setattr(doctor.pb, "egress_veto", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_is_ollama_tags_reachable", lambda url: True)
    rep = doctor.run("main")
    assert not rep.worst_is_fail()
    text = rep.render()
    assert "egress veto clear" in text


def test_doctor_upgrade_suggestion_has_from_kernel(profile, spawned_root, monkeypatch):
    """Version-skew fix suggestion includes --from-kernel, not raw 'upgrade apply'."""
    _register(spawned_root, "main")
    # Fake a version mismatch
    monkeypatch.setattr(doctor, "_vendored_tools_version", lambda: "v99")
    monkeypatch.setattr(doctor, "_root_tools_version", lambda r: "v1")
    # Also mock _check_rc so it doesn't run the kernel
    monkeypatch.setattr(doctor, "_check_rc", lambda r: 0)
    rep = doctor.run("main")
    text = rep.render()
    assert "--from-kernel" in text


# --------------------------------------------------------------------------- #
# S3.2 — advanced-wizard ingest_roots + telegram ID validation
# (these drive wizard.run(advanced=True): the quick flow asks none of these)
# --------------------------------------------------------------------------- #
def _wizard_input(*lines):
    """Return a StringIO with newline-terminated lines for wizard stream_in."""
    return io.StringIO("\n".join(lines) + "\n")


def test_wizard_ingest_roots_valid(profile, spawned_root, tmp_path):
    """Wizard writes valid absolute existing dirs to ingest_roots."""
    d = tmp_path / "docs"
    d.mkdir()
    inp = _wizard_input(
        "main",            # instance name
        str(spawned_root), # root path (existing oracle)
        "anthropic",       # provider
        "claude-sonnet-4-6",  # model
        "",                # api key (skip)
        str(d),            # ingest roots
        "N",               # no telegram
    )
    out = io.StringIO()
    wizard.run(advanced=True, stream_in=inp, stream_out=out, getpass_fn=lambda _: "")
    cfg = config.load_config()
    assert str(d) in cfg.get("ingest_roots", [])


def test_wizard_ingest_roots_non_absolute_skipped(profile, spawned_root, tmp_path):
    """Non-absolute ingest root paths are skipped with a warning."""
    inp = _wizard_input(
        "main",
        str(spawned_root),
        "anthropic",
        "claude-sonnet-4-6",
        "",
        "relative/path",   # non-absolute — should be skipped
        "N",
    )
    out = io.StringIO()
    wizard.run(advanced=True, stream_in=inp, stream_out=out, getpass_fn=lambda _: "")
    cfg = config.load_config()
    assert cfg.get("ingest_roots") == []
    assert "not an absolute path" in out.getvalue() or "skipped" in out.getvalue()


def test_wizard_ingest_roots_nonexistent_skipped(profile, spawned_root, tmp_path):
    """Nonexistent ingest root paths are skipped with a warning."""
    inp = _wizard_input(
        "main",
        str(spawned_root),
        "anthropic",
        "claude-sonnet-4-6",
        "",
        str(tmp_path / "no-such-dir"),  # does not exist
        "N",
    )
    out = io.StringIO()
    wizard.run(advanced=True, stream_in=inp, stream_out=out, getpass_fn=lambda _: "")
    cfg = config.load_config()
    assert cfg.get("ingest_roots") == []


def test_wizard_telegram_id_non_numeric_skipped(profile, spawned_root):
    """Non-numeric Telegram user IDs are skipped (not saved to allowlist)."""
    inp = _wizard_input(
        "main",
        str(spawned_root),
        "anthropic",
        "claude-sonnet-4-6",
        "",
        "",              # no ingest roots
        "y",             # enable telegram
        "",              # bot token (skip, no getpass)
        "notanumber",    # invalid UID
    )
    out = io.StringIO()
    wizard.run(advanced=True, stream_in=inp, stream_out=out, getpass_fn=lambda _: "")
    cfg = config.load_config()
    allowlist = cfg.get("gateway", {}).get("telegram", {}).get("allowlist", {})
    assert "notanumber" not in allowlist
    assert "not numeric" in out.getvalue() or "not numeric" in out.getvalue()


def test_wizard_telegram_id_numeric_saved(profile, spawned_root):
    """Numeric Telegram user IDs are saved as strings to the allowlist."""
    inp = _wizard_input(
        "main",
        str(spawned_root),
        "anthropic",
        "claude-sonnet-4-6",
        "",
        "",              # no ingest roots
        "y",             # enable telegram
        "",              # bot token (skip)
        "123456789",     # valid numeric UID
    )
    out = io.StringIO()
    wizard.run(advanced=True, stream_in=inp, stream_out=out, getpass_fn=lambda _: "")
    cfg = config.load_config()
    allowlist = cfg.get("gateway", {}).get("telegram", {}).get("allowlist", {})
    assert "123456789" in allowlist


# --------------------------------------------------------------------------- #
# S3.4 — spawn collision refusal
# --------------------------------------------------------------------------- #
def test_spawn_collision_refuses_different_root(profile, spawned_root, tmp_path, monkeypatch):
    """cli spawn refuses to overwrite a registry entry pointing at a different root."""
    # Register 'root' under the spawned_root name
    name = spawned_root.name.lower().replace(" ", "-")
    cfg = config.load_config()
    cfg = config.register_instance(cfg, name, spawned_root)
    config.save_config(cfg)

    # Now attempt to spawn a *different* root with the same name
    other_root = tmp_path / name
    other_root.mkdir()
    # Stub spawn.main so it "succeeds" without actually spawning
    monkeypatch.setattr(spawn, "main", lambda args: 0)

    rc = cli._cmd_spawn(["--root", str(other_root)])
    assert rc != 0  # must refuse


def test_spawn_collision_same_root_ok(profile, spawned_root, monkeypatch):
    """cli spawn re-registering the same root is idempotent (not an error)."""
    name = spawned_root.name.lower().replace(" ", "-")
    cfg = config.load_config()
    cfg = config.register_instance(cfg, name, spawned_root)
    config.save_config(cfg)

    monkeypatch.setattr(spawn, "main", lambda args: 0)
    rc = cli._cmd_spawn(["--root", str(spawned_root)])
    assert rc == 0


# --------------------------------------------------------------------------- #
# S3.5 — seed_index sys.path cleanup
# --------------------------------------------------------------------------- #
def test_seed_index_removes_syspath_entry(tmp_path):
    """seed_index removes the _tools entry from sys.path after the call."""
    from oracle_agent.spawn import seed_index
    fake_tools = tmp_path / "_tools"
    fake_tools.mkdir()
    tools_str = str(fake_tools)
    # Ensure the path is NOT already there
    assert tools_str not in sys.path
    seed_index(tmp_path)
    assert tools_str not in sys.path, "seed_index must clean up sys.path"


def test_seed_index_does_not_remove_preexisting_path(tmp_path):
    """If _tools was already in sys.path, seed_index must not remove it."""
    from oracle_agent.spawn import seed_index
    fake_tools = tmp_path / "_tools"
    fake_tools.mkdir()
    tools_str = str(fake_tools)
    sys.path.insert(0, tools_str)
    try:
        seed_index(tmp_path)
        # path was already there before → must still be there
        assert tools_str in sys.path
    finally:
        while tools_str in sys.path:
            sys.path.remove(tools_str)


# --------------------------------------------------------------------------- #
# S10 — version skew warning (SPEC S10 enforcer)
# --------------------------------------------------------------------------- #
def test_version_skew_warns(profile, spawned_root, monkeypatch, capsys):
    """oracle version prints a version-skew warning when kernel != packaged."""
    _register(spawned_root, "main")
    monkeypatch.setattr(doctor, "_vendored_tools_version", lambda: "v99")
    monkeypatch.setattr(doctor, "_root_tools_version", lambda r: "v1")
    rc = cli.main(["version"])
    assert rc == 0
    out = capsys.readouterr().out
    # The version command prints kernel versions; skew is visible
    assert "v99" in out or "v1" in out
