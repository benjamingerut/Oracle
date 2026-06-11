#!/usr/bin/env python3
"""connectors package -- the connector runtime registry + CLI entrypoint.

This package exposes the connector runtime that the unified ``oracle connector
<cmd>`` dispatcher routes to. It owns:

  * a small REGISTRY mapping a connector ``access_mode`` (and the reference id
    ``localfolder``) to a concrete Connector class/factory;
  * ``get_connector(root, id)`` -- load the manifest and instantiate the right
    connector;
  * ``main(argv)`` -- the CLI ``health [ID] | pull ID | probe ID | freshness ID``.

Pulls are guarded: ``pull`` acquires a read-only action grant through
``actions.py`` (when present) so a connector pull is logged as a scoped
autonomous action with blast-radius caps, and each file is classified +
policy-checked inside the connector itself. When ``actions.py`` is unavailable,
the runtime still runs the pull but records that the action gate was unavailable
-- the connector's own safe_paths + policy + containment guarantees remain in
force regardless.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Optional

try:  # flat layout (tests put _tools on sys.path)
    from connectors.base import (
        Connector,
        ConnectorContext,
        ConnectorError,
        load_manifest,
    )
    from connectors.localfolder import LocalFolderConnector
except Exception:  # pragma: no cover - package fallback
    from .base import (  # type: ignore
        Connector,
        ConnectorContext,
        ConnectorError,
        load_manifest,
    )
    from .localfolder import LocalFolderConnector  # type: ignore


def _import_policy():
    try:
        import policy  # type: ignore
        return policy
    except Exception:  # pragma: no cover - package fallback / optional
        try:
            from .. import policy  # type: ignore
            return policy
        except Exception:
            return None

__all__ = [
    "REGISTRY",
    "register",
    "get_connector",
    "get_connector_class",
    "main",
    "Connector",
    "ConnectorContext",
    "ConnectorError",
]


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
# NEW connectors register by ID ONLY (P7S-6): keying on ``access_mode`` would
# collide -- "api" across four connectors, "file_drop" across every future drop
# connector. Unknown-id resolution falls back to the manifest's required
# ``system`` field against SYSTEM_FACTORIES, so a second account
# (id: gdrive-finance, system: gdrive) resolves to the gdrive class, never to
# another "api" connector. The reference ``localfolder`` keeps its historical
# access_mode key so existing localfolder manifests resolve unchanged.
REGISTRY: dict[str, Callable[[dict], Connector]] = {
    "localfolder": LocalFolderConnector,
    "folder": LocalFolderConnector,  # legacy access_mode alias for the reference connector
}

# system -> factory map for the system-fallback resolution. Concrete remote
# connectors (T2-T6) register here under their ``system`` value.
SYSTEM_FACTORIES: dict[str, Callable[[dict], Connector]] = {}


def register(key: str, factory: Callable[[dict], Connector], *, system: Optional[str] = None) -> None:
    """Register a connector factory under ``key`` (the connector ID).

    Pass ``system=`` to ALSO register the factory for system-fallback resolution
    so a second account with a distinct id but the same ``system`` resolves to
    this class.
    """
    REGISTRY[str(key)] = factory
    if system:
        SYSTEM_FACTORIES[str(system)] = factory


def register_system(system: str, factory: Callable[[dict], Connector]) -> None:
    """Register a factory for system-fallback resolution only."""
    SYSTEM_FACTORIES[str(system)] = factory


def get_connector_class(manifest: dict) -> Callable[[dict], Connector]:
    """Resolve the connector factory: by id, then by ``system`` fallback.

    The reference connector also resolves by its legacy ``access_mode=folder``
    alias. New remote connectors never resolve by ``access_mode`` (P7S-6).
    """
    cid = str(manifest.get("id") or "")
    if cid in REGISTRY:
        return REGISTRY[cid]
    system = str(manifest.get("system") or "")
    if system in SYSTEM_FACTORIES:
        return SYSTEM_FACTORIES[system]
    # Legacy alias for the reference connector ONLY (folder access_mode).
    mode = str(manifest.get("access_mode") or "")
    if mode == "folder" and mode in REGISTRY:
        return REGISTRY[mode]
    raise ConnectorError(
        f"no connector implementation registered for id={cid!r} / "
        f"system={system!r}"
    )


def get_connector(root: Path, connector_id: str, *, validate: bool = True) -> Connector:
    """Load ``connector_id``'s manifest under ``root`` and instantiate it."""
    manifest = load_manifest(root, connector_id, validate=validate)
    factory = get_connector_class(manifest)
    return factory(manifest)


# --------------------------------------------------------------------------- #
# optional action-gate shim
# --------------------------------------------------------------------------- #
def _import_actions():
    """Import the optional actions module."""
    try:
        import actions  # type: ignore
        return actions
    except Exception:
        try:
            from .. import actions  # type: ignore
            return actions
        except Exception:
            return None


