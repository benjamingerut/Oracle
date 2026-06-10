#!/usr/bin/env python3
"""Tests for oracle_yaml.py -- the safe-subset YAML loader.

Proves: (1) the supported subset parses correctly with stable scalar behavior,
and (2) every forbidden construct RAISES UnsupportedYAML rather than being
silently accepted or mis-parsed.
"""
from __future__ import annotations

import pytest

import oracle_yaml
from oracle_yaml import UnsupportedYAML, safe_load


SAMPLE = """\
company:
  name: "Acme Corp"
  codename: 'ORACLE'
  maturity: scaffolded
  founded: 1999
  ratio: 0.25
  active: true
  retired: false
  notes: null
security:
  sensitivity_labels:
    - public
    - internal
    - confidential
routing_lanes:
  - 00_Ownership-Strategy
  - 01_Finance
nested:
  level1:
    level2:
      key: value
"""


def test_parses_supported_subset():
    data = safe_load(SAMPLE)
    assert data["company"]["name"] == "Acme Corp"
    assert data["company"]["codename"] == "ORACLE"
    assert data["company"]["founded"] == 1999
    assert data["company"]["ratio"] == 0.25
    assert data["company"]["active"] is True
    assert data["company"]["retired"] is False
    assert data["company"]["notes"] is None
    assert data["security"]["sensitivity_labels"] == ["public", "internal", "confidential"]
    assert data["routing_lanes"] == ["00_Ownership-Strategy", "01_Finance"]
    assert data["nested"]["level1"]["level2"]["key"] == "value"


def test_comments_and_blank_lines_ignored():
    text = """\
# top comment
key: value   # trailing comment

other: 42
list:
  - a   # inline
  - b
"""
    data = safe_load(text)
    assert data == {"key": "value", "other": 42, "list": ["a", "b"]}


def test_hash_inside_quotes_is_not_a_comment():
    data = safe_load('title: "a # not a comment"\n')
    assert data["title"] == "a # not a comment"


def test_ampersand_inside_quotes_is_not_an_anchor():
    data = safe_load('name: "Smith & Wesson"\n')
    assert data["name"] == "Smith & Wesson"


def test_iso_dates_remain_strings_even_when_unquoted():
    data = safe_load("created: 2026-06-08\nupdated: 2026-06-09\n")
    assert data == {"created": "2026-06-08", "updated": "2026-06-09"}


# ---------------------------------------------------------------------------
# Forbidden constructs must RAISE.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "base: &anchor\n  a: 1\nother: *anchor\n",            # anchor + alias
        "value: !!python/object/apply:os.system ['id']\n",     # tag (code-exec vector)
        "tagged: !custom thing\n",                              # generic tag
        "flow_map: {a: 1, b: 2}\n",                             # flow mapping
        "flow_seq: [1, 2, 3]\n",                                # flow sequence
        "---\ndoc: 1\n---\ndoc: 2\n",                           # multi-document
        "%YAML 1.1\n---\nkey: value\n",                         # directive
        "block: |\n  multi\n  line\n",                          # block scalar |
        "folded: >\n  folded\n  text\n",                        # block scalar >
        "done: 1\n...\n",                                       # document end marker
    ],
)
def test_forbidden_constructs_raise(text):
    with pytest.raises(UnsupportedYAML):
        safe_load(text)


def test_malicious_tag_does_not_execute():
    """The classic pyyaml RCE payload must be REFUSED, never evaluated."""
    payload = "x: !!python/object/apply:subprocess.check_output [['echo', 'pwned']]\n"
    with pytest.raises(UnsupportedYAML):
        safe_load(payload)


def test_tab_indentation_rejected():
    with pytest.raises(UnsupportedYAML):
        safe_load("key:\n\t- value\n")


def test_empty_and_none():
    assert safe_load("") is None
    assert safe_load("# only a comment\n") is None
    assert safe_load(None) is None


def test_non_str_raises_typeerror():
    with pytest.raises(TypeError):
        safe_load(123)  # type: ignore[arg-type]


def test_pure_loader_independent_of_pyyaml(monkeypatch):
    """Force the pyyaml-absent path and confirm the pure parser still works and
    still raises on forbidden constructs."""
    import builtins

    real_import = builtins.__import__

    def no_yaml(name, *a, **k):
        if name == "yaml":
            raise ImportError("simulated: pyyaml absent")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    assert safe_load(SAMPLE)["company"]["founded"] == 1999
    with pytest.raises(UnsupportedYAML):
        safe_load("flow: [1, 2]\n")
