#!/usr/bin/env python3
"""dashboard.py -- the admin systems dashboard: every subsystem, its health,
and the exact command that flips each toggle.

``./oracle dashboard`` is the admin's single control screen. It surfaces, on
one visually scannable display: subsystem status (memory, authority, index,
review inbox, loops, autonomy, signal health, improvements, scorecard,
connectors, derived memory, kernel, backup), a per-loop table with on/off
state, and a Controls table mapping every selection toggle (autonomy
enable/level, kill switch, per-loop active/paused, derived engines) to the ONE
command that changes it.

The dashboard EXECUTES nothing. It is a pure read-side rendering of current
state (self-cleaning: work an item and its glyph changes on the next render),
and every mutation it suggests routes through the already-gated verbs it
prints (``admin autonomy``, ``loops set-status``, ...). That is why it needs
no role gate of its own.

Evolution under self-improvement: the panel set is a registry (``PANELS``).
An optional ``dashboards.nosync/layout.yml`` (block-style safe-subset YAML
with ``order:`` and ``hidden:`` lists) reorders or hides panels without code
changes, so dashboard layout can evolve through the normal improvement
lifecycle: propose via ``./oracle capture feedback --target dashboard``,
apply by editing layout.yml (advisory: agent-obeyed, not code-enforced).

``publish`` renders the same state as a self-contained HTML file (zero
external assets -- local sovereignty) into ``dashboards.nosync/`` per that
folder's discipline: a rendering, not a record.

Stdlib only.
"""
from __future__ import annotations

import argparse
import html as _html
import importlib
import json
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:  # bare import (conftest puts _tools on sys.path); package fallback
    import safe_paths
except Exception:  # pragma: no cover - package import path
    from . import safe_paths  # type: ignore

__all__ = ["build", "render_md", "render_html", "read_layout", "PANELS"]

DASH_BASE = "dashboards.nosync"
LAYOUT_REL = f"{DASH_BASE}/layout.yml"
DEFAULT_HTML_NAME = "admin-dashboard.html"

# state -> glyph. ok = healthy/on, warn = needs a look, off = idle/disabled
# (a legitimate state, not an error), attention = act now.
_GLYPH = {"ok": "●", "warn": "◐", "off": "○", "attention": "✗"}
_TREND_GLYPH = {"improving": "↑", "regressing": "↓", "flat": "→", "baseline": "·"}


def _import(name: str):
    for candidate in (name, f".{name}"):
        try:
            if candidate.startswith("."):
                return importlib.import_module(candidate, package=__package__ or "_tools")
            return importlib.import_module(candidate)
        except Exception:
            continue
    return None


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _safe(fn: Callable, default: Any):
    """Run a read-side gather; a broken subsystem degrades its panel, never
    the whole dashboard (the panel will render from the default)."""
    try:
        return fn()
    except Exception:
        return default


def _cfg(root: Path) -> dict:
    yaml_mod = _import("oracle_yaml")
    p = root / "oracle.yml"
    if yaml_mod is None or not p.is_file():
        return {}
    try:
        data = yaml_mod.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _dt_day(value: Any) -> str:
    """Render a loop timestamp as its date part ('' when absent)."""
    s = str(value or "")
    return s[:10] if s else "-"


# --------------------------------------------------------------------------- #
# layout overlay -- how the dashboard evolves without code changes
# --------------------------------------------------------------------------- #
def read_layout(root: Path) -> dict:
    """Optional ``dashboards.nosync/layout.yml``: ``order:`` and ``hidden:``
    block lists of panel keys. Missing/invalid file -> defaults (every panel,
    registry order). Unknown keys are ignored, never fatal."""
    yaml_mod = _import("oracle_yaml")
    p = Path(root) / LAYOUT_REL
    out = {"order": list(PANELS), "hidden": [], "source": "default"}
    if yaml_mod is None or not p.is_file():
        return out
    try:
        data = yaml_mod.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return out
    if not isinstance(data, dict):
        return out
    hidden = [str(k) for k in (data.get("hidden") or []) if str(k) in PANELS]
    order = [str(k) for k in (data.get("order") or []) if str(k) in PANELS]
    for key in PANELS:  # anything not mentioned keeps registry order, at the end
        if key not in order:
            order.append(key)
    out.update({"order": order, "hidden": hidden, "source": LAYOUT_REL})
    return out


# --------------------------------------------------------------------------- #
# portability -- machine-local facts + every external coupling, in one place
# --------------------------------------------------------------------------- #
def _launch_agents_dir() -> Path:
    """Where launchd user agents live on this machine (tests monkeypatch)."""
    return Path.home() / "Library" / "LaunchAgents"


def _scheduler_coupling(root: Path, cfg: dict) -> dict:
    """Probe the INSTALLED scheduler (outside the root, so no in-repo scan can
    see it). The installed plist embeds the ABSOLUTE oracle root; after a
    migration it silently points at the dead path -- the worst quiet failure a
    move can cause, so we detect it here."""
    codename = str(((cfg.get("company") or {}).get("codename")) or "").lower()
    plist = _launch_agents_dir() / f"com.oracle.{codename}.loops.plist"
    info = {
        "installed": False,
        "plist": str(plist) if codename else "",
        "embedded_root": "",
        "stale_root": False,
    }
    if not codename or not plist.is_file():
        return info
    info["installed"] = True
    text = _safe(lambda: plist.read_text(encoding="utf-8"), "")
    marker = "/_tools/harness.py</string>"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("<string>") and line.endswith(marker):
            embedded = line[len("<string>"):-len(marker)]
            info["embedded_root"] = embedded
            try:
                info["stale_root"] = Path(embedded).resolve() != root.resolve()
            except OSError:
                info["stale_root"] = True
            break
    return info