def _positive_int(value, default: int = 0) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


#: The canonical gated-pull loop id (P7S-20). Admin allowlist entries, the scope
#: declaration, and the scheduled-pull loop all use THIS id. It is deliberately
#: NOT a member of actions.DETERMINISTIC_LOOPS -- credentialed network egress is
#: not a level-1 deterministic loop; only an explicit allowed_loops entry admits
#: it.
CONNECTOR_PULL_LOOP = "connector-pull"

# Fail-closed byte ceiling used when a probe cannot price the plan (unknown
# never declares 0 -- declaring 0 would make every cap check pass spuriously;
# P7S-17). 100 MiB mirrors remote._DEFAULT_MAX_BYTES.
_FAIL_CLOSED_BYTES = 100 * 1024 * 1024


def _planned_pull_scope(connector: Connector, ctx: ConnectorContext) -> dict:
    """Declare the pull's intended blast radius for the autonomy gate.

    The canonical loop id is ``connector-pull`` -- the prior hardcoded
    ``connector-health`` made every admin allowlist entry for ``connector-pull``
    a permanent deny (P7S-20). Byte pricing is FAIL-CLOSED: when the probe
    cannot price the plan, the declared bytes are the cap itself, never 0
    (P7S-17). For a RemoteConnector the cap-derived scope (no probe network
    call) is delegated to remote.planned_pull_scope.
    """
    # A RemoteConnector authorizes from caps WITHOUT a probe network call
    # (authorize-before-network; P7S-18) -- delegate to its scope builder.
    try:
        from connectors.remote import RemoteConnector, planned_pull_scope as _remote_scope
    except Exception:  # pragma: no cover - package fallback
        try:
            from .remote import RemoteConnector, planned_pull_scope as _remote_scope  # type: ignore
        except Exception:
            RemoteConnector = None  # type: ignore
            _remote_scope = None  # type: ignore
    if RemoteConnector is not None and isinstance(connector, RemoteConnector):
        return _remote_scope(connector, ctx)

    source = ctx.manifest.get("source") or {}
    if not isinstance(source, dict):
        source = {}
    cap = _positive_int(ctx.max_files)
    if not cap:
        cap = _positive_int(source.get("max_files"), 500)

    probe: dict = {}
    try:
        got = connector.probe(ctx)
        probe = got if isinstance(got, dict) else {}
    except Exception:
        probe = {}
    items = _positive_int(probe.get("items"), cap)
    planned_files = min(items, cap) if cap else items

    total_bytes = _positive_int(probe.get("total_bytes"), 0)
    # Fail closed: if the probe could not price the bytes, declare the cap, not 0.
    if total_bytes > 0:
        planned_bytes = total_bytes
    else:
        cap_bytes = _positive_int(source.get("max_bytes"), 0)
        planned_bytes = cap_bytes if cap_bytes else _FAIL_CLOSED_BYTES

    return {
        "loop": CONNECTOR_PULL_LOOP,
        "connectors": [connector.id],
        "lanes": ["_INPUT"],
        "files": planned_files,
        "bytes": planned_bytes,
        "actor": ctx.actor,
        "role": ctx.role,
    }


def _guarded_pull(connector: Connector, ctx: ConnectorContext) -> tuple[list, dict]:
    """Run a connector pull, optionally under the autonomy action gate.

    Returns (results, meta). ``meta['action_gate']`` is one of:
      * 'applied'     -- the pull ran inside actions.with_action (gated path);
      * 'unavailable' -- gating requested but actions.py is not importable yet;
      * 'direct'      -- ungated direct pull (the default for an admin-invoked
                         CLI/manual pull).

    Gating is OPT-IN via ``ctx.gated`` because autonomy is OFF by default: the
    headless harness sets ``gated=True`` so a between-sessions pull is wrapped in
    the kill-switch/allowlist/blast-radius gate and logged as an action event,
    while a direct admin pull is not blocked by the OFF-by-default autonomy
    posture. The connector's own safe_paths + policy + containment guarantees
    hold in BOTH paths, so an ungated pull is never LESS safe at the byte level
    -- only un-logged as a scoped autonomous action.
    """
    _require_pull_role(ctx)
    if not ctx.gated:
        return connector.pull(ctx), {"action_gate": "direct"}

    actions = _import_actions()
    if actions is None or not hasattr(actions, "with_action"):
        return connector.pull(ctx), {"action_gate": "unavailable"}

    # The action scope carries the connector id, the writable lane (_INPUT), and
    # the planned blast radius so the autonomy gate can enforce allowlists and
    # caps before the pull writes any bytes.
    scope = _planned_pull_scope(connector, ctx)
    try:
        with actions.with_action("connector-pull", scope, root=ctx.root):
            results = connector.pull(ctx)
        return results, {"action_gate": "applied"}
    except Exception as exc:
        # A refusal from the action gate (kill-switch present, autonomy OFF,
        # not allowlisted, cap exceeded) propagates -- the pull did NOT run.
        raise ConnectorError(f"connector pull refused by action gate: {exc}") from exc


