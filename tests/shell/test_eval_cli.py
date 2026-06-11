"""tests/shell/test_eval_cli.py -- the `oracle eval` CLI surface (P6-T6).

Asserts:
  * `oracle eval` runs the catalog and prints a scorecard; writes NOTHING;
  * `oracle eval --ci` is GREEN on main (exit 0, no safety breach);
  * `oracle eval --ci` writes NOTHING (CI cannot commit);
  * `oracle eval --dimension D` subsets; an unknown dimension errors;
  * `oracle eval --write` is the human action that writes docs/eval/<date>.md;
  * the production CLI import path stays testkit-free (cli.py imports the eval
    package lazily via importlib -- covered structurally by the converse guard
    in test_testkit.py; here we assert importing cli does not import testkit).
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _run_eval(argv):
    from oracle_agent.cli import main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(["eval", *argv])
    return rc, out.getvalue(), err.getvalue()


def test_eval_runs_and_prints_scorecard():
    rc, out, _err = _run_eval(["--dimension", "policy", "--date", "2026-06-11"])
    assert rc == 0
    assert "Oracle Eval Scorecard -- 2026-06-11" in out
    assert "| policy | safety |" in out


def test_eval_ci_green_on_main():
    """The whole catalog passes -> --ci exits 0 (the gate is green on main)."""
    rc, _out, err = _run_eval(["--ci", "--date", "2026-06-11"])
    assert rc == 0, f"oracle eval --ci was not green on main: {err}"
    assert "SAFETY FLOOR BREACH" not in err


def test_eval_default_writes_nothing(tmp_path, monkeypatch):
    """`oracle eval` (no --write) must not create docs/eval/<date>.md."""
    docs_eval = _SRC.parents[0] / "docs" / "eval"
    before = set(p.name for p in docs_eval.glob("*.md")) if docs_eval.exists() else set()
    _run_eval(["--dimension", "policy", "--date", "2026-06-11"])
    after = set(p.name for p in docs_eval.glob("*.md")) if docs_eval.exists() else set()
    assert after == before, "oracle eval (no --write) wrote a scorecard file"


def test_eval_ci_writes_nothing():
    """--ci must write nothing (CI cannot commit; a writing --ci dirties every run)."""
    docs_eval = _SRC.parents[0] / "docs" / "eval"
    before = set(p.name for p in docs_eval.glob("*.md")) if docs_eval.exists() else set()
    _run_eval(["--ci", "--date", "2026-06-11"])
    after = set(p.name for p in docs_eval.glob("*.md")) if docs_eval.exists() else set()
    assert after == before, "oracle eval --ci wrote a scorecard file"


def test_eval_unknown_dimension_errors():
    rc, _out, err = _run_eval(["--dimension", "bogus"])
    assert rc == 2
    assert "unknown dimension" in err


def test_eval_write_persists_dated_scorecard(tmp_path, monkeypatch):
    """--write persists docs/eval/<date>.md under a redirected repo root."""
    # Redirect _repo_root so we never touch the real docs/eval/.
    import importlib
    eval_cli = importlib.import_module("oracle_agent.eval.cli")
    monkeypatch.setattr(eval_cli, "_repo_root", lambda: tmp_path)

    from oracle_agent.cli import main
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        rc = main(["eval", "--dimension", "policy", "--write",
                   "--date", "2026-06-11"])
    assert rc == 0
    written = tmp_path / "docs" / "eval" / "2026-06-11.md"
    assert written.exists()
    assert "Oracle Eval Scorecard -- 2026-06-11" in written.read_text()


def test_importing_cli_does_not_import_testkit():
    """The production CLI import path stays testkit-free (P6S-12).

    Importing oracle_agent.cli must NOT pull in oracle_agent.testkit (the eval
    package is reached lazily via importlib inside the handler).
    """
    # Fresh subprocess so module-cache state from other tests doesn't mask it.
    import subprocess
    code = (
        "import sys; "
        "import oracle_agent.cli; "
        "assert 'oracle_agent.testkit' not in sys.modules, "
        "'importing cli pulled in testkit'; "
        "assert 'oracle_agent.eval' not in sys.modules, "
        "'importing cli pulled in the eval package'; "
        "print('ok')"
    )
    env = {"PYTHONPATH": str(_SRC)}
    import os
    env.update({k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    env["PYTHONPATH"] = str(_SRC)
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "ok" in proc.stdout
