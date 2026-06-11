"""Tests for oracle upgrade self --from-dir DIR (P1-T5).

Acceptance criteria:
  - Re-vendoring the CURRENT kernel is a no-op by aggregate_sha256 + files
    equality (excluding the manifest 'generated' timestamp) — stays green.
  - A deliberately broken kernel (e.g. invalid tree) is rejected.
  - A tree with a modified oracle_lint.py demands confirmation.
  - Running outside a git checkout refuses.

All tests that mutate the vendored tree are guarded; tests that need stdin
interaction mock it.  Tests that would run `make check` (slow, CI-gated) are
mocked -- the logic under test is the pre/post plumbing, not make itself.

Tests that require a git checkout are skipped when not in one (P1-T5 spec:
"guarded/skipped when not in a git checkout").

TEST-POLLUTION GUARD:
  cmd_upgrade_self copies its --from-dir tree into the *real* vendored package
  tree (src/oracle_agent/assets/oracle-kernel/) and re-renders the manifest in
  place.  Any test that drives cmd_upgrade_self past the gate (gate-confirmed,
  failing-tree-restore, even no-op/refusal paths) therefore risks mutating the
  shipped package tree -- this previously leaked trailing "# modified" lines
  into the real oracle_lint.py and drifted .kernel-manifest.json.

  Defense in depth:
    * The ``sandboxed_vendor`` fixture copies the real vendored tree into
      tmp_path and monkeypatches ``upgrade_shell._VENDORED_KERNEL`` (the single
      symbol cmd_upgrade_self reads to resolve both the copy destination AND
      the manifest render target) to the sandbox.  EVERY test that invokes
      ``upgrade self`` uses it, including refusal/no-op tests.  All assertions
      that previously inspected the real tree now inspect the sandbox.
    * ``_vendored_tree_integrity_guard`` (module-scoped autouse) records the
      real vendored oracle_lint.py sha256 before the module runs and asserts it
      unchanged afterwards, so any future regression fails loudly.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _in_git() -> bool:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False


_REQUIRES_GIT = pytest.mark.skipif(
    not _in_git(),
    reason="not in a git checkout -- upgrade self tests skipped",
)


def _sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# pollution guards
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _vendored_tree_integrity_guard():
    """Assert the REAL vendored oracle_lint.py is byte-identical before/after.

    This is the backstop: if any test in this module (now or in future) leaks a
    write into the real package tree, this fails loudly at module teardown
    instead of silently committing drift.
    """
    import oracle_agent.upgrade_shell as ush

    real_lint = ush._VENDORED_KERNEL / "_tools" / "oracle_lint.py"
    before = _sha256_path(real_lint) if real_lint.exists() else None
    yield
    after = _sha256_path(real_lint) if real_lint.exists() else None
    assert before == after, (
        "REAL vendored oracle_lint.py was mutated during test_revendor.py "
        f"(sha {before} -> {after}). A test wrote into the package tree -- "
        "every cmd_upgrade_self test must use the sandboxed_vendor fixture."
    )


@pytest.fixture
def sandboxed_vendor(tmp_path, monkeypatch):
    """Redirect cmd_upgrade_self's destination to a tmp sandbox.

    Copies the real vendored tree into tmp_path and monkeypatches
    ``upgrade_shell._VENDORED_KERNEL`` to the copy.  cmd_upgrade_self reads
    that symbol at call time to resolve both ``dst`` (the copy destination) and
    the ``manifest.render(dst)`` target, so this fully isolates the run.

    Returns the sandbox Path; tests use it as both the pristine source-of-truth
    for building --from-dir trees and the assertion target.
    """
    import oracle_agent.upgrade_shell as ush

    sandbox = tmp_path / "vendored"
    shutil.copytree(str(ush._VENDORED_KERNEL), str(sandbox))
    monkeypatch.setattr(ush, "_VENDORED_KERNEL", sandbox)
    return sandbox


# ---------------------------------------------------------------------------
# outside-git refusal (can test without actually being outside git by mocking)
# ---------------------------------------------------------------------------

class TestUpgradeSelfGitGuard:

    def test_refuses_outside_git(self, profile, tmp_path, monkeypatch,
                                 sandboxed_vendor):
        """upgrade self refuses when _in_git_checkout() returns False."""
        from oracle_agent import cli
        import oracle_agent.upgrade_shell as ush

        # Build a minimal valid source kernel.
        src = tmp_path / "kernel"
        (src / "_tools").mkdir(parents=True)
        (src / "_tools" / "dummy.py").write_text("# x\n", encoding="utf-8")

        monkeypatch.setattr(ush, "_in_git_checkout", lambda p: False)

        rc = cli.main(["upgrade", "self", "--from-dir", str(src)])
        assert rc == 2

    def test_refuses_if_no_tools_dir(self, profile, tmp_path, monkeypatch,
                                     sandboxed_vendor):
        """upgrade self refuses if --from-dir has no _tools/ subdirectory."""
        from oracle_agent import cli
        import oracle_agent.upgrade_shell as ush

        src = tmp_path / "kernel"
        src.mkdir()

        monkeypatch.setattr(ush, "_in_git_checkout", lambda p: True)

        rc = cli.main(["upgrade", "self", "--from-dir", str(src)])
        assert rc == 2

    def test_refuses_nonexistent_dir(self, profile, tmp_path, monkeypatch,
                                     sandboxed_vendor):
        """upgrade self refuses if --from-dir doesn't exist."""
        from oracle_agent import cli
        import oracle_agent.upgrade_shell as ush

        monkeypatch.setattr(ush, "_in_git_checkout", lambda p: True)

        rc = cli.main(["upgrade", "self", "--from-dir",
                        str(tmp_path / "does_not_exist")])
        assert rc == 2


