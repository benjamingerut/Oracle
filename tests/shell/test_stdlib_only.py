"""Structural guard: the entire shell is stdlib-only (DESIGN D1 / SPEC S10).

Walks every shell module under src/oracle_agent (excluding the vendored kernel
assets, which carry their own guard in CI) and asserts every top-level import
is stdlib or package-local.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "oracle_agent"


def _shell_files():
    for p in _SRC.rglob("*.py"):
        if "assets" in p.parts:
            continue
        yield p


def _top_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative = package-local
            if node.module:
                yield node.module.split(".")[0]


def test_shell_is_stdlib_only():
    stdlib = set(sys.stdlib_module_names)
    # knowledge_index is the spawned root's OWN kernel module, imported
    # dynamically by spawn.seed_index after injecting <root>/_tools -- it is
    # kernel-local, not a third-party dependency.
    # truth_map is the same: the grounding gate (agentloop/grounding.py) reads
    # the truth map via the VENDORED kernel reader, imported dynamically after
    # injecting the vendored _tools dir onto sys.path. Kernel-local, stdlib-only.
    allowed_local = {"oracle_agent", "knowledge_index", "truth_map"}
    offenders = []
    for f in _shell_files():
        for mod in _top_imports(f):
            if mod in stdlib or mod in allowed_local:
                continue
            offenders.append(f"{f.relative_to(_SRC)}: imports {mod}")
    assert not offenders, "non-stdlib imports found:\n" + "\n".join(offenders)


def test_shell_has_no_shell_true():
    """No subprocess call may pass shell=True (AST check, not grep)."""
    offenders = []
    for f in _shell_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) \
                            and kw.value.value is True:
                        offenders.append(f"{f.relative_to(_SRC)}:{node.lineno}")
    assert not offenders, f"shell=True calls found: {offenders}"