def _binary_couplings(cfg: dict, dream: dict) -> list[dict]:
    """Host binaries the oracle's config names: PATH-dependent, so they are
    couplings to THIS machine, not repo state. Only configured/enabled entries
    are listed (an oracle that never dreams has no dream coupling)."""
    out: list[dict] = []
    dream_cmd = str((dream or {}).get("command") or "").strip()
    if dream_cmd:
        prog = dream_cmd.split()[0]
        out.append({
            "kind": "binary", "name": "dream.command", "command": dream_cmd,
            "found": shutil.which(prog) is not None,
        })
    engines = ((cfg.get("derived_memory") or {}).get("engines") or {})
    for name, spec in (engines.items() if isinstance(engines, dict) else []):
        if not isinstance(spec, dict):
            continue
        if str(spec.get("enabled")).lower() not in ("true", "yes", "1", "on"):
            continue
        cmd = str(spec.get("command") or name).strip()
        out.append({
            "kind": "binary", "name": f"engine {name}", "command": cmd,
            "found": shutil.which(cmd.split()[0]) is not None,
        })
    return out


def _connector_couplings(root: Path) -> list[dict]:
    """Connector manifests are the SANCTIONED registry of external references
    (source paths, systems, credential var names) -- list each as a coupling."""
    yaml_mod = _import("oracle_yaml")
    out: list[dict] = []
    if yaml_mod is None:
        return out
    for mf in sorted((root / "Connectors").glob("*/*.manifest.yaml")):
        data = _safe(lambda m=mf: yaml_mod.safe_load(m.read_text(encoding="utf-8")), None)
        if not isinstance(data, dict):
            continue
        source = data.get("source") or {}
        auth = data.get("auth") or {}
        out.append({
            "kind": "connector",
            "id": str(data.get("id", mf.parent.name)),
            "system": str(data.get("system", "")),
            "access_mode": str(data.get("access_mode", "")),
            "source_path": str((source.get("path") if isinstance(source, dict) else "") or ""),
            "auth_vars": [str(v) for v in (auth.get("vars") or [])] if isinstance(auth, dict) else [],
        })
    return out


def _portability(root: Path, cfg: dict) -> dict:
    """Everything migration cares about: is the repo itself relocatable, and
    what on THIS machine must be re-wired after a move."""
    # read-only reuse of the lint enforcer (the panel cites it, never re-judges)
    lint = _import("oracle_lint")
    violations: list = []
    if lint is not None:
        excl = _safe(lambda: lint._scan_exclude_predicate(cfg), None)
        _safe(lambda: lint.check_external_paths(root, violations, exclude=excl), None)
    ext_paths = [
        {"path": getattr(v, "path", "?"), "line": getattr(v, "line", None)}
        for v in violations
    ]
    am = _import("actions")
    dream = _safe(lambda: dict(am.Autonomy.load(root).dream), {}) if am else {}
    binaries = _binary_couplings(cfg, dream)
    connectors = _connector_couplings(root)
    secrets_present = (root / ".env.nosync").is_file()
    auth_vars_needed = sorted({v for c in connectors for v in c["auth_vars"]})
    return {
        "environment": {
            "root": str(root.resolve()),
            "hostname": _safe(platform.node, "?"),
            "platform": _safe(platform.platform, sys.platform),
            "python": platform.python_version(),
        },
        "external_paths": ext_paths,
        "scheduler": _scheduler_coupling(root, cfg),
        "binaries": binaries,
        "connectors": connectors,
        "secrets": {
            "env_file_present": secrets_present,
            "auth_vars_needed": auth_vars_needed,
        },
    }


# --------------------------------------------------------------------------- #
# gather -- one defensive read of every subsystem
# --------------------------------------------------------------------------- #
def _gather(root: Path, t: datetime) -> dict:
    cfg = _cfg(root)
    om = _import("oracle_status")
    st = _safe(lambda: om.status(root, t), {}) if om else {}
    am = _import("actions")
    auto = _safe(lambda: am.status(root), {}) if am else {}
    lm = _import("loops")
    loop_list = _safe(lambda: lm.list_loops(root), []) if lm else []
    due_ids = _safe(lambda: {d.id for d in lm.due(root, t)}, set()) if lm else set()
    mh = _import("meta_health")
    aged = _safe(lambda: mh.aged_signals(root, now=t), []) if mh else []
    degraded = _safe(lambda: mh.degraded_loops(root), []) if mh else []
    hygiene = _safe(lambda: mh.skill_hygiene(root, now=t), []) if mh else []
    sc = _import("scorecard")
    card = _safe(lambda: sc.latest_scorecard(root), None) if sc else None
    im = _import("improvements")
    imps = _safe(lambda: im.load_all(root), []) if im else []
    rq = _import("review_queue")
    inbox_items = _safe(lambda: rq.build_queue(root, t), []) if rq else []
    return {
        "inbox_items": inbox_items,
        "portability": _safe(lambda: _portability(root, cfg), {}),
        "root": root,
        "now": t,
        "cfg": cfg,
        "status": st or {},
        "autonomy": auto or {},
        "loops": loop_list,
        "due_ids": due_ids,
        "aged_signals": aged,
        "degraded_loops": degraded,
        "skill_hygiene": hygiene,
        "scorecard": card,
        "improvements": imps,
    }


# --------------------------------------------------------------------------- #
# panels -- each returns {key,title,state,headline,lines,controls}
# --------------------------------------------------------------------------- #
def _issue(severity: str, system: str, what: str, fix: str) -> dict:
    return {"severity": severity, "system": system, "issue": what, "fix": fix}


