#!/usr/bin/env python3
"""0002_session_interfaces -- add/update Admin/User session-interface config.

Older spawned oracles may receive the new tool layer through ``oracle upgrade``
while preserving their sovereign ``oracle.yml``. This migration inserts the
default ``session_interfaces`` block when it is absent so setup audit and lint
have the machine-readable contract they now require. It also upgrades the
legacy slash-command prompt contract to the Admin-approval prompt contract.

Idempotency: if ``oracle.yml`` already has the current ``session_interfaces``
contract, the migration reports ``changed=False`` and leaves the file
byte-for-byte unchanged. Otherwise it inserts/updates a static block-style YAML
section, validates the result with the floor safe-subset YAML loader, then
writes it.
"""
from __future__ import annotations

from pathlib import Path

VERSION = "3.0.0"
DESCRIPTION = "Add or update the session_interfaces User/Admin contract in oracle.yml."

_ADMIN_PROMPT = (
    "This requires the Admin interface. Do you approve entering Admin mode "
    "for this request?"
)


_SESSION_BLOCK = """\
session_interfaces:
  default: user
  startup_prompt: false
  reset_policy: every_new_session
  modes:
    user:
      purpose: "Business-facing Oracle interface for questions, documents, feedback, company context, recommendations, and workproduct."
      tone: business_terms
      answer_protocol: required_for_material_answers
      control_plane_boundary: prompt_for_admin_approval
      admin_prompt: "This requires the Admin interface. Do you approve entering Admin mode for this request?"
      allow_capabilities:
        - ask_questions
        - provide_documents
        - give_feedback
        - teach_company_context
        - request_workproduct
        - propose_improvement
      block_capabilities:
        - change_architecture
        - install_connector
        - approve_connector
        - change_truth_authority
        - approve_raw_data_export
        - change_security_policy
        - update_oracle_config
        - approve_schema_migration
        - approve_canonical_folder_moves
        - manage_users
        - approve_sensitive_export
        - enable_autonomy
        - approve_kernel_upgrade
    admin:
      purpose: "Control-plane interface for architecture, config, governance, connectors, schemas, security, autonomy, kernel tooling, and repo structure."
      tone: control_plane_partner
      answer_protocol: required_for_material_answers
      role_gate: policy.require_role
      allow_capabilities:
        - "*"
      block_capabilities:
"""

_LEGACY_SWITCH_BLOCK = """\
  switch_commands:
    admin: "/admin"
    user: "/user"
"""

_LEGACY_BOUNDARY_BLOCK = """\
      control_plane_boundary: redirect_to_admin
      redirect: "That's an Admin-interface request. Type /admin to switch."
"""

_CURRENT_BOUNDARY_BLOCK = f"""\
      control_plane_boundary: prompt_for_admin_approval
      admin_prompt: "{_ADMIN_PROMPT}"
"""


def _safe_load(text: str):
    try:
        import oracle_yaml  # type: ignore
    except Exception:  # pragma: no cover - package import fallback
        from .. import oracle_yaml  # type: ignore
    return oracle_yaml.safe_load(text)


def _has_session_interfaces(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("session_interfaces"), dict)


def _update_legacy_session_interfaces(text: str) -> tuple[str, bool]:
    """Rewrite the legacy slash-command contract to the approval-prompt form."""
    new_text = text
    new_text = new_text.replace(_LEGACY_SWITCH_BLOCK, "")
    new_text = new_text.replace(_LEGACY_BOUNDARY_BLOCK, _CURRENT_BOUNDARY_BLOCK)
    return new_text, new_text != text


def _insert_before_top_key(text: str, key: str, block: str) -> str:
    """Insert ``block`` before a top-level ``key:`` line, or append at EOF."""
    lines = text.splitlines()
    target = f"{key}:"
    insert_at = None
    for i, line in enumerate(lines):
        if line.strip() == target and (not line or not line[0].isspace()):
            insert_at = i
            break

    block_lines = block.rstrip("\n").splitlines()
    if insert_at is None:
        out = list(lines)
        if out and out[-1].strip():
            out.append("")
        out.extend(block_lines)
        return "\n".join(out) + "\n"

    out = list(lines)
    insert = block_lines + [""]
    if insert_at > 0 and out[insert_at - 1].strip():
        insert = [""] + insert
    out[insert_at:insert_at] = insert
    return "\n".join(out) + "\n"


def apply(root: Path) -> dict:
    root = Path(root)
    cfg = root / "oracle.yml"
    if not cfg.exists():
        return {"changed": False, "notes": f"no oracle.yml at {cfg}"}

    text = cfg.read_text(encoding="utf-8")
    try:
        data = _safe_load(text)
    except Exception as exc:
        return {"changed": False, "notes": f"oracle.yml unparseable: {exc}"}

    if _has_session_interfaces(data):
        new_text, changed = _update_legacy_session_interfaces(text)
        if not changed:
            return {"changed": False, "notes": "session_interfaces already current"}
        try:
            new_data = _safe_load(new_text)
        except Exception as exc:  # pragma: no cover - defensive
            return {"changed": False, "notes": f"refused: edit broke yaml: {exc}"}
        if not _has_session_interfaces(new_data):  # pragma: no cover - defensive
            return {"changed": False, "notes": "refused: session_interfaces did not parse"}
        cfg.write_text(new_text, encoding="utf-8")  # safe_paths-internal: root-confined constant path (oracle.yml)
        return {"changed": True, "notes": "updated legacy session_interfaces prompt contract"}

    new_text = _insert_before_top_key(text, "ontology", _SESSION_BLOCK)
    try:
        new_data = _safe_load(new_text)
    except Exception as exc:  # pragma: no cover - defensive
        return {"changed": False, "notes": f"refused: edit broke yaml: {exc}"}

    if not _has_session_interfaces(new_data):  # pragma: no cover - defensive
        return {"changed": False, "notes": "refused: session_interfaces did not parse"}

    cfg.write_text(new_text, encoding="utf-8")  # safe_paths-internal: root-confined constant path (oracle.yml)
    return {"changed": True, "notes": "inserted session_interfaces default contract"}
