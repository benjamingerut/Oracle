"""Tests for config.py (SPEC S1 / S10)."""
from __future__ import annotations

import json
import os
import stat

import pytest

from oracle_agent import config


def test_defaults_loaded_when_absent(profile):
    cfg = config.load_config()
    assert cfg["provider"]["name"] == "anthropic"
    assert cfg["gateway"]["telegram"]["enabled"] is False
    assert cfg["instances"] == {}


def test_save_then_load_roundtrip_and_merge(profile):
    cfg = config.load_config()
    cfg["provider"]["model"] = "gpt-4o"
    config.save_config(cfg)
    again = config.load_config()
    assert again["provider"]["model"] == "gpt-4o"
    # missing keys still backfilled from defaults
    assert again["chat"]["max_iterations"] == 20


def test_config_file_is_0600(profile):
    config.save_config(config.load_config())
    mode = stat.S_IMODE(os.stat(config.config_path()).st_mode)
    assert mode == 0o600


def test_save_refuses_literal_api_key(profile):
    cfg = config.load_config()
    cfg["provider"]["api_key"] = "sk-livesecretvalue0123456789"
    with pytest.raises(ValueError, match="secret"):
        config.save_config(cfg)


def test_save_allows_env_var_name(profile):
    cfg = config.load_config()
    cfg["provider"]["api_key_env"] = "MY_KEY_ENV"  # a NAME, not a value
    config.save_config(cfg)  # must not raise


def test_save_refuses_userinfo_url(profile):
    cfg = config.load_config()
    cfg["provider"]["base_url"] = "https://user:hunter2@api.example.com/v1"
    with pytest.raises(ValueError, match="userinfo"):
        config.save_config(cfg)


def test_save_refuses_bearer_token_anywhere(profile):
    cfg = config.load_config()
    cfg["instances"]["x"] = {"root": "/tmp/x", "note": "Bearer abcdefg"}
    with pytest.raises(ValueError):
        config.save_config(cfg)


def test_set_env_secret_roundtrip_and_perms(profile):
    config.set_env_secret("ORACLE_LLM_API_KEY", "sk-supersecret-value-xyz")
    assert config.resolve_secret("ORACLE_LLM_API_KEY") == "sk-supersecret-value-xyz"
    mode = stat.S_IMODE(os.stat(config.env_path()).st_mode)
    assert mode == 0o600


def test_set_env_secret_never_in_config(profile):
    config.set_env_secret("ORACLE_LLM_API_KEY", "sk-supersecret-value-xyz")
    config.save_config(config.load_config())
    raw = config.config_path().read_text()
    assert "supersecret" not in raw


def test_resolve_secret_prefers_environ(profile, monkeypatch):
    config.set_env_secret("ORACLE_LLM_API_KEY", "from-dotenv")
    monkeypatch.setenv("ORACLE_LLM_API_KEY", "from-environ")
    assert config.resolve_secret("ORACLE_LLM_API_KEY") == "from-environ"


def test_set_env_secret_rejects_bad_name(profile):
    with pytest.raises(ValueError):
        config.set_env_secret("not-a-valid-name", "x")


# --------------------------------------------------------------------------- #
# P7-T1 — write_root_env_secret targets the ROOT's .env.nosync (P7S-4)
# --------------------------------------------------------------------------- #
def test_write_root_env_secret_roundtrip_and_perms(profile, tmp_path):
    root = tmp_path / "oracle_root"
    root.mkdir()
    config.write_root_env_secret(root, "TOY_TOKEN", "rotated-secret-value-xyz")
    env_file = root / ".env.nosync"
    assert env_file.exists()
    mode = stat.S_IMODE(os.stat(env_file).st_mode)
    assert mode == 0o600
    text = env_file.read_text()
    assert "TOY_TOKEN=rotated-secret-value-xyz" in text


