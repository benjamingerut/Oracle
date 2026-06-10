#!/usr/bin/env python3
"""derived_memory.py -- optional derived-memory engine manager.

Oracle's canonical memory is the governed filesystem: immutable Sources,
review-gated Findings, Contradictions, TRUTH-MAP authority, and the answer
protocol. External memory engines can be useful, but only as rebuildable
derived artifacts. This module manages that boundary without adding runtime
dependencies to the spawned kernel.

The default config in ``oracle.yml`` declares two optional engines:

* ``mempalace`` -- semantic/verbatim retrieval over exported Oracle chunks.
* ``graphify`` -- corpus graph analysis over exported Oracle chunks.

This tool does not import, install, or launch either package. It only:

* validates that the config keeps Oracle authoritative;
* reports whether the optional commands appear on PATH;
* exports sensitivity-capped chunk corpora from the rebuildable
  ``knowledge_index`` into ``_data.nosync/derived/<engine>/raw/``.

The exported files are derived and may be deleted/rebuilt at any time. They can
help retrieval and graph discovery, but they do not ground material answers.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:  # pragma: no cover - exercised in both flat and package contexts
    import ledger
    import oracle_yaml
    import policy
    import safe_paths
    import knowledge_index
except Exception:  # pragma: no cover
    from . import ledger  # type: ignore
    from . import oracle_yaml  # type: ignore
    from . import policy  # type: ignore
    from . import safe_paths  # type: ignore
    from . import knowledge_index  # type: ignore


CONTRACT_VERSION = "oracle.derived_memory.v1"
SENSITIVITY_ORDER = ["public", "internal", "confidential", "restricted", "secret"]
_SENS_RANK = {s: i for i, s in enumerate(SENSITIVITY_ORDER)}
_STRICTEST = len(SENSITIVITY_ORDER) - 1

ENGINE_DEFAULTS = {
    "mempalace": {
        "enabled": False,
        "role": "semantic_retrieval",
        "command": "mempalace",
        "export_subdir": "derived/mempalace",
        "max_default_sensitivity": "internal",
        "answer_authority": "never",
        "environment_default": "local_deterministic",
        "minimized_chars": 500,
    },
    "graphify": {
        "enabled": False,
        "role": "knowledge_graph_analysis",
        "command": "graphify",
        "export_subdir": "derived/graphify",
        "max_default_sensitivity": "internal",
        "answer_authority": "never",
        "environment_default": "local_deterministic",
        "minimized_chars": 500,
    },
}

ALLOWED_ROLES = {"semantic_retrieval", "knowledge_graph_analysis"}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sens_rank(label: Optional[str]) -> int:
    if label is None:
        return _STRICTEST
    return _SENS_RANK.get(str(label).strip().lower(), _STRICTEST)


def _load_oracle_yml(root: Path) -> dict:
    cfg = root / "oracle.yml"
    if not cfg.exists():
        raise ValueError(f"oracle.yml not found at {cfg}")
    data = oracle_yaml.safe_load(cfg.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("oracle.yml is not a mapping")
    return data


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def load_config(root) -> dict:
    """Return normalized derived-memory config with defaults merged in."""
    root = Path(root)
    data = _load_oracle_yml(root)
    section = data.get("derived_memory") or {}
    if not isinstance(section, dict):
        section = {}
    raw_engines = section.get("engines") or {}
    if not isinstance(raw_engines, dict):
        raw_engines = {}

    engines: dict[str, dict] = {}
    for name, defaults in ENGINE_DEFAULTS.items():
        raw = raw_engines.get(name) or {}
        if not isinstance(raw, dict):
            raw = {}
        merged = {**defaults, **raw}
        merged["enabled"] = _coerce_bool(merged.get("enabled"))
        try:
            merged["minimized_chars"] = int(merged.get("minimized_chars") or 0)
        except (TypeError, ValueError):
            merged["minimized_chars"] = defaults["minimized_chars"]
        engines[name] = merged

    return {
        "canonical_authority": section.get("canonical_authority") or "oracle",
        "answer_boundary": section.get("answer_boundary") or "oracle_answer_protocol_only",
        "artifact_scope": section.get("artifact_scope") or "derived_rebuildable",
        "contract_version": section.get("contract_version") or CONTRACT_VERSION,
        "engines": engines,
    }


def _engine_output_dir(root: Path, engine_cfg: dict) -> Path:
    subdir = str(engine_cfg.get("export_subdir") or "").strip()
    if not subdir:
        raise ValueError("engine export_subdir is empty")
    return safe_paths.contain(root, subdir, base="_data.nosync")


def validate_config(root) -> list[dict]:
    """Return validation problems. Empty list means the boundary is intact."""
    root = Path(root)
    cfg = load_config(root)
    problems: list[dict] = []

    if cfg["canonical_authority"] != "oracle":
        problems.append({
            "code": "canonical-authority",
            "message": "derived_memory.canonical_authority must be 'oracle'",
        })
    if cfg["answer_boundary"] != "oracle_answer_protocol_only":
        problems.append({
            "code": "answer-boundary",
            "message": "derived_memory.answer_boundary must be 'oracle_answer_protocol_only'",
        })
    if cfg["artifact_scope"] != "derived_rebuildable":
        problems.append({
            "code": "artifact-scope",
            "message": "derived_memory.artifact_scope must be 'derived_rebuildable'",
        })
    if cfg["contract_version"] != CONTRACT_VERSION:
        problems.append({
            "code": "contract-version",
            "message": f"derived_memory.contract_version must be {CONTRACT_VERSION!r}",
        })

    for name, eng in cfg["engines"].items():
        if eng.get("role") not in ALLOWED_ROLES:
            problems.append({
                "code": "engine-role",
                "engine": name,
                "message": f"unsupported role {eng.get('role')!r}",
            })
        if eng.get("answer_authority") != "never":
            problems.append({
                "code": "engine-authority",
                "engine": name,
                "message": "engine answer_authority must be 'never'",
            })
        if _sens_rank(eng.get("max_default_sensitivity")) == _STRICTEST and (
            str(eng.get("max_default_sensitivity")).strip().lower() not in SENSITIVITY_ORDER
        ):
            problems.append({
                "code": "engine-sensitivity",
                "engine": name,
                "message": f"unknown max_default_sensitivity {eng.get('max_default_sensitivity')!r}",
            })
        try:
            policy.check_processing("public", str(eng.get("environment_default") or ""))
        except Exception as exc:
            problems.append({
                "code": "engine-environment",
                "engine": name,
                "message": str(exc),
            })
        if int(eng.get("minimized_chars") or 0) <= 0:
            problems.append({
                "code": "engine-minimized-chars",
                "engine": name,
                "message": "minimized_chars must be a positive integer",
            })
        try:
            _engine_output_dir(root, eng)
        except ValueError as exc:
            problems.append({
                "code": "engine-output-path",
                "engine": name,
                "message": str(exc),
            })
        if eng.get("enabled") and shutil.which(str(eng.get("command") or "")) is None:
            problems.append({
                "code": "engine-command-missing",
                "engine": name,
                "message": f"enabled engine command not found: {eng.get('command')!r}",
            })

    return problems


def status(root) -> dict:
    """Return derived-memory engine status without requiring optional packages."""
    root = Path(root)
    cfg = load_config(root)
    problems = validate_config(root)
    engines: dict[str, dict] = {}
    for name, eng in cfg["engines"].items():
        command = str(eng.get("command") or "")
        out_dir = None
        try:
            out_dir = str(_engine_output_dir(root, eng))
        except ValueError:
            out_dir = None
        engines[name] = {
            "enabled": bool(eng.get("enabled")),
            "role": eng.get("role"),
            "command": command,
            "command_available": bool(command and shutil.which(command)),
            "export_dir": out_dir,
            "max_default_sensitivity": eng.get("max_default_sensitivity"),
            "answer_authority": eng.get("answer_authority"),
        }
    return {
        "canonical_authority": cfg["canonical_authority"],
        "answer_boundary": cfg["answer_boundary"],
        "artifact_scope": cfg["artifact_scope"],
        "contract_version": cfg["contract_version"],
        "engines": engines,
        "problems": problems,
        "ok": not problems,
    }


def _read_index_chunks(root: Path, max_sensitivity: Optional[str]) -> list[dict]:
    try:
        return knowledge_index.list_chunks(root, max_sensitivity=max_sensitivity)
    except Exception:
        return []


def _minimize_text(text: str, chars: int) -> str:
    text = text or ""
    if len(text) <= chars:
        return text
    return text[:chars].rstrip() + "\n\n[...minimized by Oracle policy...]"


def _policy_filter_chunks(
    chunks: list[dict],
    *,
    environment: str,
    minimized_chars: int,
) -> tuple[list[dict], dict]:
    out: list[dict] = []
    verdict_counts = {"allow": 0, "allow-minimized": 0, "deny": 0}
    by_sensitivity: dict[str, int] = {}
    for chunk in chunks:
        sens = str(chunk.get("sensitivity") or "secret").strip().lower()
        try:
            verdict = policy.check_processing(sens, environment)
        except Exception:
            verdict = "deny"
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        by_sensitivity[sens] = by_sensitivity.get(sens, 0) + 1
        if verdict == "deny":
            continue
        c = dict(chunk)
        c["processing_verdict"] = verdict
        if verdict == "allow-minimized":
            c["text"] = _minimize_text(str(c.get("text") or ""), minimized_chars)
            c["minimized"] = True
        else:
            c["minimized"] = False
        out.append(c)
    stats = {"verdict_counts": verdict_counts, "by_sensitivity": by_sensitivity}
    return out, stats


def _render_markdown(chunks: list[dict], *, engine: str, max_sensitivity: str) -> str:
    lines = [
        f"# Oracle Derived Corpus for {engine}",
        "",
        "This file is generated from Oracle's rebuildable knowledge index.",
        "It is a derived artifact, not source authority.",
        f"Max sensitivity included: {max_sensitivity}",
        f"Generated: {_now_iso()}",
        "",
    ]
    for c in chunks:
        title = c.get("title") or c.get("doc_id") or "Untitled"
        lines.extend([
            f"## {title}",
            "",
            f"- source_id: {c.get('source_id') or ''}",
            f"- doc_id: {c.get('doc_id') or ''}",
            f"- chunk_index: {c.get('chunk_index')}",
            f"- sensitivity: {c.get('sensitivity')}",
            f"- offsets: {c.get('start')}..{c.get('end')}",
            f"- provenance: {c.get('provenance') or ''}",
            "",
            str(c.get("text") or "").strip(),
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def _suggestion(engine: str, raw_dir: Path) -> str:
    if engine == "graphify":
        return f"graphify {raw_dir}"
    if engine == "mempalace":
        return f"mempalace mine {raw_dir} --wing oracle --room sources"
    return f"<run {engine} over {raw_dir}>"


def _ledger_path(root: Path) -> Path:
    return Path(root) / "Meta.nosync" / "ledgers" / "derived_memory.jsonl"


def _append_run_ledger(root: Path, manifest: dict) -> str:
    row = {
        "engine": manifest["engine"],
        "action": "prepare",
        "contract_version": manifest["contract_version"],
        "environment": manifest["environment"],
        "max_sensitivity": manifest["max_sensitivity"],
        "chunk_count": manifest["chunk_count"],
        "exported_chunk_count": manifest["exported_chunk_count"],
        "verdict_counts": manifest["verdict_counts"],
        "by_sensitivity": manifest["by_sensitivity"],
        "raw_dir": manifest["raw_dir"],
        "files": manifest["files"],
    }
    return ledger.append(_ledger_path(root), row, id_prefix="DM")


def prepare_engine(
    root,
    engine: str,
    *,
    max_sensitivity: Optional[str] = None,
    environment: Optional[str] = None,
) -> dict:
    """Export a sensitivity-capped corpus for one optional engine."""
    root = Path(root)
    cfg = load_config(root)
    engines = cfg["engines"]
    if engine not in engines:
        raise ValueError(f"unknown derived-memory engine {engine!r}")
    problems = validate_config(root)
    blocking = [p for p in problems if p.get("code") in {
        "canonical-authority",
        "answer-boundary",
        "artifact-scope",
        "engine-output-path",
        "engine-authority",
        "engine-role",
        "engine-sensitivity",
    } and p.get("engine") in (None, engine)]
    if blocking:
        raise ValueError("derived-memory config invalid: " + "; ".join(p["message"] for p in blocking))

    eng = engines[engine]
    ceiling = str(max_sensitivity or eng.get("max_default_sensitivity") or "internal").strip().lower()
    if ceiling not in SENSITIVITY_ORDER:
        raise ValueError(f"unknown max_sensitivity {ceiling!r}")
    env = str(environment or eng.get("environment_default") or "local_deterministic").strip().lower()
    try:
        policy.check_processing("public", env)
    except Exception as exc:
        raise ValueError(str(exc)) from exc

    chunks_all = _read_index_chunks(root, ceiling)
    chunks, policy_stats = _policy_filter_chunks(
        chunks_all,
        environment=env,
        minimized_chars=int(eng.get("minimized_chars") or 500),
    )
    out_dir = _engine_output_dir(root, eng)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    md_path = raw_dir / "oracle-index-chunks.md"
    jsonl_path = raw_dir / "oracle-index-chunks.jsonl"
    manifest_path = out_dir / "manifest.json"

    md_path.write_text(  # safe_paths-internal: raw_dir from _engine_output_dir() → safe_paths.contain()
        _render_markdown(chunks, engine=engine, max_sensitivity=ceiling),
        encoding="utf-8",
    )
    jsonl_path.write_text(  # safe_paths-internal: raw_dir from _engine_output_dir() → safe_paths.contain()
        "\n".join(json.dumps(c, ensure_ascii=False, sort_keys=True) for c in chunks) + ("\n" if chunks else ""),
        encoding="utf-8",
    )
    manifest = {
        "engine": engine,
        "contract_version": cfg["contract_version"],
        "prepared_at": _now_iso(),
        "artifact_scope": cfg["artifact_scope"],
        "canonical_authority": cfg["canonical_authority"],
        "answer_boundary": cfg["answer_boundary"],
        "environment": env,
        "max_sensitivity": ceiling,
        "chunk_count": len(chunks_all),
        "exported_chunk_count": len(chunks),
        "verdict_counts": policy_stats["verdict_counts"],
        "by_sensitivity": policy_stats["by_sensitivity"],
        "raw_dir": str(raw_dir),
        "files": [str(md_path), str(jsonl_path)],
        "suggested_command": _suggestion(engine, raw_dir),
    }
    manifest["ledger_id"] = _append_run_ledger(root, manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")  # safe_paths-internal: out_dir from _engine_output_dir() → safe_paths.contain()
    return manifest


def _print_human_status(data: dict) -> None:
    print(f"canonical_authority: {data['canonical_authority']}")
    print(f"answer_boundary: {data['answer_boundary']}")
    print(f"artifact_scope: {data['artifact_scope']}")
    print(f"contract_version: {data['contract_version']}")
    for name, eng in data["engines"].items():
        availability = "available" if eng["command_available"] else "missing"
        enabled = "enabled" if eng["enabled"] else "manual"
        print(
            f"{name}: {enabled}; role={eng['role']}; command={eng['command']} ({availability}); "
            f"max_default_sensitivity={eng['max_default_sensitivity']}"
        )
    if data["problems"]:
        print("problems:")
        for p in data["problems"]:
            engine = f" [{p['engine']}]" if p.get("engine") else ""
            print(f"- {p['code']}{engine}: {p['message']}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Manage optional derived-memory engines")
    ap.add_argument("--root", default=".", help="oracle root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="show configured derived-memory engines")
    p_status.add_argument("--json", action="store_true")

    p_check = sub.add_parser("check", help="validate derived-memory boundary config")
    p_check.add_argument("--json", action="store_true")

    p_prepare = sub.add_parser("prepare", help="export a sensitivity-capped corpus")
    p_prepare.add_argument("engine", choices=sorted(ENGINE_DEFAULTS))
    p_prepare.add_argument("--max-sensitivity", choices=SENSITIVITY_ORDER)
    p_prepare.add_argument("--environment", choices=policy.ENVIRONMENTS)
    p_prepare.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    root = Path(args.root)

    try:
        if args.cmd == "status":
            out = status(root)
            if args.json:
                print(json.dumps(out, indent=2, ensure_ascii=False))
            else:
                _print_human_status(out)
            return 0 if out["ok"] else 1
        if args.cmd == "check":
            problems = validate_config(root)
            out = {"ok": not problems, "problems": problems}
            if args.json:
                print(json.dumps(out, indent=2, ensure_ascii=False))
            elif problems:
                for p in problems:
                    engine = f" [{p['engine']}]" if p.get("engine") else ""
                    print(f"{p['code']}{engine}: {p['message']}", file=sys.stderr)
            else:
                print("derived-memory config ok")
            return 0 if not problems else 1
        if args.cmd == "prepare":
            out = prepare_engine(
                root,
                args.engine,
                max_sensitivity=args.max_sensitivity,
                environment=args.environment,
            )
            if args.json:
                print(json.dumps(out, indent=2, ensure_ascii=False))
            else:
                print(
                    f"prepared {args.engine}: chunks={out['exported_chunk_count']}/{out['chunk_count']} "
                    f"max_sensitivity={out['max_sensitivity']} raw_dir={out['raw_dir']}"
                )
                print(f"suggested command: {out['suggested_command']}")
            return 0
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