# Inbox kinds the attention panel itemizes itself (with sharper fix commands)
# -- skip them when folding inbox items in, so nothing is listed twice.
_ATTENTION_OWNED_KINDS = ("paused-loop", "aged-signal")
_ATTENTION_INBOX_MAX = 5


def _panel_attention(ctx: dict) -> dict:
    """Everything outstanding that an admin should fix or remedy, ranked
    fix-now first, each with the command that remedies it. Derives entirely
    from current state: fix the cause and the row disappears on re-render."""
    issues: list[dict] = []
    a = ctx["autonomy"]
    if a.get("kill_switch_engaged"):
        kill_file = str(a.get("kill_switch_file") or "Meta.nosync/Autonomy/KILL-SWITCH")
        issues.append(_issue(
            "fix-now", "autonomy", "kill switch engaged — all autonomous action stopped",
            f"investigate why, then: rm \"{kill_file}\"",
        ))
    for lid in ctx["degraded_loops"]:
        issues.append(_issue(
            "fix-now", "loops", f"loop {lid} degraded (repeated failures)",
            f"fix the cause, then: ./oracle loops set-status {lid} active",
        ))
    for loop in ctx["loops"]:
        if loop.status == "paused" and loop.id not in ctx["degraded_loops"]:
            reason = str(loop.get("paused_reason", "") or "no reason recorded")
            issues.append(_issue(
                "should-fix", "loops", f"loop {loop.id} paused: {reason}",
                f"./oracle loops set-status {loop.id} active",
            ))
    for s in ctx["aged_signals"]:
        issues.append(_issue(
            "fix-now" if s.get("critical") else "should-fix",
            "signals",
            f"{s.get('event_kind')} for {s.get('target', '?')} unconsumed "
            f"{s.get('age_days')}d (budget {s.get('budget_days')}d)",
            str(s.get("action", "./oracle loops due")),
        ))
    regressed = [fm for fm in ctx["improvements"] if str(fm.get("status")) == "regressed"]
    for fm in regressed:
        issues.append(_issue(
            "fix-now", "improvements",
            f"improvement {fm.get('id', '?')} REGRESSED its expected signal",
            "roll it back or re-scope: ./oracle improvements adjudicate, then ./oracle review",
        ))
    card = ctx["scorecard"]
    if card and str(card.get("trend")) == "regressing":
        issues.append(_issue(
            "should-fix", "scorecard", "composite value score is regressing",
            "./oracle loops run architecture-retrospective (regression trigger)",
        ))
    for issue in ctx["skill_hygiene"]:
        issues.append(_issue(
            "should-fix", "skills",
            f"skill {issue.get('skill', '?')}: {issue.get('kind', 'hygiene')}",
            str(issue.get("action", "./oracle skills list")),
        ))
    # backup / kernel hygiene (cheap file checks, mirrored from their panels)
    text = _safe(lambda: (ctx["root"] / "BACKUP-RECOVERY.md").read_text(encoding="utf-8"), "")
    stamp = next((ln.split(":", 1)[1].strip() for ln in text.splitlines()
                  if ln.strip().lower().startswith("last_verified_restore:")), "")
    if not stamp or stamp.lower().startswith("never"):
        issues.append(_issue(
            "should-fix", "backup", "recoverability never proven by a restore round-trip",
            "./oracle backup verify-restore",
        ))
    k = (ctx["cfg"].get("kernel") or {})
    if not (ctx["root"] / str(k.get("manifest", ".kernel-manifest.json"))).is_file():
        issues.append(_issue(
            "should-fix", "kernel", "tool-layer hash manifest missing — upgrade integrity unverifiable",
            "./oracle check",
        ))
    # portability: a stale scheduler is the worst silent migration failure
    port = ctx.get("portability") or {}
    sched = port.get("scheduler") or {}
    if sched.get("stale_root"):
        # NOTE: no embedded path here -- attention issues flow into published
        # HTML; the old root is visible in the portability panel's session view
        issues.append(_issue(
            "fix-now", "portability",
            "installed scheduler points at a previous machine's root "
            "— headless loops are silently dead",
            "re-render for this machine: scheduler/install_schedule.sh --enable",
        ))
    for v in (port.get("external_paths") or [])[:5]:
        loc = f"{v['path']}" + (f":{v['line']}" if v.get("line") else "")
        issues.append(_issue(
            "should-fix", "portability",
            f"hardcoded external path in repo at {loc} — breaks migration",
            "relocate it (connectorize or ingest), then verify: ./oracle lint",
        ))
    for b in (port.get("binaries") or []):
        if not b.get("found"):
            issues.append(_issue(
                "should-fix", "portability",
                f"host binary `{b['command']}` ({b['name']}) not found on this machine",
                "install it on this machine or update the config that names it",
            ))
    secrets = port.get("secrets") or {}
    if (secrets.get("auth_vars_needed") and not secrets.get("env_file_present")):
        issues.append(_issue(
            "should-fix", "portability",
            ".env.nosync absent but connectors declare credential vars: "
            + ", ".join(secrets["auth_vars_needed"]),
            "provision .env.nosync on this machine (template: .env.example)",
        ))
    # everything else waiting on a decision (the Review Inbox), deduped
    folded = [i for i in ctx["inbox_items"] if i.get("kind") not in _ATTENTION_OWNED_KINDS]
    for it in folded[:_ATTENTION_INBOX_MAX]:
        sev = "fix-now" if it.get("kind") == "contradiction" else "should-fix"
        issues.append(_issue(sev, f"inbox/{it.get('kind', '?')}",
                             str(it.get("title", "")), str(it.get("action", "./oracle review"))))
    if len(folded) > _ATTENTION_INBOX_MAX:
        issues.append(_issue(
            "should-fix", "inbox",
            f"...and {len(folded) - _ATTENTION_INBOX_MAX} more item(s) pending",
            "./oracle review --all",
        ))

    issues.sort(key=lambda i: i["severity"] != "fix-now")
    n_now = sum(1 for i in issues if i["severity"] == "fix-now")
    state = "attention" if n_now else ("warn" if issues else "ok")
    headline = (
        f"{n_now} to fix now · {len(issues) - n_now} should fix"
        if issues else "all clear — nothing outstanding"
    )
    return {"key": "attention", "title": "Needs attention", "state": state,
            "headline": headline, "lines": [], "issues": issues, "controls": []}


