# AgentResources.nosync

Company-local skills, references, templates, and playbooks that should **move with this oracle**.

## Purpose

This is the oracle's portable capability layer — workflows and reference material specific to this company, kept inside the kernel so they travel with it rather than depending on the host machine.

## Discipline

- **Machine-local skills are fallback only.** Prefer resources here so the oracle's capabilities are sovereign and portable.
- **Repeated successful workflows graduate into local resources.** When a workflow proves valuable across sessions, capture it here as a reusable playbook rather than re-deriving it each time — this is part of how the oracle becomes more skilled over time.
- Resources here are doctrine/templates, not executable tool code; the executable tool layer lives in `_tools/` and is the only thing `upgrade.py` ever replaces.