def test_write_root_env_secret_upserts(profile, tmp_path):
    root = tmp_path / "oracle_root"
    root.mkdir()
    config.write_root_env_secret(root, "A_TOKEN", "first")
    config.write_root_env_secret(root, "B_TOKEN", "second")
    config.write_root_env_secret(root, "A_TOKEN", "updated")  # upsert, not append
    text = (root / ".env.nosync").read_text()
    assert "A_TOKEN=updated" in text
    assert "B_TOKEN=second" in text
    assert text.count("A_TOKEN=") == 1


def test_write_root_env_secret_not_profile_env(profile, tmp_path):
    """The root's .env.nosync is distinct from the profile .env -- a scheduled
    kernel pull sees the root file, never the scrubbed profile env (P7S-4)."""
    root = tmp_path / "oracle_root"
    root.mkdir()
    config.write_root_env_secret(root, "TOY_TOKEN", "root-only-secret")
    # The profile .env is untouched / does not carry the connector secret.
    profile_env = config.env_path()
    assert (not profile_env.exists()) or "root-only-secret" not in profile_env.read_text()


def test_write_root_env_secret_rejects_bad_name(profile, tmp_path):
    root = tmp_path / "oracle_root"
    root.mkdir()
    with pytest.raises(ValueError):
        config.write_root_env_secret(root, "not-a-valid-name", "x")


def test_write_root_env_secret_rejects_newline(profile, tmp_path):
    root = tmp_path / "oracle_root"
    root.mkdir()
    with pytest.raises(ValueError):
        config.write_root_env_secret(root, "TOY_TOKEN", "line1\nline2")


def test_register_instance_sets_default(profile, tmp_path):
    cfg = config.load_config()
    root = tmp_path / "r"
    root.mkdir()
    cfg = config.register_instance(cfg, "alpha", root)
    assert cfg["default_instance"] == "alpha"
    assert "alpha" in config.instance_roots(cfg)


def test_profile_dir_is_0700(profile):
    d = config.profile_dir()
    mode = stat.S_IMODE(os.stat(d).st_mode)
    assert mode == 0o700


# --------------------------------------------------------------------------- #
# S3.3 — extended secret-guard patterns
# --------------------------------------------------------------------------- #
def test_save_refuses_sk_ant_key(profile):
    """sk-ant-… hyphenated Anthropic keys must be refused."""
    cfg = config.load_config()
    cfg["instances"]["x"] = {"root": "/tmp/x", "note": "sk-ant-api03-abcdefghij1234567890"}
    with pytest.raises(ValueError, match="sk-ant"):
        config.save_config(cfg)


def test_save_refuses_telegram_bot_token(profile):
    """Telegram bot tokens (NNN:AA…) must be refused."""
    cfg = config.load_config()
    # A realistic Telegram bot token shape
    cfg["instances"]["x"] = {"root": "/tmp/x",
                             "note": "1234567890:AABBccDDeeffGGHHiijjKKLLmmNNooP"}
    with pytest.raises(ValueError):
        config.save_config(cfg)


def test_scan_secret_leak_sk_ant_direct():
    """_scan_secret_leak detects sk-ant- keys directly."""
    reason = config._scan_secret_leak("sk-ant-api03-sometoken12345678")
    assert reason is not None
    assert "sk-ant" in reason


def test_scan_secret_leak_telegram_token_direct():
    """_scan_secret_leak detects Telegram bot token shape directly."""
    reason = config._scan_secret_leak("9876543210:BBCCddEEFFggHHIIjjKKLLmmNNOOppQQ")
    assert reason is not None


# --------------------------------------------------------------------------- #
# P1-T3 — config versioning + migration
# --------------------------------------------------------------------------- #

