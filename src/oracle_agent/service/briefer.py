"""service/briefer.py -- scheduled briefing delivery (Phase 4, P4-T8).

Moved here from Phase 5 (P5-T4): a gateway that can *push*, not just reply, is
the leverage feature. The kernel's ``leadership-briefing`` loop already produces
briefs on cadence (the harness tick) and lands them in each root's
``Workproduct.nosync/_STANDING/.registry.jsonl``. This module does NOT
re-implement cadence (P4S-15): it WATCHES the standing registry for new
``leadership-brief`` rows (keyed by ``drop_id``) and delivers each exactly once,
across restarts, to a configured allowlisted-private target.

Every security pin (P4S-15):

  * **Registry-driven, not cadence-cloning:** :func:`new_briefs` reads each
    root's standing registry for ``leadership-brief`` rows whose
    ``(instance, surface, drop_id)`` key is not yet in the persisted state.
  * **Exactly-once across restarts:** the delivery-state file lives in the
    profile dir, keyed ``(instance, surface, drop_id)``, written atomically
    (tmp+rename). **Corruption => NO send + logged + doctor flag** (fail closed:
    a missed brief beats a mis-sent one).
  * **Push targets must be provably private:** a push has no inbound message to
    assert ``is_private``, so targets MUST resolve to an already-allowlisted
    private identity -- telegram: a ``user_id`` present in the telegram
    allowlist; email: a single allowlisted address. Anything else (a group id,
    an unlisted chat, a list address) is refused (config-load + doctor, and
    again here defensively). Deny-by-default: no configured target => no
    delivery.
  * **Ceiling re-check at delivery (delivery is an EXPORT):** compare the
    registry row's DOCUMENT-level ``sensitivity`` against the target surface's
    ``max_sensitivity``; above => withhold the WHOLE brief (per-line scan is
    blocked upstream -- SH-057 -- so document-level is the only honest check).
  * **Ledger row pinned:** ``{kind:"briefing_delivery", surface, target,
    drop_id, sensitivity, ts}`` -- metadata only, appended to the root's
    ``gateway_event.jsonl``.

Stdlib only.
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Canonical sensitivity ladder (mirrors policy_bridge.CANONICAL_ORDER; an
# unknown label sorts to the strictest rank so an unrecognized document
# sensitivity is withheld, never widened -- fail closed).
_CANONICAL_ORDER = ("public", "internal", "confidential", "restricted", "secret")

_BRIEF_KIND = "leadership-brief"


def _sens_rank(label: str) -> int:
    try:
        return _CANONICAL_ORDER.index((label or "").strip().lower())
    except ValueError:
        return len(_CANONICAL_ORDER)  # unknown -> strictest (withhold)


# --------------------------------------------------------------------------- #
# Delivery value type
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Delivery:
    """One brief to deliver to one target (keyed exactly-once by the triple)."""

    instance: str
    surface: str
    drop_id: str
    sensitivity: str
    target: str          # the resolved private identity (chat_id / address)
    artifact_path: str   # canonical_location of the brief within the root
    root: Path

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.instance, self.surface, str(self.drop_id))


# --------------------------------------------------------------------------- #
# Delivery state (exactly-once, fail-closed on corruption; P4S-15/20)
# --------------------------------------------------------------------------- #
class DeliveryState:
    """Persisted ``(instance, surface, drop_id)`` set of delivered briefs.

    Corruption => ``corrupt`` flag set, the in-memory set is EMPTY, and the
    caller MUST refuse to send anything (fail closed) and raise a doctor flag.
    The state file lives in the profile dir, named ``briefing_state_<scope>.json``
    (P4S-20 naming), written atomically.
    """

    def __init__(self, path: Path | None, *, logger=None):
        self.path = Path(path) if path is not None else None
        self.logger = logger or (lambda *a: None)
        self._delivered: set[tuple[str, str, str]] = set()
        self.corrupt = False
        self._loaded = False
        self._dirty = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            delivered = data["delivered"]
            if not isinstance(delivered, list):
                raise ValueError("delivered must be a list")
            out: set[tuple[str, str, str]] = set()
            for item in delivered:
                if (not isinstance(item, (list, tuple))) or len(item) != 3:
                    raise ValueError(f"malformed delivered entry: {item!r}")
                out.add((str(item[0]), str(item[1]), str(item[2])))
            self._delivered = out
        except Exception as exc:
            # FAIL CLOSED (P4S-15): a corrupt state file means we cannot prove
            # exactly-once, so we send NOTHING and flag doctor. A missed brief
            # beats a mis-sent (or duplicated) one.
            self.corrupt = True
            self._delivered = set()
            self.logger(
                f"briefer: delivery-state file corrupt ({type(exc).__name__}); "
                "refusing ALL delivery until repaired (doctor flag)")

    def already_delivered(self, key: tuple[str, str, str]) -> bool:
        self.load()
        return key in self._delivered

    def mark(self, key: tuple[str, str, str]) -> None:
        self.load()
        if key not in self._delivered:
            self._delivered.add(key)
            self._dirty = True

    def commit(self) -> None:
        """Atomically persist the delivered set (tmp+rename, P4S-20)."""
        if not self._dirty or self.path is None or self.corrupt:
            return
        payload = json.dumps(
            {"delivered": sorted(list(k) for k in self._delivered)}) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                       prefix=".brfst-", suffix="~")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
                self._dirty = False
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            self.logger(f"briefer: delivery-state save failed: "
                        f"{type(exc).__name__}")


# --------------------------------------------------------------------------- #
# Target resolution (must be provably private; P4S-15)
# --------------------------------------------------------------------------- #
def resolve_target(cfg: dict, target: dict) -> tuple[str, str] | None:
    """Resolve one ``briefings`` target to ``(surface, identity)`` or ``None``.

    A target is accepted ONLY when it resolves to an already-allowlisted PRIVATE
    identity on its surface (P4S-15):

      * telegram: ``{"surface":"telegram","user_id":"12345"}`` and ``12345`` is
        a key in ``gateway.telegram.allowlist`` (a private 1:1 chat_id).
      * email: ``{"surface":"email","address":"ceo@co.com"}`` and the lowercased
        address is a key in ``gateway.email.allowlist`` (a single address).

    Anything else -- a group id, an unlisted chat, a list address, an unknown
    surface, a missing identity -- returns ``None`` (refused; deny-by-default).
    """
    if not isinstance(target, dict):
        return None
    surface = target.get("surface")
    gw = (cfg.get("gateway") or {})
    if surface == "telegram":
        uid = target.get("user_id")
        if uid is None:
            return None
        uid = str(uid)
        allow = ((gw.get("telegram") or {}).get("allowlist") or {})
        if uid in allow:
            return ("telegram", uid)
        return None
    if surface == "email":
        addr = target.get("address")
        if not addr:
            return None
        addr = str(addr).strip().lower()
        allow = ((gw.get("email") or {}).get("allowlist") or {})
        # Allowlist keys are lowercased exact addresses (P4S-17). Match exactly.
        if addr in {str(k).strip().lower() for k in allow}:
            return ("email", addr)
        return None
    return None


def targets_for(cfg: dict, instance: str) -> list[dict]:
    """The configured ``briefings`` delivery targets for ``instance``."""
    block = (cfg.get("briefings") or {}).get(instance) or {}
    targets = block.get("targets") or []
    return [t for t in targets if isinstance(t, dict)]


# --------------------------------------------------------------------------- #
# Registry scanning (registry-driven, not cadence-cloning; P4S-15)
# --------------------------------------------------------------------------- #
def _standing_registry(root: Path) -> Path:
    return Path(root) / "Workproduct.nosync" / "_STANDING" / ".registry.jsonl"


def _read_brief_rows(root: Path) -> list[dict]:
    """Return ``leadership-brief`` rows from a root's standing registry.

    Corruption-tolerant: an unparseable line is skipped (the kernel ledger's own
    discipline). Only rows whose ``kind`` is ``leadership-brief`` AND that carry
    a ``drop_id`` are returned.
    """
    path = _standing_registry(root)
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        if row.get("kind") != _BRIEF_KIND:
            continue
        if not row.get("drop_id"):
            continue
        out.append(row)
    return out


def new_briefs(cfg: dict, instances: dict, state: DeliveryState) -> list[Delivery]:
    """Return undelivered ``leadership-brief`` deliveries across all instances.

    For each instance with a configured (and resolvable) target, scan that
    root's standing registry for ``leadership-brief`` rows whose
    ``(instance, surface, drop_id)`` key is not yet in ``state``. Produces one
    :class:`Delivery` per (new brief, resolved target).

    Fail closed: if ``state`` is corrupt, returns ``[]`` (no send at all) -- the
    caller also checks ``state.corrupt`` to raise the doctor flag.
    """
    state.load()
    if state.corrupt:
        return []

    out: list[Delivery] = []
    for instance, root in (instances or {}).items():
        configured = targets_for(cfg, instance)
        if not configured:
            continue  # deny-by-default: no target => no delivery
        resolved: list[tuple[str, str]] = []
        for t in configured:
            r = resolve_target(cfg, t)
            if r is not None:
                resolved.append(r)
        if not resolved:
            continue

        rows = _read_brief_rows(Path(root))
        for row in rows:
            drop_id = str(row.get("drop_id"))
            sensitivity = str(row.get("sensitivity", "internal") or "internal")
            artifact = str(row.get("canonical_location", "")
                           or row.get("artifact_name", ""))
            for surface, identity in resolved:
                key = (instance, surface, drop_id)
                if state.already_delivered(key):
                    continue
                out.append(Delivery(
                    instance=instance,
                    surface=surface,
                    drop_id=drop_id,
                    sensitivity=sensitivity,
                    target=identity,
                    artifact_path=artifact,
                    root=Path(root),
                ))
    return out


# --------------------------------------------------------------------------- #
# Ceiling check (delivery is an export; P4S-15 / SH-057)
# --------------------------------------------------------------------------- #
def _surface_ceiling(cfg: dict, surface: str) -> str:
    """The effective ``max_sensitivity`` ceiling for a delivery surface.

    Mirrors the interactive ceiling: email is HARD-CAPPED at ``public`` for a
    PUSH (no inbound message to carry a DMARC-verified unlock, P4S-10) -- a
    pushed brief gets no privilege an interactive reply would not have. Other
    surfaces use their configured ``max_sensitivity``.
    """
    block = ((cfg.get("gateway") or {}).get(surface) or {})
    if surface == "email":
        # A scheduled push cannot verify DMARC on an inbound message, so the
        # email surface is public-capped for delivery (P4S-10/15 fail-closed).
        return "public"
    return str(block.get("max_sensitivity", "internal") or "internal")


def ceiling_allows(cfg: dict, surface: str, sensitivity: str) -> bool:
    """Document-level ceiling re-check (P4S-15): brief sensitivity <= surface cap."""
    return _sens_rank(sensitivity) <= _sens_rank(_surface_ceiling(cfg, surface))


# --------------------------------------------------------------------------- #
# Ledger row (pinned; metadata only; P4S-15)
# --------------------------------------------------------------------------- #
def _ledger_delivery(root: Path, delivery: Delivery, *, logger=None) -> None:
    logger = logger or (lambda *a: None)
    row = {
        "kind": "briefing_delivery",
        "surface": delivery.surface,
        "target": delivery.target,
        "drop_id": str(delivery.drop_id),
        "sensitivity": delivery.sensitivity,
        "ts": datetime.datetime.fromtimestamp(
            time.time(), datetime.timezone.utc).isoformat(),
    }
    path = Path(root) / "Meta.nosync" / "ledgers" / "gateway_event.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        from ..gateway.core import _append_jsonl
        _append_jsonl(path, row)
    except OSError as exc:
        logger(f"briefer: ledger append failed: {type(exc).__name__}")


# --------------------------------------------------------------------------- #
# deliver -- ceiling-checked send + state update (P4S-15)
# --------------------------------------------------------------------------- #
def deliver(cfg: dict, delivery: Delivery, senders: dict, state: DeliveryState,
            *, logger=None) -> bool:
    """Deliver one brief: ceiling re-check -> send -> ledger -> mark state.

    ``senders`` maps surface name -> a callable ``send(target, text)`` (the
    push side of an adapter). Returns ``True`` when the brief was sent and the
    state marked, ``False`` when it was withheld (above ceiling), refused (no
    sender / state corrupt), or already delivered.

    Exactly-once: the state is marked AFTER a successful send and the ledger row
    (at-least-once on a crash between send and mark, which the spec accepts at
    the inbound contract too, P4S-4 -- a duplicate brief, never a lost one).
    """
    logger = logger or (lambda *a: None)
    state.load()
    if state.corrupt:
        logger("briefer: state corrupt; refusing delivery (fail closed)")
        return False
    if state.already_delivered(delivery.key):
        return False

    # Document-level ceiling re-check -- delivery is an EXPORT (P4S-15/SH-057).
    if not ceiling_allows(cfg, delivery.surface, delivery.sensitivity):
        logger(
            f"briefer: WITHHELD brief {delivery.drop_id} for "
            f"{delivery.surface}:{delivery.target} -- document sensitivity "
            f"{delivery.sensitivity!r} above surface ceiling "
            f"{_surface_ceiling(cfg, delivery.surface)!r}")
        return False

    send = senders.get(delivery.surface)
    if send is None:
        logger(f"briefer: no sender for surface {delivery.surface!r}; skipping "
               f"brief {delivery.drop_id}")
        return False

    text = _brief_text(delivery)
    try:
        send(delivery.target, text)
    except Exception as exc:
        logger(f"briefer: send failed for {delivery.surface}:{delivery.target} "
               f"({type(exc).__name__}); will retry next pass")
        return False

    _ledger_delivery(delivery.root, delivery, logger=logger)
    state.mark(delivery.key)
    state.commit()
    logger(f"briefer: delivered brief {delivery.drop_id} to "
           f"{delivery.surface}:{delivery.target}")
    return True


def _brief_text(delivery: Delivery) -> str:
    """Read the brief body from its canonical location, or a pointer fallback.

    The brief artifact already passed the kernel's export policy gate when it was
    published (standing_deliverables.emit), so its bytes are export-clean at the
    document ceiling we re-checked above.
    """
    path = Path(delivery.root) / delivery.artifact_path
    try:
        if path.exists():
            body = path.read_text(encoding="utf-8", errors="replace")
            if body.strip():
                return body
    except OSError:
        pass
    return (f"A new leadership brief ({delivery.drop_id}) is available in your "
            f"oracle. (sensitivity: {delivery.sensitivity})")


# --------------------------------------------------------------------------- #
# run_once -- the serve-driven entrypoint (P4-T5 wiring)
# --------------------------------------------------------------------------- #
def run_once(cfg: dict, instances: dict, senders: dict, state: DeliveryState,
             *, logger=None) -> int:
    """Detect + deliver all new briefs once. Returns the count delivered.

    Driven by the serve loop between ticks (P4-T5). Fail-closed on state
    corruption (no send; the caller raises the doctor flag).
    """
    logger = logger or (lambda *a: None)
    state.load()
    if state.corrupt:
        return 0
    delivered = 0
    for delivery in new_briefs(cfg, instances, state):
        if deliver(cfg, delivery, senders, state, logger=logger):
            delivered += 1
    return delivered


def state_path(profile_dir: Path, scope: str = "default") -> Path:
    """P4S-20 state-file naming: ``briefing_state_<scope>.json`` in the profile."""
    return Path(profile_dir) / f"briefing_state_{scope}.json"
