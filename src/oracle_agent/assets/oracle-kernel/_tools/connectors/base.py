#!/usr/bin/env python3
"""connectors/base.py -- the connector base contract + manifest loader.

A connector is the runtime adapter that brings external material into the
oracle. This module defines the binding runtime contract every connector
honours and the loader that reads a connector manifest (the collapsed
"connector manifest field set") through the safe-subset YAML loader.

Runtime contract (interface_contracts: "connector manifest field set"):

    Connector.pull(ctx)      -> list[dict]   # ingested records (one per file)
    Connector.probe(ctx)     -> dict         # cheap read: file-type histogram etc.
    Connector.freshness(ctx) -> dict         # verdict vs the manifest SLA
    Connector.health(ctx)    -> dict         # healthy | degraded | broken

``ctx`` is a small immutable bundle (the oracle root + the loaded manifest +
optional actor/role/limits). Concrete connectors subclass ``Connector`` and
implement ``pull`` / ``probe`` / ``freshness`` / ``health``; the base supplies
SLA/freshness math, a health-state vocabulary, and shared helpers so every
connector reports the same shapes.

Manifests live at ``Connectors/<id>/<id>.manifest.yaml`` and MUST stay within
the strict oracle_yaml subset (block style; no flow ``{}``/``[]``; empty values
written as a bare ``key:``). The loader validates the manifest against
``schemas/connector.schema.json`` when schema_check is importable, but never
hard-requires it, so a connector can load on the bare floor.

Stdlib only. Floor siblings (oracle_yaml, schema_check, safe_paths) are imported
defensively so this works flat (tests inject ``_tools`` on sys.path) OR as a
package, and degrades gracefully when an optional sibling is absent.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "ConnectorError",
    "ConnectorContext",
    "Connector",
    "load_manifest",
    "validate_manifest",
    "manifest_path_for",
    "HEALTH_STATES",
    "FRESHNESS_VERDICTS",
]


# Health-state vocabulary (mirrors connector-manifests doctrine).
HEALTH_STATES = ("healthy", "degraded", "broken", "unknown", "not_configured")
# Freshness verdicts (aligns with the answer-protocol freshness vocabulary).
FRESHNESS_VERDICTS = ("fresh", "stale", "unknown")


class ConnectorError(Exception):
    """A connector-runtime error (bad manifest, refused pull, broken health)."""


# --------------------------------------------------------------------------- #
# sibling-import shims (work flat OR as a package)
# --------------------------------------------------------------------------- #
def _import_oracle_yaml():
    try:
        import oracle_yaml  # type: ignore
        return oracle_yaml
    except Exception:  # pragma: no cover - package fallback
        from .. import oracle_yaml  # type: ignore
        return oracle_yaml


def _import_schema_check():
    try:
        import schema_check  # type: ignore
        return schema_check
    except Exception:  # pragma: no cover - optional / package fallback
        try:
            from .. import schema_check  # type: ignore
            return schema_check
        except Exception:
            return None


def _import_safe_paths():
    try:
        import safe_paths  # type: ignore
        return safe_paths
    except Exception:  # pragma: no cover - package fallback
        from .. import safe_paths  # type: ignore
        return safe_paths


def _tools_dir() -> Path:
    """Directory holding the kernel tool modules (parent of this package)."""
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# manifest loading + validation
# --------------------------------------------------------------------------- #
def manifest_path_for(root: Path, connector_id: str) -> Path:
    """Canonical manifest path for a connector id under an oracle root."""
    return Path(root) / "Connectors" / connector_id / f"{connector_id}.manifest.yaml"


def _connector_schema() -> Optional[dict]:
    schema_file = _tools_dir() / "schemas" / "connector.schema.json"
    if not schema_file.exists():
        return None
    try:
        return json.loads(schema_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):  # pragma: no cover - defensive
        return None


def _strip_empty_optionals(manifest: dict, schema: dict) -> dict:
    """Recursively drop OPTIONAL keys whose value is ``None``.

    The oracle_yaml subset renders an intentionally-empty block field (e.g.
    ``forbidden_uses:``, ``biases:``, ``schema_refresh.cadence:``) as ``None``.
    The connector field set lists those optional list/string fields for
    documentation, and a fresh manifest legitimately leaves them empty.
    schema_check types them (array/string), so a ``None`` would spuriously fail.
    We therefore treat a ``None``-valued OPTIONAL key as ABSENT before
    validating, recursing into nested ``object`` sub-schemas; a key listed in
    the schema's ``required`` at its level is never stripped, so a missing/empty
    required field still fails.
    """
    if not isinstance(manifest, dict) or not isinstance(schema, dict):
        return manifest
    required = set(schema.get("required", []) or [])
    props = schema.get("properties") or {}
    out = {}
    for k, v in manifest.items():
        if v is None and k not in required:
            continue
        subschema = props.get(k)
        if isinstance(v, dict) and isinstance(subschema, dict):
            v = _strip_empty_optionals(v, subschema)
        out[k] = v
    return out


def validate_manifest(manifest: dict) -> list[str]:
    """Return schema-validation errors for a manifest (empty list == valid).

    Uses the floor ``schema_check`` validator against
    ``schemas/connector.schema.json``. If either is unavailable this returns an
    empty list (the connector still loads on a bare floor); callers that need a
    hard guarantee run the full ``oracle_lint`` gate instead.

    Empty optional block fields (``None``) are treated as absent so a fresh
    manifest with bare ``key:`` placeholders validates cleanly.
    """
    if not isinstance(manifest, dict):
        return ["manifest is not a mapping"]
    schema = _connector_schema()
    sc = _import_schema_check()
    if schema is None or sc is None:
        return []
    try:
        cleaned = _strip_empty_optionals(manifest, schema)
        return list(sc.validate(cleaned, schema))
    except Exception as exc:  # pragma: no cover - defensive
        return [f"schema validation raised: {exc}"]


def load_manifest(root: Path, connector_id: str, *, validate: bool = True) -> dict:
    """Load + parse a connector manifest, optionally validating it.

    Reads ``Connectors/<id>/<id>.manifest.yaml`` via the safe-subset YAML
    loader (so any anchor/alias/tag/flow/multi-doc construct RAISES rather than
    mis-parses). Confirms the manifest's own ``id`` matches the requested id.
    When ``validate`` is True and schema_check is present, raises
    ConnectorError on any schema violation.
    """
    path = manifest_path_for(root, connector_id)
    if not path.exists():
        raise ConnectorError(f"no manifest for connector {connector_id!r} at {path}")
    yaml_mod = _import_oracle_yaml()
    try:
        data = yaml_mod.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConnectorError(f"manifest {path} is not valid oracle-YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConnectorError(f"manifest {path} did not parse to a mapping")
    declared = str(data.get("id") or "")
    if declared and declared != connector_id:
        raise ConnectorError(
            f"manifest id {declared!r} does not match requested id {connector_id!r}"
        )
    if validate:
        errors = validate_manifest(data)
        if errors:
            raise ConnectorError(
                f"manifest {path} failed schema validation:\n  - "
                + "\n  - ".join(errors)
            )
    return data


# --------------------------------------------------------------------------- #
# runtime context
# --------------------------------------------------------------------------- #
class ConnectorContext:
    """Immutable-ish bundle handed to every connector method.

    Carries the oracle ``root``, the loaded ``manifest``, the acting
    ``actor``/``role`` (advisory-plus-logged, per GOVERNANCE.md), an optional
    ``max_files`` blast-radius cap for a single pull, a ``now`` timestamp used
    by freshness math (overridable for deterministic tests), and a ``dry_run``
    flag that lets ``probe``/``freshness``/``health`` run without copying bytes.
    """

    def __init__(
        self,
        root: Path,
        manifest: dict,
        *,
        actor: str = "connector-runtime",
        role: str = "user",
        max_files: Optional[int] = None,
        now: Optional[datetime] = None,
        dry_run: bool = False,
        sensitivity_override: Optional[str] = None,
        gated: bool = False,
    ) -> None:
        self.root = Path(root)
        self.manifest = dict(manifest)
        self.actor = actor or "connector-runtime"
        self.role = role or "user"
        self.max_files = max_files
        self.now = now or datetime.now()
        self.dry_run = bool(dry_run)
        self.sensitivity_override = sensitivity_override
        # When True, a pull is wrapped in the actions.py autonomy gate (used by
        # the headless harness path). A direct admin-invoked pull defaults to
        # ungated -- autonomy is OFF by default and must not block manual
        # operation -- but the connector's safe_paths + policy + containment
        # guarantees hold either way.
        self.gated = bool(gated)

    @property
    def connector_id(self) -> str:
        return str(self.manifest.get("id") or "")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"ConnectorContext(id={self.connector_id!r}, actor={self.actor!r}, "
            f"role={self.role!r}, dry_run={self.dry_run})"
        )


# --------------------------------------------------------------------------- #
# the base connector
# --------------------------------------------------------------------------- #
class Connector:
    """Base connector. Concrete connectors subclass and implement the runtime
    contract methods. The base supplies shared freshness/health helpers so every
    connector reports the same shapes.
    """

    #: Subclasses set this to the access_mode they implement (e.g. "folder").
    access_mode: str = "manual"

    def __init__(self, manifest: dict) -> None:
        if not isinstance(manifest, dict):
            raise ConnectorError("Connector requires a manifest mapping")
        self.manifest = dict(manifest)
        self.id = str(manifest.get("id") or "")
        if not self.id:
            raise ConnectorError("manifest has no id")

    # -- runtime contract (subclasses override pull/probe/freshness/health) -- #
    def pull(self, ctx: "ConnectorContext") -> list:
        """Bring new external material into the oracle. Returns a list of
        ingested-record dicts (one per file). The base refuses -- a connector
        with no pull implementation cannot ingest.
        """
        raise NotImplementedError(f"connector {self.id!r} does not implement pull()")

    def probe(self, ctx: "ConnectorContext") -> dict:
        """Cheap, non-destructive read describing what is available. Default
        returns an empty-but-well-formed probe."""
        return {"connector": self.id, "items": 0, "by_suffix": {}}

    def freshness(self, ctx: "ConnectorContext") -> dict:
        """Default freshness verdict from the manifest SLA + last_verified."""
        return self.freshness_from_manifest(ctx)

    def health(self, ctx: "ConnectorContext") -> dict:
        """Default health: derive a coarse state from probe + freshness."""
        probe = self.probe(ctx)
        fresh = self.freshness(ctx)
        state = "healthy"
        notes: list[str] = []
        if fresh.get("verdict") == "stale":
            state = "degraded"
            notes.append("source is past its freshness SLA")
        elif fresh.get("verdict") == "unknown":
            notes.append("freshness unknown (no last_verified or decay budget)")
        return self.health_envelope(state, notes=notes, probe=probe, freshness=fresh)

    # -- shared helpers ----------------------------------------------------- #
    def freshness_from_manifest(self, ctx: "ConnectorContext") -> dict:
        """Compute a freshness verdict from manifest ``freshness`` block.

        Uses ``freshness.last_verified`` (ISO date/datetime) and
        ``freshness.expected_decay_days``. Verdict is:
          * 'fresh'   if age_days <= expected_decay_days,
          * 'stale'   if age_days >  expected_decay_days,
          * 'unknown' if either input is missing/unparseable.
        """
        fblock = ctx.manifest.get("freshness") or self.manifest.get("freshness") or {}
        fblock = fblock if isinstance(fblock, dict) else {}
        last_verified = fblock.get("last_verified")
        decay = fblock.get("expected_decay_days")
        fclass = fblock.get("class")

        verdict = "unknown"
        age_days: Optional[float] = None
        last_dt = _parse_iso(last_verified)
        if last_dt is not None:
            age_days = max(0.0, (_naive(ctx.now) - _naive(last_dt)).total_seconds() / 86400.0)
        if age_days is not None and isinstance(decay, int):
            verdict = "fresh" if age_days <= decay else "stale"
        return {
            "connector": self.id,
            "class": fclass,
            "verdict": verdict,
            "age_days": round(age_days, 2) if age_days is not None else None,
            "expected_decay_days": decay if isinstance(decay, int) else None,
            "last_verified": last_verified,
        }

    def health_envelope(
        self,
        state: str,
        *,
        notes: Optional[list] = None,
        probe: Optional[dict] = None,
        freshness: Optional[dict] = None,
    ) -> dict:
        """Standard health dict every connector returns."""
        if state not in HEALTH_STATES:
            state = "unknown"
        return {
            "connector": self.id,
            "status": state,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "notes": list(notes or []),
            "probe": probe or {},
            "freshness": freshness or {},
        }


# --------------------------------------------------------------------------- #
# small datetime helpers (no third-party deps)
# --------------------------------------------------------------------------- #
def _parse_iso(value: Any) -> Optional[datetime]:
    """Best-effort parse of an ISO-8601 date or datetime; None on failure."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Accept a trailing Z as UTC.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    for parser in (datetime.fromisoformat,):
        try:
            return parser(s)
        except ValueError:
            pass
    # Date-only fallback.
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _naive(dt: datetime) -> datetime:
    """Drop tzinfo so naive/aware datetimes can be subtracted consistently."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
