#!/usr/bin/env python3
"""migrations -- ordered kernel migration discovery + apply.

A migration is a module named ``NNNN_<slug>.py`` living in this package, where
``NNNN`` is a zero-padded sequence number. Each migration module MUST expose:

    VERSION   : str   -- the kernel tools version this migration brings the
                         oracle TO (e.g. "3.0.0"). Informational; the ordering
                         that matters is the numeric ``NNNN`` filename prefix.
    DESCRIPTION : str -- a one-line human description.
    def apply(root: Path) -> dict
        Idempotently mutate the oracle at ``root`` and return a small report
        dict (at least ``{"changed": bool, "notes": str}``). A migration MUST be
        safe to run twice: if its change is already present it returns
        ``changed=False`` and does nothing.

Migrations are run in ascending ``NNNN`` order by :func:`apply_all`. The runner
is intentionally tiny and stdlib-only: it discovers migration modules by reading
this directory, imports them by name, sorts by the numeric prefix, and applies
each in turn, collecting per-migration reports.

IMPORTANT: migrations touch oracle.yml / Meta state, which are NOT user-supplied
*paths* -- they are constant internal locations under a given (already-resolved)
oracle root. A migration must never derive a write destination from
user-influenced input; if one ever needs to, it must route that through
``safe_paths``. The baseline migration only stamps a version field in oracle.yml.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Optional

__all__ = ["discover", "load_migration", "apply_all", "Migration"]

_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.py$")
_PKG = __name__  # "migrations" when imported bare, or "..migrations" as a pkg


class Migration:
    """A discovered migration: its numeric order, name, and loaded module."""

    def __init__(self, seq: int, name: str, module) -> None:
        self.seq = seq
        self.name = name
        self.module = module

    @property
    def version(self) -> str:
        return str(getattr(self.module, "VERSION", ""))

    @property
    def description(self) -> str:
        return str(getattr(self.module, "DESCRIPTION", ""))

    def apply(self, root: Path) -> dict:
        fn = getattr(self.module, "apply", None)
        if not callable(fn):
            raise RuntimeError(f"migration {self.name} has no apply()")
        report = fn(Path(root))
        if not isinstance(report, dict):
            report = {"changed": bool(report), "notes": ""}
        report.setdefault("changed", False)
        report.setdefault("notes", "")
        report["migration"] = self.name
        report["seq"] = self.seq
        return report


def _this_dir() -> Path:
    return Path(__file__).resolve().parent


def discover() -> list[tuple[int, str]]:
    """Return ``[(seq, module_basename)]`` for every migration, ascending by seq.

    ``module_basename`` is the filename without the ``.py`` suffix (e.g.
    ``0001_kernel_version_stamp``). The ``__init__`` and any non-conforming file
    are ignored.
    """
    found: list[tuple[int, str]] = []
    for p in sorted(_this_dir().glob("*.py")):
        m = _MIGRATION_RE.match(p.name)
        if not m:
            continue
        seq = int(m.group(1))
        found.append((seq, p.stem))
    found.sort(key=lambda t: t[0])
    return found


def _import_module(module_basename: str):
    """Import a migration module robustly whether the package was imported bare
    (``import migrations`` with ``_tools`` on sys.path) or as a sub-package
    (``from . import migrations``)."""
    # Try the package-qualified name first, then bare.
    candidates = []
    if "." in _PKG:
        candidates.append(f"{_PKG}.{module_basename}")
    candidates.append(f"migrations.{module_basename}")
    candidates.append(module_basename)
    last_exc: Optional[Exception] = None
    for name in candidates:
        try:
            return importlib.import_module(name)
        except Exception as exc:  # pragma: no cover - tried next candidate
            last_exc = exc
            continue
    # Final fallback: load by file path.
    import importlib.util

    path = _this_dir() / f"{module_basename}.py"
    if path.exists():
        spec = importlib.util.spec_from_file_location(
            f"_oracle_migration_{module_basename}", path
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    raise ImportError(
        f"cannot import migration {module_basename!r}: {last_exc}"
    )


def load_migration(seq: int, module_basename: str) -> Migration:
    module = _import_module(module_basename)
    return Migration(seq=seq, name=module_basename, module=module)


def apply_all(root: Path, *, after_seq: int = 0) -> list[dict]:
    """Apply every migration with ``seq > after_seq`` in ascending order.

    Each migration is idempotent, so passing ``after_seq=0`` (the default) is the
    safe choice: already-applied migrations report ``changed=False`` and make no
    change. Returns the ordered list of per-migration report dicts.
    """
    root = Path(root)
    reports: list[dict] = []
    for seq, basename in discover():
        if seq <= after_seq:
            continue
        mig = load_migration(seq, basename)
        reports.append(mig.apply(root))
    return reports
