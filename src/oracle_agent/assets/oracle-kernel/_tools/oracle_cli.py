#!/usr/bin/env python3
"""Unified ``./oracle <verb> ...`` dispatcher -- one verb per workflow.

The v2 surface puts the daily workflow verbs first; every verb maps onto one
kernel module and forwards the remaining arguments to that module's
``main(argv)``, so flags keep honouring each module's CLI contract. A few
verbs carry a thin translation layer (documented inline) so the common form
needs no subcommand:

    ./oracle status                      session-start report
    ./oracle search "<terms>"            retrieval over the knowledge index
    ./oracle answer --object "<bo>"      graduated answer preflight (0/2/3/4)
    ./oracle ingest <paths...>           batch ingest; outside paths staged in
    ./oracle review                      the Review Inbox
    ./oracle brief                       leadership brief (gen|publish)
    ./oracle remember --user-request ... capture session memory
    ./oracle checkpoint                  session-close: due loops + report
    ./oracle capture feedback|value|failure ...
    ./oracle loops list|due|run|complete ...
    ./oracle check                       audit + lint in one gate
    ./oracle admin <area> ...            truth|policy|backup|upgrade|autonomy|
                                         connector|session control plane

Modules are imported LAZILY -- only when their verb is invoked -- so a partial
tool layer never breaks the dispatcher.
"""
from __future__ import annotations

import importlib
import sys


# verb -> (module name, human description). Daily verbs first, control plane
# under ``admin``, and the full v1 power-user groups remain available.
_GROUPS: dict[str, tuple[str, str]] = {
    # -- daily verbs ---------------------------------------------------------
    "status": ("oracle_status", "session-start report: maturity, inbox, due loops"),
    "search": ("knowledge_index", "retrieval over the knowledge index"),
    "answer": ("answer_protocol", "graduated answer preflight (grounded/supported/caveated/refused)"),
    "ingest": ("ingest_pipeline", "ingest files/folders; outside paths staged in non-destructively"),
    "review": ("review_queue", "the Review Inbox: everything waiting on a decision"),
    "brief": ("briefing", "leadership brief: the oracle's proactive voice"),
    "remember": ("session_memory", "capture session memory (alias of session-memory capture)"),
    "checkpoint": ("oracle_status", "session-close: run due builtin loops + report"),
    "capture": ("capture", "feedback/value/failure capture"),
    "loops": ("loops", "loop due-ness engine + runner"),
    "check": ("setup_audit", "audit + lint, one verification gate"),
    "dashboard": ("dashboard", "admin systems dashboard: subsystem health + selection toggles"),
    # -- control plane (also reachable as ./oracle admin <area>) -------------
    "truth": ("truth_map", "truth-map authority: rows/resolve/propose/promote/validate"),
    "policy": ("policy", "processing/export/role policy gate"),
    "backup": ("backup", "tiered backup + restore-verify"),
    "upgrade": ("upgrade", "tool-layer-only kernel migration"),
    "actions": ("actions", "autonomous-action chokepoint"),
    "connector": ("connectors", "connector runtime entrypoint"),
    "session": ("session_interface", "session interface resolver + capability gate"),
    # -- engine groups (power use; the daily verbs cover the common paths) ---
    "artifact": ("artifact_io", "contained, policy-gated artifact I/O"),
    "ledger": ("ledger", "durable JSONL ledger verify/repair/render"),
    "lint": ("oracle_lint", "schema-validating oracle linter"),
    "audit": ("setup_audit", "deep bootstrap audit"),
    "secret": ("secret_scan", "broadened secret scanner"),
    "index": ("knowledge_index", "retrieval index build/query"),
    "skills": ("skills", "managed oracle-local skills repository"),
    "source": ("source_record", "immutable Source-record generator"),
    "derive": ("derive", "review-gated derivation"),
    "contradiction": ("contradiction", "contradiction adjudicator"),
    "recommendation": ("recommendation", "recommendation adjudicator"),
    "deliverables": ("standing_deliverables", "standing-deliverable generators"),
    "synthesis": ("synthesis", "insight-synthesis worklists"),
    "derived-memory": ("derived_memory", "optional derived memory engines"),
    "session-memory": ("session_memory", "session capture, decomposition, and dreaming"),
    "scorecard": ("scorecard", "value scorecard: KPIs + trend from ledgers"),
    "improvements": ("improvements", "improvement lifecycle: adjudicate + aging"),
    "meta-health": ("meta_health", "telemetry consumer: loop health, signal aging"),
    "harness": ("harness", "headless scheduler pass / dream session"),
}

