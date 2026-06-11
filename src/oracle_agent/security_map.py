"""Security guarantee map for the oracle shell layer.

Enumerates every shell security/correctness guarantee implied by STRESS.md
(C1-C3, H1-H4, M1-M5, L1-L3, STRESS-I1-I4) and SPEC.md S10, each pointing
at its enforcing test or lint target.

Frozen interface (PHASE-1-foundation-hardening.md):
  GUARANTEES: list[Guarantee]
  ADVISORY_ALLOWED: frozenset[str]
  def verify_enforcers(repo_root: Path) -> list[str]

Stdlib only -- no third-party imports.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Guarantee:
    id: str          # "SH-001"
    statement: str   # one sentence
    enforcer: str    # "tests/shell/test_foo.py::test_bar" or "lint:<target>"
    kind: str        # "test" | "lint" | "advisory"
    source: str      # originating doc reference, e.g. "C1", "H2", "SPEC-S10"


# ---------------------------------------------------------------------------
# Guarantee registry
# ---------------------------------------------------------------------------

GUARANTEES: list[Guarantee] = [

    # ------------------------------------------------------------------
    # C1 -- output ceiling enforcement: all read verbs filtered
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-001",
        statement=(
            "oracle_answer output above the session ceiling is withheld and replaced "
            "with a refusal stub before entering the model context."
        ),
        enforcer="tests/shell/test_verbtools.py::test_answer_above_ceiling_is_withheld",
        kind="test",
        source="C1",
    ),
    Guarantee(
        id="SH-002",
        statement=(
            "oracle_brief, oracle_checkpoint, and oracle_loops_due are structurally "
            "excluded from the schema when environment == external."
        ),
        enforcer="tests/shell/test_verbtools.py::test_external_drops_checkpoint_and_loops_due",
        kind="test",
        source="C1",
    ),
    Guarantee(
        id="SH-003",
        statement=(
            "A hallucinated call to a verb dropped for the current environment is "
            "denied fail-closed by the dispatcher."
        ),
        enforcer="tests/shell/test_verbtools.py::test_dropped_verb_denied_on_external",
        kind="test",
        source="C1",
    ),
    Guarantee(
        id="SH-004",
        statement=(
            "The gateway surface never exposes oracle_ingest, oracle_brief, or "
            "oracle_checkpoint regardless of environment."
        ),
        enforcer="tests/shell/test_verbtools.py::test_gateway_surface_is_reduced",
        kind="test",
        source="C1",
    ),
    Guarantee(
        id="SH-005",
        statement=(
            "No control-plane verb (admin, truth, policy-mutate, upgrade, backup) "
            "appears in any tool schema on any surface."
        ),
        enforcer="tests/shell/test_verbtools.py::test_no_control_plane_tool_anywhere",
        kind="test",
        source="C1",
    ),

    # ------------------------------------------------------------------
    # C2 -- redirect blocking + local_agent host guard
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-006",
        statement=(
            "HTTP 3xx redirects raise immediately; Authorization and body are never "
            "re-sent cross-origin."
        ),
        enforcer="tests/shell/test_llm_client.py::test_redirect_is_blocked",
        kind="test",
        source="C2",
    ),
    Guarantee(
        id="SH-007",
        statement=(
            "A local_agent LLMClient refuses at request time if the resolved host is "
            "not loopback, closing the TOCTOU window."
        ),
        enforcer="tests/shell/test_llm_client.py::test_per_request_guard_local_agent_blocks_swapped_url",
        kind="test",
        source="C2",
    ),
    Guarantee(
        id="SH-008",
        statement=(
            "HTTP (non-TLS) connections carrying an API key to a non-loopback host "
            "are refused before any data is sent."
        ),
        enforcer="tests/shell/test_llm_client.py::test_http_plaintext_with_api_key_to_nonloopback_refused",
        kind="test",
        source="C2",
    ),

    # ------------------------------------------------------------------
    # C3 -- policy bridge never imports root code
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-009",
        statement=(
            "The policy bridge never imports code from a registered instance root; "
            "policy checks shell out to the root's own oracle CLI."
        ),
        enforcer="tests/shell/test_policy_bridge.py::test_ceiling_local_agent_is_internal",
        kind="test",
        source="C3",
    ),
    Guarantee(
        id="SH-010",
        statement=(
            "max_sensitivity_for fails closed to 'public' when the root oracle CLI "
            "returns an error."
        ),
        enforcer="tests/shell/test_policy_bridge.py::test_ceiling_fails_closed_to_public_on_error",
        kind="test",
        source="C3",
    ),

    # ------------------------------------------------------------------
    # H1 -- minimized status snapshot
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-011",
        statement=(
            "The system prompt's embedded status snapshot contains only maturity rung "
            "and bare counts; it never includes most_urgent, due-loop titles, or object names."
        ),
        enforcer="tests/shell/test_agentloop.py::test_system_prompt_byte_stable_across_turns",
        kind="test",
        source="H1",
    ),
    Guarantee(
        id="SH-012",
        statement=(
            "oracle_status output sent to the model is replaced by the minimized "
            "snapshot, not the full status JSON."
        ),
        enforcer="tests/shell/test_verbtools.py::test_status_is_minimized",
        kind="test",
        source="H1",
    ),

    # ------------------------------------------------------------------
    # H2 -- allow-minimized is not a grant
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-013",
        statement=(
            "The sensitivity ceiling is the highest label whose policy verdict is "
            "exactly 'allow'; allow-minimized is never auto-released."
        ),
        enforcer="tests/shell/test_policy_bridge.py::test_ceiling_external_is_public",
        kind="test",
        source="H2",
    ),
    Guarantee(
        id="SH-014",
        statement=(
            "For a local_agent environment the sensitivity ceiling resolves to "
            "'internal', not 'confidential' or above."
        ),
        enforcer="tests/shell/test_policy_bridge.py::test_ceiling_local_agent_is_internal",
        kind="test",
        source="H2",
    ),

    # ------------------------------------------------------------------
    # H3 -- gateway serves private chats only
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-015",
        statement=(
            "The gateway ignores any update that is not a private chat where "
            "chat.id == from.id, making no LLM call and sending no reply."
        ),
        enforcer="tests/shell/test_telegram.py::test_group_chat_ignored_even_for_allowlisted",
        kind="test",
        source="H3",
    ),
    Guarantee(
        id="SH-016",
        statement=(
            "The gateway ignores updates with no 'from' field."
        ),
        enforcer="tests/shell/test_telegram.py::test_fromless_update_ignored",
        kind="test",
        source="H3",
    ),
    Guarantee(
        id="SH-017",
        statement=(
            "An unknown sender (not in the allowlist) is silently ignored with no "
            "reply and no LLM call."
        ),
        enforcer="tests/shell/test_telegram.py::test_unknown_sender_ignored_no_reply",
        kind="test",
        source="H3",
    ),

    # ------------------------------------------------------------------
    # H4 -- oracle_ingest path allowlist
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-018",
        statement=(
            "oracle_ingest rejects any path not under a configured ingest_roots entry "
            "without spawning a subprocess."
        ),
        enforcer="tests/shell/test_verbtools.py::test_ingest_denies_outside_ingest_roots",
        kind="test",
        source="H4",
    ),
    Guarantee(
        id="SH-019",
        statement=(
            "oracle_ingest denies all model-driven ingest when ingest_roots is empty "
            "(fail-closed)."
        ),
        enforcer="tests/shell/test_verbtools.py::test_ingest_denied_when_no_ingest_roots_configured",
        kind="test",
        source="H4",
    ),
    Guarantee(
        id="SH-020",
        statement=(
            "oracle_ingest rejects paths that resolve under the profile directory, "
            "preventing secret theft via ~/.oracle/.env."
        ),
        enforcer="tests/shell/test_verbtools.py::test_ingest_denies_profile_dir",
        kind="test",
        source="H4",
    ),
    Guarantee(
        id="SH-021",
        statement=(
            "oracle_ingest rejects paths that resolve under any other registered "
            "instance root."
        ),
        enforcer="tests/shell/test_verbtools.py::test_ingest_denies_sibling_instance",
        kind="test",
        source="H4",
    ),
    Guarantee(
        id="SH-022",
        statement=(
            "Symlink escapes in ingest paths are detected after resolve(); a symlink "
            "pointing outside ingest_roots is denied."
        ),
        enforcer="tests/shell/test_verbtools.py::test_ingest_symlink_escape_denied",
        kind="test",
        source="H4",
    ),

    # ------------------------------------------------------------------
    # M1 -- env scrub
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-023",
        statement=(
            "The subprocess environment passed to oracle verbs is scrubbed of all "
            "*_KEY/*_TOKEN/*_SECRET/*_PASSWORD vars and the resolved provider.api_key_env "
            "and gateway.*.token_env names."
        ),
        enforcer="tests/shell/test_verbtools.py::test_env_is_scrubbed",
        kind="test",
        source="M1",
    ),

    # ------------------------------------------------------------------
    # M2 -- atomic 0600 secret write
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-024",
        statement=(
            "set_env_secret writes the .env file atomically via os.open(O_CREAT|O_WRONLY"
            "|O_TRUNC, 0o600) + os.replace; the file is never world-readable at any point."
        ),
        enforcer="tests/shell/test_config.py::test_set_env_secret_roundtrip_and_perms",
        kind="test",
        source="M2",
    ),

    # ------------------------------------------------------------------
    # M3 -- config secret-guard
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-025",
        statement=(
            "save_config raises ValueError if any string value contains a literal API "
            "key, bearer token, sk- prefix secret, or userinfo URL."
        ),
        enforcer="tests/shell/test_config.py::test_save_refuses_literal_api_key",
        kind="test",
        source="M3",
    ),
    Guarantee(
        id="SH-026",
        statement=(
            "save_config raises on values containing '://<user>:<pass>@' userinfo."
        ),
        enforcer="tests/shell/test_config.py::test_save_refuses_userinfo_url",
        kind="test",
        source="M3",
    ),
    Guarantee(
        id="SH-027",
        statement=(
            "save_config raises on values matching 'Bearer <token>' patterns."
        ),
        enforcer="tests/shell/test_config.py::test_save_refuses_bearer_token_anywhere",
        kind="test",
        source="M3",
    ),
    Guarantee(
        id="SH-028",
        statement=(
            "save_config raises on Anthropic sk-ant- style secret keys stored as "
            "literal values."
        ),
        enforcer="tests/shell/test_config.py::test_save_refuses_sk_ant_key",
        kind="test",
        source="M3",
    ),
    Guarantee(
        id="SH-029",
        statement=(
            "save_config raises on Telegram bot token patterns stored as literal values."
        ),
        enforcer="tests/shell/test_config.py::test_save_refuses_telegram_bot_token",
        kind="test",
        source="M3",
    ),

    # ------------------------------------------------------------------
    # M4 -- write provenance (advisory: attribution, not access filtering)
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-030",
        statement=(
            "Gateway-sourced capture/remember calls are tagged with "
            "--actor gateway_user:<id> provenance and rate-limited per user per hour."
        ),
        enforcer="tests/shell/test_telegram.py::test_write_rate_limit",
        kind="test",
        source="M4",
    ),

    # ------------------------------------------------------------------
    # M5 -- sensitivity flag smuggling prevention
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-031",
        statement=(
            "Any model-supplied --max-sensitivity or -S= token is stripped from "
            "search terms before argv composition; the dispatcher appends exactly one "
            "--max-sensitivity <ceiling> last."
        ),
        enforcer="tests/shell/test_verbtools.py::test_smuggled_sensitivity_flag_stripped_from_search_terms",
        kind="test",
        source="M5",
    ),

    # ------------------------------------------------------------------
    # L1/L2/L3 -- loopback classification
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-032",
        statement=(
            "environment_for classifies a URL as local_agent only when the hostname "
            "is the exact string 'localhost' or a literal IPv4/IPv6 loopback address; "
            "DNS is never consulted."
        ),
        enforcer="tests/shell/test_policy_bridge.py::test_only_exact_localhost_name_is_loopback",
        kind="test",
        source="L1",
    ),
    Guarantee(
        id="SH-033",
        statement=(
            "Non-loopback IPs such as 169.254.169.254 (cloud metadata) are classified "
            "as external; only literal 127.0.0.0/8 and ::1 qualify as loopback. "
            "0.0.0.0 (unspecified address) is not loopback and resolves to external."
        ),
        enforcer="tests/shell/test_policy_bridge.py::test_environment_classification[http://169.254.169.254/v1-external]",
        kind="test",
        source="L1",
    ),
    Guarantee(
        id="SH-034",
        statement=(
            "localhost.evil.com and similar subdomain rebinding attempts are "
            "classified as external."
        ),
        enforcer="tests/shell/test_policy_bridge.py::test_localhost_subdomain_is_external",
        kind="test",
        source="L2",
    ),
    Guarantee(
        id="SH-035",
        statement=(
            "IPv6 loopback (::1) is correctly classified as local_agent via ipaddress, "
            "not string matching."
        ),
        enforcer="tests/shell/test_policy_bridge.py::test_ipv6_loopback_forms",
        kind="test",
        source="L3",
    ),

    # ------------------------------------------------------------------
    # STRESS-I1 -- context eviction preserves tool-call pairing
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-036",
        statement=(
            "Context overflow eviction removes whole turn groups (user→next user); "
            "a tool_calls message is never left without its tool replies."
        ),
        enforcer="tests/shell/test_agentloop.py::test_eviction_preserves_toolcall_pairing",
        kind="test",
        source="STRESS-I1",
    ),

    # ------------------------------------------------------------------
    # STRESS-I2 -- ingest fail-closed when ingest_roots empty
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-037",
        statement=(
            "When ingest_roots is not configured the allowlist check denies all "
            "model-driven ingest (fail-closed, not skip-check)."
        ),
        enforcer="tests/shell/test_verbtools.py::test_ingest_denied_when_no_ingest_roots_configured",
        kind="test",
        source="STRESS-I2",
    ),

    # ------------------------------------------------------------------
    # STRESS-I3 -- subprocess env scrub for scheduler and kernel passthrough
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-038",
        statement=(
            "The scheduler tick_instance subprocess and the kernel passthrough "
            "both receive a scrubbed environment that excludes LLM and Telegram "
            "key vars."
        ),
        enforcer="tests/shell/test_verbtools.py::test_env_is_scrubbed",
        kind="test",
        source="STRESS-I3",
    ),

    # ------------------------------------------------------------------
    # STRESS-I4 -- access-refusal keywords not over-broad
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-039",
        statement=(
            "The gateway's access-change refusal matches only access-specific phrases "
            "and does not refuse legitimate messages that happen to contain 'approve' "
            "or 'grant'."
        ),
        enforcer="tests/shell/test_telegram.py::test_access_change_request_refused",
        kind="test",
        source="STRESS-I4",
    ),

    # ------------------------------------------------------------------
    # SPEC S10 / additional guarantees from SPEC test plan
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-040",
        statement=(
            "A prompt injection delivered inside a tool result stays as data; it "
            "does not elevate to a system-level command."
        ),
        enforcer="tests/shell/test_agentloop.py::test_injection_in_tool_output_stays_data",
        kind="test",
        source="SPEC-S5",
    ),
    Guarantee(
        id="SH-041",
        statement=(
            "The agent loop enforces a max_iterations cap; when reached it issues "
            "one forced 'answer now' turn with tools disabled."
        ),
        enforcer="tests/shell/test_agentloop.py::test_iteration_cap_forces_answer",
        kind="test",
        source="SPEC-S5",
    ),
    Guarantee(
        id="SH-042",
        statement=(
            "Per-root flock serializes two concurrent run_verb calls to the same "
            "instance, preventing ledger corruption and sqlite lock errors."
        ),
        enforcer="tests/shell/test_scheduler.py::test_flock_serializes_two_concurrent_run_verbs",
        kind="test",
        source="SPEC-S6",
    ),
    Guarantee(
        id="SH-043",
        statement=(
            "The scheduler tick skips a root non-blockingly when the per-root lock "
            "is held by another process, rather than stalling the daemon."
        ),
        enforcer="tests/shell/test_scheduler.py::test_tick_skips_when_root_locked_nb",
        kind="test",
        source="SPEC-S6",
    ),
    Guarantee(
        id="SH-044",
        statement=(
            "When autonomy is disabled, tick_instance returns skipped=True with rc=0 "
            "and does not spawn the harness subprocess."
        ),
        enforcer="tests/shell/test_scheduler.py::test_tick_skips_when_autonomy_off",
        kind="test",
        source="SPEC-S6",
    ),
    Guarantee(
        id="SH-045",
        statement=(
            "API keys and secrets never appear in LLM client exception messages, "
            "repr output, or log lines."
        ),
        enforcer="tests/shell/test_llm_client.py::test_api_key_never_in_error",
        kind="test",
        source="SPEC-S2",
    ),
    Guarantee(
        id="SH-046",
        statement=(
            "The gateway LRU loop cache evicts the least-recently-used entry when "
            "capacity (64) is reached."
        ),
        enforcer="tests/shell/test_telegram.py::test_lru_cache_evicts_least_recently_used",
        kind="test",
        source="SPEC-S7",
    ),
    Guarantee(
        id="SH-047",
        statement=(
            "The gateway turn acquires and holds the per-root flock for the entire "
            "duration of the turn."
        ),
        enforcer="tests/shell/test_telegram.py::test_gateway_turn_holds_root_lock",
        kind="test",
        source="SPEC-S7",
    ),
    Guarantee(
        id="SH-048",
        statement=(
            "The oracle_agent shell package imports only stdlib and package-local "
            "modules; no third-party runtime dependency is introduced."
        ),
        enforcer="tests/shell/test_stdlib_only.py::test_shell_is_stdlib_only",
        kind="test",
        source="SPEC-S10",
    ),
    Guarantee(
        id="SH-049",
        statement=(
            "No shell source module uses shell=True in any subprocess call."
        ),
        enforcer="tests/shell/test_stdlib_only.py::test_shell_has_no_shell_true",
        kind="test",
        source="SPEC-S10",
    ),
    Guarantee(
        id="SH-050",
        statement=(
            "The doctor command checks each instance's kernel tools_version against "
            "the vendored version and warns on skew."
        ),
        enforcer="tests/shell/test_cli.py::test_version_skew_warns",
        kind="test",
        source="SPEC-S8",
    ),
    Guarantee(
        id="SH-051",
        statement=(
            "oracle chat --max-sensitivity can only lower the ceiling, never raise it "
            "above the provider-derived ceiling."
        ),
        enforcer="tests/shell/test_cli.py::test_chat_ceiling_can_only_lower",
        kind="test",
        source="SPEC-S8",
    ),
    Guarantee(
        id="SH-052",
        statement=(
            "doctor --name scopes all checks to only the named instance and does not "
            "iterate over others."
        ),
        enforcer="tests/shell/test_cli.py::test_doctor_named_instance_only",
        kind="test",
        source="SPEC-S8",
    ),
    Guarantee(
        id="SH-053",
        statement=(
            "doctor flags a non-HTTPS non-loopback provider endpoint as [fail]."
        ),
        enforcer="tests/shell/test_cli.py::test_doctor_non_https_non_loopback_fails",
        kind="test",
        source="SPEC-S8",
    ),
    Guarantee(
        id="SH-054",
        statement=(
            "The upgrade suggestion in doctor output references --from-kernel with "
            "the vendored kernel directory, not a version string."
        ),
        enforcer="tests/shell/test_cli.py::test_doctor_upgrade_suggestion_has_from_kernel",
        kind="test",
        source="SPEC-S8",
    ),
    Guarantee(
        id="SH-055",
        statement=(
            "The secret-scan lint gate (make secret) runs over all shell source "
            "files and the vendored kernel template tree on every CI run."
        ),
        enforcer="lint:secret",
        kind="lint",
        source="SPEC-S1",
    ),

    Guarantee(
        id="SH-056",
        statement=(
            "oracle_brief is only offered when ceiling >= 'internal' AND environment "
            "!= external; it is structurally absent from external schemas."
        ),
        enforcer="tests/shell/test_verbtools.py::test_external_drops_brief",
        kind="test",
        source="C1",
    ),

    # ------------------------------------------------------------------
    # Advisory guarantee (SPEC-level, not C*/H* — per P1S-10)
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-057",
        statement=(
            "Per-line sensitivity scanning of oracle_brief output is NOT implemented "
            "in v1. briefing.py emits only a document-level ceiling; no per-line or "
            "per-section markers exist. The availability gate (SH-056) is the current "
            "enforcer. Per-line scan is upstream kernel work (roadmap)."
        ),
        enforcer="tests/shell/test_verbtools.py::test_external_drops_brief",
        kind="advisory",
        source="SPEC-S4",
    ),

    # ------------------------------------------------------------------
    # C2 (P2S-2) -- egress veto: loopback != processing locality
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-058",
        statement=(
            "A loopback endpoint serving a provably cloud-proxied model (an Ollama "
            "':cloud' model, or one whose /api/tags entry carries a non-empty "
            "remote_host) is reclassified external by the egress veto, capping the "
            "ceiling at public on every surface."
        ),
        enforcer="tests/shell/test_policy_bridge.py::test_egress_veto_in_build_loop_forces_external",
        kind="test",
        source="C2",
    ),

    # ------------------------------------------------------------------
    # P3 -- forced grounding (Phase 3). The gate is OBJECT-level, not
    # proposition-level (P3S-4): it forces answer-protocol invocation and
    # verdict-obligation compliance PER BUSINESS OBJECT. It does not verify the
    # asserted proposition against the grounded payload -- that is Phase 6/8
    # eval territory. The wording below is deliberately limited to that.
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-059",
        statement=(
            "No material company claim is released to any user without a covering "
            "answer-protocol envelope for its business object whose obligations the "
            "text honors (gateway: no override). The gate is object-level, not "
            "proposition-level: it forces protocol invocation and verdict-obligation "
            "compliance per object; it does not verify the asserted proposition."
        ),
        enforcer="tests/shell/test_agentloop.py::test_enforce_repair_exhaustion_redacts_with_notice",
        kind="test",
        source="P3S-4",
    ),
    Guarantee(
        id="SH-060",
        statement=(
            "The gateway surface runs forced-grounding in ENFORCE, hard-coded in "
            "builder.build_loop: a config attempting to lower it (or a stray gateway "
            "grounding key) still yields ENFORCE. There is no gateway grounding key "
            "in the config schema, so the mode is beyond the reach of config "
            "migration, prompt injection, or tool output."
        ),
        enforcer="tests/shell/test_agentloop.py::test_build_loop_gateway_enforce_immutable_to_config",
        kind="test",
        source="P3S-11",
    ),
    Guarantee(
        id="SH-061",
        statement=(
            "An assertion on a refused-class envelope is reported as mismatched and "
            "withheld; a withheld:true envelope (even with a grounded exit_code) is "
            "treated as refused-class, since the model never saw the grounded payload."
        ),
        enforcer="tests/shell/test_grounding.py::test_withheld_envelope_is_mismatched",
        kind="test",
        source="P3S-1",
    ),
    Guarantee(
        id="SH-062",
        statement=(
            "On exhausted repair budget the unbacked/mismatched claim units are "
            "redacted whole (never shipped with a disclaimer); a fully-redacted reply "
            "ships notice + footer alone. Any grounding-gate exception withholds the "
            "ENTIRE reply (fail closed, never fail open)."
        ),
        enforcer="tests/shell/test_agentloop.py::test_gate_error_withholds_entire_reply",
        kind="test",
        source="P3S-8",
    ),
    Guarantee(
        id="SH-063",
        statement=(
            "The local forced-grounding default lives in the single SECURITY_KEYS-"
            "protected config key chat.grounding_default; a migration that drops or "
            "alters it is refused at load, so an operator's deliberate ENFORCE can "
            "never be silently flipped back to OBSERVE."
        ),
        enforcer="tests/shell/test_config.py::test_grounding_default_drop_caught",
        kind="test",
        source="P3S-11",
    ),
    Guarantee(
        id="SH-064",
        statement=(
            "Connector credentials live only in the instance root's .env.nosync "
            "(written by the shell's write_root_env_secret or the kernel's sanctioned "
            "rotated-token writer, 0600) and resolve there even under the shell's "
            "scrubbed kernel-subprocess environment; they never land in config.json."
        ),
        enforcer=(
            "tests/shell/test_wizard_connectors.py::"
            "test_scrubbed_env_pull_resolves_auth_from_root_env_nosync"
        ),
        kind="test",
        source="P7S-4",
    ),
    Guarantee(
        id="SH-065",
        statement=(
            "Wizard-driven connector setup holds the per-root flock across the whole "
            "first pull + ingest, so it cannot interleave with a serve tick or "
            "gateway turn on the same root."
        ),
        enforcer=(
            "tests/shell/test_wizard_connectors.py::"
            "test_flock_held_during_pull_and_ingest"
        ),
        kind="test",
        source="P7S-22",
    ),

    # ------------------------------------------------------------------
    # P4 -- gateway platform (Phase 4). The builder fails CLOSED on surface,
    # the gateway core is the sole decision point, and write provenance is
    # surface-namespaced (P4S-1/2/17).
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-066",
        statement=(
            "builder.grounding_for fails CLOSED on surface: any surface that is not "
            "exactly 'local' yields GroundingPolicy.ENFORCE plus the gateway "
            "wall-clock cap and the reduced gateway tool surface. A wiring mistake "
            "that leaks a transport name (http/slack/email) into build_loop cannot "
            "fall through to the local OBSERVE default; the loop surface is always "
            "the literal 'gateway' and the transport name lives only in "
            "InboundMessage.surface."
        ),
        enforcer=(
            "tests/shell/test_agentloop.py::test_build_loop_http_surface_is_gateway_class"
        ),
        kind="test",
        source="P4S-1",
    ),
    Guarantee(
        id="SH-067",
        statement=(
            "GatewayCore injects the ceiling (per-surface max_sensitivity, "
            "public-capped on a non-private channel), the surface-namespaced write "
            "actor, and its own allow_write write-gate into a pinned loop_builder "
            "signature -- not a prebuilt factory closed over someone else's ceiling. "
            "An adapter bug or a serve-wiring slip can drop a message but can never "
            "substitute any of the three (the 'holder' hack is gone)."
        ),
        enforcer=(
            "tests/shell/test_gateway_core.py::test_core_injects_ceiling_actor_and_gate"
        ),
        kind="test",
        source="P4S-2",
    ),
    Guarantee(
        id="SH-068",
        statement=(
            "Gateway write provenance is surface-namespaced as "
            "gateway_user:<surface>:<id> so M4 attribution survives multiple "
            "surfaces (the seam Phase 5's identity model consumes). The metadata-only "
            "gateway_turn ledger row carries the transport 'surface' and never "
            "serializes message bodies or the adapter 'meta' dict."
        ),
        enforcer=(
            "tests/shell/test_gateway_core.py::test_core_namespaces_actor_per_surface"
        ),
        kind="test",
        source="P4S-17",
    ),

    # ------------------------------------------------------------------
    # P8 -- retrieval quality (Phase 8). An embedding request IS content
    # egress: the policy bridge's environment x sensitivity ceiling, INCLUDING
    # the egress veto, applies to embedding requests exactly as to chat
    # requests, enforced at the SHELL dispatch in agentloop/embedder.py (I5),
    # failing closed (I4). The egress guarantees point at the SHELL enforcer
    # tests, never at the kernel embedding_event stamp (which is shell
    # ATTESTATION the kernel cannot verify -- P8S-15).
    # ------------------------------------------------------------------
    Guarantee(
        id="SH-069",
        statement=(
            "An embedding request never carries content above the embedding "
            "endpoint's POST-VETO environment ceiling: the egress veto applies "
            "to embedding endpoints exactly as to chat endpoints, so a loopback "
            "Ollama listener serving a ':cloud' (or remote_host-proxied) "
            "embedding model is reclassified external and embeds nothing above "
            "public."
        ),
        enforcer=(
            "tests/shell/test_embedder_enforcer.py::test_embed_ceiling_applies_egress_veto"
        ),
        kind="test",
        source="P8S-1",
    ),
    Guarantee(
        id="SH-070",
        statement=(
            "An over-ceiling chunk is dropped at the embed dispatch against its "
            "CURRENT (re-read) sensitivity and is never present in any embedding "
            "request; an external/public-ceiling embedder embeds public chunks "
            "only (the dispatch boundary holds, zero reclassification window)."
        ),
        enforcer=(
            "tests/shell/test_embedder_enforcer.py::"
            "test_embed_dispatch_blocks_over_ceiling_chunks"
        ),
        kind="test",
        source="P8S-14",
    ),
    Guarantee(
        id="SH-071",
        statement=(
            "An external/vetoed embedding endpoint (post-veto ceiling 'public') "
            "never receives a non-public query: vector search is disabled for "
            "every internal-and-above surface by the frozen query rule "
            "(rank(retrieval_ceiling) <= rank(embed_ceiling)), and retrieval "
            "falls back to lexical, silently and correctly."
        ),
        enforcer=(
            "tests/shell/test_embedder_enforcer.py::"
            "test_external_embedder_disables_vector_search_above_public"
        ),
        kind="test",
        source="P8S-3",
    ),
    Guarantee(
        id="SH-072",
        statement=(
            "Any error computing the embedding endpoint's ceiling fails closed "
            "to 'public', so no embedding request leaves above public when the "
            "classification path itself errors (fail closed, I4)."
        ),
        enforcer=(
            "tests/shell/test_embedder_enforcer.py::"
            "test_ceiling_error_fails_closed_no_egress"
        ),
        kind="test",
        source="P8S-1",
    ),
    Guarantee(
        id="SH-073",
        statement=(
            "Any embed transport failure (network error, the 10 s query-path "
            "timeout, or a malformed response) degrades the search silently to "
            "lexical -- no error is surfaced to the model, and the query vector "
            "is simply absent."
        ),
        enforcer=(
            "tests/shell/test_embedder_enforcer.py::"
            "test_query_embedder_silent_lexical_on_transport_failure"
        ),
        kind="test",
        source="P8S-3",
    ),
    Guarantee(
        id="SH-074",
        statement=(
            "The vectors-* CLI surface (vectors-add, vectors-pending, "
            "vectors-prune, --qvec-stdin) is structurally absent from every tool "
            "schema on every surface, so the model gains neither a bulk "
            "corpus-text export nor a vector-injection tool (SH-005-style "
            "structural exclusion)."
        ),
        enforcer=(
            "tests/shell/test_verbtools.py::"
            "test_vectors_subcommands_absent_from_all_tool_schemas"
        ),
        kind="test",
        source="P8S-10",
    ),
    Guarantee(
        id="SH-075",
        statement=(
            "The query-vector stdin payload is composed EXCLUSIVELY by shell "
            "code -- a config-sourced embedding-model string plus computed "
            "floats -- so the model-supplied search terms never reach the stdin "
            "channel and the argv chokepoint discipline (I2) is preserved."
        ),
        enforcer=(
            "tests/shell/test_verbtools.py::"
            "test_search_qvec_payload_composed_shell_side_only"
        ),
        kind="test",
        source="P8S-3",
    ),
    Guarantee(
        id="SH-076",
        statement=(
            "Every embedding batch is ledgered metadata-only: the "
            "embedding_event row carries source ids, chunk count, model and the "
            "applied-ceiling attestation, and NEVER chunk text or vectors. The "
            "environment/ceiling stamp is shell ATTESTATION (the kernel cannot "
            "verify shell egress, P8S-15); the ENFORCED egress guarantees point "
            "at the shell enforcer tests above, not at this row."
        ),
        enforcer=(
            "tests/shell/test_embedder_enforcer.py::"
            "test_embed_pending_writes_metadata_only_ledger"
        ),
        kind="test",
        source="P8S-15",
    ),
    Guarantee(
        id="SH-077",
        statement=(
            "Vectors are content-equivalent to their chunk and never out-clear "
            "it: the shell handoff file carrying vectors to the kernel "
            "vectors-add chokepoint is created 0600 under the in-root tmp.nosync "
            "directory and is deleted after the call returns, so vectors never "
            "transit a world-readable tmp path (P8S-10)."
        ),
        enforcer=(
            "tests/shell/test_embedder_enforcer.py::"
            "test_embed_pending_handoff_file_is_gone_after"
        ),
        kind="test",
        source="P8S-10",
    ),
]

# ---------------------------------------------------------------------------
# Advisory allowlist
# Note: C*/H* sourced guarantees MUST NOT appear here (P1S-10).
# Only advisory guarantees from M/L/STRESS-I/SPEC sources are permitted.
# ---------------------------------------------------------------------------

ADVISORY_ALLOWED: frozenset[str] = frozenset({
    "SH-057",  # per-line brief scan not implemented in v1; upstream kernel work (SPEC-S4)
})


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

def _makefile_targets(repo_root: Path) -> set[str]:
    """Parse the top-level Makefile and return all defined target names."""
    makefile = repo_root / "Makefile"
    if not makefile.exists():
        return set()
    targets: set[str] = set()
    for line in makefile.read_text().splitlines():
        # Match lines like "target:" or "target: deps" — exclude variables
        stripped = line.split("#")[0].rstrip()
        if ":" in stripped and not stripped.startswith("\t") and not stripped.startswith(" "):
            candidate = stripped.split(":")[0].strip()
            # Skip variable assignments (contain =) and multi-word keys
            if candidate and " " not in candidate and "=" not in candidate:
                targets.add(candidate)
    return targets


def _collect_pytest_nodes(repo_root: Path) -> set[str]:
    """Run pytest --collect-only -q and return all collected node ids."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "tests/shell/"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    nodes: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith("=") and not line.startswith("no tests"):
            # strip leading markers/counts
            node = line.split(" ")[0]
            nodes.add(node)
    return nodes


