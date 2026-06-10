"""agentloop/builder.py -- assemble an AgentLoop for an instance + surface.

Single place that wires provider config -> LLMClient, root + environment ->
ceiling -> Dispatcher -> system prompt -> AgentLoop. Used by both ``oracle
chat`` (local surface) and the Telegram gateway (gateway surface), so the
policy bridge and ceiling logic are applied identically on every surface.
"""
from __future__ import annotations

from pathlib import Path

from .. import config
from ..llm.client import LLMClient
from . import policy_bridge as pb
from .loop import AgentLoop, build_system_prompt
from .verbtools import Dispatcher


def build_loop(cfg: dict, root: Path, *, surface: str,
               ceiling_override: str | None = None,
               write_actor: str | None = None,
               write_gate=None) -> AgentLoop:
    """Construct a ready AgentLoop for ``root`` on ``surface``.

    ``ceiling_override`` may only LOWER the computed ceiling (SPEC S8 chat
    ``--max-sensitivity``; gateway ``max_sensitivity``).
    """
    prov = cfg.get("provider") or {}
    base_url = prov.get("base_url", "")
    environment = pb.environment_for(base_url)
    order = pb.sensitivity_order(root)
    ceiling = pb.max_sensitivity_for(root, environment,
                                     bool(prov.get("local_is_confined", False)))
    if ceiling_override:
        ceiling = pb.min_sensitivity(ceiling, ceiling_override, order)

    api_key_env = prov.get("api_key_env") or ""
    api_key = config.resolve_secret(api_key_env) if api_key_env else None
    scrub = [api_key_env] if api_key_env else []
    # also scrub any gateway token env names
    tg = ((cfg.get("gateway") or {}).get("telegram") or {})
    if tg.get("token_env"):
        scrub.append(tg["token_env"])

    client = LLMClient(base_url, prov.get("model", ""), api_key=api_key)

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
    return AgentLoop(
        client, dispatcher, system_prompt,
        max_iterations=int(chat_cfg.get("max_iterations", 20)),
        history_max_chars=int(chat_cfg.get("history_max_chars", 400000)),
        max_tokens=int(prov.get("max_tokens", 4096)),
    )