def _panel_memory(ctx: dict) -> dict:
    m = (ctx["status"].get("memory") or {})
    src = int(m.get("sources", 0))
    headline = (
        f"{src} sources · {m.get('findings', 0)} findings · "
        f"{m.get('models', 0)} models · {m.get('questions', 0)} questions · "
        f"{m.get('contradictions', 0)} contradictions"
    )
    return {
        "key": "memory",
        "title": "Memory",
        "state": "ok" if src else "off",
        "headline": headline if src else "no evidence ingested yet — ./oracle ingest <paths...>",
        "lines": [],
        "controls": [],
    }


def _panel_authority(ctx: dict) -> dict:
    a = (ctx["status"].get("authority") or {})
    rows, confirmed, promotable = (
        int(a.get("rows", 0)), int(a.get("confirmed", 0)), int(a.get("promotable", 0)),
    )
    if rows == 0:
        state, headline = "off", "truth map empty — rows auto-propose on ingest"
    elif confirmed == 0:
        state, headline = "warn", f"0/{rows} rows confirmed — answers cap at supported (exit 2)"
    else:
        state = "ok"
        headline = f"{confirmed}/{rows} rows confirmed"
    lines = []
    if promotable:
        lines.append(
            f"{promotable} row(s) promotable now: "
            "`./oracle admin truth promote --object \"<bo>\" --actor <admin>`"
        )
    return {"key": "authority", "title": "Truth authority", "state": state,
            "headline": headline, "lines": lines, "controls": []}


def _panel_index(ctx: dict) -> dict:
    db = ctx["root"] / "_data.nosync" / "index" / "knowledge.db"
    present = db.is_file()
    return {
        "key": "index",
        "title": "Knowledge index",
        "state": "ok" if present else "off",
        "headline": "built (rebuildable derived data)" if present
        else "not built yet — builds on first `./oracle index build` / ingest",
        "lines": [],
        "controls": [],
    }


def _panel_inbox(ctx: dict) -> dict:
    inbox = (ctx["status"].get("review_inbox") or {})
    total = int(inbox.get("total", 0))
    by_kind = inbox.get("by_kind") or {}
    most = inbox.get("most_urgent") or {}
    kinds = " · ".join(f"{k}:{v}" for k, v in sorted(by_kind.items())) or ""
    lines = []
    if most:
        lines.append(f"top item [{most.get('kind', '?')}]: {most.get('action', '')}")
    return {
        "key": "inbox",
        "title": "Review inbox",
        "state": "warn" if total else "ok",
        "headline": f"{total} item(s) pending" + (f" — {kinds}" if kinds else "")
        if total else "empty — nothing waiting on a decision",
        "lines": lines,
        "controls": [],
    }


def _loop_state(loop, due_ids: set, degraded: list) -> str:
    if loop.id in degraded:
        return "attention"
    if loop.status == "paused":
        return "off"
    if loop.status != "active":
        return "off"
    return "warn" if loop.id in due_ids else "ok"


def _panel_loops(ctx: dict) -> dict:
    loops, due_ids, degraded = ctx["loops"], ctx["due_ids"], ctx["degraded_loops"]
    active = [l for l in loops if l.status == "active"]
    paused = [l for l in loops if l.status == "paused"]
    rows = []
    controls = []
    for loop in loops:
        if loop.status in ("retired",):
            continue
        state = _loop_state(loop, due_ids, degraded)
        note = ""
        if loop.id in degraded:
            note = "degraded (repeated failures)"
        elif loop.status == "paused":
            note = f"paused: {loop.get('paused_reason', '') or 'no reason recorded'}"
        elif loop.id in due_ids:
            note = "due now"
        rows.append({
            "state": state,
            "id": loop.id,
            "status": loop.status,
            "cadence": loop.cadence or "-",
            "last_run": _dt_day(loop.frontmatter.get("last_run")),
            "next_review": _dt_day(loop.frontmatter.get("next_review")),
            "note": note,
        })
        flip = "active" if loop.status == "paused" else "paused"
        controls.append({
            "control": f"loop {loop.id}",
            "state": "on" if loop.status == "active" else loop.status,
            "command": f"./oracle loops set-status {loop.id} {flip}"
            + (" --reason \"<why>\"" if flip == "paused" else ""),
        })
    n_due = sum(1 for r in rows if r["note"] == "due now")
    state = "attention" if degraded else ("warn" if (paused or n_due) else "ok")
    headline = (
        f"{len(active)} active · {n_due} due · {len(paused)} paused"
        + (f" · {len(degraded)} degraded" if degraded else "")
    )
    return {"key": "loops", "title": "Loops", "state": state, "headline": headline,
            "lines": [], "rows": rows, "controls": controls}


