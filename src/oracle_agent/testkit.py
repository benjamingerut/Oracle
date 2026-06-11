"""oracle_agent/testkit.py -- eval substrate for shell tests (P1-T2).

Ships in the package so Phase 6 scoring can import it.  Module-scope imports
are stdlib + oracle_agent ONLY (never pytest).  The pytest fixture shim
(session-scoped spawned_root, pytest.skip) stays in tests/shell/conftest.py;
this module exposes only the pure spawn_test_root helper that conftest calls.

Constraint: no production module (cli/builder/loop/serve/gateway/scheduler/
config/doctor/wizard/spawn) may import this module.  Enforced by
test_testkit.py::test_no_production_module_imports_testkit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Sensitivity ordering (mirrors policy_bridge.CANONICAL_ORDER -- no import)
# ---------------------------------------------------------------------------
_CANONICAL_ORDER = ["public", "internal", "confidential", "restricted", "secret"]


def _sens_rank(label: str, order: list[str]) -> int:
    """Index of *label* in *order*; unknown labels get the strictest rank."""
    try:
        return order.index(label)
    except ValueError:
        return len(order)  # unknown -> beyond strictest


# ---------------------------------------------------------------------------
# ScriptedResponse
# ---------------------------------------------------------------------------

class ScriptedResponse:
    """Builder that produces a real llm.client.ChatResponse / ToolCall objects.

    Usage::

        ScriptedResponse("hello world")         # plain text turn
        ScriptedResponse(tool_calls=[
            ("call_1", "oracle_search", '{"terms": "revenue"}'),
        ])
    """

    def __init__(
        self,
        content: str | None = None,
        *,
        tool_calls: list[tuple[str, str, str]] | None = None,
        finish_reason: str | None = None,
    ):
        self._content = content
        self._tool_calls = tool_calls or []
        self._finish_reason = finish_reason

    def build(self):
        """Return a real :class:`oracle_agent.llm.client.ChatResponse`."""
        from oracle_agent.llm.client import ChatResponse, ToolCall

        tcs = [ToolCall(id=id_, name=name, arguments=args)
               for id_, name, args in self._tool_calls]
        return ChatResponse(
            content=self._content,
            tool_calls=tcs,
            finish_reason=self._finish_reason,
        )


# ---------------------------------------------------------------------------
# FakeLLM
# ---------------------------------------------------------------------------

class FakeLLM:
    """Scripted LLM client that records every chat() call.

    ``script`` is a list of :class:`ScriptedResponse` (or already-built
    :class:`~oracle_agent.llm.client.ChatResponse`) objects returned in order.
    When the script is exhausted a RuntimeError is raised so tests fail loudly.

    ``seen`` records every (messages_snapshot, has_tools) pair sent by the
    caller, in call order.  ``all_messages`` is the flat list of every message
    dict ever seen across all calls (useful for content scanning).
    """

    def __init__(self, script: list):
        self.script: list = list(script)
        self._index: int = 0
        # Each entry: (list[dict], bool) -- snapshot of messages + whether tools
        # were passed.
        self.seen: list[tuple[list[dict], bool]] = []

    def chat(self, messages, tools=None, **kw):
        """Called by AgentLoop._call via chat_with_retry."""
        self.seen.append((list(messages), bool(tools)))
        if self._index >= len(self.script):
            raise RuntimeError(
                f"FakeLLM script exhausted after {self._index} calls; "
                "add more ScriptedResponse entries"
            )
        item = self.script[self._index]
        self._index += 1
        # Accept either a pre-built ChatResponse or a ScriptedResponse builder.
        if hasattr(item, "build"):
            return item.build()
        return item

    # -- content scanning ---------------------------------------------------- #

    @property
    def all_messages(self) -> list[dict]:
        """All message dicts ever passed to chat(), flattened across calls."""
        out: list[dict] = []
        for msgs, _ in self.seen:
            out.extend(msgs)
        return out

    def assert_no_content_above(self, ceiling: str,
                                order: list[str] | None = None) -> None:
        """Assert no recorded message content exposes above-ceiling material.

        Scans every message dict in ``all_messages`` for verbtools sensitivity
        markers that indicate above-ceiling content leaked to the LLM:

        1. The verbtools ``[withheld: this answer requires X clearance`` stub
           carries the required clearance label X.  If X ranks above *ceiling*
           the withheld stub's presence proves the ceiling check fired -- but the
           CALLER can also plant raw above-ceiling content in tool-result messages
           (simulating a broken dispatcher that forgot to withhold) and this
           method will catch that.

        2. JSON search results include a ``"sensitivity": "X"`` field per hit.
           Any occurrence of ``"sensitivity": "X"`` where X ranks above ceiling
           indicates above-ceiling content leaked through the retrieval layer.

        Both checks work by scanning the string content of every message.  An
        ``AssertionError`` is raised on the FIRST violation found, with a
        descriptive message.

        ``order`` defaults to the canonical sensitivity ladder.
        """
        eff_order = list(order or _CANONICAL_ORDER)
        ceiling_rank = _sens_rank(ceiling, eff_order)

        for call_idx, (msgs, _) in enumerate(self.seen):
            for msg_idx, msg in enumerate(msgs):
                content = msg.get("content") or ""
                if not isinstance(content, str):
                    content = str(content)

                # --- Check 1: verbtools withheld marker -----------------------
                # Pattern: "[withheld: this answer requires {label} clearance"
                # We extract the label from such a marker and check its rank.
                idx = 0
                while True:
                    pos = content.find("[withheld: this answer requires ", idx)
                    if pos == -1:
                        break
                    rest = content[pos + len("[withheld: this answer requires "):]
                    end = rest.find(" clearance")
                    if end != -1:
                        label = rest[:end].strip()
                        label_rank = _sens_rank(label, eff_order)
                        if label_rank > ceiling_rank:
                            raise AssertionError(
                                f"FakeLLM.assert_no_content_above({ceiling!r}): "
                                f"call {call_idx} msg {msg_idx} (role={msg.get('role')!r}) "
                                f"contains withheld-marker for label {label!r} "
                                f"(rank {label_rank} > ceiling rank {ceiling_rank}). "
                                f"Content excerpt: {content[pos:pos+120]!r}"
                            )
                    idx = pos + 1

                # --- Check 2: raw sensitivity label in JSON search output ----
                # Pattern: "sensitivity": "X" where X is above ceiling.
                # Also catches raw sensitivity labels embedded in tool result
                # content (a planted-leak scenario).
                for label in eff_order:
                    if _sens_rank(label, eff_order) <= ceiling_rank:
                        continue  # at or below ceiling -- OK
                    # Check for JSON-style marker: "sensitivity": "label"
                    json_marker = f'"sensitivity": "{label}"'
                    if json_marker in content:
                        raise AssertionError(
                            f"FakeLLM.assert_no_content_above({ceiling!r}): "
                            f"call {call_idx} msg {msg_idx} (role={msg.get('role')!r}) "
                            f"contains JSON sensitivity marker {json_marker!r} "
                            f"(label rank {_sens_rank(label, eff_order)} > "
                            f"ceiling rank {ceiling_rank})."
                        )
                    # Check for bare label marker as used in verbtools output:
                    # "[sensitivity: label]" or "sensitivity_ceiling: label"
                    for bare_pattern in (
                        f"[sensitivity: {label}]",
                        f"sensitivity_ceiling: {label}",
                        f'"sensitivity_ceiling": "{label}"',
                    ):
                        if bare_pattern in content:
                            raise AssertionError(
                                f"FakeLLM.assert_no_content_above({ceiling!r}): "
                                f"call {call_idx} msg {msg_idx} (role={msg.get('role')!r}) "
                                f"contains above-ceiling marker {bare_pattern!r}."
                            )


# ---------------------------------------------------------------------------
# FakeEmbedClient (Phase 8 / P8-T4, additive to the frozen P1-T2 interface)
# ---------------------------------------------------------------------------

class FakeEmbedClient:
    """Scripted embeddings client that RECORDS every request payload.

    Mirrors the surface of :class:`oracle_agent.llm.client.EmbedClient` --
    ``embed(texts, *, model) -> list[list[float]]`` -- but performs no network
    egress. Every ``embed`` call appends ``{"model": model, "texts": [...]}`` to
    ``requests`` so a test can assert exactly which text reached the (simulated)
    embedding endpoint. This is the embedding analogue of
    :meth:`FakeLLM.assert_no_content_above`: the egress enforcer's whole job is
    to keep above-ceiling chunk/query text OUT of any embedding request, and
    this fake makes that byte-checkable.

    Vectors are produced by ``vector_fn(text) -> list[float]`` (a deterministic
    synthetic embedding by default), so the recorded count matches the input and
    the kernel ``vectors-add`` round-trips. Set ``fail=True`` (or a specific
    ``exc``) to simulate a transport failure, exercising the silent-lexical
    degradation path.
    """

    def __init__(self, vector_fn=None, *, dim: int = 8, fail: bool = False,
                 exc: Exception | None = None):
        self.requests: list[dict] = []
        self._dim = dim
        self._vector_fn = vector_fn or self._default_vector
        self.fail = fail
        self._exc = exc

    def _default_vector(self, text: str) -> list[float]:
        """Deterministic non-zero synthetic embedding (stdlib hashlib)."""
        import hashlib

        out: list[float] = []
        for i in range(self._dim):
            h = hashlib.sha256(f"{i}:{text}".encode("utf-8")).digest()
            out.append((h[0] / 255.0) - 0.5 + 1e-6)  # avoid an all-zero vector
        return out

    def embed(self, texts, *, model: str):
        self.requests.append({"model": model, "texts": list(texts)})
        if self.fail or self._exc is not None:
            from oracle_agent.llm.client import LLMError
            raise self._exc or LLMError(
                "network", "simulated embed transport failure",
                status=None, retryable=True,
            )
        return [self._vector_fn(str(t)) for t in texts]

    # -- content scanning ---------------------------------------------------- #

    @property
    def all_texts(self) -> list[str]:
        """Every text string ever sent to ``embed()``, across all requests."""
        out: list[str] = []
        for req in self.requests:
            out.extend(req.get("texts") or [])
        return out

    def assert_no_text(self, needle: str) -> None:
        """Assert ``needle`` never appeared in any embedding request text.

        The primary enforcer assertion: an above-ceiling chunk's distinctive
        text (or an above-ceiling query) must never reach the embedder. The test
        plants a known marker in the above-ceiling content and asserts it never
        egressed.
        """
        for req_idx, req in enumerate(self.requests):
            for txt_idx, txt in enumerate(req.get("texts") or []):
                if needle in str(txt):
                    raise AssertionError(
                        f"FakeEmbedClient.assert_no_text({needle!r}): found in "
                        f"request {req_idx} text {txt_idx} (model="
                        f"{req.get('model')!r}). Above-ceiling content egressed "
                        f"to the embedding endpoint."
                    )

    def assert_no_requests(self) -> None:
        """Assert the embedder was never called at all (zero egress)."""
        if self.requests:
            raise AssertionError(
                f"FakeEmbedClient.assert_no_requests(): expected zero embedding "
                f"requests but recorded {len(self.requests)} "
                f"(models: {[r.get('model') for r in self.requests]})."
            )


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@dataclass
class Harness:
    """Test harness wrapping a spawned oracle root.

    Typically obtained via :func:`spawn_test_root` and then wrapped::

        root = spawn_test_root(tmp_path / "root")
        h = Harness(root)
        loop = h.chat(script=[ScriptedResponse("hello")])
        result = loop.run_turn("hi")
    """

    root: Path

    def chat(
        self,
        script: list,
        surface: str = "local",
        environment: str = "local_agent",
        grounding=None,
        **loop_kwargs,
    ):
        """Return an :class:`~oracle_agent.agentloop.loop.AgentLoop` wired
        with a :class:`FakeLLM` that will replay *script*.

        The provider config is synthesized so that
        ``policy_bridge.environment_for`` derives *environment* from a real
        URL (loopback for ``local_agent``, a non-loopback for ``external``),
        exercising the real classification path instead of bypassing it.

        ``grounding`` is the :class:`~oracle_agent.agentloop.loop.GroundingPolicy`
        (Phase 3, required by the loop). It defaults to ``OBSERVE`` -- the v1
        footer-only behavior -- so existing tests keep the same behavior; pass
        ``GroundingPolicy.ENFORCE`` to exercise the repair loop. Repair-aware
        scripting just works: ``FakeLLM`` consumes one ``ScriptedResponse`` per
        model call, so an assert -> repair -> ground sequence is scripted as
        three (or more) entries in ``script``.

        Extra ``loop_kwargs`` (e.g. ``max_iterations``, ``max_repair``,
        ``turn_wall_clock``, ``clock``) are forwarded to ``AgentLoop``.

        The loop is built via the real builder's logic (Dispatcher, system
        prompt) with the FakeLLM swapped in for LLMClient.
        """
        from oracle_agent.agentloop import policy_bridge as pb
        from oracle_agent.agentloop.loop import (
            AgentLoop, GroundingPolicy, build_system_prompt,
        )
        from oracle_agent.agentloop.verbtools import Dispatcher

        if grounding is None:
            grounding = GroundingPolicy.OBSERVE

        fake_llm = FakeLLM([s.build() if hasattr(s, "build") else s
                            for s in script])

        # Synthesize a base_url whose host satisfies environment_for().
        if environment == "local_agent":
            base_url = "http://127.0.0.1:1/v1"
        else:
            base_url = "https://api.openai.com/v1"

        # Verify the real policy_bridge.environment_for gives what we expect.
        derived = pb.environment_for(base_url)
        assert derived == environment, (
            f"Harness.chat: synthesized URL {base_url!r} derives environment "
            f"{derived!r} but {environment!r} was requested."
        )

        order = pb.sensitivity_order(self.root)
        ceiling = pb.max_sensitivity_for(self.root, environment)

        dispatcher = Dispatcher(
            root=Path(self.root),
            surface=surface,
            environment=environment,
            max_sensitivity=ceiling,
            order=order,
        )
        system_prompt = build_system_prompt(
            Path(self.root), surface, environment, ceiling
        )
        return AgentLoop(
            fake_llm, dispatcher, system_prompt,
            grounding=grounding,
            retry_kwargs={"sleep": lambda *_: None},
            **loop_kwargs,
        )

    def gateway(self, updates: list[dict], allowlist: dict):
        """Return the Telegram adapter+core composite
        (:class:`~oracle_agent.gateway.telegram.TelegramGateway`) wired with a
        fake API that replays *updates* (P4-T1 / P4S-6 amendment).

        The P1-frozen ``Harness.gateway`` is deliberately amended here: it now
        returns the adapter+core composite (the Phase-4 ``TelegramGateway`` IS
        that composite) with the same assertion hooks -- ``_loops_ref``,
        ``_api_ref``, and ``sent``. Its loop factory STOPS hand-mirroring the
        builder's grounding decision and instead goes through the same
        fail-closed path: it consults ``builder.grounding_for`` so a fake model
        scripting ungrounded prose through the gateway is gated exactly as
        production gates it (P4S-1 fail-closed: surface != "local" => ENFORCE).

        The ledger dir is created under the root if not already present so
        the gateway can record turns without failing.
        """
        from oracle_agent.gateway.telegram import TelegramGateway, _noop_lock

        ledger_dir = self.root / "Meta.nosync" / "ledgers"
        ledger_dir.mkdir(parents=True, exist_ok=True)

        cfg = {"gateway": {"telegram": {
            "enabled": True,
            "allowlist": allowlist,
            "max_sensitivity": "internal",
            "per_user_writes_per_hour": 20,
        }}}

        api = _FakeAPI(updates=list(updates))

        loops: dict = {}

        def factory(user_id, instance, r):
            from oracle_agent.agentloop.builder import grounding_for
            from oracle_agent.agentloop.loop import AgentLoop
            from oracle_agent.agentloop.verbtools import Dispatcher
            from oracle_agent.agentloop import policy_bridge as pb

            fake_llm = FakeLLM([])
            order = pb.sensitivity_order(r)
            ceiling = "public"
            dispatcher = Dispatcher(
                root=Path(r), surface="gateway",
                environment="external",
                max_sensitivity=ceiling, order=order,
            )
            # Go through the SAME fail-closed decision the builder makes -- not a
            # hand-pinned ENFORCE. The loop surface is the literal "gateway", so
            # grounding_for returns ENFORCE via the fail-closed (!= "local")
            # branch (P4S-1).
            grounding = grounding_for(cfg, "gateway")
            loop = AgentLoop(
                fake_llm, dispatcher, "SYS",
                grounding=grounding,
                turn_wall_clock=120.0,
                retry_kwargs={"sleep": lambda *_: None},
            )
            loops[(user_id, instance)] = loop
            return loop

        gw = TelegramGateway(
            api, cfg, {"main": self.root}, factory,
            clock=lambda: 1000.0,
            sleep=lambda t: None,
            profile_dir=None,
            root_lock_factory=_noop_lock,
        )
        gw._loops_ref = loops  # expose for assertions
        gw._api_ref = api      # expose for assertions
        gw.sent = api.sent     # expose sent replies for assertions (P4S-6)
        return gw


@dataclass
class _FakeAPI:
    """Minimal fake Telegram API for use in Harness.gateway()."""

    updates: list = field(default_factory=list)
    sent: list = field(default_factory=list)
    fail: bool = False

    def get_updates(self, offset, timeout=25):
        if self.fail:
            raise OSError("simulated network error")
        return [u for u in self.updates if u["update_id"] >= offset]

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


# ---------------------------------------------------------------------------
# spawn_test_root -- pure helper (no pytest)
# ---------------------------------------------------------------------------

def spawn_test_root(dest: Path, name: str = "testco") -> Path:
    """Spawn a real oracle root at *dest* and return it.

    Pure helper: no pytest dependency.  Raises :class:`RuntimeError` on
    failure so callers (conftest or direct users) can decide how to handle it.
    """
    src = Path(__file__).resolve().parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src.parent) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [
            sys.executable, "-m", "oracle_agent.spawn",
            "--root", str(dest),
            "--company-name", name,
            "--codename", name.lower().replace(" ", "_"),
            "--admin-name", f"{name} Admin",
        ],
        capture_output=True, text=True, env=env,
    )
    if proc.returncode != 0 or not (dest / "oracle.yml").exists():
        raise RuntimeError(
            f"spawn_test_root failed (rc={proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
    return dest
