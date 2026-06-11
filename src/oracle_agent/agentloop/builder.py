"""agentloop/builder.py -- assemble an AgentLoop for an instance + surface.

Single place that wires provider config -> LLMClient, root + environment ->
ceiling -> Dispatcher -> system prompt -> AgentLoop. Used by both ``oracle
chat`` (local surface) and the Telegram gateway (gateway surface), so the
policy bridge and ceiling logic are applied identically on every surface.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .. import config
from ..llm.client import LLMClient
from . import policy_bridge as pb
from .loop import AgentLoop, GroundingPolicy, build_system_prompt
from .verbtools import Dispatcher


# Per-turn wall-clock ceiling on the gateway (P3S-7): aligned with
# Dispatcher.timeout (120s). A repair storm must not stall the single-threaded
# serve loop under LOCK_EX, so the whole turn (original + repairs) is bounded.
_GATEWAY_TURN_WALL_CLOCK = 120.0


def grounding_for(cfg: dict, surface: str,
                  grounding_override: str | None = None) -> GroundingPolicy:
    """Decide the forced-grounding policy for ``surface`` (P3-T4, P3S-9/11).

    ``build_loop`` is the SOLE decision point. The rule is:

      * ``surface == "gateway"`` -> ``ENFORCE``, HARD-CODED. Config cannot lower
        it; there is no gateway grounding key at all (P3S-11). A
        ``grounding_override`` is ignored on the gateway -- unbacked claims reach
        other people there, so it never waits on the budget gate (gateway-first
        rollout). This is the ``security_map``-enforced guarantee.
      * any local surface -> the single config key ``chat.grounding_default``
        (default ``observe`` until the P3-T7 budget gate flips it), unless the
        operator passes ``grounding_override`` (the ``oracle chat --grounding``
        opt-up/opt-down, logged by the CLI).

    An unknown mode string raises ``ValueError`` so the caller surfaces a clear
    error rather than silently falling back to a less-strict mode.
    """
    if surface == "gateway":
        return GroundingPolicy.ENFORCE  # hard-coded, config-immutable (P3S-11)
    raw = grounding_override
    if raw is None:
        raw = ((cfg.get("chat") or {}).get("grounding_default") or "observe")
    raw = str(raw).strip().lower()
    try:
        return GroundingPolicy(raw)
    except ValueError:
        raise ValueError(
            f"unknown grounding mode {raw!r}; expected 'observe' or 'enforce'"
        )


def build_loop(cfg: dict, root: Path, *, surface: str,
               ceiling_override: str | None = None,
               grounding_override: str | None = None,
               write_actor: str | None = None,
               write_gate=None) -> AgentLoop:
    """Construct a ready AgentLoop for ``root`` on ``surface``.

    ``ceiling_override`` may only LOWER the computed ceiling (SPEC S8 chat
    ``--max-sensitivity``; gateway ``max_sensitivity``).  Unknown/mis-cased
    labels raise ``ValueError`` so the caller can surface a clear error (CLI
    exits non-zero; gateway refuses to start).

    ``grounding_override`` is the ``oracle chat --grounding`` flag (local only;
    ignored on the gateway, which is always ``ENFORCE``). The builder is the
    sole forced-grounding decision point (P3S-9): the mode is fixed at
    construction and no tool output, prompt injection, or config read can flip
    it mid-session.
    """
    prov = cfg.get("provider") or {}
    base_url = prov.get("base_url", "")
    model = prov.get("model", "")
    environment = pb.environment_for(base_url)
    # Egress veto (STRESS C2 / P2S-2): a loopback listener serving a provably
    # cloud-proxied model (e.g. an Ollama ``*:cloud`` model that forwards to
    # ollama.com) is treated as EXTERNAL for every downstream decision. Network
    # locality is not processing locality. Fail toward the stricter outcome;
    # egress_veto never raises.
    if environment == "local_agent":
        veto = pb.egress_veto(base_url, model)
        if veto:
            print(
                f"oracle: egress veto — loopback endpoint reclassified as "
                f"external: {veto}",
                file=sys.stderr,
            )
            environment = "external"
    order = pb.sensitivity_order(root)
    # NOTE: local_is_confined was removed (S1 remediation — dead security knob).
    # A real confidential-tier confinement mechanism lands in roadmap Phase 2.
    ceiling = pb.max_sensitivity_for(root, environment)
    if ceiling_override:
        # Validate before applying; unknown labels are a configuration error.
        pb.validate_sensitivity_label(ceiling_override, order)
        ceiling = pb.min_sensitivity(ceiling, ceiling_override, order)

    api_key_env = prov.get("api_key_env") or ""
    api_key = config.resolve_secret(api_key_env) if api_key_env else None
    scrub = [api_key_env] if api_key_env else []
    # also scrub any gateway token env names
    tg = ((cfg.get("gateway") or {}).get("telegram") or {})
    if tg.get("token_env"):
        scrub.append(tg["token_env"])

    client = LLMClient(base_url, prov.get("model", ""), api_key=api_key,
                       environment=environment)

    instance_roots = config.instance_roots(cfg)
    sibling_roots = [r for r in instance_roots.values()
                     if Path(r).resolve() != Path(root).resolve()]
    ingest_roots = [Path(p) for p in (cfg.get("ingest_roots") or [])]

    dispatcher = Dispatcher(
        root=Path(root), surface=surface, environment=environment,
        max_sensitivity=ceiling, order=order,
        ingest_roots=ingest_roots, sibling_roots=sibling_roots,
        profile_dir=config.profile_dir(), scrub_env=scrub,
        tool_result_max_chars=int((cfg.get("chat") or {}).get("tool_result_max_chars", 20000)),
        write_actor=write_actor,
        write_gate=write_gate,
    )
    system_prompt = build_system_prompt(Path(root), surface, environment, ceiling)
    chat_cfg = cfg.get("chat") or {}
    grounding = grounding_for(cfg, surface, grounding_override)
    # The gateway runs the whole turn (original + repairs) under a wall-clock
    # ceiling so a repair storm cannot stall the serve loop (P3S-7). Local chat
    # has no wall-clock ceiling (the operator owns the terminal).
    turn_wall_clock = _GATEWAY_TURN_WALL_CLOCK if surface == "gateway" else None
    return AgentLoop(
        client, dispatcher, system_prompt,
        grounding=grounding,
        turn_wall_clock=turn_wall_clock,
        max_iterations=int(chat_cfg.get("max_iterations", 20)),
        history_max_chars=int(chat_cfg.get("history_max_chars", 400000)),
        max_tokens=int(prov.get("max_tokens", 4096)),
    )
