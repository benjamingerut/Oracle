"""oracle_agent -- sovereign company oracle: governed knowledge kernel + agent shell.

Two layers:

* ``oracle_agent.assets/oracle-kernel`` -- the vendored, stdlib-only Oracle
  kernel (39 deterministic tools + doctrine + playbooks). Spawned verbatim
  into each oracle root; every spawned root is self-contained and survives
  this package being uninstalled.
* the shell (``cli``, ``llm``, ``agentloop``, ``service``, ``gateway``,
  ``wizard``, ``doctor``) -- the installable system-agent layer: a global
  ``oracle`` command, a model-agnostic LLM loop whose only tools are kernel
  verbs, a scheduler daemon, and messaging surfaces. Also stdlib-only.
"""
__version__ = "1.0.0"
