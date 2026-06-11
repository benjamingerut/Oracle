"""curator.py -- the operating-agent curator on the LOCAL attended surface (P5-T7b).

The curator works the kernel Review Inbox (``review_queue.py``'s ranked items)
from the local attended surface (``oracle curate``). It LISTS the ranked queue,
PREPARES a resolution for each item, and APPLIES it through EXISTING kernel verbs
ONLY (I2) -- with per-item human confirmation.

Three load-bearing disciplines (P5S-6, the A9 "subcommands pinned in code"
pattern):

  1. **Fixed kind -> verb mapping, value slots only.** The curator NEVER executes
     a queue item's free-text ``action`` string -- that string derives from
     ingested/contradiction/finding content and is UNTRUSTED. Instead each item
     *kind* maps to an allowlisted kernel verb PINNED in this module, and item
     fields fill VALUE SLOTS only. A poisoned ``action`` that smuggles
     ``; rm -rf /`` is simply never read.

  2. **Control-plane stays Admin-interface-only.** Kinds whose resolution is a
     control-plane act (truth promotion, autonomy promotion, authority wiring,
     contradiction adjudication) map to ``CONTROL_PLANE``: the curator PREPARES
     guidance but NEVER applies -- those remain admin approval flows, untouched.

  3. **Autonomy-gated apply, ledgered attribution.** Below the required autonomy
     level the curator prepares but refuses to apply. Every applied action runs
     under the resolving Principal's ``--actor``/``--role`` and is recorded to a
     ``curator_event`` ledger row naming the human (the ``local_user:<id>`` form,
     P5S-11). Apply verbs that are themselves autonomy-gated (e.g.
     ``loops run --headless``) keep their kernel gate -- the curator's own check
     is an additional fail-closed guard, not a replacement.

**Named residual (accepted, SECURITY.md):** a poisoned queue item can still
*steer* USER-tier writes by the curator -- bounded by the fail-closed
``ingest_roots`` allowlist, ``status: needs_review`` on everything derived, and
the ``policy.require_role`` control-plane gate that holds regardless of item
content.

Stdlib only.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import config
from .agentloop.verbtools import run_verb

# Curator apply requires at least level-1 autonomy (the deterministic-loops rung):
# the only auto-applyable resolutions are gated loop runs. Below this the curator
# prepares but never applies (acceptance: "with autonomy below the gate, apply is
# refused and prepare still works").
CURATOR_MIN_LEVEL = 1

CURATOR_LEDGER_REL = "Meta.nosync/ledgers/curator_event.jsonl"


# --------------------------------------------------------------------------- #
# the fixed kind -> resolution mapping (A9 discipline; value slots only)
# --------------------------------------------------------------------------- #
# Disposition sentinels for kinds with no auto-apply verb.
PREPARE_ONLY = "prepare-only"      # human/agent judgement; no single safe verb
CONTROL_PLANE = "control-plane"    # admin-interface-only; curator never applies


@dataclass(frozen=True)
class Pinned:
    """A pinned kernel-verb resolution: argv built from STRUCTURED fields only.

    ``build_argv`` receives the queue item AND a resolution context (the live,
    kernel-owned ``loops due`` ids -- structured data, never the item's free
    text) and returns the FULL argv for ``run_verb`` (subcommand pinned here,
    item/context values in value slots only) or ``None`` when no slot can be
    filled. ``label`` is shown to the operator before confirmation.
    """
    label: str
    build_argv: Callable[[dict, dict, "Principal"], Optional[list]]
    gated: bool = True  # the verb itself applies the autonomy gate (--headless)


@dataclass(frozen=True)
class Principal:
    """The resolving local-surface human (P5S-11: ``local_user:<id>``).

    Identity.py (P5-T2) is the eventual home of full Principal resolution; until
    it lands the curator resolves the local attended principal directly from the
    instance's bootstrap admin, on the local surface where admin role is honored.
    """
    user_id: str
    role: str

    @property
    def actor(self) -> str:
        return f"local_user:{self.user_id}"


def _argv_loop_run(item: dict, ctx: dict, principal: "Principal") -> Optional[list]:
    """Build ``loops run <loop_id> --headless`` for a loop-backed queue item.

    The loop id is taken from the live, kernel-owned ``loops due`` listing
    (``ctx['due_ids']`` -- STRUCTURED data), NEVER parsed from the item's
    free-text title/action. We apply to every currently-due loop the kernel
    reports; ``--headless`` keeps the kernel's own autonomy gate engaged.
    """
    due = ctx.get("due_ids") or []
    if not due:
        return None
    # One loop per planned argv; the driver fans out over due_ids itself, so
    # here we return the template for a single id chosen by the driver.
    lid = ctx.get("_loop_id")
    if not lid or lid not in due:
        return None
    return ["loops", "run", str(lid), "--headless", "--json"]


# kind -> Pinned | PREPARE_ONLY | CONTROL_PLANE. EVERY review_queue kind is
# named here so an unmapped kind is impossible (default-deny in plan_item).
KIND_MAP: dict[str, object] = {
    # loop-backed: the only auto-applyable resolutions (gated loop runs).
    "unconsumed-events": Pinned(
        "run the consuming loop (loops run <id> --headless)", _argv_loop_run),
    "aged-signal": Pinned(
        "run the consuming loop (loops run <id> --headless)", _argv_loop_run),
    # control-plane: admin-interface-only; curator never applies.
    "contradiction": CONTROL_PLANE,
    "promotable-row": CONTROL_PLANE,
    "authority-candidate": CONTROL_PLANE,
    "autonomy": CONTROL_PLANE,
    # human/agent judgement; no single deterministic safe verb -> prepare only.
    "paused-loop": PREPARE_ONLY,
    "needs-ocr": PREPARE_ONLY,
    "needs-review-finding": PREPARE_ONLY,
    "needs-review-query": PREPARE_ONLY,
    "stale-question": PREPARE_ONLY,
    "stale-model": PREPARE_ONLY,
    "stale-improvement": PREPARE_ONLY,
}


# --------------------------------------------------------------------------- #
# principal resolution (local attended surface)
# --------------------------------------------------------------------------- #
def local_principal(root: Path) -> Principal:
    """Resolve the local attended principal (P5S-11 ``local_user:<id>`` form).

    The local surface honors the admin role for the human at the keyboard. We
    read the bootstrap admin name from oracle.yml (text scan, stdlib only) as the
    stable id; role is ``admin`` on the local control surface.
    """
    name = "operator"
    p = Path(root) / "oracle.yml"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return Principal(user_id=name, role="admin")
    in_admin = False
    for raw in lines:
        s = raw.strip()
        if s.startswith("bootstrap_admin:"):
            in_admin = True
            continue
        if in_admin:
            if s.startswith("name:"):
                got = s.split(":", 1)[1].strip().strip('"').strip("'")
                if got:
                    name = got
                break
            if s and not raw.startswith(" "):
                break
    # Slugify so the actor string stays greppable + shell-safe.
    slug = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name) or "operator"
    return Principal(user_id=slug, role="admin")


# --------------------------------------------------------------------------- #
# autonomy level (cheap text scan; the kernel verb is the authoritative gate)
# --------------------------------------------------------------------------- #
def _autonomy_level(root: Path) -> int:
    p = Path(root) / "Meta.nosync" / "Autonomy" / "autonomy.yml"
    if not p.exists():
        return 0
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue
            if line.startswith("level:"):
                try:
                    return int(line.split(":", 1)[1].strip().strip('"').strip("'"))
                except ValueError:
                    return 0
    except OSError:
        return 0
    return 0


# --------------------------------------------------------------------------- #
# queue + plan
# --------------------------------------------------------------------------- #
def list_queue(root: Path, *, limit: int = 0) -> list[dict]:
    """The ranked Review Inbox as JSON via the kernel ``review`` verb (read-only).

    Goes through the kernel CLI (not an in-process import) so the curator sees
    exactly what the operator's ``oracle review`` shows.
    """
    rc, out, _err = run_verb(root, ["review", "list", "--json", "--all"])
    if rc != 0:
        return []
    try:
        items = json.loads(out.strip() or "[]")
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return items[:limit] if limit else items


def _due_ids(root: Path) -> list[str]:
    """Live, kernel-owned due-loop ids (STRUCTURED slot source; never item text)."""
    rc, out, _err = run_verb(root, ["loops", "due", "--json"])
    if rc != 0:
        return []
    try:
        data = json.loads(out.strip() or "[]")
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(d.get("id")) for d in data if isinstance(d, dict) and d.get("id")]


@dataclass
class Plan:
    item: dict
    disposition: str                 # "apply" | PREPARE_ONLY | CONTROL_PLANE | "unmapped"
    label: str = ""
    argvs: list = field(default_factory=list)   # one or more pinned argv lists


def plan_item(root: Path, item: dict, *, due_ids: Optional[list] = None) -> Plan:
    """Map an item's KIND to a pinned resolution. NEVER reads the item action text.

    Returns a :class:`Plan`. For loop-backed kinds the value slot (loop id) comes
    from ``due_ids`` (the live kernel listing). Control-plane and prepare-only
    kinds yield no applyable argv. An unknown kind is default-denied
    (``disposition='unmapped'``) -- it is never applied.
    """
    kind = str(item.get("kind", ""))
    mapped = KIND_MAP.get(kind)
    if mapped is None:
        return Plan(item=item, disposition="unmapped",
                    label=f"unmapped kind {kind!r}: no pinned verb (apply refused)")
    if mapped is CONTROL_PLANE:
        return Plan(item=item, disposition=CONTROL_PLANE,
                    label="control-plane (Admin interface only): curator prepares, "
                          "never applies")
    if mapped is PREPARE_ONLY:
        return Plan(item=item, disposition=PREPARE_ONLY,
                    label="human/agent judgement: no single safe verb to auto-apply")
    assert isinstance(mapped, Pinned)
    due = due_ids if due_ids is not None else _due_ids(root)
    argvs: list = []
    if mapped.build_argv is _argv_loop_run:
        # Fan out over every currently-due loop (structured slot source).
        for lid in due:
            ctx = {"due_ids": due, "_loop_id": lid}
            argv = mapped.build_argv(item, ctx, local_principal(root))
            if argv:
                argvs.append(argv)
    else:  # pragma: no cover - reserved for future pinned verbs
        argv = mapped.build_argv(item, {"due_ids": due}, local_principal(root))
        if argv:
            argvs.append(argv)
    if not argvs:
        return Plan(item=item, disposition=PREPARE_ONLY,
                    label="no due loop to run for this item right now (prepare only)")
    return Plan(item=item, disposition="apply", label=mapped.label, argvs=argvs)


# --------------------------------------------------------------------------- #
# apply (autonomy-gated; ledgered with the resolving principal)
# --------------------------------------------------------------------------- #
def _ledger_curator_event(root: Path, *, principal: Principal, item: dict,
                          argv: list, result: str, reason: str = "") -> None:
    """Append a metadata-only ``curator_event`` row naming the resolving human.

    Best-effort: a ledger write failure never blocks the operator. We record the
    item KIND + the pinned verb (never the item's free-text action), the
    resolving ``actor``/``role``, and the outcome.
    """
    import datetime

    row = {
        "kind": "curator_event",
        "item_kind": str(item.get("kind", "")),
        "verb": " ".join(str(a) for a in argv[:2]) if argv else "",
        "result": str(result),
        "reason": str(reason or ""),
        "actor": principal.actor,
        "role": principal.role,
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    path = Path(root) / CURATOR_LEDGER_REL
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as exc:  # pragma: no cover - best-effort
        print(f"curator: ledger write failed: {type(exc).__name__}", file=sys.stderr)


@dataclass
class ApplyResult:
    applied: bool
    status: str         # "applied" | "refused-autonomy" | "control-plane" |
                        # "prepare-only" | "unmapped" | "failed"
    reason: str = ""
    outputs: list = field(default_factory=list)


def apply_plan(root: Path, plan: Plan, principal: Principal, *,
               autonomy_level: Optional[int] = None) -> ApplyResult:
    """Apply a prepared plan through pinned kernel verbs ONLY (autonomy-gated).

    Refuses (never applies) when:
      * the disposition is CONTROL_PLANE (admin-interface-only) or unmapped;
      * it is PREPARE_ONLY (no safe verb);
      * the autonomy level is below ``CURATOR_MIN_LEVEL``.

    Otherwise runs each pinned argv via ``run_verb`` (the argv chokepoint) and
    ledgers a ``curator_event`` naming the resolving principal.
    """
    if plan.disposition == CONTROL_PLANE:
        _ledger_curator_event(root, principal=principal, item=plan.item, argv=[],
                              result="refused", reason="control-plane")
        return ApplyResult(False, "control-plane",
                           "control-plane resolution stays Admin-interface-only")
    if plan.disposition == "unmapped":
        return ApplyResult(False, "unmapped", "no pinned verb for this kind")
    if plan.disposition == PREPARE_ONLY:
        return ApplyResult(False, "prepare-only", plan.label)

    level = _autonomy_level(root) if autonomy_level is None else autonomy_level
    if level < CURATOR_MIN_LEVEL:
        _ledger_curator_event(root, principal=principal, item=plan.item, argv=[],
                              result="refused", reason=f"autonomy level {level} < "
                              f"{CURATOR_MIN_LEVEL}")
        return ApplyResult(False, "refused-autonomy",
                           f"apply requires autonomy level >= {CURATOR_MIN_LEVEL} "
                           f"(level={level}); prepared only")

    outputs: list = []
    ok = True
    for argv in plan.argvs:
        # Thread the resolving principal where the verb accepts it (loops run does
        # not take --actor/--role; the gate uses the action scope's actor). The
        # attribution lives in the curator_event ledger row regardless.
        rc, out, err = run_verb(root, argv)
        outputs.append({"argv": argv, "rc": rc,
                        "out": (out or "")[:2000], "err": (err or "")[:500]})
        result = "applied" if rc == 0 else "failed"
        if rc != 0:
            ok = False
        _ledger_curator_event(root, principal=principal, item=plan.item, argv=argv,
                              result=result, reason="" if rc == 0 else (err or "")[:200])
    return ApplyResult(ok, "applied" if ok else "failed",
                       "" if ok else "one or more pinned verbs failed", outputs)


# --------------------------------------------------------------------------- #
# the attended driver (oracle curate) + CLI
# --------------------------------------------------------------------------- #
def _render_item(i: int, item: dict, plan: Plan) -> str:
    age = f" ({item['age_days']}d)" if item.get("age_days") else ""
    detail = f" [{item['detail']}]" if item.get("detail") else ""
    head = f"{i}. {item.get('kind')}{detail}: {item.get('title')}{age}"
    sub = f"   -> {plan.disposition}: {plan.label}"
    return head + "\n" + sub


def curate(root: Path, *, stream_in=None, stream_out=None,
           apply: bool = True, limit: int = 0) -> int:
    """Drive the Review Inbox interactively (the ``oracle curate`` body).

    Lists the ranked queue, prints each item's prepared disposition, and -- for
    applyable items -- asks the operator y/N before running the pinned verb. With
    ``apply=False`` (a dry/prepare run) it only prepares and prints, never applies.
    """
    out = stream_out or sys.stdout
    inp = stream_in or sys.stdin
    principal = local_principal(root)
    level = _autonomy_level(root)

    items = list_queue(root, limit=limit)
    out.write(f"oracle curate — {len(items)} item(s) | actor {principal.actor} "
              f"| autonomy level {level}\n")
    if level < CURATOR_MIN_LEVEL:
        out.write(f"  note: autonomy level {level} < {CURATOR_MIN_LEVEL}: apply is "
                  "REFUSED for every item; prepare still works.\n")
    if not items:
        out.write("Review inbox: empty. Nothing waiting on a decision.\n")
        return 0

    due = _due_ids(root)
    applied = 0
    for i, item in enumerate(items, 1):
        plan = plan_item(root, item, due_ids=due)
        out.write(_render_item(i, item, plan) + "\n")
        if not apply or plan.disposition != "apply":
            continue
        for argv in plan.argvs:
            out.write(f"   apply: oracle {' '.join(argv)} ? (y/N) ")
            out.flush()
            line = (inp.readline() or "").strip().lower()
            if not line.startswith("y"):
                out.write("   (skipped)\n")
                continue
            res = apply_plan(
                root,
                Plan(item=item, disposition="apply", label=plan.label, argvs=[argv]),
                principal, autonomy_level=level)
            out.write(f"   {res.status}"
                      + (f": {res.reason}" if res.reason else "") + "\n")
            if res.applied:
                applied += 1
    out.write(f"\ncurate: {applied} action(s) applied.\n")
    return 0


def main(argv: Optional[list] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="oracle curate",
        description="Work the Review Inbox through pinned kernel verbs "
        "(kind->verb map; never executes item action text).")
    ap.add_argument("name", nargs="?", help="instance name (default: resolved)")
    ap.add_argument("--prepare-only", action="store_true",
                    help="prepare + list dispositions; never apply")
    ap.add_argument("--limit", type=int, default=0, help="max items (0 = all)")
    ns = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    from .cli import resolve_instance
    cfg = config.load_config()
    _name, root = resolve_instance(cfg, ns.name)
    return curate(root, apply=not ns.prepare_only, limit=ns.limit)


if __name__ == "__main__":
    raise SystemExit(main())
