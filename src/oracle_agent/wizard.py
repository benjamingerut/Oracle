"""wizard.py -- interactive first-run setup (SPEC S8.1).

Two flows. The DEFAULT quick flow (``run_quick``, reached via ``run()``) is a
short, layperson-friendly path: company -> admin name -> provider menu -> API
key -> success banner. The instance is fixed (``default_instance`` or
``main``), the root is defaulted (``~/oracles/<instance>``), and the full
doctor report is printed ONLY when a check fails -- otherwise a one-line
ready-to-chat banner. The ADVANCED flow (``run_advanced``, reached via
``run(advanced=True)`` / ``oracle setup --advanced``) is the full wizard below.

Advanced flow walks the operator through: instance + root, spawn (or adopt),
provider preset, model, API key (stored to .env, never echoed), ingest roots,
optional Telegram, and an optional "Connect knowledge sources?" connector step
(P7-T8/T11) plus the dream-actuator step. Idempotent: re-running updates rather
than duplicates. Every prompt is skippable.

Connector step (P7-T8): pick a connector type, render its manifest from the
shipped template into the instance root's Connectors/<id>/, prompt EXPLICIT scope
allowlists (default-deny stays if skipped -- empty NEVER means "everything"),
collect secrets via getpass into <root>/.env.nosync (via
config.write_root_env_secret -- never config.json, never the profile .env that
the kernel subprocess cannot see), print provider-specific provisioning
instructions, then show a DRY-RUN plan BEFORE any bytes move. On confirmation the
first pull + ingest runs UNDER THE PER-ROOT FLOCK (P7S-22) so it cannot race a
serve tick on the same root. First-run experience (P7-T11): progress counts
(ingested / skipped / refused), the doctor zero-sources warning clearing, and a
note that review-inbox authority proposals carry connector provenance.

In a non-TTY run (no real terminal) the connector step is skipped with a printed
note -- the secret prompts (getpass) need a controlling terminal, and a scripted
stream drives the rest of the wizard.

Stdlib only.
"""
from __future__ import annotations

import getpass
import subprocess
import sys
from pathlib import Path

from . import config, doctor, spawn

