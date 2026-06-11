"""Wizard operating-agent (dream actuator) step (P5-T7a #1).

These drive the wizard's optional dream step with SCRIPTED input against a
testkit-spawned root. They prove:

  * the step writes the dream.* keys EXCLUSIVELY through the
    ``oracle admin autonomy set-dream`` kernel verb (a raw autonomy.yml write
    path does not exist in the wizard);
  * the verb refuses to touch level/caps -- a level-0 root stays level-0/off
    after configuring the command;
  * the wizard never raises the autonomy level itself;
  * the dream step on a level<2 root explains that dream sessions stay BLOCKED;
  * a non-TTY blank/skip answer skips the step cleanly.

Written in the test_wizard_connectors.py style (scripted stdin double).
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from oracle_agent import wizard


class _Script:
    """A line-feeding stdin double (``_ask`` calls ``.readline()`` in order)."""

    def __init__(self, lines: list[str]):
        self._buf = io.StringIO("".join(l if l.endswith("\n") else l + "\n"
                                        for l in lines))

    def readline(self) -> str:
        return self._buf.readline()


def _set_level(root: Path, *, enabled: bool, level: int) -> None:
    d = root / "Meta.nosync" / "Autonomy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "autonomy.yml").write_text(
        f"enabled: {'true' if enabled else 'false'}\n"
        f"level: {level}\n"
        "allowed_loops:\n"
        "writable_lanes:\n"
        "readonly_connectors:\n"
        "blast_radius_caps:\n"
        "  max_files_per_run: 50\n"
        "  max_bytes: 10000000\n"
        'kill_switch_file: "Meta.nosync/Autonomy/KILL-SWITCH"\n'
        "dream:\n"
        "  command:\n"
        "  max_minutes: 30\n"
        "  max_inbox_items: 10\n",
        encoding="utf-8",
    )


def _read_autonomy_text(root: Path) -> str:
    return (root / "Meta.nosync" / "Autonomy" / "autonomy.yml").read_text()


# --------------------------------------------------------------------------- #
# the step configures dream.* via the kernel verb only (never a raw write)
# --------------------------------------------------------------------------- #
def test_dream_step_writes_via_set_dream_verb(profile, tmp_path):
    from oracle_agent.testkit import spawn_test_root
    root = tmp_path / "root"
    try:
        spawn_test_root(root, name="Dream Wiz")
    except RuntimeError as exc:
        pytest.skip(f"spawn failed: {exc}")
    _set_level(root, enabled=False, level=0)

    # yes / command / minutes / items
    script = _Script(["y", "claude -p", "20", "7"])
    out = io.StringIO()
    wizard.dream_step(root, "main", stream_in=script, stream_out=out)

    text = _read_autonomy_text(root)
    # the dream subtree is set...
    assert 'command: "claude -p"' in text
    assert "max_minutes: 20" in text
    assert "max_inbox_items: 7" in text
    # ...and the verb PRESERVED level/enabled (never raised them).
    assert "level: 0" in text
    assert "enabled: false" in text
    assert "configured via the set-dream verb" in out.getvalue()


def test_dream_step_does_not_raise_level(profile, tmp_path):
    """Configuring the command never raises the autonomy level (earned flow)."""
    from oracle_agent.testkit import spawn_test_root
    root = tmp_path / "root"
    try:
        spawn_test_root(root, name="Dream Wiz2")
    except RuntimeError as exc:
        pytest.skip(f"spawn failed: {exc}")
    _set_level(root, enabled=False, level=0)

    out = io.StringIO()
    wizard.dream_step(root, "main", stream_in=_Script(["y", "claude -p", "", ""]),
                      stream_out=out)
    # caps preserved at 50/10000000 (unchanged), level still 0.
    assert wizard._autonomy_level(root) == 0
    text = _read_autonomy_text(root)
    assert "max_files_per_run: 50" in text
    # the operator is told sessions remain BLOCKED below level 2.
    assert "LEVEL 2" in out.getvalue() or "level 2" in out.getvalue().lower()


def test_dream_step_level2_root_reports_eligible(profile, tmp_path):
    from oracle_agent.testkit import spawn_test_root
    root = tmp_path / "root"
    try:
        spawn_test_root(root, name="Dream Wiz3")
    except RuntimeError as exc:
        pytest.skip(f"spawn failed: {exc}")
    _set_level(root, enabled=True, level=2)

    out = io.StringIO()
    wizard.dream_step(root, "main", stream_in=_Script(["y", "claude -p", "30", "10"]),
                      stream_out=out)
    text = _read_autonomy_text(root)
    assert 'command: "claude -p"' in text
    assert "level: 2" in text  # unchanged
    assert "gate-eligible" in out.getvalue()


# --------------------------------------------------------------------------- #
# skip paths
# --------------------------------------------------------------------------- #
def test_dream_step_declined_is_clean_skip(profile, tmp_path):
    from oracle_agent.testkit import spawn_test_root
    root = tmp_path / "root"
    try:
        spawn_test_root(root, name="Dream Wiz4")
    except RuntimeError as exc:
        pytest.skip(f"spawn failed: {exc}")
    _set_level(root, enabled=False, level=0)

    out = io.StringIO()
    wizard.dream_step(root, "main", stream_in=_Script(["N"]), stream_out=out)
    # no command written; the step was skipped.
    assert "command:\n" in _read_autonomy_text(root)
    assert "no dream actuator configured" in out.getvalue()


def test_dream_step_blank_command_skips(profile, tmp_path):
    from oracle_agent.testkit import spawn_test_root
    root = tmp_path / "root"
    try:
        spawn_test_root(root, name="Dream Wiz5")
    except RuntimeError as exc:
        pytest.skip(f"spawn failed: {exc}")
    _set_level(root, enabled=False, level=0)

    out = io.StringIO()
    wizard.dream_step(root, "main", stream_in=_Script(["y", ""]), stream_out=out)
    assert "command:\n" in _read_autonomy_text(root)
    assert "no dream command" in out.getvalue()


# --------------------------------------------------------------------------- #
# the wizard never offers a raw autonomy.yml write path
# --------------------------------------------------------------------------- #
def test_wizard_has_no_raw_autonomy_write():
    """The wizard module writes autonomy.yml ONLY via the set-dream kernel verb.

    Grep the wizard source: it must never write to autonomy.yml directly (the
    only path to dream.* is the kernel verb, P5S-7).
    """
    src = Path(wizard.__file__).read_text()
    # the dream step composes the set-dream verb argv, never a raw file write.
    assert '"set-dream"' in src
    # isolate the dream_step body and assert it never writes a file directly.
    body = src.split("def dream_step", 1)[1].split("\ndef ", 1)[0]
    assert "set-dream" in body
    # the step performs NO direct file writes at all (no raw autonomy.yml path).
    assert ".write_text" not in body
    assert "open(" not in body