def _require_pull_role(ctx: ConnectorContext) -> None:
    role = (getattr(ctx, "role", "") or "").strip()
    if role in ("", "system"):
        return
    policy_mod = _import_policy()
    if policy_mod is None:
        raise ConnectorError("connector pull role gate unavailable: policy module not importable")
    try:
        policy_mod.require_role(
            getattr(ctx, "actor", "connector-runtime"),
            role,
            "provide_documents",
            root=ctx.root,
        )
    except PermissionError as exc:
        raise ConnectorError(f"connector pull role denied: {exc}") from exc


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _known_connector_ids(root: Path) -> list:
    """Discover connector ids from the Connectors/ folder (manifests present)."""
    cdir = Path(root) / "Connectors"
    if not cdir.is_dir():
        return []
    ids: list = []
    for sub in sorted(cdir.iterdir()):
        if sub.is_dir() and (sub / f"{sub.name}.manifest.yaml").exists():
            ids.append(sub.name)
    return ids


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="oracle connector",
        description="Connector runtime: health/pull/probe/freshness.",
    )
    parser.add_argument("--root", default=".", help="oracle root")
    parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of text"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_health = sub.add_parser("health", help="health for one connector or all")
    p_health.add_argument("id", nargs="?", help="connector id (omit for all)")

    p_pull = sub.add_parser("pull", help="pull new material into _INPUT")
    p_pull.add_argument("id", help="connector id")
    p_pull.add_argument("--dry-run", action="store_true", help="plan only; copy nothing")
    p_pull.add_argument("--actor", default="connector-cli")
    p_pull.add_argument("--role", default="user")
    p_pull.add_argument("--max-files", type=int, default=None)

    p_probe = sub.add_parser("probe", help="file-type histogram of the source")
    p_probe.add_argument("id", help="connector id")

    p_fresh = sub.add_parser("freshness", help="freshness verdict vs SLA")
    p_fresh.add_argument("id", help="connector id")

    args = parser.parse_args(argv)
    root = Path(args.root)

    def emit(obj) -> None:
        if args.json:
            print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
        else:
            print(_render(obj))

    try:
        if args.cmd == "health":
            if args.id:
                conn = get_connector(root, args.id)
                ctx = ConnectorContext(root, conn.manifest)
                report = conn.health(ctx)
                emit(report)
                return 0 if report.get("status") in ("healthy", "degraded") else 1
            # all connectors
            reports = []
            worst_ok = True
            for cid in _known_connector_ids(root):
                try:
                    conn = get_connector(root, cid)
                    ctx = ConnectorContext(root, conn.manifest)
                    rep = conn.health(ctx)
                except ConnectorError as exc:
                    rep = {"connector": cid, "status": "broken", "notes": [str(exc)]}
                if rep.get("status") == "broken":
                    worst_ok = False
                reports.append(rep)
            emit(reports)
            return 0 if worst_ok else 1

        conn = get_connector(root, args.id)

        if args.cmd == "pull":
            ctx = ConnectorContext(
                root,
                conn.manifest,
                actor=args.actor,
                role=args.role,
                max_files=args.max_files,
                dry_run=args.dry_run,
            )
            results, meta = _guarded_pull(conn, ctx)
            ingested = [r for r in results if r.get("action") == "ingested"]
            refused = [r for r in results if r.get("action") == "refused"]
            payload = {
                "connector": conn.id,
                "action_gate": meta.get("action_gate"),
                "ingested": len(ingested),
                "refused": len(refused),
                "results": results,
            }
            emit(payload)
            # Non-zero if any file was refused on containment grounds.
            return 0 if not refused else 1

        if args.cmd == "probe":
            ctx = ConnectorContext(root, conn.manifest, dry_run=True)
            emit(conn.probe(ctx))
            return 0

        if args.cmd == "freshness":
            ctx = ConnectorContext(root, conn.manifest, dry_run=True)
            report = conn.freshness(ctx)
            emit(report)
            return 0 if report.get("verdict") != "stale" else 1

    except ConnectorError as exc:
        print(f"CONNECTOR ERROR: {exc}", file=sys.stderr)
        return 2

    return 2


def _render(obj) -> str:
    """Tiny human renderer for the CLI text mode."""
    if isinstance(obj, list):
        return "\n".join(_render(o) for o in obj)
    if isinstance(obj, dict):
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False, default=str)
            lines.append(f"{k}: {v}")
        return "\n".join(lines)
    return str(obj)


if __name__ == "__main__":
    raise SystemExit(main())