PRESETS = {
    "nvidia": ("https://integrate.api.nvidia.com/v1", "meta/llama-3.3-70b-instruct", "ORACLE_LLM_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1", "claude-sonnet-4-6", "ORACLE_LLM_API_KEY"),
    "openai": ("https://api.openai.com/v1", "gpt-4o", "ORACLE_LLM_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "anthropic/claude-sonnet-4-6", "ORACLE_LLM_API_KEY"),
    "ollama": ("http://localhost:11434/v1", "llama3.1", ""),  # local, no key
    "custom": ("", "", "ORACLE_LLM_API_KEY"),
}


def _ask(prompt: str, default: str = "", *, stream_in=None, stream_out=None) -> str:
    out = stream_out or sys.stdout
    inp = stream_in or sys.stdin
    suffix = f" [{default}]" if default else ""
    out.write(f"{prompt}{suffix}: ")
    out.flush()
    line = inp.readline()
    if not line:
        return default
    line = line.strip()
    return line or default


# --------------------------------------------------------------------------- #
# connector step (P7-T8 / P7-T11)
# --------------------------------------------------------------------------- #
#
# Catalog of the connectors the wizard can set up. Each entry pins (frozen by the
# kernel manifests + RemoteConnector scope keys):
#   id            -- the connector id == the Connectors/<id>/ folder + manifest
#   label         -- a short human label
#   allowlists    -- ordered list of (source.<key>, prompt, kind) where kind is
#                    "list" (comma-separated -> YAML block list) or "scalar"
#                    (a single string value, e.g. the IMAP host / slack zip path).
#                    The scope allowlist is prompted EXPLICITLY; a blank answer
#                    leaves the bare key (default-deny -> refuses the pull).
#   secrets       -- env-var NAMES collected via getpass into <root>/.env.nosync
#   auth_flow     -- "loopback" (gdrive) | "device" (msgraph) | None (static)
#   provisioning  -- the provider-app provisioning lines printed before secrets
#
# Provider base URLs / endpoints are PINNED IN THE KERNEL CONNECTOR CODE, never
# here and never in the manifest (P7S-5) -- the wizard only writes the scope
# allowlist, the auth var NAMES (already in the shipped template), and the
# secret VALUES into the root's .env.nosync.
_CONNECTORS = {
    "gdrive": {
        "label": "Google Drive",
        "allowlists": [("folder_ids", "Drive folder ids to pull (comma-separated)", "list")],
        "secrets": ["GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN"],
        "auth_flow": "loopback",
        "provisioning": [
            "Google Drive setup (one-time, in the Google Cloud Console):",
            "  1. Create / pick a GCP project; enable the Google Drive API.",
            "  2. Configure the OAuth consent screen (Internal if Workspace).",
            "  3. Create an OAuth client of type 'Desktop app'; note the client",
            "     id + secret -- you'll paste them next.",
            "  4. The wizard runs a loopback browser flow to capture the offline",
            "     refresh token (Google's device flow cannot grant Drive scopes).",
        ],
    },
    "msgraph": {
        "label": "Microsoft 365 (SharePoint / OneDrive)",
        "allowlists": [
            ("sites", "SharePoint site ids to pull (comma-separated, blank=none)", "list"),
            ("drives", "OneDrive drive ids to pull (comma-separated, blank=none)", "list"),
        ],
        "secrets": ["MSGRAPH_CLIENT_ID", "MSGRAPH_REFRESH_TOKEN"],
        "auth_flow": "device",
        "provisioning": [
            "Microsoft 365 setup (one-time, in Azure / Entra ID):",
            "  1. Register an application (Microsoft Entra ID > App registrations).",
            "  2. Add delegated read scopes (Files.Read.All / Sites.Read.All).",
            "  3. Enable the public-client / device-code flow for the app.",
            "  4. Note the Application (client) id -- you'll paste it next; the",
            "     wizard runs the device-code flow to capture the refresh token.",
        ],
    },
    "notion": {
        "label": "Notion",
        "allowlists": [
            ("page_ids", "Notion page ids to pull (comma-separated, blank=none)", "list"),
            ("database_ids", "Notion database ids to pull (comma-separated, blank=none)", "list"),
        ],
        "secrets": ["NOTION_TOKEN"],
        "auth_flow": None,
        "provisioning": [
            "Notion setup (one-time):",
            "  1. Create an internal integration at notion.so/my-integrations;",
            "     copy its Internal Integration Token -- you'll paste it next.",
            "  2. SHARE each allowlisted page/database WITH the integration",
            "     (the integration only sees pages explicitly shared with it).",
        ],
    },
    "imap-mailbox": {
        "label": "IMAP mailbox",
        "allowlists": [
            ("host", "IMAP server host (e.g. imap.gmail.com)", "scalar"),
            ("folders", "Mailbox folders to pull (comma-separated)", "list"),
        ],
        "secrets": ["IMAP_USERNAME", "IMAP_APP_PASSWORD"],
        "auth_flow": None,
        "provisioning": [
            "IMAP mailbox setup (one-time):",
            "  1. Generate an APP PASSWORD at your mail provider (Gmail / O365",
            "     have retired basic auth except app passwords).",
            "  2. The connector uses certificate-verified IMAP4_SSL and opens",
            "     folders read-only (EXAMINE) -- it never mutates flags.",
        ],
    },
    "slack-export": {
        "label": "Slack workspace export",
        "allowlists": [
            ("path", "Path to the Slack export .zip", "scalar"),
            ("channels", "Channel names to pull (comma-separated)", "list"),
        ],
        "secrets": [],
        "auth_flow": None,
        "provisioning": [
            "Slack export setup (one-time, no token, no network):",
            "  1. A workspace admin downloads the export zip from",
            "     <workspace>.slack.com/services/export.",
            "  2. Point source.path at that .zip -- the connector reads it offline.",
        ],
    },
}

# Connectors that carry a scheduled cadence line in their source block (the
# pinned grammar hourly|daily|weekly|<N>h|<N>d; default daily). Used when
# rendering the manifest's source block.
_CADENCE_CONNECTORS = {"gdrive", "msgraph", "notion", "imap-mailbox"}


def _slug_yaml(value: str) -> str:
    """Render a scalar source value as a safe double-quoted YAML scalar."""
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _render_manifest(cid: str, *, sensitivity: str, scope: dict) -> str:
    """Render a schema-valid manifest for *cid* from the shipped template shape.

    ``scope`` maps source.<key> -> a value: a list (block list) or a scalar
    string. A blank/empty list answer is RENDERED AS A BARE KEY (default-deny:
    the kernel refuses the pull -- empty never means 'everything', P7S-13). The
    auth var NAMES come from the catalog (values live only in .env.nosync).
    """
    spec = _CONNECTORS[cid]
    system = {"gdrive": "gdrive", "msgraph": "msgraph", "notion": "notion",
              "imap-mailbox": "imap", "slack-export": "slack"}[cid]
    access_mode = "file_drop" if cid == "slack-export" else "api"
    lines = [
        f"# {cid} -- rendered by the Oracle setup wizard (P7-T8). Pull-only.",
        "# Scope allowlist below is default-deny: a bare 'key:' refuses the pull",
        "# (empty NEVER means 'everything'; I4 / P7S-13). Auth var NAMES only --",
        "# secret VALUES live in <root>/.env.nosync (0600), never here.",
        "# ROTATION = re-run the wizard step (upsert). REVOCATION = remove the var",
        "# AND revoke at the provider (removing the var disables the connector",
        "# here; the upstream token stays valid until revoked there -- P7S-6).",
        f"id: {cid}",
        f"system: {system}",
        "status: active",
        f"access_mode: {access_mode}",
        "locality: external_only" if access_mode == "api" else "locality: snapshot_local",
        "capture_tier: snapshot",
    ]
    # auth block.
    if spec["secrets"]:
        lines.append("auth:")
        lines.append(f"  method: {'oauth' if spec['auth_flow'] else 'token'}")
        lines.append("  vars:")
        for var in spec["secrets"]:
            lines.append(f"    - {var}")
    else:
        lines.append("auth:")
        lines.append("  method: none")
    lines += [
        "permissions: read_only",
        "freshness:",
        "  class: api" if access_mode == "api" else "  class: snapshot",
        "  expected_decay_days: 7",
        "schema_refresh:",
        "  enabled: false",
        "  remote_probe: false",
        "source:",
    ]
    for key, _prompt, kind in spec["allowlists"]:
        val = scope.get(key)
        if kind == "list":
            if isinstance(val, list) and val:
                lines.append(f"  {key}:")
                for item in val:
                    lines.append(f"    - {item}")
            else:
                lines.append(f"  {key}:")  # bare key -> default-deny refuse
        else:  # scalar
            if val:
                lines.append(f"  {key}: {_slug_yaml(val)}")
            else:
                lines.append(f"  {key}:")
    if cid == "imap-mailbox":
        lines.append("  since_days: 30")
    lines.append(f"  default_sensitivity: {sensitivity}")
    lines.append("  max_files: 500")
    if cid in _CADENCE_CONNECTORS:
        lines.append("  cadence: daily")
    return "\n".join(lines) + "\n"


def _parse_list(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _kernel(root: Path, argv: list[str], timeout: float = 120.0):
    """Run ``<root>/oracle <argv...>`` as a SCRUBBED-ENV argv subprocess.

    The scrubbed env drops every *_KEY/_TOKEN/_SECRET/_PASSWORD var (P7S-4), so
    the kernel pull can resolve its auth ONLY from the root's own .env.nosync --
    proving the wizard wrote the secret where a scheduled kernel pull can read it
    (the profile .env would be invisible). Returns (rc, stdout, stderr).
    """
    from .agentloop.verbtools import _scrubbed_env

    oracle = Path(root) / "oracle"
    proc = subprocess.run(
        [sys.executable, str(oracle), *argv],
        cwd=str(root), capture_output=True, text=True,
        timeout=timeout, env=_scrubbed_env(),
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _pull_counts(results: list) -> dict:
    """Tally a pull result list into the first-run progress shape (P7-T11)."""
    counts = {"ingested": 0, "planned": 0, "skipped_policy": 0,
              "skipped_out_of_scope": 0, "refused": 0, "failed": 0, "skipped": 0}
    for r in results or []:
        action = r.get("action") if isinstance(r, dict) else None
        if action in counts:
            counts[action] += 1
    return counts


def connector_step(root: Path, name: str, *, stream_in=None, stream_out=None,
                   getpass_fn=getpass.getpass) -> None:
    """Optional 'Connect knowledge sources?' wizard step (P7-T8 / P7-T11).

    Idempotent and fully skippable. In a non-TTY run the step is skipped with a
    printed note (the getpass secret prompts need a controlling terminal).
    """
    out = stream_out or sys.stdout

    want = _ask("Connect a knowledge source now? (y/N)", "N",
                stream_in=stream_in, stream_out=stream_out).lower()
    if not want.startswith("y"):
        out.write("  note: no connector configured. Run setup again any time to add one.\n")
        return

    # Secret prompts (getpass) need a controlling terminal; a scripted non-TTY
    # run cannot safely echo-suppress, so skip the step with a printed note.
    if not sys.stdin.isatty() and stream_in is None:
        out.write("  note: connector setup needs an interactive terminal — skipped.\n")
        return

    options = list(_CONNECTORS)
    out.write("Connector types:\n")
    for i, cid in enumerate(options, 1):
        out.write(f"  {i}) {cid} — {_CONNECTORS[cid]['label']}\n")
    pick = _ask("Pick a connector (number or id, blank to skip)", "",
                stream_in=stream_in, stream_out=stream_out).strip()
    if not pick:
        out.write("  note: no connector picked — skipped.\n")
        return
    cid = None
    if pick.isdigit() and 1 <= int(pick) <= len(options):
        cid = options[int(pick) - 1]
    elif pick in _CONNECTORS:
        cid = pick
    if cid is None:
        out.write(f"  warning: unknown connector {pick!r} — skipped.\n")
        return

    spec = _CONNECTORS[cid]
    out.write(f"\nConfiguring connector: {cid} ({spec['label']})\n")

    # Provider-specific provisioning instructions (printed BEFORE secrets).
    for ln in spec["provisioning"]:
        out.write(ln + "\n")

    # Explicit scope allowlist prompts (default-deny if skipped).
    scope: dict = {}
    for key, prompt, kind in spec["allowlists"]:
        raw = _ask("  " + prompt, "", stream_in=stream_in, stream_out=stream_out)
        if kind == "list":
            scope[key] = _parse_list(raw)
        else:
            scope[key] = raw.strip()

    # Sensitivity floor: confidential for mail (presumptively sensitive), else
    # internal. Ambiguity always classifies UP downstream (I4).
    sensitivity = "confidential" if cid == "imap-mailbox" else "internal"

    # Render + write the manifest into Connectors/<id>/<id>.manifest.yaml.
    manifest_dir = Path(root) / "Connectors" / cid
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_text = _render_manifest(cid, sensitivity=sensitivity, scope=scope)
    (manifest_dir / f"{cid}.manifest.yaml").write_text(manifest_text, encoding="utf-8")
    out.write(f"  manifest written: Connectors/{cid}/{cid}.manifest.yaml\n")

    # Collect secrets via getpass into the ROOT's .env.nosync (never config.json,
    # never the profile .env -- the kernel subprocess can only see the root file).
    for var in spec["secrets"]:
        out.write(f"  {var} (stored to <root>/.env.nosync; never echoed; blank to skip): ")
        out.flush()
        try:
            if sys.stdin.isatty():
                secret = getpass_fn("")
            elif stream_in is not None:
                secret = stream_in.readline().strip()
            else:
                secret = ""
        except Exception:
            secret = ""
        if secret:
            config.write_root_env_secret(root, var, secret)
            out.write("  saved.\n")
        else:
            out.write("  (skipped)\n")

    # Auth-flow note (the loopback / device flow is run by the kernel connector
    # at first pull when the refresh token is absent; the wizard documents it).
    if spec["auth_flow"] == "loopback":
        out.write("  note: gdrive uses a loopback browser OAuth flow to mint the "
                  "refresh token on first authorize.\n")
    elif spec["auth_flow"] == "device":
        out.write("  note: msgraph uses the device-code flow to mint the refresh "
                  "token on first authorize.\n")

    # Validate the manifest via the kernel (health loads + schema-validates it).
    rc, hout, herr = _kernel(root, ["connector", "--json", "health", cid])
    if rc == 2:  # ConnectorError: manifest invalid / failed to load
        out.write(f"  warning: connector health could not load {cid}: "
                  f"{(herr or hout).strip()[:200]}\n")

    # DRY-RUN plan BEFORE any bytes move (P7-T8 acceptance).
    out.write("\nDry-run plan (no bytes move yet):\n")
    rc, dout, derr = _kernel(root, ["connector", "--json", "pull", cid, "--dry-run"])
    plan = _parse_pull_payload(dout)
    if plan is None:
        out.write(f"  could not plan the pull: {(derr or dout).strip()[:200]}\n")
        out.write("  (fix the allowlist / credentials, then re-run setup)\n")
        return
    pcounts = _pull_counts(plan.get("results", []))
    out.write(f"  planned items: {pcounts['planned']}  "
              f"out-of-scope: {pcounts['skipped_out_of_scope']}  "
              f"refused: {pcounts['refused']}\n")

    proceed = _ask("Proceed with the first pull + ingest now? (y/N)", "N",
                   stream_in=stream_in, stream_out=stream_out).lower()
    if not proceed.startswith("y"):
        out.write("  note: first pull deferred. Re-run setup or pull later to fetch.\n")
        return

    _first_pull_and_ingest(root, name, cid, stream_out=out)


def _bootstrap_admin_name(root: Path) -> str:
    """Best-effort read of the spawned bootstrap admin name from oracle.yml.

    Used as the default ``--actor`` for the admin-only set-dream verb. A plain
    text scan (stdlib only, no YAML lib); falls back to 'Admin' if unreadable.
    """
    p = Path(root) / "oracle.yml"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "Admin"
    in_admin = False
    for raw in lines:
        s = raw.strip()
        if s.startswith("bootstrap_admin:"):
            in_admin = True
            continue
        if in_admin:
            if s.startswith("name:"):
                return s.split(":", 1)[1].strip().strip('"').strip("'") or "Admin"
            # leave the block on the next dedented top-level-ish key
            if s and not raw.startswith(" "):
                break
    return "Admin"


def dream_step(root: Path, name: str, *, stream_in=None, stream_out=None) -> None:
    """Optional operating-agent (dream actuator) wizard step (P5-T7a #1).

    Configures the headless dream actuator -- ``dream.command`` (e.g. ``claude
    -p``), ``dream.max_minutes``, ``dream.max_inbox_items`` -- EXCLUSIVELY through
    the constrained ``oracle admin autonomy set-dream`` kernel verb (P5S-7). The
    wizard NEVER writes ``autonomy.yml`` raw: that file also carries
    ``level``/caps/kill-switch, and ``dream.command`` is arbitrary argv the
    harness executes. The verb touches ONLY the ``dream.*`` subtree and is
    admin-only.

    The wizard CONFIGURES the actuator; it never raises the autonomy level
    (promotion stays an earned, admin-approved kernel flow). After configuring,
    it prints the level-2 gate status + the harness dry-run verdict so the
    operator sees exactly why dream sessions are or are not yet live.
    """
    out = stream_out or sys.stdout

    want = _ask("Configure the operating agent (headless dream actuator) now? (y/N)",
                "N", stream_in=stream_in, stream_out=stream_out).lower()
    if not want.startswith("y"):
        out.write("  note: no dream actuator configured. Run setup again any time.\n")
        return

    out.write(
        "  The dream actuator is the agent-harness command the scheduler runs to\n"
        "  convene a bounded, autonomy-gated review session. It is arbitrary argv,\n"
        "  so it is written ONLY through the admin-only set-dream kernel verb (it\n"
        "  can never touch the autonomy level or caps).\n"
    )
    command = _ask("Dream command (agent harness invocation, e.g. 'claude -p'; "
                   "blank to skip)", "",
                   stream_in=stream_in, stream_out=stream_out).strip()
    if not command:
        out.write("  note: no dream command entered — skipped.\n")
        return
    minutes = _ask("Session timeout in minutes", "30",
                   stream_in=stream_in, stream_out=stream_out).strip() or "30"
    items = _ask("Max Review-Inbox items per session", "10",
                 stream_in=stream_in, stream_out=stream_out).strip() or "10"
    actor = _bootstrap_admin_name(root)

    # Write the dream.* subtree via the kernel verb ONLY (never a raw autonomy.yml
    # write). admin autonomy set-dream -> actions module set-dream subcommand.
    argv = ["admin", "autonomy", "set-dream",
            "--actor", actor, "--role", "admin",
            "--command", command,
            "--max-minutes", minutes,
            "--max-inbox-items", items]
    rc, sout, serr = _kernel(root, argv)
    if rc != 0:
        out.write(f"  warning: set-dream verb failed: {(serr or sout).strip()[:200]}\n")
        return
    out.write(f"  dream actuator configured via the set-dream verb (command='{command}').\n")

    # Show the level-2 gate status + the harness dry-run verdict (P5-T7a #2 echo).
    lvl = _autonomy_level(root)
    if lvl < 2:
        out.write(
            f"  note: autonomy is at level {lvl}. Dream sessions require LEVEL 2 and\n"
            "        remain BLOCKED until you earn + approve a promotion\n"
            "        (oracle admin autonomy promote). Configuring the command does\n"
            "        not raise the level — that stays an earned, admin-approved flow.\n"
        )
    else:
        out.write("  autonomy is at level >=2: dream sessions are gate-eligible.\n")
    rc, dout, derr = _kernel(root, ["harness", "--root", str(root), "--dream", "--dry-run"])
    verdict = _parse_dream_dryrun(dout)
    if verdict:
        out.write(f"  dry-run verdict: {verdict}\n")


def _autonomy_level(root: Path) -> int:
    """Cheap text-scan of the autonomy level (mirrors scheduler.autonomy_level)."""
    p = Path(root) / "Meta.nosync" / "Autonomy" / "autonomy.yml"
    if not p.exists():
        return 0
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue
            if line.startswith("level:"):
                try:
                    return int(line.split(":", 1)[1].strip().strip('"').strip("'"))
                except ValueError:
                    return 0
    except OSError:
        return 0
    return 0


def _parse_dream_dryrun(text: str) -> str:
    """Pull the verdict+reason out of a ``harness --dream --dry-run`` JSON report."""
    import json
    try:
        data = json.loads((text or "").strip())
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    verdict = data.get("verdict") or data.get("status") or ""
    reason = data.get("reason") or ""
    return f"{verdict} ({reason})" if reason else str(verdict)


def _parse_pull_payload(text: str):
    import json
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _first_pull_and_ingest(root: Path, name: str, cid: str, *, stream_out) -> None:
    """Run the confirmed first pull + ingest UNDER THE PER-ROOT FLOCK (P7S-22).

    The flock (``~/.oracle/locks/<instance>.lock``) is held across BOTH the pull
    and the ingest so the first-run sequence cannot race a ``serve`` tick on the
    same root (SQLite contention / lost loop-note updates). A direct kernel-CLI
    pull by an admin is inherently OUTSIDE the shell's locks -- documented,
    admin-at-own-risk (P7-T8).
    """
    out = stream_out
    from .service.scheduler import root_lock

    out.write("\nFirst pull + ingest (holding the per-root lock):\n")
    out.write("  note: a direct kernel-CLI pull by an admin runs OUTSIDE this "
              "lock (admin-at-own-risk).\n")
    with root_lock(name):
        rc, pout, perr = _kernel(root, ["connector", "--json", "pull", cid])
        payload = _parse_pull_payload(pout)
        if payload is None:
            out.write(f"  pull failed: {(perr or pout).strip()[:200]}\n")
            return
        results = payload.get("results", [])
        counts = _pull_counts(results)
        out.write(f"  pulled: ingested={counts['ingested']}  "
                  f"skipped-by-policy={counts['skipped_policy']}  "
                  f"skipped-out-of-scope={counts['skipped_out_of_scope']}  "
                  f"refused={counts['refused']}  failed={counts['failed']}\n")

        # Ingest the freshly landed _INPUT/<id>/ files (still under the lock).
        landed_dir = Path(root) / "Workproduct.nosync" / "_INPUT" / cid
        floor = "confidential" if cid == "imap-mailbox" else "internal"
        ingested_n = 0
        if counts["ingested"] > 0 and landed_dir.is_dir():
            rc, iout, ierr = _kernel(
                root, ["ingest", "batch", str(landed_dir),
                       "--connector", cid, "--sensitivity", floor])
            if rc != 0 and "--connector" in (ierr or ""):
                # older ingest CLI without --connector: retry plain.
                rc, iout, ierr = _kernel(root, ["ingest", "batch", str(landed_dir)])
            ingested_n = _count_ingested(iout)
            out.write(f"  ingested into the corpus: {ingested_n} source record(s)\n")
        else:
            out.write("  nothing new landed to ingest.\n")

    # First-run experience (P7-T11): show the corpus growing + review note.
    n_sources = doctor._count_real_sources(Path(root))
    if n_sources > 0:
        out.write(f"  corpus now holds {n_sources} ingested source(s) "
                  "(the 'no ingested sources' warning has cleared).\n")
    out.write("  review-inbox authority proposals from these documents carry "
              f"their connector tag ({cid}) so attacker-supplied material is "
              "visibly attributable. Run 'oracle review' to triage them.\n")


def _count_ingested(text: str) -> int:
    """Parse 'batch: ingested=N failed=M' from the ingest CLI text output."""
    import re
    m = re.search(r"ingested=(\d+)", text or "")
    return int(m.group(1)) if m else 0


# --------------------------------------------------------------------------- #
# provider menu (quick flow). Numbered, layperson-friendly; maps to PRESETS.
# --------------------------------------------------------------------------- #
_PROVIDER_MENU = [
    ("nvidia", "NVIDIA — free API key, many open models, no install (recommended)"),
    ("ollama", "Ollama — fully local & private, no account, free forever"),
    ("anthropic", "Claude by Anthropic"),
    ("openai", "OpenAI"),
    ("openrouter", "OpenRouter"),
    ("custom", "Other (OpenAI-compatible endpoint)"),
]

# Where to get an API key, by preset (printed before the hidden key prompt).
_KEY_URLS = {
    "nvidia": "https://build.nvidia.com",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openai": "https://platform.openai.com/api-keys",
    "openrouter": "https://openrouter.ai/keys",
}

# NVIDIA NIM (build.nvidia.com): one free key unlocks many OpenAI-compatible
# open models. Offered as a short sub-menu when the operator picks NVIDIA so
# they choose quality-vs-speed at setup time (P-setup-free).
_NVIDIA_MODELS = [
    ("meta/llama-3.3-70b-instruct",
     "Llama 3.3 70B — strongest, best at the answer-protocol tool use (recommended)"),
    ("meta/llama-3.1-8b-instruct",
     "Llama 3.1 8B — faster, lighter on free credits"),
]

# Local Ollama (loopback → policy_bridge classifies it local_agent → may see up
# to `internal`). Smart-detected at setup so the free local path is turnkey
# when Ollama is already present.
_OLLAMA_HOST = "http://localhost:11434"
_OLLAMA_RECOMMENDED = "llama3.1"


def _read_secret(*, stream_in, out, getpass_fn) -> str:
    """Read a hidden secret using the same getpass/stream pattern as the full
    flow: a real tty uses getpass (no echo); a scripted stream reads a line."""
    try:
        if sys.stdin.isatty():
            return getpass_fn("")
        return stream_in.readline().strip() if stream_in else ""
    except Exception:
        return ""


def _pick_nvidia_model(*, stream_in, stream_out) -> str:
    """Offer the NVIDIA model sub-menu (number, or a pasted model id). Returns a
    usable model id; defaults to the strongest recommended model."""
    out = stream_out or sys.stdout
    default = _NVIDIA_MODELS[0][0]
    out.write("\nWhich model? (all free on NVIDIA's hosted tier)\n")
    for i, (_mid, label) in enumerate(_NVIDIA_MODELS, 1):
        out.write(f"  {i}. {label}\n")
    other = len(_NVIDIA_MODELS) + 1
    out.write(f"  {other}. Other (paste a model id from build.nvidia.com)\n")
    pick = _ask("Pick one (number or model id)", "1",
                stream_in=stream_in, stream_out=stream_out).strip()
    if pick.isdigit():
        n = int(pick)
        if 1 <= n <= len(_NVIDIA_MODELS):
            return _NVIDIA_MODELS[n - 1][0]
        if n == other:
            return _ask("Model id", default,
                        stream_in=stream_in, stream_out=stream_out).strip() or default
        return default
    # A non-numeric answer that looks like a model id is taken verbatim.
    return pick if "/" in pick else default


def _ollama_installed_models(timeout: float = 1.5):
    """Return the list of installed Ollama model base-names if the local Ollama
    server answers, else None (server down / not installed). Stdlib-only with a
    short timeout so a missing Ollama never stalls setup."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(_OLLAMA_HOST + "/api/tags", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    names = []
    for m in (data.get("models") or []):
        base = (m.get("name") or "").split(":")[0]
        if base:
            names.append(base)
    return names


def _ollama_pull(model: str) -> bool:
    """`ollama pull MODEL`, streaming progress to the terminal. True on success;
    never raises (a missing binary / failed pull degrades to a printed hint)."""
    import shutil
    if not shutil.which("ollama"):
        return False
    try:
        return subprocess.run(["ollama", "pull", model]).returncode == 0
    except Exception:
        return False


def _configure_ollama(*, stream_in, stream_out) -> str:
    """Smart-detect a local Ollama and return the model id to use, printing the
    right guidance. On a real tty with Ollama running but empty, offer to pull
    the recommended model. Always returns a usable default model id."""
    import shutil
    out = stream_out or sys.stdout
    installed = _ollama_installed_models()
    if installed is None:
        if shutil.which("ollama"):
            out.write("\nOllama is installed but not answering on localhost:11434.\n"
                      "  Start it, then pull a model:\n"
                      "    ollama serve &\n"
                      f"    ollama pull {_OLLAMA_RECOMMENDED}\n")
        else:
            out.write("\nOllama runs locally — no API key, fully private.\n"
                      "  Install it from https://ollama.com, then pull a model:\n"
                      f"    ollama pull {_OLLAMA_RECOMMENDED}\n")
        return _OLLAMA_RECOMMENDED
    if installed:
        chosen = _OLLAMA_RECOMMENDED if _OLLAMA_RECOMMENDED in installed else installed[0]
        out.write(f"\nFound Ollama running with '{chosen}' — using it. No API key needed.\n")
        return chosen
    # Server up, but no models pulled yet.
    out.write("\nOllama is running but has no models yet.\n")
    if sys.stdin.isatty():
        ans = _ask(f"Download {_OLLAMA_RECOMMENDED} now (~4.7GB)? (Y/n)", "Y",
                   stream_in=stream_in, stream_out=stream_out).strip().lower()
        if ans in ("", "y", "yes"):
            out.write(f"Pulling {_OLLAMA_RECOMMENDED} (this can take a few minutes)...\n")
            if _ollama_pull(_OLLAMA_RECOMMENDED):
                out.write(f"  pulled {_OLLAMA_RECOMMENDED}.\n")
            else:
                out.write(f"  pull failed — run `ollama pull {_OLLAMA_RECOMMENDED}` manually.\n")
        else:
            out.write(f"  skipped — run `ollama pull {_OLLAMA_RECOMMENDED}` when ready.\n")
    else:
        out.write(f"  Run: ollama pull {_OLLAMA_RECOMMENDED}\n")
    return _OLLAMA_RECOMMENDED


def run(advanced: bool = False, *, stream_in=None, stream_out=None,
        getpass_fn=getpass.getpass) -> int:
    """Dispatch to the quick flow (default) or the full advanced flow."""
    if advanced:
        return run_advanced(stream_in=stream_in, stream_out=stream_out,
                            getpass_fn=getpass_fn)
    return run_quick(stream_in=stream_in, stream_out=stream_out,
                     getpass_fn=getpass_fn)


def run_quick(*, stream_in=None, stream_out=None,
              getpass_fn=getpass.getpass) -> int:
    """Short, layperson-friendly setup (the new default, SPEC S8.1).

    company -> admin name -> provider menu -> API key -> success banner. No
    instance/root/ingest/Telegram/connector/dream questions: the instance is
    fixed (``default_instance`` or ``main``), the root is defaulted, and the
    full doctor report is printed ONLY when something actually fails. Fully
    scriptable via ``stream_in`` (a newline-only stream completes with all
    defaults and no key).
    """
    out = stream_out or sys.stdout
    out.write("Oracle setup (about a minute)\n"
              "-----------------------------\n")
    cfg = config.load_config()

    company = _ask("What company or team is this oracle for?", "My Company",
                   stream_in=stream_in, stream_out=stream_out)
    admin_default = (getpass.getuser() or "Admin")
    admin_default = admin_default[:1].upper() + admin_default[1:]
    admin = _ask("Your name (the oracle's admin)", admin_default,
                 stream_in=stream_in, stream_out=stream_out)

    # Instance fixed; root = the registered root for that instance if present,
    # else ~/oracles/<instance>. No questions asked.
    name = cfg.get("default_instance") or "main"
    existing = (cfg.get("instances") or {}).get(name, {}).get("root")
    root = (Path(existing).expanduser().resolve() if existing
            else (Path.home() / "oracles" / name).resolve())

    if (root / "oracle.yml").exists():
        out.write(f"Adopting existing oracle at {root}\n")
    else:
        out.write(f"Setting up your oracle at {root} ... ")
        out.flush()
        # Quick mode hides the spawn machinery (kernel stamping, loop records,
        # audit/lint chatter) from the operator; it is shown only on failure,
        # where it carries the diagnosis.
        import contextlib
        import io
        spawn_log = io.StringIO()
        with contextlib.redirect_stdout(spawn_log), contextlib.redirect_stderr(spawn_log):
            rc = spawn.main(["--root", str(root), "--company-name", company,
                             "--admin-name", admin])
        if rc != 0:
            out.write("failed.\n\n")
            out.write(spawn_log.getvalue())
            out.write("\nSetup could not create the oracle; aborting.\n")
            return rc
        out.write("done.\n")
    cfg = config.register_instance(cfg, name, root)

    # Provider — numbered menu (number OR preset name accepted; default "1").
    out.write("\nWhich AI provider should power your oracle?\n")
    for i, (_pid, label) in enumerate(_PROVIDER_MENU, 1):
        out.write(f"  {i}. {label}\n")
    pick = _ask("Pick one (number or name)", "1",
                stream_in=stream_in, stream_out=stream_out).strip()
    preset = "anthropic"
    if pick.isdigit() and 1 <= int(pick) <= len(_PROVIDER_MENU):
        preset = _PROVIDER_MENU[int(pick) - 1][0]
    elif pick in PRESETS:
        preset = pick
    base, model, key_env = PRESETS.get(preset, PRESETS["custom"])

    # "custom" has no defaults: ask base URL + model id (only here). NVIDIA and
    # Ollama get tailored sub-flows (model sub-menu / local smart-detect).
    if preset == "custom" or not base:
        base = _ask("Base URL (…/v1)", base or "",
                    stream_in=stream_in, stream_out=stream_out)
        model = _ask("Model id", model or "",
                     stream_in=stream_in, stream_out=stream_out)
    elif preset == "nvidia":
        model = _pick_nvidia_model(stream_in=stream_in, stream_out=stream_out)
    elif preset == "ollama":
        model = _configure_ollama(stream_in=stream_in, stream_out=stream_out)

    cfg["provider"].update({"name": preset, "base_url": base, "model": model,
                            "api_key_env": key_env})

    # Ollama needs no key (its guidance was already printed by _configure_ollama).
    if key_env:
        url = _KEY_URLS.get(preset)
        if preset == "nvidia":
            out.write(f"\nGet a free API key here: {url}\n")
            out.write("  (sign in, then 'Get API Key' — new accounts get free credits)\n")
        elif url:
            out.write(f"\nGet an API key here: {url}\n")
        out.write("Paste your API key (hidden; press Enter to add it later): ")
        out.flush()
        secret = _read_secret(stream_in=stream_in, out=out, getpass_fn=getpass_fn)
        if secret:
            config.set_env_secret(key_env, secret)
            out.write("  key saved.\n")
        else:
            out.write("  (no key yet — add one later with `oracle model set`)\n")

    config.save_config(cfg)

    # Finish: run doctor, but DO NOT dump the full report unless something fails.
    rep = doctor.run(name)
    if rep.worst_is_fail():
        out.write("\nSetup finished, but the health check found problems:\n\n")
        out.write(rep.render() + "\n")
        return 1
    n_warn = sum(1 for level, _, _ in rep.rows if level == doctor.WARN)
    out.write(
        f"\n✓ Your oracle is ready at {root}\n"
        "\n"
        "  Try:   oracle chat              talk with your oracle\n"
        "         oracle ingest <file>     teach it from your documents\n"
        "\n"
        "  Later: oracle setup --advanced  connectors, Telegram, scheduling\n"
        f"         oracle doctor            full health report "
        f"({n_warn} optional item{'s' if n_warn != 1 else ''} pending)\n"
    )
    return 0


def run_advanced(*, stream_in=None, stream_out=None, getpass_fn=getpass.getpass) -> int:
    out = stream_out or sys.stdout
    out.write("Oracle setup\n============\n")
    cfg = config.load_config()

    name = _ask("Instance name", cfg.get("default_instance") or "main",
                stream_in=stream_in, stream_out=stream_out)
    default_root = str(Path.home() / "oracles" / name)
    existing = (cfg.get("instances") or {}).get(name, {}).get("root", default_root)
    root_str = _ask("Oracle root path", existing, stream_in=stream_in, stream_out=stream_out)
    root = Path(root_str).expanduser().resolve()

    if (root / "oracle.yml").exists():
        out.write(f"Adopting existing oracle at {root}\n")
    else:
        company = _ask("Company name", "My Company", stream_in=stream_in, stream_out=stream_out)
        admin = _ask("Admin (your) name", "Admin", stream_in=stream_in, stream_out=stream_out)
        out.write(f"Spawning oracle at {root} ...\n")
        rc = spawn.main(["--root", str(root), "--company-name", company,
                         "--admin-name", admin])
        if rc != 0:
            out.write("Spawn failed; aborting setup.\n")
            return rc
    cfg = config.register_instance(cfg, name, root)

    # provider
    preset = _ask("Provider preset (nvidia/anthropic/openai/openrouter/ollama/custom)",
                  cfg.get("provider", {}).get("name", "anthropic"),
                  stream_in=stream_in, stream_out=stream_out)
    base, model, key_env = PRESETS.get(preset, PRESETS["custom"])
    if preset == "custom" or not base:
        base = _ask("Base URL (…/v1)", cfg.get("provider", {}).get("base_url", ""),
                    stream_in=stream_in, stream_out=stream_out)
    model = _ask("Model id", model or cfg.get("provider", {}).get("model", ""),
                 stream_in=stream_in, stream_out=stream_out)
    cfg["provider"].update({"name": preset, "base_url": base, "model": model,
                            "api_key_env": key_env})

    if key_env:
        out.write(f"API key (stored to .env as {key_env}; leave blank to skip): ")
        out.flush()
        try:
            secret = getpass_fn("") if sys.stdin.isatty() else (stream_in.readline().strip() if stream_in else "")
        except Exception:
            secret = ""
        if secret:
            config.set_env_secret(key_env, secret)
            out.write("  key saved.\n")

    # ingest roots
    existing_roots = cfg.get("ingest_roots") or []
    existing_roots_str = ",".join(str(r) for r in existing_roots)
    ingest_raw = _ask(
        "Ingest root directories (comma-separated absolute paths, blank=none)",
        existing_roots_str,
        stream_in=stream_in, stream_out=stream_out,
    )
    new_ingest_roots = []
    if ingest_raw.strip():
        for part in ingest_raw.split(","):
            part = part.strip()
            if not part:
                continue
            p = Path(part).expanduser()
            if not p.is_absolute():
                out.write(f"  warning: {part!r} is not an absolute path — skipped\n")
                continue
            if not p.exists():
                out.write(f"  warning: {part!r} does not exist — skipped\n")
                continue
            new_ingest_roots.append(str(p))
    cfg["ingest_roots"] = new_ingest_roots
    if not new_ingest_roots:
        out.write("  note: ingest_roots is empty — the chat agent cannot ingest from disk.\n")
        out.write("        Add directories to config.json ingest_roots later.\n")

    # optional telegram
    want_tg = _ask("Enable Telegram gateway? (y/N)", "N",
                   stream_in=stream_in, stream_out=stream_out).lower()
    if want_tg.startswith("y"):
        token_env = cfg["gateway"]["telegram"].get("token_env", "ORACLE_TELEGRAM_TOKEN")
        out.write(f"Telegram bot token (stored as {token_env}; blank to skip): ")
        out.flush()
        try:
            tok = getpass_fn("") if sys.stdin.isatty() else (stream_in.readline().strip() if stream_in else "")
        except Exception:
            tok = ""
        if tok:
            config.set_env_secret(token_env, tok)
        uid_raw = _ask("Your Telegram numeric user ID (to allowlist)", "",
                       stream_in=stream_in, stream_out=stream_out)
        cfg["gateway"]["telegram"]["enabled"] = True
        if uid_raw:
            uid_raw = uid_raw.strip()
            if not uid_raw.lstrip("-").isdigit():
                out.write(f"  warning: Telegram user ID {uid_raw!r} is not numeric — "
                          "skipped (non-numeric IDs never match)\n")
            else:
                # store as string (the shape that matches str(from.id))
                cfg["gateway"]["telegram"].setdefault("allowlist", {})[uid_raw] = {
                    "role": "user", "instance": name}

    config.save_config(cfg)

    # optional connector step (P7-T8 / P7-T11) -- pick a source, render its
    # manifest, collect creds into the root's .env.nosync, dry-run plan, then a
    # first pull + ingest under the per-root flock. Fully skippable + idempotent.
    out.write("\nConnect knowledge sources\n-------------------------\n")
    try:
        connector_step(root, name, stream_in=stream_in, stream_out=stream_out,
                       getpass_fn=getpass_fn)
    except Exception as exc:  # a connector misstep never aborts setup
        out.write(f"  warning: connector step error (skipped): {exc}\n")

    # optional operating-agent step (P5-T7a #1) -- configure the dream actuator
    # via the set-dream kernel verb ONLY. Fully skippable + idempotent; never
    # raises the autonomy level.
    out.write("\nOperating agent (dream actuator)\n--------------------------------\n")
    try:
        dream_step(root, name, stream_in=stream_in, stream_out=stream_out)
    except Exception as exc:  # a dream-step misstep never aborts setup
        out.write(f"  warning: dream step error (skipped): {exc}\n")

    out.write("\nRunning doctor ...\n\n")
    rep = doctor.run(name)
    out.write(rep.render() + "\n")
    return 1 if rep.worst_is_fail() else 0
