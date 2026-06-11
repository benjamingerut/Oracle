"""Structural guard: the entire shell is stdlib-only (DESIGN D1 / SPEC S10).

Walks every shell module under src/oracle_agent (excluding the vendored kernel
assets, which carry their own guard in CI) and asserts every import (walked via
``ast.walk``, so function-local imports are caught too) is stdlib or
package-local.

P4S-14 amendment: Slack's Option-A Socket Mode uses an OPTIONAL websocket
dependency. The import is deliberately FUNCTION-LOCAL and try/except-guarded in
``gateway/slack.py`` (never at module scope), so the adapter imports cleanly
when the dep is absent. That one optional import is allowlisted ONLY at its
specific (module, name) coordinate via ``_OPTIONAL_GUARDED`` -- the surface area
of the carve-out is pinned, not a blanket pass for ``websockets`` anywhere. A
companion test (``test_slack_imports_cleanly_when_dep_absent``) proves the
clean-absence property.
"""
from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "oracle_agent"

# P4S-14 optional-guarded allowlist: (module-relative-path, imported-top-name).
# The ONLY non-stdlib import permitted in the shell, and only because it is
# function-local + try/except-guarded in gateway/slack.py (the injectable
# transport means no Slack guarantee depends on it).
_OPTIONAL_GUARDED: set[tuple[str, str]] = {
    ("gateway/slack.py", "websockets"),
}


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
        rel = str(f.relative_to(_SRC))
        for mod in _top_imports(f):
            if mod in stdlib or mod in allowed_local:
                continue
            if (rel, mod) in _OPTIONAL_GUARDED:
                continue  # P4S-14: pinned optional-guarded carve-out
            offenders.append(f"{rel}: imports {mod}")
    assert not offenders, "non-stdlib imports found:\n" + "\n".join(offenders)


def test_optional_guarded_imports_are_function_local_and_guarded():
    """The P4S-14 carve-out must be FUNCTION-LOCAL + try/except guarded.

    A blanket allowlist would let someone move the optional import to module
    scope (breaking clean-absence). This asserts every ``_OPTIONAL_GUARDED``
    import in its file is (a) nested inside a function (never at module top
    level) and (b) wrapped in a ``try``/``except`` -- the two properties that
    make the dep truly optional.
    """
    for rel, name in _OPTIONAL_GUARDED:
        path = _SRC / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))

        # Collect every Import node importing `name`, with whether it sits at
        # module scope and whether it is under a Try.
        module_scope_hits = []
        guarded_hits = 0
        total_hits = 0

        class _Visitor(ast.NodeVisitor):
            def __init__(self):
                self.func_depth = 0
                self.try_depth = 0

            def visit_FunctionDef(self, node):
                self.func_depth += 1
                self.generic_visit(node)
                self.func_depth -= 1

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Try(self, node):
                self.try_depth += 1
                self.generic_visit(node)
                self.try_depth -= 1

            def visit_Import(self, node):
                nonlocal guarded_hits, total_hits
                for alias in node.names:
                    if alias.name.split(".")[0] == name:
                        total_hits += 1
                        if self.func_depth == 0:
                            module_scope_hits.append(node.lineno)
                        if self.try_depth > 0:
                            guarded_hits += 1
                self.generic_visit(node)

        _Visitor().visit(tree)
        assert total_hits > 0, f"{rel}: expected an optional import of {name!r}"
        assert not module_scope_hits, (
            f"{rel}: optional import {name!r} appears at MODULE scope (lines "
            f"{module_scope_hits}) -- it must be function-local (P4S-14)")
        assert guarded_hits == total_hits, (
            f"{rel}: optional import {name!r} is not try/except-guarded at every "
            f"occurrence (P4S-14)")


def test_slack_imports_cleanly_when_dep_absent():
    """gateway/slack imports + constructs an adapter with the dep absent (P4S-14).

    The clean-absence companion test: even with ``websockets`` unimportable,
    importing ``oracle_agent.gateway.slack`` succeeds, ``transport_available()``
    returns False, and a ``SlackAdapter`` can be built over an INJECTED fake
    transport (every guarantee is dep-free). We simulate absence by blocking the
    import of ``websockets`` for the duration of the test.
    """
    import importlib

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "websockets" or name.startswith("websockets."):
            raise ImportError("simulated: websockets absent")
        return real_import(name, *args, **kwargs)

    saved = sys.modules.pop("websockets", None)
    builtins.__import__ = _blocking_import
    try:
        slack = importlib.import_module("oracle_agent.gateway.slack")
        importlib.reload(slack)
        # The module imported cleanly; the optional probe reports absence.
        assert slack.transport_available() is False
        # And an adapter still constructs over an injected transport.

        class _FakeTransport:
            def events(self):
                return []

            def ack(self, _):
                pass

            def post_message(self, *_):
                pass

            def post_typing(self, *_):
                pass

        class _FakeCore:
            def handle(self, msg, *, on_authorized=None):
                return None

        adapter = slack.SlackAdapter(_FakeTransport(), _FakeCore())
        assert adapter.surface == "slack"
    finally:
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules["websockets"] = saved


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