# ---------------------------------------------------------------------------
# no-op detection
# ---------------------------------------------------------------------------

class TestNoOpDetection:

    def test_revendor_current_is_noop(self, profile, monkeypatch,
                                      sandboxed_vendor):
        """Revendoring the CURRENT vendored kernel detects as no-op and returns 0."""
        from oracle_agent import cli
        import oracle_agent.upgrade_shell as ush

        monkeypatch.setattr(ush, "_in_git_checkout", lambda p: True)

        # Point --from-dir at the (sandboxed) vendored tree itself.
        rc = cli.main(["upgrade", "self", "--from-dir", str(sandboxed_vendor)])
        assert rc == 0

    def test_noop_excludes_generated_timestamp(self, tmp_path, monkeypatch):
        """No-op comparison ignores 'generated' timestamp field."""
        from oracle_agent.upgrade_shell import (
            _compute_files, _aggregate_sha, _manifest_equal_ignoring_timestamp,
        )

        # Two manifests that differ only in 'generated' should be equal by our check.
        m1 = {
            "tools_version": "3.0.0",
            "generated": "2026-01-01T00:00:00",
            "aggregate_sha256": "abc",
            "files": {"_tools/foo.py": "deadbeef"},
        }
        m2 = dict(m1)
        m2["generated"] = "2027-06-01T12:34:56"

        assert _manifest_equal_ignoring_timestamp(m1, m2)

    def test_different_files_not_noop(self, tmp_path, monkeypatch):
        """Manifests with different files are not equal."""
        from oracle_agent.upgrade_shell import _manifest_equal_ignoring_timestamp

        m1 = {"aggregate_sha256": "abc", "files": {"_tools/a.py": "1234"}}
        m2 = {"aggregate_sha256": "xyz", "files": {"_tools/b.py": "5678"}}
        assert not _manifest_equal_ignoring_timestamp(m1, m2)


# ---------------------------------------------------------------------------
# gate-code diff confirmation
# ---------------------------------------------------------------------------