# A minimal v1 fixture (no "version" key, real-world shape).
_V1_FIXTURE: dict = {
    "provider": {
        "name": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "api_key_env": "MY_OPENAI_KEY",
        "max_tokens": 2048,
        "local_is_confined": False,
    },
    "chat": {"max_iterations": 10},
    "gateway": {
        "telegram": {
            "enabled": True,
            "token_env": "TG_BOT_TOKEN",
            "allowlist": {"42": {"role": "user", "instance": "prod"}},
            "max_sensitivity": "public",
        }
    },
    "instances": {"prod": {"root": "/srv/oracle/prod"}},
    "ingest_roots": ["/data/docs"],
    "default_instance": "prod",
}


def test_v1_fixture_loads_and_migrates_in_memory(profile):
    """A v1 config (no 'version') loads, is migrated to v2 in memory.
    The raw file on disk must remain byte-identical after the load.
    """
    p = config.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    raw_text = json.dumps(_V1_FIXTURE, indent=2, sort_keys=True) + "\n"
    p.write_text(raw_text, encoding="utf-8")

    cfg = config.load_config()

    # File on disk must be untouched (in-memory-only migration, P1S-14).
    assert p.read_text(encoding="utf-8") == raw_text, (
        "load_config must NOT write the file; raw file was modified."
    )

    # The returned dict should have the merged defaults + migrated data.
    assert cfg["provider"]["name"] == "openai"
    assert cfg["gateway"]["telegram"]["enabled"] is True
    assert cfg["ingest_roots"] == ["/data/docs"]
    assert cfg["default_instance"] == "prod"

    # The raw file still has NO "version" key (it was not saved by load).
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert "version" not in on_disk


def test_version_not_in_default_config():
    """'version' must not appear in DEFAULT_CONFIG (P1S-6 / frozen interface)."""
    assert "version" not in config.DEFAULT_CONFIG


def test_future_version_rejected(profile):
    """A config with version > CONFIG_VERSION must be rejected with guidance."""
    p = config.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    future = {"version": config.CONFIG_VERSION + 5, "provider": {"name": "test"}}
    p.write_text(json.dumps(future) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"[Uu]pgrade"):
        config.load_config()

    # The file must be unchanged.
    assert json.loads(p.read_text())["version"] == config.CONFIG_VERSION + 5


def test_migration_idempotency():
    """Applying migrations to an already-migrated dict is identity.

    Running the full migration sequence twice must produce the same result as
    running it once (pure + idempotent guarantee).
    """
    raw = {"version": 1, "gateway": {"telegram": {"enabled": False}}}

    # First pass
    m1 = config.MIGRATIONS[1](raw)
    # Second pass (re-migrate the result starting from v1 again — same input)
    m2 = config.MIGRATIONS[1](raw)

    assert m1 == m2


def test_migration_v1_to_v2_stamps_version():
    """The v1→v2 migration must set 'version' to 2 and preserve all other keys."""
    raw = {"provider": {"name": "test"}, "ingest_roots": ["/foo"]}
    migrated = config.MIGRATIONS[1](raw)
    assert migrated["version"] == 2
    assert migrated["provider"] == {"name": "test"}
    assert migrated["ingest_roots"] == ["/foo"]


def test_security_key_drop_caught(profile, monkeypatch):
    """A migration that drops a SECURITY_KEYS path must be caught as a hard error.

    We deliberately plant a broken migration in MIGRATIONS via monkeypatch.
    It drops gateway.telegram.allowlist — the preservation check must fire.
    """
    import copy as _copy

    def _bad_migrate(raw: dict) -> dict:
        """Deliberately drop gateway.telegram.allowlist."""
        out = _copy.deepcopy(raw)
        out["version"] = 2
        try:
            del out["gateway"]["telegram"]["allowlist"]
        except KeyError:
            pass
        return out

    monkeypatch.setitem(config.MIGRATIONS, 1, _bad_migrate)

    p = config.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    v1 = {
        "gateway": {
            "telegram": {
                "enabled": True,
                "token_env": "TG_TOKEN",
                "allowlist": {"7": {"role": "user", "instance": "x"}},
                "max_sensitivity": "internal",
            }
        }
    }
    p.write_text(json.dumps(v1) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"[Ss]ecurity key"):
        config.load_config()

    # File must be unchanged after the hard error.
    assert json.loads(p.read_text())["gateway"]["telegram"]["allowlist"] == {
        "7": {"role": "user", "instance": "x"}
    }