# ./oracle admin <area> ... -> module (the control plane, grouped for the docs'
# mental model; each area is also a top-level group above).
_ADMIN_AREAS: dict[str, str] = {
    "truth": "truth_map",
    "policy": "policy",
    "backup": "backup",
    "upgrade": "upgrade",
    "autonomy": "actions",
    "connector": "connectors",
    "session": "session_interface",
    "dashboard": "dashboard",
}

_DAILY = (
    "status", "search", "answer", "ingest", "review", "brief",
    "remember", "checkpoint", "capture", "loops", "check", "dashboard", "admin",
)


def _resolve_module(mod_name: str):
    """Import a kernel module by name, tolerating package and flat layouts."""
    try:
        return importlib.import_module(mod_name)
    except ImportError:
        pkg = __package__ or ""
        if pkg:
            return importlib.import_module(f".{mod_name}", package=pkg)
        raise


def _print_groups(stream=sys.stdout) -> None:
    stream.write("Daily verbs:\n")
    width = max(len(g) for g in _GROUPS) + 2
    for verb in _DAILY:
        if verb == "admin":
            stream.write(f"  {'admin <area>'.ljust(width)}  control plane: {', '.join(sorted(_ADMIN_AREAS))}\n")
            continue
        _mod, desc = _GROUPS[verb]
        stream.write(f"  {verb.ljust(width)}  {desc}\n")
    stream.write("\nEngine groups (power use):\n")
    for group in sorted(_GROUPS):
        if group in _DAILY:
            continue
        _mod, desc = _GROUPS[group]
        stream.write(f"  {group.ljust(width)}  {desc}\n")


def _split_root(rest: list[str]) -> tuple[list[str], list[str]]:
    """Pull leading/anywhere ``--root X`` / ``--root=X`` out of ``rest``."""
    prefix: list[str] = []
    tail: list[str] = []
    i = 0
    while i < len(rest):
        cur = rest[i]
        if cur == "--root" and i + 1 < len(rest):
            prefix.extend(rest[i : i + 2])
            i += 2
            continue
        if cur.startswith("--root="):
            prefix.append(cur)
            i += 1
            continue
        tail.append(cur)
        i += 1
    return prefix, tail


def _first_positional(args: list[str]) -> str:
    for a in args:
        if not a.startswith("-"):
            return a
    return ""


