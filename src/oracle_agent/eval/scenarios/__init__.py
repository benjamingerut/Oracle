"""Scenario catalogs, one module per dimension (P6-T2..T4).

Each module exposes ``scenarios() -> list[Scenario]``. Net-new-only rule
(P6S-9): every scenario names its guarantee and is composition-level
(multi-turn / cross-surface, through the real AgentLoop / GatewayCore /
adapters) or covers a NEW guarantee -- never a copy of an existing SH-xxx unit
test at the same level.
"""