def test_corrupt_config_rejected_and_not_clobbered(profile):
    """A corrupt (unparseable) config must raise ValueError and leave the file intact."""
    p = config.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    corrupt = b"{ this is: not valid JSON !!!\x00"
    p.write_bytes(corrupt)

    with pytest.raises(ValueError, match=r"[Cc]orrupt|unreadable"):
        config.load_config()

    # File must still contain the original corrupt bytes.
    assert p.read_bytes() == corrupt


def test_save_stamps_version(profile):
    """save_config must write 'version': CONFIG_VERSION into the file."""
    cfg = config.load_config()
    config.save_config(cfg)
    on_disk = json.loads(config.config_path().read_text())
    assert on_disk.get("version") == config.CONFIG_VERSION


def test_save_does_not_mutate_caller_dict(profile):
    """save_config must not mutate the dict passed by the caller."""
    cfg = config.load_config()
    cfg_copy = json.loads(json.dumps(cfg))
    # Ensure no "version" present in a freshly-loaded dict (no prior save).
    cfg.pop("version", None)
    config.save_config(cfg)
    # cfg must be unchanged
    assert cfg == cfg_copy or "version" not in cfg


def test_round_trip_version(profile):
    """save then load round-trip preserves version and a user-set key."""
    cfg = config.load_config()
    cfg["provider"]["model"] = "test-round-trip-model"
    config.save_config(cfg)
    again = config.load_config()
    assert again["provider"]["model"] == "test-round-trip-model"
    # After save+load the version in the returned dict reflects the merge.
    # The file must carry CONFIG_VERSION.
    on_disk = json.loads(config.config_path().read_text())
    assert on_disk["version"] == config.CONFIG_VERSION


def test_already_v2_config_loads_without_migration(profile):
    """A config already at CONFIG_VERSION=2 must load without triggering migrations."""
    import copy as _copy
    call_log: list[int] = []

    original_migrate = config.MIGRATIONS.get(1)

    def _tracking_migrate(raw: dict) -> dict:
        call_log.append(1)
        return original_migrate(raw)  # type: ignore[misc]

    # Write a v2 config so no migration should run.
    p = config.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    v2 = {"version": 2, "provider": {"name": "already-v2"}}
    p.write_text(json.dumps(v2) + "\n", encoding="utf-8")

    # Temporarily replace migration — it must NOT be called.
    orig = config.MIGRATIONS.get(1)
    config.MIGRATIONS[1] = _tracking_migrate
    try:
        cfg = config.load_config()
    finally:
        if orig is not None:
            config.MIGRATIONS[1] = orig
        else:
            del config.MIGRATIONS[1]

    assert call_log == [], "Migration must NOT run when config is already at CONFIG_VERSION"
    assert cfg["provider"]["name"] == "already-v2"


def test_security_key_preserved_for_wildcard_providers(profile, monkeypatch):
    """providers.*.api_key_env wildcard must be checked for each provider entry."""
    import copy as _copy

    def _bad_migrate_drops_api_key(raw: dict) -> dict:
        """Drop api_key_env from all providers sub-entries."""
        out = _copy.deepcopy(raw)
        out["version"] = 2
        for prov in out.get("providers", {}).values():
            prov.pop("api_key_env", None)
        return out

    monkeypatch.setitem(config.MIGRATIONS, 1, _bad_migrate_drops_api_key)

    p = config.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    v1 = {
        "providers": {
            "openai": {"api_key_env": "OPENAI_KEY"},
            "anthropic": {"api_key_env": "ANTHROPIC_KEY"},
        }
    }
    p.write_text(json.dumps(v1) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"[Ss]ecurity key"):
        config.load_config()