def _panel_autonomy(ctx: dict) -> dict:
    a = ctx["autonomy"]
    enabled = bool(a.get("enabled"))
    level = int(a.get("level", 0) or 0)
    kill = bool(a.get("kill_switch_engaged"))
    caps = a.get("blast_radius_caps") or {}
    eff = a.get("effective_allowed_loops") or []
    lanes = a.get("writable_lanes") or []
    conns = a.get("readonly_connectors") or []
    proposal = a.get("pending_proposal")

    if kill:
        state = "attention"
        headline = "KILL SWITCH ENGAGED — all autonomous action hard-stopped"
    elif enabled and level > 0:
        state = "ok"
        headline = f"ON — level {level}/3"
    else:
        state = "off"
        headline = "OFF (spawn default) — nothing runs headless"

    ladder = "".join("■" if i < level else "□" for i in range(3))
    lines = [
        f"ladder [{ladder}] 0 none · 1 deterministic loops · 2 +dream · 3 +auto-apply",
        f"headless-allowed loops: {len(eff)} · writable lanes: {len(lanes)} · "
        f"read-only connectors: {len(conns)}",
        f"blast caps: {caps.get('max_files_per_run', 0)} files / {caps.get('max_bytes', 0)} bytes per run",
    ]
    if proposal:
        lines.append("pending promotion proposal — review with `./oracle admin autonomy readiness`")

    kill_file = str(a.get("kill_switch_file") or "Meta.nosync/Autonomy/KILL-SWITCH")
    controls = [
        {"control": "autonomy master + level", "state": f"{'on' if enabled else 'off'} (level {level})",
         "command": "./oracle admin autonomy promote --actor \"<admin>\"  (demote to lower)"},
        {"control": "kill switch", "state": "ENGAGED" if kill else "clear",
         "command": (f"rm \"{kill_file}\"" if kill else f"touch \"{kill_file}\"")},
    ]
    return {"key": "autonomy", "title": "Autonomy", "state": state, "headline": headline,
            "lines": lines, "controls": controls}


def _panel_signals(ctx: dict) -> dict:
    aged, hygiene = ctx["aged_signals"], ctx["skill_hygiene"]
    critical = [s for s in aged if s.get("critical")]
    if critical:
        state = "attention"
    elif aged or hygiene:
        state = "warn"
    else:
        state = "ok"
    headline = (
        f"{len(aged)} aged signal(s) ({len(critical)} critical) · "
        f"{len(hygiene)} skill-hygiene issue(s)"
        if (aged or hygiene) else "no signal ages silently — all consumed within budget"
    )
    lines = []
    for s in aged[:3]:
        lines.append(
            f"[{s.get('event_kind')}] {s.get('target', '')} — {s.get('age_days')}d old "
            f"(budget {s.get('budget_days')}d)"
        )
    return {"key": "signals", "title": "Signal health", "state": state,
            "headline": headline, "lines": lines, "controls": []}


def _panel_improvements(ctx: dict) -> dict:
    counts: dict[str, int] = {}
    for fm in ctx["improvements"]:
        s = str(fm.get("status", "proposed"))
        counts[s] = counts.get(s, 0) + 1
    total = sum(counts.values())
    if counts.get("regressed"):
        state = "attention"
    elif counts.get("proposed") or counts.get("needs_review"):
        state = "warn"
    elif total:
        state = "ok"
    else:
        state = "off"
    headline = (
        " · ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
        if total else "no improvements recorded yet"
    )
    return {"key": "improvements", "title": "Improvements", "state": state,
            "headline": headline, "lines": [], "controls": []}


def _panel_scorecard(ctx: dict) -> dict:
    card = ctx["scorecard"]
    if not card:
        return {"key": "scorecard", "title": "Value scorecard", "state": "off",
                "headline": "none yet — the value-scorecard loop generates monthly",
                "lines": [], "controls": []}
    trend = str(card.get("trend", "") or "baseline")
    glyph = _TREND_GLYPH.get(trend, "·")
    composite = card.get("composite")
    headline = f"composite {composite} · trend {glyph} {trend}"
    state = "attention" if trend == "regressing" else "ok"
    return {"key": "scorecard", "title": "Value scorecard", "state": state,
            "headline": headline, "lines": [], "controls": []}


def _panel_connectors(ctx: dict) -> dict:
    cfg = ctx["cfg"]
    known = (cfg.get("connectors") or {}).get("known") or []
    if isinstance(known, dict):
        known = list(known)
    manifests = _safe(
        lambda: sorted(p.name for p in (ctx["root"] / "Connectors").glob("*.manifest.yaml")
                       if not p.name.startswith("connector-template")),
        [],
    )
    n = max(len(known) if isinstance(known, list) else 0, len(manifests))
    return {
        "key": "connectors",
        "title": "Connectors",
        "state": "ok" if n else "off",
        "headline": f"{n} installed" if n else "none installed — ./oracle admin connector (admin)",
        "lines": [f"manifests: {', '.join(manifests)}"] if manifests else [],
        "controls": [],
    }


def _panel_derived(ctx: dict) -> dict:
    engines = ((ctx["cfg"].get("derived_memory") or {}).get("engines") or {})
    controls = []
    on = []
    for name, spec in (engines.items() if isinstance(engines, dict) else []):
        enabled = bool(isinstance(spec, dict) and str(spec.get("enabled")).lower() in ("true", "yes", "1", "on"))
        if enabled:
            on.append(name)
        controls.append({
            "control": f"derived engine {name}",
            "state": "on" if enabled else "off",
            "command": f"edit oracle.yml derived_memory.engines.{name}.enabled (admin) · "
            "verify: ./oracle derived-memory check",
        })
    n = len(engines) if isinstance(engines, dict) else 0
    return {
        "key": "derived",
        "title": "Derived memory",
        "state": "ok" if on else "off",
        "headline": f"{len(on)}/{n} engine(s) enabled" + (f": {', '.join(on)}" if on else ""),
        "lines": [],
        "controls": controls,
    }


