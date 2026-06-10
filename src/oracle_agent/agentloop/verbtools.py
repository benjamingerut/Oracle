"""agentloop/verbtools.py -- kernel verbs as the ONLY agent tools (SPEC S4).

The agent loop never runs a shell, never writes files, never calls a kernel
module in-process. Its entire capability surface is a fixed allowlist of the
root's own ``./oracle`` verbs, invoked as argv subprocesses (never shell=True),
with the subcommand pinned in code (model arguments fill value slots only).

This is the load-bearing design choice (DESIGN D2): every governance property
the kernel enforces -- path containment, immutability, policy, review-gating --
survives the new runtime because the model can only act through these
chokepoints.

Ceiling enforcement (STRESS C1) happens HERE, in code:
  * search           : ``--max-sensitivity <ceiling>`` forced; model sensitivity
                       tokens stripped (M5).
  * answer           : envelope parsed from stdout; if the answer's required
                       clearance exceeds the ceiling the grounded payload is
                       withheld (verdict + suggested_fix still returned).
  * brief            : only offered at internal+ and non-external; lines above
                       the ceiling dropped.
  * status           : replaced by the minimized snapshot (built in loop.py).
  * ingest           : path-allowlisted (H4); profile dir + sibling roots denied.
Output is capped at ``tool_result_max_chars``.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import policy_bridge

# --------------------------------------------------------------------------- #
# tool schemas (OpenAI function-calling format)
# --------------------------------------------------------------------------- #
_SCHEMAS: dict[str, dict] = {
    "oracle_status": {
        "description": "Current oracle status: maturity rung and memory/authority counts. No arguments.",
        "parameters": {"type": "object", "properties": {}},
    },
    "oracle_search": {
        "description": "Full-text search over the oracle's knowledge index. Returns matching chunks (capped at the provider's sensitivity ceiling).",
        "parameters": {"type": "object", "properties": {
            "terms": {"type": "string", "description": "search terms"},
            "k": {"type": "integer", "description": "max results (default 8)"},
        }, "required": ["terms"]},
    },
    "oracle_answer": {
        "description": "Run the graduated answer protocol for a business object. Returns a verdict (grounded/supported/caveated/refused) you MUST obey when stating the claim.",
        "parameters": {"type": "object", "properties": {
            "business_object": {"type": "string"},
            "question": {"type": "string"},
        }, "required": ["business_object"]},
    },
    "oracle_review": {
        "description": "The review inbox: items waiting on a decision (counts; titles only on a local trusted model).",
        "parameters": {"type": "object", "properties": {}},
    },
    "oracle_ingest": {
        "description": "Ingest files or folders into the oracle (local operator surface only; paths must be within configured ingest roots).",
        "parameters": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
        }, "required": ["paths"]},
    },
    "oracle_remember": {
        "description": "Record a material session into the oracle's memory.",
        "parameters": {"type": "object", "properties": {
            "user_request": {"type": "string"},
            "answer_summary": {"type": "string"},
            "business_objects": {"type": "array", "items": {"type": "string"}},
            "learned_claims": {"type": "array", "items": {"type": "string"}},
        }, "required": ["user_request", "answer_summary"]},
    },
    "oracle_capture": {
        "description": "Capture a feedback, value, or failure event.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["feedback", "value", "failure"]},
            "target": {"type": "string"},
            "polarity": {"type": "string", "enum": ["positive", "neutral", "negative"]},
            "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "failure_mode": {"type": "string"},
        }, "required": ["kind", "target"]},
    },
    "oracle_brief": {
        "description": "Generate the leadership brief skeleton (claims labeled by authority).",
        "parameters": {"type": "object", "properties": {}},
    },
    "oracle_checkpoint": {
        "description": "Close the session: run due builtin loops and report. Writes.",
        "parameters": {"type": "object", "properties": {}},
    },
    "oracle_loops_due": {
        "description": "List loops currently due.",
        "parameters": {"type": "object", "properties": {}},
    },
}

_LOCAL_TOOLS = [
    "oracle_status", "oracle_search", "oracle_answer", "oracle_review",
    "oracle_ingest", "oracle_remember", "oracle_capture", "oracle_brief",
    "oracle_checkpoint", "oracle_loops_due",
]
_GATEWAY_TOOLS = [
    "oracle_status", "oracle_search", "oracle_answer", "oracle_review",
    "oracle_capture", "oracle_remember",
]


def tool_schemas(surface: str, environment: str) -> list[dict]:
    """OpenAI tool schemas for a (surface, environment) combination (S4)."""
    names = list(_LOCAL_TOOLS if surface == "local" else _GATEWAY_TOOLS)
    if environment == "external":
        # Output sensitivity not self-attestable for these -> drop on external.
        for drop in ("oracle_brief",):
            if drop in names:
                names.remove(drop)
    out = []
    for name in names:
        spec = _SCHEMAS[name]
        out.append({"type": "function", "function": {
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
        }})
    return out


# --------------------------------------------------------------------------- #
# subprocess dispatch
# --------------------------------------------------------------------------- #
_SCRUB_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def _scrubbed_env(extra_drop: list[str] | None = None) -> dict:
    """Process env with all secret-shaped vars removed (STRESS M1)."""
    drop = set(extra_drop or [])
    env = {}
    for k, v in os.environ.items():
        up = k.upper()
        if any(up.endswith(s) for s in _SCRUB_SUFFIXES) or k in drop:
            continue
        env[k] = v
    return env


def run_verb(root: Path, argv: list[str], timeout: float = 120.0,
             scrub_env: list[str] | None = None) -> tuple[int, str, str]:
    """Run ``root/oracle <argv...>`` as an argv subprocess. Returns (rc, out, err).

    Never ``shell=True``. stdout and stderr are captured SEPARATELY so the
    answer envelope (stdout) is never corrupted by warnings (stderr).
    """
    oracle = Path(root) / "oracle"
    cmd = [sys.executable, str(oracle), *argv]
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                          timeout=timeout, env=_scrubbed_env(scrub_env))
    return proc.returncode, proc.stdout or "", proc.stderr or ""


@dataclass
class ToolOutcome:
    text: str
    envelope: dict | None = None
    rc: int = 0


_SENSITIVITY_FLAG_RE = re.compile(r"^--max-sensitivity(=.*)?$")
_SECRETISH_PATH_RE = re.compile(r"(?i)(\.env|id_rsa|\.pem|secret|credential)")


@dataclass
class Dispatcher:
    root: Path
    surface: str            # "local" | "gateway"
    environment: str        # "local_agent" | "external"
    max_sensitivity: str
    order: list[str] = field(default_factory=lambda: list(policy_bridge.CANONICAL_ORDER))
    ingest_roots: list[Path] = field(default_factory=list)
    sibling_roots: list[Path] = field(default_factory=list)
    profile_dir: Path | None = None
    scrub_env: list[str] = field(default_factory=list)
    tool_result_max_chars: int = 20000
    write_actor: str | None = None   # e.g. "gateway_user:123" (M4 provenance)
    write_gate: object = None        # optional callable() -> bool (M4 rate limit)
    timeout: float = 120.0

    def _allowed(self, name: str) -> bool:
        return any(t["function"]["name"] == name
                   for t in tool_schemas(self.surface, self.environment))

    def dispatch(self, name: str, arguments: dict) -> ToolOutcome:
        if not self._allowed(name):
            return ToolOutcome(f"[error: tool '{name}' is not available on this surface]", rc=2)
        handler = getattr(self, f"_do_{name}", None)
        if handler is None:
            return ToolOutcome(f"[error: no handler for '{name}']", rc=2)
        if not isinstance(arguments, dict):
            return ToolOutcome(f"[error: arguments for '{name}' must be an object]", rc=2)
        try:
            return handler(arguments)
        except subprocess.TimeoutExpired:
            return ToolOutcome(f"[error: '{name}' timed out]", rc=124)
        except Exception as exc:  # never leak a stack to the model
            return ToolOutcome(f"[error running '{name}': {type(exc).__name__}]", rc=1)

    # -- helpers ------------------------------------------------------------ #
    def _cap(self, text: str) -> str:
        if len(text) <= self.tool_result_max_chars:
            return text
        return text[: self.tool_result_max_chars] + "\n[...output truncated]"

    def _rank(self, label: str) -> int:
        return policy_bridge.sensitivity_rank(label, self.order)

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        return run_verb(self.root, argv, timeout=self.timeout, scrub_env=self.scrub_env)

    # -- read verbs --------------------------------------------------------- #
    def _do_oracle_status(self, args: dict) -> ToolOutcome:
        # Always minimized (S5); loop.py builds the prompt snapshot, but the
        # tool returns the same minimized view so nothing richer leaks.
        rc, out, _err = self._run(["status", "--json"])
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return ToolOutcome("[error: status unavailable]", rc=1)
        view = {
            "rung": (data.get("maturity") or {}).get("rung"),
            "memory": data.get("memory"),
            "authority": {k: (data.get("authority") or {}).get(k)
                          for k in ("rows", "confirmed")},
            "review_inbox_total": (data.get("review_inbox") or {}).get("total"),
        }
        return ToolOutcome(self._cap(json.dumps(view, indent=2)), rc=rc)

    def _do_oracle_search(self, args: dict) -> ToolOutcome:
        terms = str(args.get("terms", "")).strip()
        if not terms:
            return ToolOutcome("[error: search needs 'terms']", rc=2)
        k = args.get("k", 8)
        try:
            k = max(1, min(int(k), 25))
        except (TypeError, ValueError):
            k = 8
        # Ceiling forced last; any model sensitivity token already excluded
        # because the schema has no such field -- but strip defensively (M5).
        argv = ["search", "query", f"--q={terms}", "--k", str(k),
                "--max-sensitivity", self.max_sensitivity]
        argv = [a for a in argv if not _SENSITIVITY_FLAG_RE.match(a)] + \
               ["--max-sensitivity", self.max_sensitivity]
        rc, out, _err = self._run(argv)
        return ToolOutcome(self._cap(out.strip() or "[no results]"), rc=rc)

    def _do_oracle_answer(self, args: dict) -> ToolOutcome:
        obj = str(args.get("business_object", "")).strip()
        if not obj:
            return ToolOutcome("[error: answer needs 'business_object']", rc=2)
        argv = ["answer", "--object", obj, "--format", "json"]
        q = str(args.get("question", "")).strip()
        if q:
            argv += ["--question", q]
        rc, out, _err = self._run(argv)  # rc is the VERDICT (0/2/3/4), not error
        envelope = None
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            return ToolOutcome("[error: answer envelope unparseable]", rc=1)
        # Ceiling enforcement (C1): withhold grounded payload above ceiling.
        ceiling = (envelope.get("sensitivity_ceiling") or "public")
        if self._rank(ceiling) > self._rank(self.max_sensitivity):
            fix = envelope.get("suggested_fix") or []
            stub = (f"[withheld: this answer requires {ceiling} clearance, above the "
                    f"{self.max_sensitivity} ceiling for this provider]")
            if fix:
                stub += "\nTo proceed, the operator can run:\n" + "\n".join(f"  {c}" for c in fix)
            return ToolOutcome(stub, envelope=envelope, rc=rc)
        return ToolOutcome(self._cap(out.strip()), envelope=envelope, rc=rc)

    def _do_oracle_review(self, args: dict) -> ToolOutcome:
        # Titles only on a trusted local model; counts otherwise (C1).
        if self.environment == "local_agent" and self.surface == "local":
            argv = ["review", "list", "--json", "--limit", "15"]
        else:
            argv = ["review", "summary", "--json"]
        rc, out, _err = self._run(argv)
        return ToolOutcome(self._cap(out.strip() or "[inbox empty]"), rc=rc)

    def _do_oracle_brief(self, args: dict) -> ToolOutcome:
        if self.environment == "external" or self._rank(self.max_sensitivity) < self._rank("internal"):
            return ToolOutcome("[brief is not available below an internal local ceiling]", rc=2)
        rc, out, _err = self._run(["brief", "gen", "--json"])
        return ToolOutcome(self._cap(out.strip()), rc=rc)

    def _do_oracle_loops_due(self, args: dict) -> ToolOutcome:
        rc, out, _err = self._run(["loops", "due", "--json"])
        return ToolOutcome(self._cap(out.strip() or "[no loops due]"), rc=rc)

    def _do_oracle_checkpoint(self, args: dict) -> ToolOutcome:
        rc, out, _err = self._run(["checkpoint", "--json"])
        return ToolOutcome(self._cap(out.strip()), rc=rc)

    # -- write verbs -------------------------------------------------------- #
    def _do_oracle_ingest(self, args: dict) -> ToolOutcome:
        raw = args.get("paths")
        if not isinstance(raw, list) or not raw:
            return ToolOutcome("[error: ingest needs a non-empty 'paths' array]", rc=2)
        resolved: list[str] = []
        for p in raw:
            try:
                rp = Path(str(p)).expanduser().resolve()
            except (OSError, RuntimeError):
                return ToolOutcome(f"[error: bad path {p!r}]", rc=2)
            denied = self._ingest_denied(rp)
            if denied:
                return ToolOutcome(f"[denied: {denied}]", rc=2)
            if not rp.exists():
                return ToolOutcome(f"[error: path does not exist: {rp}]", rc=2)
            resolved.append(str(rp))
        rc, out, _err = self._run(["ingest", "batch", *resolved, "--json"])
        return ToolOutcome(self._cap(out.strip()), rc=rc)

    def _ingest_denied(self, rp: Path) -> str | None:
        s = str(rp)
        if _SECRETISH_PATH_RE.search(rp.name):
            return f"path looks secret-bearing: {rp.name}"
        if self.profile_dir and _is_within(rp, self.profile_dir):
            return "path is inside the Oracle profile directory"
        for sib in self.sibling_roots:
            if _is_within(rp, sib):
                return "path is inside another oracle instance"
        if self.ingest_roots:
            if not any(_is_within(rp, ir) for ir in self.ingest_roots):
                return "path is not within a configured ingest root"
        return None

    def _write_allowed(self) -> bool:
        gate = self.write_gate
        if gate is None:
            return True
        try:
            return bool(gate())
        except Exception:
            return False

    def _do_oracle_remember(self, args: dict) -> ToolOutcome:
        if not self._write_allowed():
            return ToolOutcome("[denied: write rate limit reached for this user]", rc=2)
        ur = str(args.get("user_request", "")).strip()
        ans = str(args.get("answer_summary", "")).strip()
        if not ur or not ans:
            return ToolOutcome("[error: remember needs 'user_request' and 'answer_summary']", rc=2)
        argv = ["remember", "--user-request", ur, "--answer-summary", ans]
        for bo in _str_list(args.get("business_objects")):
            argv += ["--business-object", bo]
        for c in _str_list(args.get("learned_claims")):
            argv += ["--learned-claim", c]
        if self.write_actor:
            argv += ["--actor", self.write_actor]
        argv += ["--json"]
        rc, out, _err = self._run(argv)
        return ToolOutcome(self._cap(out.strip() or "[remembered]"), rc=rc)

    def _do_oracle_capture(self, args: dict) -> ToolOutcome:
        if not self._write_allowed():
            return ToolOutcome("[denied: write rate limit reached for this user]", rc=2)
        kind = str(args.get("kind", "")).strip()
        target = str(args.get("target", "")).strip()
        if kind not in ("feedback", "value", "failure") or not target:
            return ToolOutcome("[error: capture needs 'kind' (feedback|value|failure) and 'target']", rc=2)
        argv = ["capture", kind, "--target", target]
        if kind in ("feedback", "value"):
            pol = str(args.get("polarity", "neutral")).strip() or "neutral"
            argv += ["--polarity", pol]
        else:  # failure
            sev = str(args.get("severity", "low")).strip() or "low"
            argv += ["--severity", sev]
            fm = str(args.get("failure_mode", "")).strip()
            if fm:
                argv += ["--failure-mode", fm]
        if self.write_actor:
            argv += ["--actor", self.write_actor]
        argv += ["--json"]
        rc, out, _err = self._run(argv)
        return ToolOutcome(self._cap(out.strip() or "[captured]"), rc=rc)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _str_list(v) -> list[str]:
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return []


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path = path.resolve()
        parent = parent.resolve()
    except (OSError, RuntimeError):
        return False
    return path == parent or parent in path.parents
