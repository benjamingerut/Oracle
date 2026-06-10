#!/usr/bin/env python3
"""CI grep-guard: containment is a STRUCTURAL invariant, not a re-proven property.

Every filesystem write that touches a user-/config-influenced path MUST go
through ``safe_paths`` (contain / safe_copy_verify_delete). This guard greps
every kernel ``_tools/**/*.py`` file -- EXCEPT ``safe_paths.py`` itself, which is
where the raw primitives legitimately live -- for two bypass shapes:

  1. ``shutil.move`` / ``shutil.copy`` / ``shutil.copy2`` (raw file movement)
  2. ``open(<expr>, 'w'|'a'|...)`` where the first argument is NOT a string
     literal (i.e. a variable/expression path that could be user-influenced)

A line may opt out only by carrying the documented marker ``# safe_paths-internal``
(used inside the floor durability primitive ``ledger.py``, which is itself a
single chokepoint analogous to ``safe_paths``). Any other hit FAILS the build,
so reintroducing a raw ``shutil.move`` anywhere in the tool layer turns CI red.

This guard runs meaningfully at full-verify, when the whole tool layer exists.
During the floor-first build it tolerates a partial ``_tools`` tree (it scans
whatever ``.py`` files are present) and never errors on a missing sibling.
"""
from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
ALLOW_MARKER = "# safe_paths-internal"
WRITE_MODES = {"w", "a", "w+", "a+", "wb", "ab", "wt", "at", "x", "xb", "xt"}


def _tool_files() -> list[Path]:
    if not _TOOLS.is_dir():
        return []
    files: list[Path] = []
    for p in sorted(_TOOLS.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        if p.name == "safe_paths.py":
            continue  # the one place raw primitives may live
        files.append(p)
    return files


def _allowlisted_lines(source: str) -> set[int]:
    """Return the set of 1-based line numbers that carry the allow marker.

    We look at the raw source text per-line (cheap and robust) AND, to be safe
    against multi-line statements, also map any logical line that contains a
    comment token with the marker.
    """
    allowed: set[int] = set()
    for i, line in enumerate(source.splitlines(), start=1):
        if ALLOW_MARKER in line:
            allowed.add(i)
    # Also honour the marker when it sits on a continuation/closing line of a
    # multi-line call: capture comment tokens via tokenize.
    try:
        toks = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in toks:
            if tok.type == tokenize.COMMENT and ALLOW_MARKER in tok.string:
                allowed.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return allowed


class _BypassVisitor(ast.NodeVisitor):
    """Collect raw shutil.move/copy/copy2 and open(non-literal, write-mode)."""

    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []  # (lineno, description)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func

        # shutil.move / shutil.copy / shutil.copy2
        if isinstance(func, ast.Attribute) and func.attr in (
            "move",
            "copy",
            "copy2",
            "copyfile",
        ):
            base = func.value
            if isinstance(base, ast.Name) and base.id == "shutil":
                self.hits.append((node.lineno, f"shutil.{func.attr}(...)"))

        # bare move/copy/copy2 imported via "from shutil import move"
        if isinstance(func, ast.Name) and func.id in ("move", "copy", "copy2", "copyfile"):
            self.hits.append((node.lineno, f"{func.id}(...) [from shutil import]"))

        # open(path, mode) with a write mode and a NON-literal path argument
        if isinstance(func, ast.Name) and func.id == "open":
            self._check_open(node)

        self.generic_visit(node)

    def _check_open(self, node: ast.Call) -> None:
        if not node.args:
            return
        path_arg = node.args[0]
        mode = self._mode_of(node)
        if mode is None:
            return  # default mode 'r' -> read, not a write bypass
        if not self._is_write_mode(mode):
            return
        # A constant string-literal path is a fixed, non-user-influenced target.
        if isinstance(path_arg, ast.Constant) and isinstance(path_arg.value, str):
            return
        self.hits.append((node.lineno, f"open(<non-literal>, {mode!r})"))

    @staticmethod
    def _mode_of(node: ast.Call) -> str | None:
        # positional mode is args[1]
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            v = node.args[1].value
            if isinstance(v, str):
                return v
        # keyword mode=
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                v = kw.value.value
                if isinstance(v, str):
                    return v
        # mode present but non-constant -> treat conservatively as unknown write
        if len(node.args) >= 2 or any(kw.arg == "mode" for kw in node.keywords):
            return "?"
        return None

    @staticmethod
    def _is_write_mode(mode: str) -> bool:
        if mode == "?":
            return True  # unknown mode: be conservative, count as a write
        return any(c in mode for c in ("w", "a", "x", "+"))


def _scan_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - surfaces as its own failure
        return [f"{path.name}: could not parse ({exc})"]
    allowed = _allowlisted_lines(source)
    visitor = _BypassVisitor()
    visitor.visit(tree)
    violations: list[str] = []
    for lineno, desc in visitor.hits:
        if lineno in allowed:
            continue
        violations.append(f"{path.name}:{lineno}: {desc}")
    return violations


def test_no_raw_filesystem_bypass_in_tool_layer():
    files = _tool_files()
    if not files:
        pytest.skip("no _tools/*.py present yet (floor-first build)")
    all_violations: list[str] = []
    for f in files:
        all_violations.extend(_scan_file(f))
    assert not all_violations, (
        "Raw filesystem-write bypasses found outside safe_paths.py "
        "(route through safe_paths or tag '# safe_paths-internal'):\n  "
        + "\n  ".join(all_violations)
    )


def test_guard_detects_a_planted_bypass(tmp_path: Path):
    """Meta-test: the guard actually catches a raw shutil.move / open-write."""
    bad = tmp_path / "_tools" / "evil.py"
    bad.parent.mkdir(parents=True)
    bad.write_text(
        "import shutil\n"
        "def go(src, dst, p):\n"
        "    shutil.move(src, dst)\n"
        "    f = open(p, 'w')\n"
        "    f.close()\n",
        encoding="utf-8",
    )
    violations = _scan_file(bad)
    descs = " ".join(violations)
    assert "shutil.move" in descs
    assert "open(<non-literal>" in descs


def test_guard_allows_marker_and_literal(tmp_path: Path):
    """Meta-test: marker-tagged lines and literal-path opens are NOT flagged."""
    ok = tmp_path / "_tools" / "fine.py"
    ok.parent.mkdir(parents=True)
    ok.write_text(
        "def go(p):\n"
        "    f = open(p, 'a')  # safe_paths-internal\n"
        "    f.close()\n"
        "    g = open('/tmp/constant-literal.log', 'w')\n"
        "    g.close()\n"
        "    h = open(p)  # read mode, fine\n"
        "    h.close()\n",
        encoding="utf-8",
    )
    violations = _scan_file(ok)
    assert violations == [], violations