def _panel_kernel(ctx: dict) -> dict:
    k = (ctx["cfg"].get("kernel") or {})
    version = str(k.get("tools_version", "?"))
    sha = str(k.get("tools_sha256", ""))[:12]
    manifest = (ctx["root"] / str(k.get("manifest", ".kernel-manifest.json"))).is_file()
    return {
        "key": "kernel",
        "title": "Kernel",
        "state": "ok" if manifest else "warn",
        "headline": f"tools {version} ({sha or 'unstamped'}) · "
        + ("manifest present" if manifest else "manifest MISSING — upgrade integrity unverifiable"),
        "lines": ["verify everything: `./oracle check`"],
        "controls": [],
    }


def _panel_portability(ctx: dict) -> dict:
    """Migration view: machine-local facts (shown, never stored in the repo)
    plus every external coupling that must be re-wired after a move."""
    port = ctx.get("portability") or {}
    env = port.get("environment") or {}
    ext = port.get("external_paths") or []
    sched = port.get("scheduler") or {}
    binaries = port.get("binaries") or []
    connectors = port.get("connectors") or []
    secrets = port.get("secrets") or {}

    missing_bins = [b for b in binaries if not b.get("found")]
    vars_needed = secrets.get("auth_vars_needed") or []
    secrets_gap = bool(vars_needed) and not secrets.get("env_file_present")

    if sched.get("stale_root"):
        state = "attention"
    elif ext or missing_bins or secrets_gap:
        state = "warn"
    else:
        state = "ok"

    n_couplings = (1 if sched.get("installed") else 0) + len(binaries) + len(connectors) \
        + (1 if (secrets.get("env_file_present") or vars_needed) else 0)
    headline = (
        ("repo relocatable — 0 hardcoded external paths" if not ext
         else f"{len(ext)} hardcoded external path(s) IN repo — breaks migration")
        + f" · {n_couplings} machine coupling(s)"
    )

    # session_lines may carry MACHINE-LOCAL ABSOLUTE PATHS: rendered in the
    # terminal/JSON only, NEVER into published HTML -- the external-path lint
    # scans dashboards.nosync, and persisting this machine's home-anchored
    # root into a render would (correctly) turn the relocatability gate red.
    session_lines = [
        f"this machine (shown, never stored): root {env.get('root', '?')}",
    ]
    if sched.get("stale_root") and sched.get("embedded_root"):
        session_lines.append(f"scheduler's embedded root (previous machine): {sched.get('embedded_root')}")

    lines = [
        f"host {env.get('hostname', '?')} · {env.get('platform', '?')} · python {env.get('python', '?')}",
    ]
    for v in ext[:3]:
        loc = f"{v['path']}" + (f":{v['line']}" if v.get("line") else "")
        lines.append(f"hardcoded path at {loc} — relocate it (connectorize/ingest), then `./oracle lint`")
    plist_name = Path(str(sched.get("plist") or "scheduler.plist")).name
    if sched.get("installed"):
        if sched.get("stale_root"):
            lines.append(
                "scheduler INSTALLED but points at a previous machine's root "
                "— re-run scheduler/install_schedule.sh --enable"
            )
        else:
            lines.append(f"scheduler installed for this root (~/Library/LaunchAgents/{plist_name})")
    else:
        lines.append("scheduler not installed on this machine (headless loops idle) — scheduler/install_schedule.sh")
    for b in binaries:
        lines.append(
            f"host binary `{b['command']}` ({b['name']}): "
            + ("found on PATH" if b.get("found") else "NOT FOUND on this machine")
        )
    if secrets.get("env_file_present"):
        lines.append(".env.nosync present — migrate it out-of-band (never via git/backup tier 3)")
    elif vars_needed:
        lines.append(
            f".env.nosync ABSENT but connectors need: {', '.join(vars_needed)} — provision from .env.example"
        )
    for c in connectors:
        target = c.get("source_path") or c.get("system") or "?"
        lines.append(f"connector {c['id']} ({c.get('access_mode', '?')}) → {target}")

    return {"key": "portability", "title": "Portability & migration", "state": state,
            "headline": headline, "lines": lines, "session_lines": session_lines,
            "controls": [], "data": port}


def _panel_backup(ctx: dict) -> dict:
    text = _safe(lambda: (ctx["root"] / "BACKUP-RECOVERY.md").read_text(encoding="utf-8"), "")
    stamp = ""
    for line in text.splitlines():
        if line.strip().lower().startswith("last_verified_restore:"):
            stamp = line.split(":", 1)[1].strip()
            break
    proven = bool(stamp) and not stamp.lower().startswith("never")
    return {
        "key": "backup",
        "title": "Backup",
        "state": "ok" if proven else "warn",
        "headline": f"last verified restore: {stamp}" if proven
        else "recoverability never proven — `./oracle backup verify-restore`",
        "lines": [],
        "controls": [],
    }