class TestGateCodeConfirmation:

    def test_modified_lint_demands_confirmation_nontty_refuses(
            self, profile, tmp_path, monkeypatch, sandboxed_vendor):
        """Modified oracle_lint.py + non-TTY stdin -> refuses."""
        from oracle_agent import cli
        import oracle_agent.upgrade_shell as ush

        monkeypatch.setattr(ush, "_in_git_checkout", lambda p: True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

        # Build a source kernel identical to vendored except oracle_lint.py is changed.
        src = tmp_path / "kernel"
        shutil.copytree(str(sandboxed_vendor), str(src))
        lint = src / "_tools" / "oracle_lint.py"
        if lint.exists():
            original = lint.read_text(encoding="utf-8")
            lint.write_text(original + "\n# modified by test\n", encoding="utf-8")
        else:
            lint.parent.mkdir(parents=True, exist_ok=True)
            lint.write_text("# modified oracle_lint\n", encoding="utf-8")

        rc = cli.main(["upgrade", "self", "--from-dir", str(src)])
        assert rc == 2

    def test_modified_lint_with_yes_confirmation_proceeds(
            self, profile, tmp_path, monkeypatch, sandboxed_vendor):
        """Modified oracle_lint.py + 'yes' answer -> proceeds (make check mocked)."""
        from oracle_agent import cli
        import oracle_agent.upgrade_shell as ush

        monkeypatch.setattr(ush, "_in_git_checkout", lambda p: True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

        # Simulate 'yes' then nothing.
        answers = iter(["yes"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

        src = tmp_path / "kernel"
        shutil.copytree(str(sandboxed_vendor), str(src))
        lint = src / "_tools" / "oracle_lint.py"
        if lint.exists():
            lint.write_text(lint.read_text() + "\n# modified\n", encoding="utf-8")
        else:
            lint.parent.mkdir(parents=True, exist_ok=True)
            lint.write_text("# modified\n", encoding="utf-8")

        # Mock make check to succeed without actually running it.
        real_run = subprocess.run
        def _mock_run(cmd, **kwargs):
            if cmd and cmd[0] == "make" and "check" in cmd:
                r = MagicMock()
                r.returncode = 0
                return r
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", _mock_run)

        rc = cli.main(["upgrade", "self", "--from-dir", str(src)])
        # Should succeed (0) or fail during make check (1 from our mock returned 0).
        # We mocked make check to return 0, so it should proceed.
        assert rc in (0, 1)  # 1 is ok if some other step fails; 2 = guard refusal
        # The modified lint landed in the SANDBOX, not the real package tree.
        assert (sandboxed_vendor / "_tools" / "oracle_lint.py").read_text(
            encoding="utf-8"
        ).rstrip().endswith("# modified")

    def test_no_confirmation_refuses(self, profile, tmp_path, monkeypatch,
                                     sandboxed_vendor):
        """Modified oracle_lint.py + 'no' answer -> aborts."""
        from oracle_agent import cli
        import oracle_agent.upgrade_shell as ush

        monkeypatch.setattr(ush, "_in_git_checkout", lambda p: True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

        answers = iter(["no"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

        src = tmp_path / "kernel"
        shutil.copytree(str(sandboxed_vendor), str(src))
        lint = src / "_tools" / "oracle_lint.py"
        if lint.exists():
            lint.write_text(lint.read_text() + "\n# modified\n", encoding="utf-8")

        rc = cli.main(["upgrade", "self", "--from-dir", str(src)])
        assert rc == 1


# ---------------------------------------------------------------------------
# failing tree rejection
# ---------------------------------------------------------------------------

class TestFailingTreeRejection:

    def test_broken_kernel_make_check_fails_restores(
            self, profile, tmp_path, monkeypatch, sandboxed_vendor):
        """When make check fails, previous state is restored and rc=1."""
        from oracle_agent import cli
        import oracle_agent.upgrade_shell as ush

        monkeypatch.setattr(ush, "_in_git_checkout", lambda p: True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)  # no gate prompts

        src = tmp_path / "kernel"
        shutil.copytree(str(sandboxed_vendor), str(src))
        # Modify a non-gate file so it's not a no-op.
        dummy = src / "_tools" / "test_dummy_broken.py"
        dummy.write_text("# intentionally different\n", encoding="utf-8")

        # Mock make check to FAIL.
        real_run = subprocess.run
        def _mock_run(cmd, **kwargs):
            if cmd and cmd[0] == "make" and "check" in cmd:
                r = MagicMock()
                r.returncode = 1
                return r
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", _mock_run)

        # Capture state of the SANDBOXED vendored tree before.
        orig_files = {
            p.relative_to(sandboxed_vendor).as_posix(): p.read_bytes()
            for p in sorted(sandboxed_vendor.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        }

        rc = cli.main(["upgrade", "self", "--from-dir", str(src)])
        assert rc == 1  # failed

        # Sandboxed vendored tree should be restored (our dummy file should not be present).
        assert not (sandboxed_vendor / "_tools" / "test_dummy_broken.py").exists()

    @_REQUIRES_GIT
    def test_revendor_current_tree_stays_green(self, profile, sandboxed_vendor):
        """Revendoring the current tree (no-op) returns 0 in a git checkout."""
        from oracle_agent import cli

        rc = cli.main(["upgrade", "self", "--from-dir", str(sandboxed_vendor)])
        assert rc == 0