def _node_has_skip_marker(repo_root: Path, node_id: str) -> bool:
    """Return True if the pytest node is marked skip, skipif, or xfail."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header",
         node_id],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    combined = result.stdout + result.stderr
    for marker in ("skip", "skipif", "xfail"):
        if marker in combined.lower():
            # Look for the marker in verbose collect output
            result2 = subprocess.run(
                [sys.executable, "-m", "pytest", "--collect-only", "-v", "--no-header",
                 node_id],
                capture_output=True,
                text=True,
                cwd=str(repo_root),
            )
            if f"<{marker}" in result2.stdout.lower() or f"mark.{marker}" in result2.stdout.lower():
                return True
    return False


def verify_enforcers(repo_root: Path) -> list[str]:
    """Return a list of violation strings.

    Violation classes (all four per frozen interface):
    1. Non-advisory guarantee whose enforcer is not a collected pytest node.
    2. Enforcer node is marked skip/skipif/xfail.
    3. kind='lint' enforcer does not name a real Makefile target
       (format: 'lint:<target>').
    4. kind='advisory' guarantee whose id is not in ADVISORY_ALLOWED.
    """
    violations: list[str] = []

    # Collect all pytest nodes once
    collected_nodes = _collect_pytest_nodes(repo_root)

    # Parse Makefile targets once
    makefile_targets = _makefile_targets(repo_root)

    for g in GUARANTEES:
        # ---- Violation class 4: advisory not in allowlist ----
        if g.kind == "advisory" and g.id not in ADVISORY_ALLOWED:
            violations.append(
                f"{g.id}: kind='advisory' but id not in ADVISORY_ALLOWED"
            )
            continue  # skip further checks for this guarantee

        if g.kind == "lint":
            # ---- Violation class 3: lint target not in Makefile ----
            if not g.enforcer.startswith("lint:"):
                violations.append(
                    f"{g.id}: kind='lint' but enforcer does not start with 'lint:' "
                    f"(got: {g.enforcer!r})"
                )
                continue
            target = g.enforcer[len("lint:"):]
            if target not in makefile_targets:
                violations.append(
                    f"{g.id}: lint target '{target}' not found in Makefile "
                    f"(known targets: {sorted(makefile_targets)})"
                )
            # lint guarantees don't have pytest nodes to check
            continue

        if g.kind == "advisory":
            # Advisory in allowlist — no further enforcement required
            continue

        # kind == "test" checks below
        # ---- Violation class 1: enforcer not a collected pytest node ----
        if g.enforcer not in collected_nodes:
            violations.append(
                f"{g.id}: enforcer node not collected by pytest: {g.enforcer!r}"
            )
            continue

        # ---- Violation class 2: enforcer node has skip/skipif/xfail marker ----
        if _node_has_skip_marker(repo_root, g.enforcer):
            violations.append(
                f"{g.id}: enforcer node is marked skip/skipif/xfail: {g.enforcer!r}"
            )

    return violations


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

def render_security_md(guarantees: list[Guarantee] = GUARANTEES) -> str:
    """Render the GUARANTEES list as docs/SECURITY.md content."""
    lines = [
        "# Shell Security Guarantees",
        "",
        "Auto-generated by `src/oracle_agent/security_map.py`. "
        "Do not edit by hand — regenerate via the test suite drift check.",
        "",
        "## Overview",
        "",
        f"Total guarantees: **{len(guarantees)}** "
        f"({sum(1 for g in guarantees if g.kind == 'test')} test-enforced, "
        f"{sum(1 for g in guarantees if g.kind == 'lint')} lint-enforced, "
        f"{sum(1 for g in guarantees if g.kind == 'advisory')} advisory).",
        "",
        "Every guarantee below names its enforcing test or lint target. "
        "The pytest suite (`tests/shell/`) is the CI gate — `make check` runs it "
        "on every cell. No separate CI step is needed (PHASE-1-foundation-hardening.md P1F-14).",
        "",
        "## Advisory guarantees",
        "",
        "Guarantees marked **advisory** below document a known limitation or deferred "
        "implementation. They must appear in `ADVISORY_ALLOWED` in `security_map.py`. "
        "No guarantee derived from a STRESS C*/H* finding may be advisory (P1S-10).",
        "",
        "---",
        "",
        "## Guarantees",
        "",
    ]

    # Group by source prefix
    by_source: dict[str, list[Guarantee]] = {}
    for g in guarantees:
        prefix = g.source.split("-")[0] if "-" in g.source else g.source
        by_source.setdefault(prefix, []).append(g)

    # Ordered output — write all guarantees in id order for determinism
    for g in sorted(guarantees, key=lambda x: x.id):
        kind_badge = {"test": "TEST", "lint": "LINT", "advisory": "ADVISORY"}.get(g.kind, g.kind.upper())
        lines.append(f"### {g.id} [{kind_badge}] — {g.source}")
        lines.append("")
        lines.append(g.statement)
        lines.append("")
        if g.kind == "advisory":
            lines.append(f"*Enforcer (availability gate):* `{g.enforcer}`")
            lines.append("")
            lines.append(
                f"*Advisory:* This guarantee documents a known v1 limitation. "
                f"It appears in `ADVISORY_ALLOWED`."
            )
        else:
            lines.append(f"*Enforcer:* `{g.enforcer}`")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "*This file is drift-tested by `tests/shell/test_security_map.py`. "
        "Any change to `GUARANTEES` must be followed by regenerating this file.*"
    )
    lines.append("")

    return "\n".join(lines)