PANELS: dict[str, Callable[[dict], dict]] = {
    "attention": _panel_attention,
    "memory": _panel_memory,
    "authority": _panel_authority,
    "index": _panel_index,
    "inbox": _panel_inbox,
    "loops": _panel_loops,
    "autonomy": _panel_autonomy,
    "signals": _panel_signals,
    "improvements": _panel_improvements,
    "scorecard": _panel_scorecard,
    "connectors": _panel_connectors,
    "derived": _panel_derived,
    "kernel": _panel_kernel,
    "portability": _panel_portability,
    "backup": _panel_backup,
}


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def build(root, now: Optional[datetime] = None) -> dict:
    """Assemble the full dashboard state. Read-only."""
    root = Path(root)
    t = _now(now)
    ctx = _gather(root, t)
    layout = read_layout(root)
    panels = []
    for key in layout["order"]:
        if key in layout["hidden"]:
            continue
        panel = _safe(lambda k=key: PANELS[k](ctx), None)
        if panel is None:
            panel = {"key": key, "title": key, "state": "warn",
                     "headline": "panel failed to render", "lines": [], "controls": []}
        panels.append(panel)
    controls = [c for p in panels for c in p.get("controls", [])]
    company = (ctx["cfg"].get("company") or {})
    return {
        "generated": t.isoformat(),
        "company": str(company.get("name", "")),
        "codename": str(company.get("codename", "")),
        "maturity": ctx["status"].get("maturity") or {},
        "layout_source": layout["source"],
        "panels": panels,
        "controls": controls,
        "suggested_next": ctx["status"].get("suggested_next") or [],
    }


# --------------------------------------------------------------------------- #
# renderers
# --------------------------------------------------------------------------- #
def render_md(d: dict) -> str:
    m = d.get("maturity") or {}
    title = d.get("company") or "Oracle"
    code = f" ({d['codename']})" if d.get("codename") else ""
    lines = [
        f"# {title}{code} — admin dashboard",
        "",
        f"_{d['generated']} · maturity rung {m.get('rung', '?')}: {m.get('label', 'unknown')}"
        + (f" · layout: {d['layout_source']}" if d.get("layout_source") != "default" else "")
        + "_",
        "",
        "## Systems",
        "",
        "| | system | status |",
        "|---|---|---|",
    ]
    for p in d["panels"]:
        lines.append(f"| {_GLYPH.get(p['state'], '?')} | {p['title']} | {p['headline']} |")
    lines.append("")

    for p in d["panels"]:
        # session_lines may carry machine-local absolute paths; the terminal
        # render is ephemeral, so they are shown here but never in render_html
        extra = (p.get("session_lines") or []) + (p.get("lines") or [])
        rows = p.get("rows") or []
        issues = p.get("issues") or []
        if not extra and not rows and not issues:
            continue
        lines += [f"## {p['title']}", ""]
        if issues:  # the attention list: severity-ranked, each with its remedy
            for i, it in enumerate(issues, 1):
                glyph = _GLYPH["attention"] if it["severity"] == "fix-now" else _GLYPH["warn"]
                lines.append(f"{i}. {glyph} **[{it['system']}]** {it['issue']}")
                lines.append(f"   - fix: `{it['fix']}`")
        if rows:  # the loops table
            lines += [
                "| | loop | cadence | last run | next review | note |",
                "|---|---|---|---|---|---|",
            ]
            for r in rows:
                lines.append(
                    f"| {_GLYPH.get(r['state'], '?')} | {r['id']} | {r['cadence']} "
                    f"| {r['last_run']} | {r['next_review']} | {r['note']} |"
                )
            lines += ["", "Toggle any loop: `./oracle loops set-status <id> active|paused --reason \"<why>\"`"]
        for ln in extra:
            lines.append(f"- {ln}")
        lines.append("")

    if d.get("controls"):
        lines += ["## Controls — flip any toggle with one command", "",
                  "| toggle | state | command |", "|---|---|---|"]
        for c in d["controls"]:
            lines.append(f"| {c['control']} | {c['state']} | `{c['command']}` |")
        lines.append("")

    if d.get("suggested_next"):
        lines += ["## Do next", ""]
        for s in d["suggested_next"]:
            lines.append(f"- {s}")
        lines.append("")
    lines.append(
        "_Read-only render of current state — regenerate with `./oracle dashboard`; "
        "layout evolves via dashboards.nosync/layout.yml (see `dashboard panels`)._"
    )
    return "\n".join(lines)


_HTML_STATE_COLOR = {"ok": "#34d399", "warn": "#fbbf24", "off": "#64748b", "attention": "#f87171"}


