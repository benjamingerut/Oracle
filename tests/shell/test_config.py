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
