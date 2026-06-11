#!/usr/bin/env python3
"""connectors/localfolder.py -- the read-only reference connector.

``localfolder`` pulls files from a single admin-approved local source folder
into the oracle's intake lane (``Workproduct.nosync/_INPUT``). It is the
worked example every other connector copies, and it demonstrates the full
safety discipline the runtime contract demands:

  * the source path is read from the connector manifest's ``source.path`` and
    every file pulled MUST be CONTAINED within that configured root -- a file
    that escapes the root (via ``..`` or a symlink) is REFUSED, never copied;
  * intake sensitivity is classified at pull time (intake_classify when
    present, else a conservative built-in heuristic) and the per-file
    processing verdict is checked through ``policy.check_processing`` -- a file
    whose sensitivity is denied for local agent processing is SKIPPED, not
    ingested;
  * bytes land in ``_INPUT`` via ``safe_paths.contain`` +
    ``safe_paths.safe_copy_verify_delete`` from a verified staging copy, so the
    ORIGINAL source file is never destroyed and the destination can never
    escape the oracle root;
  * the pull is read-only: the connector copies FROM the source (leaving it in
    place) and is refused entirely if the manifest declares
    ``permissions: read_write``.

Health states (``healthy | degraded | broken``) report whether the source
folder exists/readable, whether the last pull is inside the freshness SLA, and
whether the file population is plausible -- wired to the connector-health loop.

Stdlib only. Optional siblings (intake_classify, actions) are imported
defensively so the connector still runs on the bare floor.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

try:  # flat layout (tests put _tools on sys.path)
    from connectors.base import Connector, ConnectorContext, ConnectorError
except Exception:  # pragma: no cover - package fallback
    from .base import Connector, ConnectorContext, ConnectorError  # type: ignore

__all__ = ["LocalFolderConnector", "ID"]

ID = "localfolder"

# Intake lane within Workproduct.nosync. contain() defaults base to
# "Workproduct.nosync"; we land pulled files under _INPUT/<connector-id>/.
_INTAKE_BASE = "Workproduct.nosync"
_INTAKE_PREFIX = "_INPUT"

# A sane ceiling so a misconfigured pull cannot copy an unbounded tree in one
# run. Overridable via manifest source.max_files or ctx.max_files.
_DEFAULT_MAX_FILES = 500
# Skip obviously-non-document noise even before classification.
_SKIP_NAMES = {".DS_Store", "Thumbs.db", ".gitkeep"}


# --------------------------------------------------------------------------- #
# sibling-import shims (optional; degrade gracefully)
# --------------------------------------------------------------------------- #
def _import_safe_paths():
    try:
        import safe_paths  # type: ignore
        return safe_paths
    except Exception:  # pragma: no cover - package fallback
        from .. import safe_paths  # type: ignore
        return safe_paths


def _import_policy():
    try:
        import policy  # type: ignore
        return policy
    except Exception:  # pragma: no cover - package fallback
        try:
            from .. import policy  # type: ignore
            return policy
        except Exception:
            return None


def _import_intake_classify():
    """intake_classify is a P3 sibling that may still be building. Optional."""
    try:
        import intake_classify  # type: ignore
        return intake_classify
    except Exception:
        try:
            from .. import intake_classify  # type: ignore
            return intake_classify
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# built-in fallback sensitivity classifier (used when intake_classify absent)
# --------------------------------------------------------------------------- #
# Filename/path keyword -> sensitivity label. Stricter-row-wins: the most
# sensitive matching keyword decides. This is intentionally conservative and is
# ONLY the fallback; intake_classify (when present) is authoritative.
_SENS_ORDER = ["public", "internal", "confidential", "restricted", "secret"]
_KEYWORD_SENS = {
    "secret": ["secret", "credential", "password", "private-key", "id_rsa", ".pem", ".key"],
    "restricted": ["ssn", "salary", "payroll", "medical", "phi", "pii", "passport"],
    "confidential": [
        "confidential", "contract", "nda", "financial", "finance", "bank",
        "invoice", "cap-table", "board", "legal",
    ],
    "internal": ["internal", "draft", "memo", "notes", "ops", "roadmap"],
}


def _fallback_classify(path: Path, connector_default: str = "internal") -> str:
    """Conservative built-in classifier; stricter keyword wins, else default."""
    name = path.name.lower()
    full = str(path).lower()
    best_rank = _SENS_ORDER.index(connector_default) if connector_default in _SENS_ORDER else 1
    for label, keywords in _KEYWORD_SENS.items():
        if any(k in name or k in full for k in keywords):
            rank = _SENS_ORDER.index(label)
            if rank > best_rank:
                best_rank = rank
    return _SENS_ORDER[best_rank]


def _classify(path: Path, connector_default: str, root: Path) -> str:
    """Classify a file's intake sensitivity, preferring intake_classify.

    Uses ``intake_classify.classify_file(path, connector_default=floor)`` -- the
    real signature (P7S-16). The earlier ``classify(path, …)`` call raised a
    TypeError that was silently swallowed, degrading every pull to the
    filename-keyword fallback; the content-signal classifier is now actually
    reached, with the connector default as the FLOOR.
    """
    ic = _import_intake_classify()
    if ic is not None and hasattr(ic, "classify_file"):
        try:
            result = ic.classify_file(path, connector_default=connector_default)
            # classify_file returns a dict carrying the final label.
            if isinstance(result, dict):
                label = result.get("label") or result.get("sensitivity")
            else:
                label = result
            if label in _SENS_ORDER:
                return label
        except Exception:
            pass  # fall back below
    return _fallback_classify(path, connector_default)


# --------------------------------------------------------------------------- #
# the connector
# --------------------------------------------------------------------------- #
class LocalFolderConnector(Connector):
    """Read-only reference connector pulling from a configured local folder."""

    access_mode = "folder"

    # -- helpers -------------------------------------------------------------
    def _source_block(self, ctx: ConnectorContext) -> dict:
        src = ctx.manifest.get("source") or self.manifest.get("source") or {}
        return src if isinstance(src, dict) else {}

    def _source_root(self, ctx: ConnectorContext) -> Path:
        """Resolved, real source root the pull is confined to.

        Read from manifest ``source.path``. The path is realpath-resolved once
        here; every candidate file is then checked to live WITHIN this real
        root, closing both the ``..`` traversal and the symlink-escape vectors.
        """
        block = self._source_block(ctx)
        raw = block.get("path")
        if not raw or not str(raw).strip():
            raise ConnectorError(
                f"{self.id}: manifest source.path is required (the folder to pull from)"
            )
        root = Path(os.path.realpath(os.path.expanduser(str(raw))))
        return root

    def _connector_default_sensitivity(self, ctx: ConnectorContext) -> str:
        if ctx.sensitivity_override in _SENS_ORDER:
            return ctx.sensitivity_override
        block = self._source_block(ctx)
        cand = block.get("default_sensitivity")
        return cand if cand in _SENS_ORDER else "internal"

    def _max_files(self, ctx: ConnectorContext) -> int:
        if isinstance(ctx.max_files, int) and ctx.max_files > 0:
            return ctx.max_files
        block = self._source_block(ctx)
        cand = block.get("max_files")
        if isinstance(cand, int) and cand > 0:
            return cand
        return _DEFAULT_MAX_FILES

    def _assert_read_only(self) -> None:
        perms = str(self.manifest.get("permissions") or "unknown")
        if perms == "read_write":
            raise ConnectorError(
                f"{self.id}: refusing to run a read_write connector as a "
                f"read-only pull (manifest permissions=read_write)"
            )

    def _candidate_files(self, source_root: Path) -> list:
        """Every regular file under the source root, sorted for determinism.

        Symlinked files are excluded here (defense in depth); the per-file
        containment check below is the binding guarantee.
        """
        out: list = []
        if not source_root.exists() or not source_root.is_dir():
            return out
        for dirpath, dirnames, filenames in os.walk(source_root, followlinks=False):
            # Do not descend into symlinked directories.
            dirnames[:] = [d for d in dirnames if not Path(dirpath, d).is_symlink()]
            for fn in sorted(filenames):
                if fn in _SKIP_NAMES:
                    continue
                p = Path(dirpath) / fn
                if p.is_symlink():
                    continue
                if p.is_file():
                    out.append(p)
        out.sort()
        return out

    def _within_source(self, source_root: Path, candidate: Path) -> bool:
        """True iff the realpath of ``candidate`` lies under ``source_root``.

        This is the connector's own containment guarantee for the SOURCE side
        (safe_paths.contain guards the DESTINATION side). We reuse
        safe_paths.is_within when available, else compute commonpath directly.
        """
        sp = _import_safe_paths()
        try:
            if sp is not None and hasattr(sp, "is_within"):
                return bool(sp.is_within(source_root, candidate))
        except Exception:
            pass
        try:  # pragma: no cover - only when safe_paths missing
            real = Path(os.path.realpath(candidate))
            common = os.path.commonpath([str(real), str(source_root)])
            return common == str(source_root)
        except (ValueError, OSError):
            return False

    # -- runtime contract ----------------------------------------------------
    def pull(self, ctx: ConnectorContext) -> list:
        """Copy new files from the configured source folder into _INPUT.

        For each candidate file, in order:
          1. confirm it is CONTAINED within the configured source root
             (reject traversal / symlink escapes) -- else record a refusal;
          2. classify its intake sensitivity;
          3. check the processing verdict for local_agent; SKIP if denied;
          4. compute a CONTAINED destination under _INPUT via safe_paths;
          5. stage a copy to a temp file, then move it into _INPUT through
             safe_copy_verify_delete (copy -> fsync -> sha256-verify -> delete
             the STAGE, leaving the ORIGINAL source intact).

        Returns a list of per-file result dicts. ``dry_run`` performs steps 1-4
        and reports the plan without copying bytes.
        """
        self._assert_read_only()
        sp = _import_safe_paths()
        if sp is None:  # pragma: no cover - floor always ships safe_paths
            raise ConnectorError(f"{self.id}: safe_paths is required to pull")
        policy = _import_policy()

        source_root = self._source_root(ctx)
        default_sens = self._connector_default_sensitivity(ctx)
        cap = self._max_files(ctx)

        results: list = []
        ingested = 0
        for candidate in self._candidate_files(source_root):
            if ingested >= cap:
                results.append({"action": "skipped", "reason": "max_files cap reached", "src": str(candidate)})
                continue

            # (1) source-side containment.
            if not self._within_source(source_root, candidate):
                results.append({
                    "action": "refused",
                    "reason": "path escapes configured source root",
                    "src": str(candidate),
                })
                continue

            # (2) classify sensitivity.
            sensitivity = _classify(candidate, default_sens, ctx.root)

            # (3) processing-gate for local agent handling.
            if policy is not None:
                try:
                    verdict = policy.check_processing(sensitivity, "local_agent")
                except Exception:
                    verdict = "deny"
                if verdict == "deny":
                    results.append({
                        "action": "skipped",
                        "reason": f"processing denied for sensitivity={sensitivity}",
                        "src": str(candidate),
                        "sensitivity": sensitivity,
                    })
                    continue

            # (4) contained destination under _INPUT/<id>/<date>_<slug><suffix>.
            try:
                dest = self._dest_for(sp, ctx.root, candidate)
            except ValueError as exc:
                results.append({
                    "action": "refused",
                    "reason": f"destination containment refused: {exc}",
                    "src": str(candidate),
                })
                continue

            if ctx.dry_run:
                results.append({
                    "action": "planned",
                    "src": str(candidate),
                    "dst": str(dest),
                    "sensitivity": sensitivity,
                })
                ingested += 1
                continue

            # (5) stage + non-destructive move into _INPUT.
            try:
                sha = self._copy_in(sp, candidate, dest)
            except Exception as exc:
                results.append({
                    "action": "failed",
                    "reason": str(exc),
                    "src": str(candidate),
                    "dst": str(dest),
                })
                continue

            results.append({
                "action": "ingested",
                "src": str(candidate),
                "dst": str(dest),
                "sensitivity": sensitivity,
                "sha256_12": sha,
                "connector": self.id,
            })
            ingested += 1

        return results

    def _dest_for(self, sp, root: Path, candidate: Path) -> Path:
        """Compute a contained destination Path under _INPUT for ``candidate``.

        Filename is ``YYYY-MM-DD_<slug><suffix>`` with the suffix preserved from
        the validated source. The candidate string is built from SAFE pieces and
        passed through safe_paths.contain so the result provably lives under
        ``root/Workproduct.nosync/_INPUT/<id>/``.
        """
        suffix = candidate.suffix  # includes leading dot, e.g. ".csv"
        stem = candidate.stem or "file"
        slug = sp.safe_slug(stem)
        date_prefix = sp.today() if hasattr(sp, "today") else datetime.now().strftime("%Y-%m-%d")
        # Validate the suffix conservatively: only [a-z0-9] after the dot.
        clean_suffix = ""
        if suffix:
            tail = suffix.lstrip(".").lower()
            if tail and tail.isalnum():
                clean_suffix = "." + tail
        filename = f"{date_prefix}_{slug}{clean_suffix}"
        candidate_rel = f"{_INTAKE_PREFIX}/{self.id}/{filename}"
        # contain() default base is Workproduct.nosync.
        return sp.contain(root, candidate_rel, base=_INTAKE_BASE)

    def _copy_in(self, sp, candidate: Path, dest: Path) -> str:
        """Non-destructively land ``candidate`` at ``dest`` without destroying
        the source.

        safe_copy_verify_delete DELETES its source after a verified copy. Since
        a connector pull must LEAVE the original source folder intact, we first
        copy the source to a temp STAGE file, then hand the STAGE to
        safe_copy_verify_delete (which consumes the stage, not the original).
        All write I/O goes through safe_paths / a fixed temp path -- never a raw
        write on a user-influenced destination.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Stage in a private temp dir; shutil.copy2 here targets a temp path we
        # control, not a user-influenced destination, but we still route the
        # final landing through safe_copy_verify_delete.
        stage_dir = Path(tempfile.mkdtemp(prefix="oracle-localfolder-"))
        stage = stage_dir / candidate.name
        try:
            # Copy source -> stage (read source, write our own temp). This is the
            # ONE copy this module performs; it is to a private temp path, and is
            # marked for the no-bypass guard. The original source is untouched.
            shutil.copy2(str(candidate), str(stage))  # safe_paths-internal: stage to private temp
            sha = sp.safe_copy_verify_delete(stage, dest)
            return sha
        finally:
            # Clean up the staging directory (stage file is consumed on success).
            try:
                if stage.exists():
                    stage.unlink()
            except OSError:
                pass
            try:
                stage_dir.rmdir()
            except OSError:
                pass

    def probe(self, ctx: ConnectorContext) -> dict:
        """File-type histogram of the configured source folder (cheap read)."""
        try:
            source_root = self._source_root(ctx)
        except ConnectorError as exc:
            return {"connector": self.id, "items": 0, "by_suffix": {}, "error": str(exc)}
        files = self._candidate_files(source_root)
        hist: Counter = Counter()
        total_bytes = 0
        for p in files:
            hist[(p.suffix.lower() or "<none>")] += 1
            try:
                total_bytes += p.stat().st_size
            except OSError:
                pass
        return {
            "connector": self.id,
            "source": str(source_root),
            "items": len(files),
            "total_bytes": total_bytes,
            "by_suffix": dict(sorted(hist.items())),
        }

    def freshness(self, ctx: ConnectorContext) -> dict:
        """Freshness verdict vs the manifest SLA (uses the base SLA math)."""
        return self.freshness_from_manifest(ctx)

    def health(self, ctx: ConnectorContext) -> dict:
        """Report healthy | degraded | broken for the configured folder.

        broken   -> source.path missing, or the folder does not exist / is not
                    readable, or the connector is mis-declared read_write.
        degraded -> source readable but past its freshness SLA, OR empty.
        healthy  -> source readable, populated, and within (or unknown) SLA.
        """
        notes: list = []
        # broken: read_write misuse.
        try:
            self._assert_read_only()
        except ConnectorError as exc:
            return self.health_envelope("broken", notes=[str(exc)])
        # broken: missing/invalid source path.
        try:
            source_root = self._source_root(ctx)
        except ConnectorError as exc:
            return self.health_envelope("broken", notes=[str(exc)])
        if not source_root.exists() or not source_root.is_dir():
            return self.health_envelope(
                "broken",
                notes=[f"source folder does not exist or is not a directory: {source_root}"],
            )
        if not os.access(source_root, os.R_OK):
            return self.health_envelope(
                "broken", notes=[f"source folder is not readable: {source_root}"]
            )

        probe = self.probe(ctx)
        fresh = self.freshness(ctx)
        state = "healthy"
        if probe.get("items", 0) == 0:
            state = "degraded"
            notes.append("source folder is empty (no files to pull)")
        if fresh.get("verdict") == "stale":
            state = "degraded"
            notes.append("source is past its freshness SLA")
        elif fresh.get("verdict") == "unknown":
            notes.append("freshness unknown (no last_verified or decay budget)")
        return self.health_envelope(state, notes=notes, probe=probe, freshness=fresh)


def build(manifest: dict) -> LocalFolderConnector:
    """Factory used by the connector registry."""
    return LocalFolderConnector(manifest)