def render_html(d: dict) -> str:
    """Self-contained dark-theme HTML (no external assets)."""
    e = _html.escape
    m = d.get("maturity") or {}

    def pill(state: str, text: str = "") -> str:
        color = _HTML_STATE_COLOR.get(state, "#64748b")
        label = e(text or state)
        return (
            f'<span class="pill" style="--c:{color}">'
            f'<span class="dot"></span>{label}</span>'
        )

    cards = []
    for p in d["panels"]:
        body = "".join(f"<div class='line'>{e(ln)}</div>" for ln in (p.get("lines") or []))
        if p.get("issues"):
            trs = "".join(
                f"<tr><td>{pill('attention' if i['severity'] == 'fix-now' else 'warn', i['severity'])}</td>"
                f"<td>{e(i['system'])}</td><td>{e(i['issue'])}</td>"
                f"<td><code>{e(i['fix'])}</code></td></tr>"
                for i in p["issues"]
            )
            body += (
                "<table><thead><tr><th></th><th>system</th><th>issue</th>"
                "<th>fix</th></tr></thead>"
                f"<tbody>{trs}</tbody></table>"
            )
        if p.get("rows"):
            trs = "".join(
                f"<tr><td>{pill(r['state'], r['state'])}</td><td class='mono'>{e(r['id'])}</td>"
                f"<td>{e(r['cadence'])}</td><td>{e(r['last_run'])}</td>"
                f"<td>{e(r['next_review'])}</td><td>{e(r['note'])}</td></tr>"
                for r in p["rows"]
            )
            body += (
                "<table><thead><tr><th></th><th>loop</th><th>cadence</th>"
                "<th>last run</th><th>next review</th><th>note</th></tr></thead>"
                f"<tbody>{trs}</tbody></table>"
            )
        cards.append(
            f"<section class='card{' wide' if (p.get('rows') or p.get('issues')) else ''}'>"
            f"<header><h2>{e(p['title'])}</h2>{pill(p['state'])}</header>"
            f"<p class='headline'>{e(p['headline'])}</p>{body}</section>"
        )

    controls = "".join(
        f"<tr><td>{e(c['control'])}</td><td>{e(str(c['state']))}</td>"
        f"<td><code>{e(c['command'])}</code></td></tr>"
        for c in d.get("controls", [])
    )
    next_items = "".join(f"<li>{e(s)}</li>" for s in d.get("suggested_next", []))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(d.get('company') or 'Oracle')} — admin dashboard</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 2rem; background: #0b1020; color: #e2e8f0;
         font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.5rem; letter-spacing: .02em; }}
  .meta {{ color: #94a3b8; margin-bottom: 1.5rem; }}
  .grid {{ display: grid; gap: 1rem; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }}
  .card {{ background: #111831; border: 1px solid #1f2a4d; border-radius: 12px; padding: 1rem 1.1rem; }}
  .card.wide {{ grid-column: 1 / -1; }}
  .card header {{ display: flex; align-items: center; justify-content: space-between; gap: .5rem; }}
  .card h2 {{ margin: 0; font-size: .95rem; text-transform: uppercase;
              letter-spacing: .08em; color: #93c5fd; }}
  .headline {{ margin: .5rem 0 .25rem; font-weight: 600; }}
  .line {{ color: #94a3b8; font-size: .9rem; margin-top: .25rem; }}
  .pill {{ display: inline-flex; align-items: center; gap: .4ch; font-size: .75rem;
           padding: .15rem .6rem; border-radius: 999px; border: 1px solid var(--c);
           color: var(--c); white-space: nowrap; }}
  .pill .dot {{ width: .5em; height: .5em; border-radius: 50%; background: var(--c); }}
  table {{ width: 100%; border-collapse: collapse; margin-top: .75rem; font-size: .9rem; }}
  th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #1f2a4d; }}
  th {{ color: #94a3b8; font-weight: 500; }}
  code, .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85em;
                 background: #0b1020; padding: .1rem .35rem; border-radius: 6px; }}
  ul {{ margin: .5rem 0 0; padding-left: 1.2rem; }}
</style></head><body>
<h1>{e(d.get('company') or 'Oracle')}{e(' (' + d['codename'] + ')' if d.get('codename') else '')} — admin dashboard</h1>
<div class="meta">{e(d['generated'])} · maturity rung {e(str(m.get('rung', '?')))}: {e(str(m.get('label', 'unknown')))}</div>
<div class="grid">
{''.join(cards)}
<section class="card wide"><header><h2>Controls</h2></header>
<table><thead><tr><th>toggle</th><th>state</th><th>command</th></tr></thead>
<tbody>{controls or '<tr><td colspan=3>none</td></tr>'}</tbody></table></section>
<section class="card wide"><header><h2>Do next</h2></header>
<ul>{next_items or '<li>nothing pending</li>'}</ul></section>
</div>
<div class="meta" style="margin-top:1.5rem">Read-only render — regenerate with
<code>./oracle dashboard publish</code>. A rendering, not a record (dashboards.nosync discipline).</div>
</body></html>
"""


def publish(root, *, out_name: str = DEFAULT_HTML_NAME, now: Optional[datetime] = None) -> dict:
    """Render the dashboard to a self-contained HTML file in dashboards.nosync/."""
    root = Path(root)
    d = build(root, now)
    dst = safe_paths.contain(root, out_name, base=DASH_BASE)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(render_html(d), encoding="utf-8")  # contained path (safe_paths)
    return {"published": str(dst), "panels": len(d["panels"]), "generated": d["generated"]}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dashboard",
        description="Admin systems dashboard: subsystem health + selection toggles.",
    )
    ap.add_argument("--root", default=".", help="oracle root")
    sub = ap.add_subparsers(dest="cmd", required=False)
    p_show = sub.add_parser("show", help="render the dashboard (default)")
    p_show.add_argument("--json", action="store_true")
    p_controls = sub.add_parser("controls", help="just the toggle table")
    p_controls.add_argument("--json", action="store_true")
    sub.add_parser("panels", help="list panel registry + layout overlay")
    p_pub = sub.add_parser("publish", help="write self-contained HTML into dashboards.nosync/")
    p_pub.add_argument("--out", default=DEFAULT_HTML_NAME, help="output file name")

    args = ap.parse_args(argv)
    root = Path(args.root)
    cmd = args.cmd or "show"

    if cmd == "show":
        d = build(root)
        print(json.dumps(d, indent=2, default=str) if getattr(args, "json", False) else render_md(d))
        return 0
    if cmd == "controls":
        d = build(root)
        if getattr(args, "json", False):
            print(json.dumps(d["controls"], indent=2, default=str))
        else:
            for c in d["controls"]:
                print(f"{c['control']:<40} {str(c['state']):<18} {c['command']}")
        return 0
    if cmd == "panels":
        layout = read_layout(root)
        for key in layout["order"]:
            flag = " (hidden)" if key in layout["hidden"] else ""
            print(f"{key}{flag}")
        print(f"layout source: {layout['source']}")
        return 0
    if cmd == "publish":
        try:
            result = publish(root, out_name=args.out)
        except ValueError as exc:
            print(f"publish refused: {exc}")
            return 1
        print(json.dumps(result, indent=2))
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
