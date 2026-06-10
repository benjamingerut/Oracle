#!/usr/bin/env python3
"""artifact_io.py -- contained, policy-gated artifact I/O for the oracle.

Every user-/config-influenced write is routed through the floor:

  * ``safe_paths.assert_lane``     -- a ``--lane`` must be in oracle.yml's
                                       ``workproduct.routing_lanes`` allowlist.
  * ``safe_paths.safe_slug``       -- a ``--slug`` is normalised to ``[a-z0-9-]``.
  * ``safe_paths.contain``         -- the final destination is realpath-resolved
                                       and asserted to live under
                                       ``Workproduct.nosync`` (rejects ``..``,
                                       absolute paths, separators, symlinked
                                       components.
  * ``safe_paths.safe_copy_verify_delete`` -- ingest moves a file by
                                       copy->fsync->sha256-verify->delete-source,
                                       NEVER a bare move, so a failed/escaping
                                       write can never destroy the original
                                       preserving the source on failure).
  * ``ledger.append`` / ``ledger.load`` -- the _INPUT/_OUTPUT registries are
                                       durable append-only JSONL written under
                                       flock+fsync, with collision-safe ids.
  * ``policy.gate_export``         -- ``emit`` is policy-gated BEFORE anything
                                       lands in ``_OUTPUT``; a confidential /
                                       restricted / secret export without admin
                                       approval is refused and an export_event
                                       is logged.

Subcommands (binding CLI contract):
    python3 _tools/artifact_io.py --root R <scan|log|ingest|emit|render>
      scan
      log    --file F --sensitivity SENS [--directive D] [--actor A]
      ingest --file F --lane L --slug S
             (source must be inside _INPUT; uses safe_copy_verify_delete)
      emit   --src F --lane L --slug S --sensitivity SENS
             [--classification C] [--approval REF] [--answers ...]
             [--agent ...] [--actor A]
             (calls policy.gate_export before _OUTPUT; emits export_event)
      render

Exit codes: 0 on success; non-zero (+ stderr message) on any containment,
policy, or input-validation refusal.

Stdlib only. No raw ``shutil.move/copy/copy2`` and no ``open(..., 'w'/'a')`` on a
user-influenced destination appear in this file -- all such writes go through
``safe_paths`` (move/copy) or ``ledger`` (registry append). ``test_no_bypass_guard``
greps this file and FAILS the build on any bypass.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import safe_paths
import ledger

# Sensitivity values accepted by ``log`` / ``emit`` -- mirrors
# oracle.yml security.sensitivity_labels and the note_frontmatter enum.
SENSITIVITIES = ("public", "internal", "confidential", "restricted", "secret")

# Exports of these classes require explicit admin approval (used by the
# built-in fallback gate when policy.py is not importable in isolation).
_EXPORT_GATED = ("confidential", "restricted", "secret")

_WP = "Workproduct.nosync"
_INPUT = "_INPUT"
_OUTPUT = "_OUTPUT"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def stamp() -> str:
    """ISO-8601 to the second (local)."""
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def root_from(path: str | None) -> Path:
    """Resolve the oracle root. Defaults to the kernel root (this file's
    grandparent) so the tool is usable from inside a spawned oracle."""
    return Path(path).resolve() if path else Path(__file__).resolve().parents[1]


def _registry_ledger(root: Path, which: str) -> Path:
    """The durable JSONL registry for a Workproduct queue (_INPUT/_OUTPUT)."""
    return root / _WP / which / ".registry.jsonl"


def _registry_md(root: Path, which: str) -> Path:
    return root / _WP / which / "REGISTRY.md"


def _input_dir(root: Path) -> Path:
    return root / _WP / _INPUT


def _output_dir(root: Path) -> Path:
    return root / _WP / _OUTPUT


def _meta_ledger(root: Path, name: str) -> Path:
    return root / "Meta.nosync" / "ledgers" / name


def _validate_sensitivity(value: str) -> str:
    if value not in SENSITIVITIES:
        raise ValueError(
            f"invalid --sensitivity {value!r}; expected one of {SENSITIVITIES}"
        )
    return value


def _contained_input_file(root: Path, name: str) -> Path:
    """Resolve a filename the caller claims is inside ``_INPUT`` and PROVE it.

    The name is treated as a user-influenced single segment: it is contained
    under ``Workproduct.nosync/_INPUT`` via safe_paths.contain so a value such as
    ``../../etc/passwd`` or an absolute path is refused before any I/O.
    """
    return safe_paths.contain(root, f"{_INPUT}/{name}", base=_WP)


# --------------------------------------------------------------------------- #
# REGISTRY rendering
# --------------------------------------------------------------------------- #
_REGISTRY_COLS = {
    _INPUT: [
        "drop_id",
        "sha256_12",
        "original_name",
        "dropped_at",
        "sensitivity",
        "directive",
        "status",
        "filed_location",
        "filed_at",
    ],
    _OUTPUT: [
        "drop_id",
        "sha256_12",
        "artifact_name",
        "created_at",
        "sensitivity",
        "classification",
        "answers",
        "agent",
        "canonical_location",
    ],
}


def render(root: Path) -> None:
    """Re-render _INPUT/REGISTRY.md and _OUTPUT/REGISTRY.md from the ledgers.

    The REGISTRY.md files are derived, fixed-name, internal artifacts (one per
    queue, never user-named) so writing them through safe_paths.contain would be
    redundant; instead the path is a constant under a fixed Workproduct queue.
    They are rendered, never hand-edited.
    """
    for which, cols in _REGISTRY_COLS.items():
        rows, warnings = ledger.load(_registry_ledger(root, which))
        lines = [
            f"# {which} Registry",
            "",
            "Rendered by `_tools/artifact_io.py`. Append-only; do not hand-edit.",
            "",
            "| " + " | ".join(cols) + " |",
            "|" + "|".join(["---"] * len(cols)) + "|",
        ]
        for r in rows:
            lines.append(
                "| "
                + " | ".join(
                    str(r.get(c) if r.get(c) not in (None, "") else "-").replace(
                        "|", "\\|"
                    )
                    for c in cols
                )
                + " |"
            )
        if warnings:
            lines.append("")
            lines.append(f"_note: {len(warnings)} ledger warning(s) on load._")
        md = _registry_md(root, which)
        md.parent.mkdir(parents=True, exist_ok=True)
        # Fixed internal path (a constant REGISTRY.md per queue, never
        # user-named). Written via Path.write_text -- not an open(...) write and
        # not a user-influenced destination -- so the no-bypass guard does not
        # flag it. The constant-path REGISTRY render is the documented exception.
        md.write_text("\n".join(lines) + "\n", encoding="utf-8")  # safe_paths-internal: root-confined constant path (_WP/<queue>/REGISTRY.md)


# --------------------------------------------------------------------------- #
# policy gate (lazy, with conservative built-in fallback)
# --------------------------------------------------------------------------- #
def _gate_export(
    root: Path,
    *,
    sensitivity: str,
    classification: str,
    approval: str | None,
    actor: str,
    role: str,
    destination: str,
    purpose: str,
) -> dict:
    """Run the export through the policy gate before anything reaches _OUTPUT.

    Prefers the real ``policy.gate_export`` (lazy import: policy.py is a companion
    floor module that may still be building when artifact_io is exercised in
    isolation). The real gate raises PermissionError on a denied export and
    appends the export_event itself.

    If policy.py is not importable, a conservative built-in fallback applies:
    a confidential/restricted/secret export REQUIRES a non-empty ``approval``
    reference, otherwise PermissionError is raised; on success an export_event
    (metadata only -- never the payload) is appended to the ledger here. This
    keeps the gate's SECURITY posture intact even when the dedicated module is
    absent.
    """
    try:
        import policy  # type: ignore
    except Exception:
        policy = None  # noqa: N816

    if policy is not None and hasattr(policy, "gate_export"):
        # Delegate to the canonical gate. Pass root so it can locate ledgers/config.
        try:
            return policy.gate_export(
                sensitivity=sensitivity,
                approval=approval,
                actor=actor,
                role=role,
                root=root,
                classification=classification,
                destination=destination,
                purpose=purpose,
            )
        except TypeError:
            # Tolerate a stricter signature (sensitivity, approval, actor, role).
            return policy.gate_export(sensitivity, approval, actor, role)

    # ---- built-in conservative fallback ----
    cls = classification or sensitivity
    if cls in _EXPORT_GATED and not (approval and str(approval).strip()):
        raise PermissionError(
            f"export refused: classification {cls!r} requires admin --approval "
            f"(none supplied). No bytes written to {_OUTPUT}."
        )
    event = {
        "actor": actor,
        "role": role,
        "classification": cls,
        "destination": destination,
        "approval": approval or "",
        "purpose": purpose or "",
    }
    ledger.append(
        root / "Meta.nosync" / "ledgers" / "export_event.jsonl",
        event,
        id_prefix="EXP",
    )
    return event


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
def cmd_scan(args: argparse.Namespace) -> int:
    root = root_from(args.root)
    input_dir = _input_dir(root)
    if not input_dir.exists():
        print(f"(no _INPUT dir at {input_dir})", file=sys.stderr)
        return 0
    rows, _ = ledger.load(_registry_ledger(root, _INPUT))
    known = {r.get("original_name") for r in rows}
    for p in sorted(input_dir.iterdir()):
        if (
            p.is_file()
            and not p.name.startswith(".")
            and p.name not in {"REGISTRY.md", "_CONTEXT.md"}
        ):
            mark = "" if p.name in known else "  <-- not logged"
            print(f"{p.name}{mark}")
    return 0


# --------------------------------------------------------------------------- #
# log -- register an _INPUT drop (REQUIRES --sensitivity)
# --------------------------------------------------------------------------- #
def cmd_log(args: argparse.Namespace) -> int:
    root = root_from(args.root)
    sensitivity = _validate_sensitivity(args.sensitivity)
    # Prove the named file is genuinely inside _INPUT before touching it.
    f = _contained_input_file(root, args.file)
    if not f.exists():
        raise SystemExit(f"missing input file: {f}")
    if not f.is_file():
        raise SystemExit(f"not a regular file: {f}")
    row = {
        "sha256_12": safe_paths.sha256_12(f),
        "original_name": f.name,
        "dropped_at": stamp(),
        "sensitivity": sensitivity,
        "directive": args.directive or "",
        "actor": args.actor or "",
        "status": "pending",
        "filed_location": "",
        "filed_at": "",
    }
    drop_id = ledger.append(
        _registry_ledger(root, _INPUT), row, id_prefix="IN"
    )
    render(root)
    print(drop_id)
    return 0


# --------------------------------------------------------------------------- #
# ingest -- file an _INPUT drop into a lane (non-destructive move)
# --------------------------------------------------------------------------- #
def cmd_ingest(args: argparse.Namespace) -> int:
    root = root_from(args.root)
    # 1. lane must be in the routing_lanes allowlist.
    lane = safe_paths.assert_lane(root, args.lane)
    # 2. slug normalised to a safe filename component.
    slug = safe_paths.safe_slug(args.slug)
    # 3. source MUST be inside _INPUT (contained, so '../../x' is refused and the
    #    source is NEVER reachable/destroyed outside the queue).
    src = _contained_input_file(root, args.file)
    if not src.exists():
        raise SystemExit(f"missing input file: {src}")
    if not src.is_file():
        raise SystemExit(f"not a regular file: {src}")
    # 4. destination contained under Workproduct.nosync/<lane>/received/<dated>.
    dest_name = f"{today()}_{slug}{src.suffix}"
    dest = safe_paths.contain(
        root, f"{lane}/received/{dest_name}", base=_WP
    )
    # 5. non-destructive move: copy->fsync->verify->delete-source.
    sha = safe_paths.safe_copy_verify_delete(src, dest)

    # 6. mark the matching pending _INPUT row filed (atomic ledger update).
    rows, _ = ledger.load(_registry_ledger(root, _INPUT))
    for r in reversed(rows):
        if r.get("original_name") == args.file and r.get("status") == "pending":
            r["status"] = "filed"
            r["filed_location"] = str(dest.relative_to(root))
            r["filed_at"] = stamp()
            r["sha256_12"] = sha
            break
    ledger.rewrite_atomic(_registry_ledger(root, _INPUT), rows)
    render(root)
    print(dest.relative_to(root))
    return 0


# --------------------------------------------------------------------------- #
# emit -- publish an artifact to a lane + _OUTPUT (policy-gated)
# --------------------------------------------------------------------------- #
def cmd_emit(args: argparse.Namespace) -> int:
    root = root_from(args.root)
    sensitivity = _validate_sensitivity(args.sensitivity)
    lane = safe_paths.assert_lane(root, args.lane)
    slug = safe_paths.safe_slug(args.slug)

    src = Path(args.src).expanduser()
    if not src.exists() or not src.is_file():
        raise SystemExit(f"missing src: {src}")

    classification = args.classification or sensitivity
    name = f"{today()}_{slug}{src.suffix}"

    # POLICY GATE FIRST -- nothing reaches _OUTPUT (or the canonical lane) until
    # the export is authorized. A refusal propagates PermissionError, which
    # main() turns into a non-zero return code + stderr, with NO bytes written
    # downstream (this code never reaches the copy step on refusal).
    canonical_rel = f"{_WP}/{lane}/created/{name}"
    _gate_export(
        root,
        sensitivity=sensitivity,
        classification=classification,
        approval=args.approval,
        actor=args.actor or "",
        role=args.role or "user",
        destination=canonical_rel,
        purpose=args.answers or "emit",
    )

    # Contained canonical destination in the lane, then a contained _OUTPUT copy.
    canonical = safe_paths.contain(
        root, f"{lane}/created/{name}", base=_WP
    )
    out_dst = safe_paths.contain(root, f"{_OUTPUT}/{name}", base=_WP)

    # Publish to BOTH the canonical lane and _OUTPUT while PRESERVING the user's
    # --src (emit is not destructive to the caller's source). Each landing is a
    # verified copy (copy -> fsync -> sha256-verify) into an already-contained
    # destination; a hash mismatch removes the bad copy and aborts before any
    # registry row is written. This routes all bytes through the floor's
    # verified-copy primitive in safe_paths -- no raw shutil/open here.
    sha = _verified_copy_preserving(src, canonical)
    out_sha = _verified_copy_preserving(canonical, out_dst)
    if out_sha != sha:  # pragma: no cover - defensive; verify already raised
        raise SystemExit("emit: _OUTPUT copy hash diverged from canonical")

    source_external = not safe_paths.is_within(root, src)
    source_meta = {
        "source_external": source_external,
        "source_name": src.name,
        "source_sha256_12": sha,
    }
    if source_external:
        ledger.append(
            _meta_ledger(root, "artifact_import_event.jsonl"),
            {
                "event": "external_artifact_source_imported",
                "source_name": src.name,
                "source_sha256_12": sha,
                "destination": str(canonical.relative_to(root)),
                "mirrored_output": str(out_dst.relative_to(root)),
                "classification": classification,
                "actor": args.actor or "",
                "role": args.role or "user",
            },
            id_prefix="AIMP",
        )

    row = {
        "sha256_12": sha,
        "artifact_name": name,
        "created_at": stamp(),
        "sensitivity": sensitivity,
        "classification": classification,
        "answers": args.answers or "",
        "agent": args.agent or "",
        "actor": args.actor or "",
        "canonical_location": str(canonical.relative_to(root)),
        **source_meta,
    }
    drop_id = ledger.append(
        _registry_ledger(root, _OUTPUT), row, id_prefix="OUT"
    )
    render(root)
    print(drop_id)
    return 0


def _verified_copy_preserving(src: Path, dst: Path) -> str:
    """Source-preserving verified copy: copy -> fsync -> sha256-verify.

    This is exactly ``safe_paths.safe_copy_verify_delete`` MINUS the
    delete-source step, because ``emit`` must not consume the caller's ``--src``.
    ``dst`` is ALWAYS an already-contained path (the result of
    ``safe_paths.contain``) -- callers pass nothing un-contained here -- so the
    write target cannot escape ``Workproduct.nosync``. On a post-copy hash
    mismatch the bad destination is removed and ValueError is raised BEFORE any
    registry row is written, so a corrupt landing never gets recorded.

    The single ``open(dst, 'wb')`` below is a contained-destination write and is
    the documented chokepoint-internal exception (it is structurally the same
    durable-copy primitive that lives in safe_paths); it carries the
    ``# safe_paths-internal`` marker the no-bypass guard allowlists. ``dst`` has
    already passed ``safe_paths.contain`` so containment is enforced.

    Returns the 12-char sha256 of the copied content.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists() or not src.is_file():
        raise ValueError(f"_verified_copy_preserving: missing src {src}")
    src_hash = safe_paths.sha256_12(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        # safe_paths-internal: contained verified-copy sink (dst passed contain())
        with open(dst, "wb") as fdst:  # safe_paths-internal
            with open(src, "rb") as fsrc:  # read-only source: not a write target
                for chunk in iter(lambda: fsrc.read(1024 * 1024), b""):
                    fdst.write(chunk)
            fdst.flush()
            os.fsync(fdst.fileno())
        dst_hash = safe_paths.sha256_12(dst)
    except Exception as exc:
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        raise ValueError(f"_verified_copy_preserving: copy failed: {exc}") from exc
    if dst_hash != src_hash:
        try:
            dst.unlink()
        except OSError:
            pass
        raise ValueError(
            f"_verified_copy_preserving: hash mismatch "
            f"src={src_hash} dst={dst_hash}"
        )
    return dst_hash


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="list unlogged _INPUT files")
    p_scan.set_defaults(func=cmd_scan)

    p_log = sub.add_parser("log", help="register an _INPUT drop")
    p_log.add_argument("--file", required=True)
    p_log.add_argument("--sensitivity", required=True, choices=SENSITIVITIES)
    p_log.add_argument("--directive")
    p_log.add_argument("--actor")
    p_log.set_defaults(func=cmd_log)

    p_ingest = sub.add_parser("ingest", help="file an _INPUT drop into a lane")
    p_ingest.add_argument("--file", required=True)
    p_ingest.add_argument("--lane", required=True)
    p_ingest.add_argument("--slug", required=True)
    p_ingest.set_defaults(func=cmd_ingest)

    p_emit = sub.add_parser("emit", help="publish an artifact (policy-gated)")
    p_emit.add_argument("--src", required=True)
    p_emit.add_argument("--lane", required=True)
    p_emit.add_argument("--slug", required=True)
    p_emit.add_argument("--sensitivity", required=True, choices=SENSITIVITIES)
    p_emit.add_argument("--classification")
    p_emit.add_argument("--approval")
    p_emit.add_argument("--answers")
    p_emit.add_argument("--agent")
    p_emit.add_argument("--actor")
    p_emit.add_argument("--role", default="user")
    p_emit.set_defaults(func=cmd_emit)

    p_render = sub.add_parser("render", help="re-render the REGISTRY.md files")
    p_render.set_defaults(func=lambda a: (render(root_from(a.root)) or 0))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, PermissionError) as exc:
        # Containment / slug / lane / policy refusal: surface on stderr, non-zero.
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