def _translate(group: str, rest: list[str]) -> tuple[str, list[str]]:
    """Map a daily verb's natural form onto its module's CLI contract."""
    root_args, tail = _split_root(rest)

    if group == "status":
        return "oracle_status", [*root_args, "status", *tail]

    if group == "checkpoint":
        return "oracle_status", [*root_args, "checkpoint", *tail]

    if group == "search":
        # ./oracle search "<terms...>" [--k N] [--max-sensitivity S] [--json]
        #                              [--qvec-stdin]
        # The passthrough allowlist (P8S-4) MUST include the vector subcommands:
        # without 'vectors-add' here, ``oracle search vectors-add --file ...``
        # would be silently rewritten into a TEXT QUERY for the literal string
        # "vectors-add" and exit 0, no-opping the whole vector pipeline while
        # looking green. ``--qvec-stdin`` is a bare flag, so it passes through
        # the flags loop unchanged.
        _SEARCH_SUBCOMMANDS = (
            "query", "build", "add", "stats", "reindex",
            "vectors-add", "vectors-pending", "vectors-prune",
        )
        terms = [a for a in tail if not a.startswith("-")]
        flags: list[str] = []
        skip_next = False
        for i, a in enumerate(tail):
            if skip_next:
                flags.append(a)
                skip_next = False
                continue
            if a.startswith("-"):
                flags.append(a)
                if a in ("--k", "--max-sensitivity") and i + 1 < len(tail):
                    skip_next = True
        if terms and _first_positional(tail) not in _SEARCH_SUBCOMMANDS:
            return "knowledge_index", [*root_args, "query", "--q", " ".join(terms), *flags]
        return "knowledge_index", [*root_args, *tail]

    if group == "answer":
        # ./oracle answer --object ... (default subcommand) | answer research ...
        if tail and tail[0].startswith("-") and tail[0] not in ("-h", "--help"):
            return "answer_protocol", [*root_args, "answer", *tail]
        return "answer_protocol", [*root_args, *tail]

    if group == "ingest":
        # ./oracle ingest <paths...> -> batch (run/batch pass through)
        first = _first_positional(tail)
        if first and first not in ("run", "batch"):
            return "ingest_pipeline", [*root_args, "batch", *tail]
        return "ingest_pipeline", [*root_args, *tail]

    if group == "brief":
        first = _first_positional(tail)
        if first not in ("gen", "publish"):
            return "briefing", [*root_args, "gen", *tail]
        return "briefing", [*root_args, *tail]

    if group == "remember":
        first = _first_positional(tail)
        if first not in ("capture", "decompose", "dream", "export-derived", "list"):
            return "session_memory", [*root_args, "capture", *tail]
        return "session_memory", [*root_args, *tail]

    mod_name, _desc = _GROUPS[group]
    return mod_name, rest


def _run_module(mod_name: str, args: list[str], *, group: str) -> int:
    try:
        module = _resolve_module(mod_name)
    except ImportError as exc:
        sys.stderr.write(
            f"oracle: verb '{group}' is unavailable "
            f"(module '{mod_name}' could not be imported: {exc})\n"
        )
        return 3
    entry = getattr(module, "main", None)
    if not callable(entry):
        sys.stderr.write(
            f"oracle: module '{mod_name}' has no callable main(); cannot dispatch\n"
        )
        return 3
    result = entry(args)
    return int(result) if isinstance(result, int) else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(
            "usage: oracle <verb> [args...]\n\n"
            "Session protocol: `oracle status` -> work -> `oracle checkpoint`.\n\n"
        )
        _print_groups()
        return 0

    group = argv[0]
    rest = argv[1:]

    # ./oracle check == audit + lint (one gate, combined exit code).
    if group == "check":
        audit_rc = _run_module("setup_audit", rest, group="check")
        lint_rc = _run_module("oracle_lint", rest, group="check")
        return audit_rc or lint_rc

    # ./oracle admin <area> ... -> control-plane module.
    if group == "admin":
        if not rest or rest[0] in ("-h", "--help"):
            sys.stdout.write("usage: oracle admin <area> [args...]\n\nAreas:\n")
            for area in sorted(_ADMIN_AREAS):
                sys.stdout.write(f"  {area}\n")
            return 0
        area, area_rest = rest[0], rest[1:]
        if area not in _ADMIN_AREAS:
            sys.stderr.write(
                f"oracle: unknown admin area '{area}' "
                f"(expected one of {', '.join(sorted(_ADMIN_AREAS))})\n"
            )
            return 2
        # Hoist --root ahead of the subcommand (module parsers declare it at
        # the top level, before their subparsers).
        root_args, tail = _split_root(area_rest)
        return _run_module(_ADMIN_AREAS[area], [*root_args, *tail], group=f"admin {area}")

    if group not in _GROUPS:
        sys.stderr.write(f"oracle: unknown verb '{group}'\n\n")
        _print_groups(sys.stderr)
        return 2

    mod_name, args = _translate(group, rest)
    return _run_module(mod_name, args, group=group)


if __name__ == "__main__":
    raise SystemExit(main())
