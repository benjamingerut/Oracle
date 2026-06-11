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
from ..llm.client import EmbedClient, LLMClient
from . import embedder as emb
from . import policy_bridge as pb
from .loop import AgentLoop, GroundingPolicy, build_system_prompt
from .verbtools import Dispatcher


# Per-turn wall-clock ceiling on the gateway (P3S-7): aligned with
# Dispatcher.timeout (120s). A repair storm must not stall the single-threaded
# serve loop under LOCK_EX, so the whole turn (original + repairs) is bounded.
_GATEWAY_TURN_WALL_CLOCK = 120.0


def grounding_for(cfg: dict, surface: str,
                  grounding_override: str | None = None) -> GroundingPolicy:
    """Decide the forced-grounding policy for ``surface`` (P3-T4, P3S-9/11; P4S-1).

    ``build_loop`` is the SOLE decision point. The rule, INVERTED to fail
    CLOSED on surface (P4S-1):

      * any surface that is NOT exactly ``"local"`` -> ``ENFORCE``, HARD-CODED,
        plus the gateway wall-clock cap (applied in ``build_loop``). The literal
        gateway loop surface is ``"gateway"``, but a future wiring mistake that
        leaks a transport name (``"http"``, ``"slack"``, ...) into ``build_loop``
        must NOT silently fall through to the local OBSERVE default. So the
        branch is fail-closed: anything but ``"local"`` is gateway-class. Config
        cannot lower it; there is no gateway grounding key at all (P3S-11). A
        ``grounding_override`` is ignored on every non-local surface -- unbacked
        claims reach other people there, so it never waits on the budget gate
        (gateway-first rollout). This is the ``security_map``-enforced guarantee.
      * ``surface == "local"`` -> the single config key ``chat.grounding_default``
        (default ``observe`` until the P3-T7 budget gate flips it), unless the
        operator passes ``grounding_override`` (the ``oracle chat --grounding``
        opt-up/opt-down, logged by the CLI).

    An unknown mode string raises ``ValueError`` so the caller surfaces a clear
    error rather than silently falling back to a less-strict mode.
    """
    if surface != "local":
        return GroundingPolicy.ENFORCE  # fail-closed, config-immutable (P4S-1/P3S-11)
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
               write_role: str = "user",
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

    ``write_actor``/``write_role`` are the resolved Principal's attribution
    (P5-T2): they are threaded into the model-invokable write verbs' argv as
    ``--actor``/``--role`` so the kernel ledgers name *who* wrote under *what*
    role. Both are attribution only -- role is role-INVARIANT through these
    verbs (P5S-13) and NEVER widens the model's tool surface (I2). ``write_role``
    defaults to ``"user"``: the attended local surface is a non-privileged
    model-driven write path; admin is reserved for the human's direct kernel
    CLI. The gateway always injects an explicitly resolved (and clamped) role
    via the pinned ``loop_builder`` seam, so the default never applies there.
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

    # Phase 8 (P8-T4): the optional embedding seam. The query-embedder is a PURE
    # callable injected into the Dispatcher; when no embedding model is
    # configured it stays ``None`` and search is lexical exactly as today. The
    # embed client is a SEPARATE one-purpose instance (P8S-2) keyed to the
    # EMBEDDING endpoint's own base_url and POST-VETO environment -- never the
    # chat client with a swapped path. The post-veto embed ceiling is computed
    # here at build time (NOT per-search; the veto's 3 s probe must not ride the
    # search path). The frozen query rule (rank(retrieval) <= rank(embed)) is
    # decided inside ``build_query_embedder`` -- verbtools holds no ceiling logic.
    query_embedder = None
    emb_cfg = (prov.get("embeddings") or {})
    embed_model = (emb_cfg.get("model") or "").strip()
    if embed_model:
        embed_base_url = (emb_cfg.get("base_url") or base_url or "")
        embed_key_env = (emb_cfg.get("api_key_env") or api_key_env or "")
        # Named to form the scanner-suppressed self-assignment kwarg below.
        api_key_embed = config.resolve_secret(embed_key_env) if embed_key_env else None
        # Independent post-veto classification of the EMBEDDING endpoint (P8S-1).
        embed_env = pb.environment_for(embed_base_url)
        if embed_env == "local_agent":
            embed_veto = pb.egress_veto(embed_base_url, embed_model)
            if embed_veto:
                print(
                    f"oracle: embedding egress veto — loopback embedder "
                    f"reclassified as external: {embed_veto}",
                    file=sys.stderr,
                )
                embed_env = "external"
        embed_ceiling = emb.embedding_ceiling(root, embed_base_url, embed_model)
        try:
            embed_client = EmbedClient(embed_base_url, api_key=api_key_embed,
                                       environment=embed_env)
        except Exception as exc:  # plaintext-key refusal etc. -> lexical only
            print(
                f"oracle: embedding client construction failed "
                f"({type(exc).__name__}); vector search disabled (lexical only)",
                file=sys.stderr,
            )
            embed_client = None
        if embed_client is not None:
            query_embedder = emb.build_query_embedder(
                embed_client, embed_model=embed_model,
                embed_ceiling=embed_ceiling, retrieval_ceiling=ceiling,
                order=order,
            )

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
        write_role=write_role,
        write_gate=write_gate,
        query_embedder=query_embedder,
    )
    system_prompt = build_system_prompt(Path(root), surface, environment, ceiling)
    chat_cfg = cfg.get("chat") or {}
    grounding = grounding_for(cfg, surface, grounding_override)
    # The gateway runs the whole turn (original + repairs) under a wall-clock
    # ceiling so a repair storm cannot stall the serve loop (P3S-7). Local chat
    # has no wall-clock ceiling (the operator owns the terminal). Fail-closed on
    # surface (P4S-1): any non-local surface gets the wall-clock cap, so a leaked
    # transport name can never run the gateway uncapped.
    turn_wall_clock = None if surface == "local" else _GATEWAY_TURN_WALL_CLOCK
    # P3-T7 shadow capture (P3S-10): consent is read from config ONLY for a LOCAL
    # surface. The gateway NEVER gets shadow capture -- structurally it is
    # ENFORCE (never reaches the OBSERVE branch where capture lives) and here the
    # consent is hard-forced False for any non-local surface, so claim text can
    # never land in a file on a gateway-built loop.
    shadow_consent = (surface == "local"
                      and bool(chat_cfg.get("grounding_shadow", False)))
    return AgentLoop(
        client, dispatcher, system_prompt,
        grounding=grounding,
        turn_wall_clock=turn_wall_clock,
        shadow_consent=shadow_consent,
        max_iterations=int(chat_cfg.get("max_iterations", 20)),
        history_max_chars=int(chat_cfg.get("history_max_chars", 400000)),
        max_tokens=int(prov.get("max_tokens", 4096)),
    )
