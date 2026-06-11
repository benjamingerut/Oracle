"""doctor.py -- diagnose the install, profile, instances, and provider (SPEC S8.2).

Each check prints ``[ok]/[warn]/[fail]`` with a one-line fix. Exit 0 iff no
``[fail]``. Read-only: doctor never mutates state.

Stdlib only.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config
from .agentloop import policy_bridge as pb

OK, WARN, FAIL = "ok", "warn", "fail"


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, msg: str, fix: str = "") -> None:
        self.rows.append((level, msg, fix))

    def worst_is_fail(self) -> bool:
        return any(level == FAIL for level, _, _ in self.rows)

    def render(self) -> str:
        out = []
        for level, msg, fix in self.rows:
            line = f"[{level}] {msg}"
            if fix and level != OK:
                line += f"\n        fix: {fix}"
            out.append(line)
        return "\n".join(out)


def _vendored_tools_version() -> str | None:
    manifest = (Path(__file__).resolve().parent / "assets" / "oracle-kernel"
                / ".kernel-manifest.json")
    try:
        return json.loads(manifest.read_text()).get("tools_version")
    except (OSError, json.JSONDecodeError):
        return None


def _root_tools_version(root: Path) -> str | None:
    try:
        return json.loads((root / ".kernel-manifest.json").read_text()).get("tools_version")
    except (OSError, json.JSONDecodeError):
        return None


def _is_non_loopback_http(base_url: str) -> bool:
    """Return True iff ``base_url`` is an ``http://`` URL whose host is NOT loopback.

    Self-contained: no DNS; checks literal host string only (matching S1's
    client refusal rule).  Loopback = 127.0.0.0/8, ::1, or the literal
    hostname ``localhost``.
    """
    if not base_url:
        return False
    try:
        parsed = urllib.parse.urlparse(base_url)
    except Exception:
        return False
    if (parsed.scheme or "").lower() != "http":
        return False
    host = parsed.hostname or ""
    if host.lower() == "localhost":
        return False
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_loopback:
            return False
    except ValueError:
        pass  # not a bare IP — treat as non-loopback
    return True


def _is_ollama_tags_reachable(base_url: str) -> bool:
    """Return True iff the base_url ORIGIN answers a parseable Ollama /api/tags.

    Read-only, 3s budget. Used to distinguish "Ollama, egress veto clear" from
    "non-Ollama loopback server we cannot vet" (STRESS C2). Any error -> False.
    """
    if not base_url:
        return False
    try:
        parts = urllib.parse.urlsplit(base_url)
    except ValueError:
        return False
    if not parts.scheme or not parts.hostname:
        return False
    host = parts.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port else host
    url = f"{parts.scheme}://{netloc}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            body = resp.read().decode("utf-8", "replace")
        data = json.loads(body)
    except (urllib.error.URLError, socket.timeout, OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("models"), list)


def _kernel_index_stats(root: Path) -> dict | None:
    """Run the kernel's ``oracle search stats`` (read-only) and parse the JSON.

    Returns the stats dict (chunks, vectors, vector_coverage, by_embedding_model,
    dim_mismatches, ...) or ``None`` on any failure. Doctor stays read-only.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [sys.executable, str(root / "oracle"), "search", "stats"],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


#: Mirror of knowledge_index.VECTOR_CONTINGENCY_THRESHOLD (P8S-7). The kernel is
#: vendored and never imported by the shell (I3); this constant is duplicated as
#: DATA so doctor can warn at the same corpus crossing the kernel pins. Kept in
#: sync by test_embedder_enforcer.py::test_doctor_contingency_threshold_matches_kernel.
_VECTOR_CONTINGENCY_THRESHOLD = 100_000


def _check_vectors(rep: "Report", name: str, root: Path,
                   embed_model: str, embed_env: str) -> None:
    """Per-instance vector-store health (P8-T6): coverage, orphan, contingency.

    Read-only via ``oracle search stats``. Reports:
      * vector coverage for the active embedding model (a coverage COLLAPSE
        relative to the chunk count is the post-reindex / DB-loss signature,
        since a wipe now implies a full-corpus re-egress -- P8S-13);
      * dim mismatches (same model name, different dim -- never fused, P8S-11);
      * a warning once the active-model vector count crosses the corpus
        contingency threshold (reduced-dimensions-then-int8 ladder, P8S-7).

    The orphan-vector backstop (P8S-6) is reported only when non-empty -- a
    single-transaction lifecycle should keep it empty; a non-empty result is the
    crash-tolerance signal that a vector outlived its chunk.
    """
    stats = _kernel_index_stats(root)
    if stats is None:
        return  # stats unavailable -> nothing to report (not a failure)
    chunks = int(stats.get("chunks") or 0)
    vec_total = int(stats.get("vectors") or 0)
    by_model = stats.get("by_embedding_model") or {}
    coverage = stats.get("vector_coverage") or {}
    dim_mismatches = int(stats.get("dim_mismatches") or 0)

    if not embed_model:
        if vec_total:
            rep.add(WARN, f"instance '{name}': {vec_total} vector(s) present but no "
                          "embedding model is configured (orphaned coverage)",
                    "set provider.embeddings.model or run vectors-prune")
        return

    # Vector search is only meaningful for surfaces at/below the embed ceiling;
    # an external/public embedder embeds public chunks only. State coverage.
    active_cov = float(coverage.get(embed_model) or 0.0)
    active_n = int(by_model.get(embed_model) or 0)
    if chunks == 0:
        pass  # zero-source instance already warned elsewhere
    elif active_n == 0:
        rep.add(WARN, f"instance '{name}': embedding model {embed_model!r} has no "
                      "vectors yet (search is lexical-only until backfill runs)",
                "the backfill drains pending chunks on scheduler ticks "
                "(autonomy must be enabled); a reindex/DB-loss implies a "
                "full-corpus re-embed through the egress endpoint")
    elif active_cov < 0.5:
        rep.add(WARN, f"instance '{name}': vector coverage for {embed_model!r} is "
                      f"{active_cov:.0%} ({active_n}/{chunks}) — likely a coverage "
                      "collapse after a reindex/_wipe/DB loss (full-corpus re-embed "
                      "needed; auditable via embedding_event)",
                "let the backfill re-embed (autonomy on); confidential+ stays "
                "lexical-only by design")
    else:
        rep.add(OK, f"instance '{name}': vector coverage {active_cov:.0%} for "
                    f"{embed_model!r}")

    if dim_mismatches:
        rep.add(WARN, f"instance '{name}': {dim_mismatches} vector(s) share the "
                      "active model name but a different dim — skipped, never "
                      "fused (P8S-11)",
                "vectors-prune the stale-dim model, then re-embed under the "
                "current dim")

    if active_n >= _VECTOR_CONTINGENCY_THRESHOLD:
        rep.add(WARN, f"instance '{name}': {active_n} vectors for {embed_model!r} "
                      f"crosses the brute-force contingency threshold "
                      f"({_VECTOR_CONTINGENCY_THRESHOLD}) — interactive search "
                      "latency may degrade on the floor interpreter",
                "activate the contingency ladder IN ORDER: reduced provider "
                "`dimensions` (e.g. 256-512) first, then int8 quantization")

    orphans = _kernel_orphan_vectors(root)
    if orphans:
        rep.add(FAIL, f"instance '{name}': {len(orphans)} orphan vector(s) — a "
                      "vector outlived its chunk (crash mid single-transaction "
                      "lifecycle, P8S-6)",
                "run a reindex to rebuild the index cleanly (vectors are "
                "re-embedded through the egress endpoint)")


def _kernel_orphan_vectors(root: Path) -> list:
    """Cheap orphan-vector backstop (P8S-6) via the kernel index module.

    Shells out to a one-liner that calls ``KnowledgeIndex.orphan_vectors()``
    against the root's own vendored kernel (read-only). Any error -> empty (the
    backstop must never itself red-flag a healthy install).
    """
    import subprocess

    code = (
        "import json,sys;"
        "sys.path.insert(0, '_tools');"
        "import knowledge_index as k;"
        "idx=k.KnowledgeIndex(root='.');"
        "print(json.dumps(idx.orphan_vectors()));"
        "idx.close()"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return []
    out = (proc.stdout or "").strip()
    if not out:
        return []
    try:
        data = json.loads(out.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return []
    return data if isinstance(data, list) else []


def _count_real_sources(root: Path) -> int:
    """Count non-template markdown files in ``Memory.nosync/Sources/``.

    Template/context sentinel files start with ``_``; everything else is a
    real source record.
    """
    sources_dir = root / "Memory.nosync" / "Sources"
    if not sources_dir.is_dir():
        return 0
    return sum(
        1 for p in sources_dir.iterdir()
        if p.suffix.lower() == ".md" and not p.name.startswith("_")
    )


def _known_connector_ids(root: Path) -> list[str]:
    """Discover connector ids from the REAL manifest layout
    Connectors/<id>/<id>.manifest.yaml (the same discovery the kernel runtime
    and dashboard use; NOT the old top-level glob that found nothing)."""
    cdir = root / "Connectors"
    if not cdir.is_dir():
        return []
    ids: list[str] = []
    for sub in sorted(cdir.iterdir()):
        try:
            if sub.is_dir() and (sub / f"{sub.name}.manifest.yaml").exists():
                ids.append(sub.name)
        except OSError:
            continue
    return ids


def _connector_health(root: Path) -> list[dict]:
    """Run the kernel's ``connector health --json`` (read-only: PROBES, never
    pulls) and return the per-connector report list. A non-zero rc still yields
    the parsed reports (the verb reports broken connectors with rc 1)."""
    import subprocess

    try:
        proc = subprocess.run(
            [sys.executable, str(root / "oracle"), "connector", "--json", "health"],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return []
    out = (proc.stdout or "").strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [r for r in data if isinstance(r, dict)]


def _is_unconfigured_scaffold(note: str) -> bool:
    """True iff a ``broken`` health note is the default-deny / no-source signal
    of a pristine connector scaffold a fresh spawn ships (awaiting admin setup),
    rather than a genuine misconfiguration (unresolved declared auth vars,
    schema-invalid manifest, read_write misuse)."""
    n = (note or "").lower()
    markers = (
        "scope allowlist is empty",
        "source.path is required",
        "source path is required",
        "is required (the",
    )
    return any(m in n for m in markers)


def _check_connectors(rep: "Report", name: str, root: Path) -> None:
    """Add per-connector doctor rows for one instance.

    Each configured connector is checked for: manifest schema-valid + auth vars
    resolvable + ``health`` not ``broken`` -- each with a one-line fix. Doctor
    stays read-only (it probes via ``connector health``; it never pulls). The
    egress-honesty note (P7S-6) is pinned on the revocation fix line: removing a
    credential var disables the connector here, but the upstream token stays
    valid until revoked AT the provider.
    """
    cids = _known_connector_ids(root)
    if not cids:
        return  # no connectors configured -> nothing to report (not a warning)
    reports = {str(r.get("connector") or ""): r for r in _connector_health(root)}
    for cid in cids:
        rep_row = reports.get(cid)
        if rep_row is None:
            # health verb could not report it -> manifest likely invalid or the
            # connector failed to load. This is the schema/load failure path.
            rep.add(WARN, f"instance '{name}': connector '{cid}' health unavailable "
                          "(manifest invalid or adapter failed to load)",
                    f"oracle kernel {name} -- connector health {cid}  (read the error; "
                    "fix the manifest or auth vars)")
            continue
        status = str(rep_row.get("status") or "unknown")
        notes = rep_row.get("notes") or []
        first_note = str(notes[0]) if notes else ""
        if status == "broken" and _is_unconfigured_scaffold(first_note):
            # A pristine scaffold a fresh spawn ships (empty default-deny
            # allowlist / no source path / no auth vars yet) is NOT misconfigured
            # -- it is simply awaiting admin setup. WARN, not FAIL, so a healthy
            # fresh spawn does not red-flag on its own connector scaffolds.
            rep.add(WARN, f"instance '{name}': connector '{cid}' not configured yet"
                          + (f" — {first_note}" if first_note else ""),
                    f"configure its scope allowlist + credentials, then re-check: "
                    f"oracle kernel {name} -- connector health {cid}")
        elif status == "broken":
            # Genuinely misconfigured: schema-invalid manifest, unresolved auth
            # vars (declared but missing), read_write misuse. The fix names the
            # most common cause plus the egress-honesty caveat (P7S-6).
            rep.add(FAIL, f"instance '{name}': connector '{cid}' broken"
                          + (f" — {first_note}" if first_note else ""),
                    f"resolve auth vars in {root}/.env.nosync (e.g. app password for "
                    f"imap-mailbox), fix the manifest/allowlist, then re-check: "
                    f"oracle kernel {name} -- connector health {cid}. "
                    "NOTE: removing a credential var disables the connector here, but "
                    "the upstream token stays valid until you revoke it AT the provider")
        elif status == "degraded":
            rep.add(WARN, f"instance '{name}': connector '{cid}' degraded"
                          + (f" — {first_note}" if first_note else ""),
                    f"oracle kernel {name} -- connector freshness {cid}  "
                    "(pull to refresh once it's due)")
        elif status in ("healthy", "not_configured"):
            rep.add(OK, f"instance '{name}': connector '{cid}' {status}")
        else:
            rep.add(WARN, f"instance '{name}': connector '{cid}' health {status}"
                          + (f" — {first_note}" if first_note else ""),
                    f"oracle kernel {name} -- connector health {cid}")


def run(instance: str | None = None) -> Report:
    rep = Report()

    # python
    if sys.version_info >= (3, 10):
        rep.add(OK, f"python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        rep.add(FAIL, f"python {sys.version_info.major}.{sys.version_info.minor} < 3.10",
                "install Python 3.10+")

    # profile perms
    pdir = config.profile_dir()
    pmode = stat.S_IMODE(os.stat(pdir).st_mode)
    rep.add(OK if pmode == 0o700 else WARN, f"profile dir {pdir} mode {oct(pmode)}",
            f"chmod 700 {pdir}")
    env_file = config.env_path()
    if env_file.exists():
        emode = stat.S_IMODE(os.stat(env_file).st_mode)
        rep.add(OK if emode == 0o600 else FAIL, f".env mode {oct(emode)}",
                f"chmod 600 {env_file}")

    # config parse + secret guard
    try:
        cfg = config.load_config()
        config._scan_secret_leak(cfg) and rep.add(
            FAIL, "config.json contains a literal secret",
            "move secrets to .env; store only env-var names in config.json")
        if not config._scan_secret_leak(cfg):
            rep.add(OK, "config.json parses and holds no inline secrets")
    except ValueError as exc:
        rep.add(FAIL, f"config.json error: {exc}", "fix or delete config.json")
        return rep

    # ingest_roots — global config-level check (instance-independent)
    ingest_roots = cfg.get("ingest_roots") or []
    if not ingest_roots:
        rep.add(WARN, "ingest_roots is empty — your oracle cannot ingest from chat",
                "add directories to config.json ingest_roots")

    # instances
    roots = config.instance_roots(cfg)
    if instance is not None:
        # filter to the named instance only
        if instance in roots:
            roots = {instance: roots[instance]}
        else:
            rep.add(FAIL, f"no instance named {instance!r} "
                          f"(known: {', '.join(sorted(config.instance_roots(cfg))) or 'none'})",
                    "run `oracle instances list` to see registered instances")
            return rep
    if not roots:
        rep.add(WARN, "no instances registered", "run `oracle setup` or `oracle spawn`")
    vendored = _vendored_tools_version()
    # Phase 8 (P8-T6): resolve the embedding endpoint's POST-VETO environment +
    # ceiling once, so the per-instance vector-health check and the
    # embedding-endpoint provider row agree. The embedding endpoint is
    # classified INDEPENDENTLY of the chat endpoint (P8S-1).
    _prov = cfg.get("provider") or {}
    _emb_cfg = (_prov.get("embeddings") or {})
    embed_model = (_emb_cfg.get("model") or "").strip()
    embed_base_url = (_emb_cfg.get("base_url") or _prov.get("base_url") or "")
    embed_env = pb.environment_for(embed_base_url) if embed_model else "external"
    for name, root in roots.items():
        if not (root / "oracle.yml").exists():
            rep.add(FAIL, f"instance '{name}': root missing oracle.yml ({root})",
                    "re-spawn or fix the path with `oracle instances add`")
            continue
        rc = _check_rc(root)
        rep.add(OK if rc == 0 else WARN, f"instance '{name}': oracle check rc={rc}",
                "run `oracle kernel {name} -- check` for details".format(name=name))
        rtv = _root_tools_version(root)
        if rtv is None:
            rep.add(WARN, f"instance '{name}': kernel manifest not stamped",
                    "re-spawn to stamp the manifest")
        elif vendored and rtv != vendored:
            _kernel_src = str(Path(__file__).resolve().parent / "assets" / "oracle-kernel")
            rep.add(WARN, f"instance '{name}': kernel {rtv} != packaged {vendored}",
                    f"run `oracle upgrade kernel {name}` "
                    f"(or: oracle kernel {name} -- admin upgrade apply "
                    f"--from-kernel {_kernel_src})")
        else:
            rep.add(OK, f"instance '{name}': kernel {rtv}")
        # zero-sources check
        n_sources = _count_real_sources(root)
        if n_sources == 0:
            rep.add(WARN, f"instance '{name}': no ingested sources (oracle knows nothing yet)",
                    f"oracle kernel {name} -- ingest batch <path>")
        else:
            rep.add(OK, f"instance '{name}': {n_sources} source(s) ingested")

        # per-instance connector health (read-only: doctor PROBES, never pulls;
        # a remote probe is authenticated egress -- the same kind doctor already
        # performs for the LLM provider -- but no bytes are pulled into _INPUT).
        _check_connectors(rep, name, root)

        # per-instance vector-store health (P8-T6): coverage, dim mismatches,
        # orphan backstop, contingency threshold. Read-only via search stats.
        _check_vectors(rep, name, root, embed_model, embed_env)

    # provider
    prov = cfg.get("provider") or {}
    env_key = prov.get("api_key_env") or ""
    base_url = prov.get("base_url", "")
    model = prov.get("model", "")
    environment = pb.environment_for(base_url)
    rep.add(OK, f"provider env: {environment} ({base_url})")
    if environment == "local_agent":
        # Egress veto (STRESS C2 / P2S-2): a loopback listener is not a
        # processing-locality guarantee. Verify the model is not provably
        # cloud-proxied; read-only, 3s probe budget.
        veto = pb.egress_veto(base_url, model, timeout=3.0)
        if veto:
            rep.add(FAIL,
                    f"local model {model!r} is cloud-proxied: {veto}",
                    "use a fully local model (e.g. qwen3.6-32k) or accept "
                    "public-only")
        elif _is_ollama_tags_reachable(base_url):
            rep.add(OK, "local model: ceiling up to internal (egress veto clear)")
        else:
            rep.add(WARN,
                    "cannot verify processing locality of loopback endpoint "
                    "(non-Ollama server?) — loopback != no forwarding (STRESS C2)",
                    "if this is Ollama, ensure /api/tags is reachable; otherwise "
                    "confirm the server does not forward off-box")
    else:
        rep.add(OK, "external model: ceiling public (confidential+ withheld)")
    # non-https non-loopback endpoint is a hard FAIL
    if _is_non_loopback_http(base_url):
        rep.add(FAIL,
                "LLM endpoint is plain http:// to a non-loopback host — "
                "API key would be sent in cleartext",
                "set a https:// base_url (oracle model set --base-url ...)")
    key = config.resolve_secret(env_key) if env_key else None
    if env_key and not key:
        rep.add(WARN, f"provider API key env '{env_key}' is unset",
                f"oracle model set --key-env {env_key}, then add it to .env")
    elif env_key:
        rep.add(OK, f"provider API key resolvable via {env_key}")
    _probe_models(rep, base_url)

    # embedding endpoint (Phase 8 / P8-T6). The embedding endpoint is content
    # egress; its POST-VETO environment + ceiling are classified INDEPENDENTLY
    # of the chat endpoint (P8S-1). Doctor names the veto reason when one fired,
    # and states plainly that internal+ surfaces are lexical-only when the
    # embedder is external/vetoed.
    if not embed_model:
        rep.add(OK, "embedding endpoint: not configured (vector search disabled; "
                    "lexical-only retrieval)")
    else:
        _emb_veto = None
        if embed_env == "local_agent":
            _emb_veto = pb.egress_veto(embed_base_url, embed_model, timeout=3.0)
        post_veto_env = "external" if _emb_veto else embed_env
        if _emb_veto:
            # A vetoed loopback config: name the proxied remote host (the veto
            # reason carries it) and state the consequence.
            rep.add(FAIL,
                    f"embedding model {embed_model!r} on a loopback endpoint is "
                    f"cloud-proxied: {_emb_veto} — reclassified external, so "
                    "internal+ surfaces are embedded NOT AT ALL (lexical-only)",
                    "use a fully local embedder, or accept public-only vector "
                    "search")
        elif post_veto_env == "external":
            rep.add(OK, f"embedding endpoint: external ({embed_base_url}) — "
                        f"PUBLIC chunks/queries only; internal+ surfaces are "
                        f"lexical-only by design (the egress ceiling is public)")
        elif _is_ollama_tags_reachable(embed_base_url):
            rep.add(OK, f"embedding endpoint: local_agent, egress veto clear "
                        f"({embed_model!r}) — chunks up to internal embedded")
        else:
            rep.add(WARN,
                    f"embedding endpoint {embed_base_url}: cannot verify "
                    "processing locality (non-Ollama loopback server?) — "
                    "loopback != no forwarding (STRESS C2)",
                    "if this is Ollama ensure /api/tags is reachable; otherwise "
                    "confirm the embedder does not forward off-box")
        if _is_non_loopback_http(embed_base_url):
            rep.add(FAIL,
                    "embedding endpoint is plain http:// to a non-loopback host "
                    "— an API key would be sent in cleartext",
                    "set an https:// embeddings base_url")

    # gateway surfaces (Phase 4 / P4-T7): per-surface doctor matrix. Each
    # ENABLED surface is validated against the pinned checks; HTTP's identity is
    # the token, so the "allowlist non-empty" check does NOT apply to it
    # (P4S-20). Read-only: these rows replicate the adapters' own startup gates
    # (email.py's authserv/dmarc cap, http.py's validate_bind) without mutating.
    _check_gateway_surfaces(rep, cfg)

    # briefing delivery targets (Phase 4 / P4-T8): every configured target must
    # resolve to an allowlisted private identity on its surface, else refused
    # (P4S-15). Read-only config-level check (the sibling's briefer resolver is
    # preferred when present; otherwise validated locally from CONFIG alone).
    _check_briefings(rep, cfg)

    return rep


# --------------------------------------------------------------------------- #
# Phase 4 (P4-T7): per-surface gateway doctor matrix.
# --------------------------------------------------------------------------- #
def _websocket_lib_present() -> bool:
    """True iff the optional Slack Socket Mode websocket library is importable.

    Uses ``importlib.util.find_spec`` so doctor never module-imports the
    optional dep (matching the adapter's dep-absent graceful-disable discipline,
    P4S-14)."""
    import importlib.util
    return importlib.util.find_spec("websockets") is not None


def _check_gateway_surfaces(rep: "Report", cfg: dict) -> None:
    gw = cfg.get("gateway") or {}
    _check_telegram_surface(rep, gw.get("telegram") or {})
    _check_slack_surface(rep, gw.get("slack") or {})
    _check_email_surface(rep, gw.get("email") or {})
    _check_http_surface(rep, gw.get("http") or {})


def _check_telegram_surface(rep: "Report", tg: dict) -> None:
    """telegram: token resolvable; allowlist non-empty."""
    if not tg.get("enabled"):
        return
    if not config.resolve_secret(tg.get("token_env") or ""):
        rep.add(FAIL, "telegram enabled but token unresolved",
                f"add {tg.get('token_env')} to .env")
    elif not (tg.get("allowlist") or {}):
        rep.add(WARN, "telegram enabled but allowlist empty (no one can use it)",
                "add user IDs to gateway.telegram.allowlist in config.json")
    else:
        rep.add(OK, f"telegram enabled, {len(tg['allowlist'])} allowed user(s)")


def _check_slack_surface(rep: "Report", sl: dict) -> None:
    """slack: token resolvable; allowlist non-empty; optional websocket dep
    present (else a WARN that Slack is disabled until the dep is installed)."""
    if not sl.get("enabled"):
        return
    if not config.resolve_secret(sl.get("token_env") or ""):
        rep.add(FAIL, "slack enabled but token unresolved",
                f"add {sl.get('token_env')} to .env")
        return
    if not (sl.get("allowlist") or {}):
        rep.add(WARN, "slack enabled but allowlist empty (no one can use it)",
                "add U… member ids to gateway.slack.allowlist in config.json")
        return
    if not _websocket_lib_present():
        rep.add(WARN, "slack configured but websocket lib absent — disabled",
                "install the optional Socket Mode websocket library, or set "
                "gateway.slack.enabled=false")
        return
    rep.add(OK, f"slack enabled, {len(sl['allowlist'])} allowed user(s)")


def _check_email_surface(rep: "Report", em: dict) -> None:
    """email: creds resolvable; allowlist non-empty; dedicated-mailbox ack;
    authserv_id unset => public-cap WARN; TLS hosts set.

    Replicates email.py's layered fail-closed identity gate read-only: with no
    ``authserv_id`` the surface is hard-capped at ``public`` no matter what
    ``max_sensitivity`` config names (P4S-10)."""
    if not em.get("enabled"):
        return
    user_ok = bool(config.resolve_secret(em.get("user_env") or ""))
    pass_ok = bool(config.resolve_secret(em.get("pass_env") or ""))
    if not (user_ok and pass_ok):
        missing = []
        if not user_ok:
            missing.append(em.get("user_env") or "user_env")
        if not pass_ok:
            missing.append(em.get("pass_env") or "pass_env")
        rep.add(FAIL, "email enabled but credentials unresolved "
                      f"({', '.join(missing)})",
                f"add {' and '.join(missing)} to .env")
        return
    if not (em.get("allowlist") or {}):
        rep.add(WARN, "email enabled but allowlist empty (no one can use it)",
                "add lowercased addresses to gateway.email.allowlist in config.json")
        return
    # TLS hosts (IMAP4_SSL / SMTP STARTTLS are mandatory; an empty host means
    # the adapter cannot connect at all).
    imap_host = (em.get("imap_host") or "").strip()
    smtp_host = (em.get("smtp_host") or "").strip()
    if not imap_host or not smtp_host:
        missing = []
        if not imap_host:
            missing.append("imap_host")
        if not smtp_host:
            missing.append("smtp_host")
        rep.add(FAIL, f"email enabled but TLS host(s) unset ({', '.join(missing)})",
                "set gateway.email.imap_host and gateway.email.smtp_host "
                "(IMAP4_SSL / SMTP STARTTLS are mandatory)")
        return
    # Dedicated-mailbox acknowledgement (P4S-12): a shared human mailbox races
    # \Seen with the human's client. We cannot probe IMAP read-only here, so we
    # require an explicit operator ack flag in config.
    if not em.get("dedicated_mailbox_ack"):
        rep.add(WARN, "email enabled but dedicated-mailbox not acknowledged "
                      "(a shared human mailbox races \\Seen with the human's client)",
                "use a DEDICATED mailbox for the oracle, then set "
                "gateway.email.dedicated_mailbox_ack=true in config.json")
    # authserv_id unset => the surface is public-capped regardless of config
    # max_sensitivity (P4S-10). This is a WARN, not a FAIL: public is a valid,
    # safe operating ceiling.
    if not em.get("authserv_id"):
        rep.add(WARN, "email capped at public (no authserv_id configured — "
                      "From is forgeable and DKIM is unverifiable in stdlib over "
                      "IMAP, so internal+ is locked until verified DMARC)",
                "configure a trusted gateway.email.authserv_id (the host id of an "
                "Authentication-Results header you trust) to unlock internal")
    else:
        rep.add(OK, f"email enabled, {len(em['allowlist'])} allowed sender(s), "
                    "authserv_id set (internal unlockable via verified DMARC)")


def _check_http_surface(rep: "Report", ht: dict) -> None:
    """http: token resolvable (FAIL if not); bind parses as a literal loopback
    IP; port sane. NB: "allowlist non-empty" does NOT apply to HTTP — its
    identity is the token (P4S-20)."""
    if not ht.get("enabled"):
        return
    # Fail-closed startup (P4S-7): no token => the adapter refuses to start.
    if not config.resolve_secret(ht.get("token_env") or ""):
        rep.add(FAIL, "http enabled but token unresolved (the adapter refuses to "
                      "start — there is no unauthenticated mode)",
                f"add {ht.get('token_env')} to .env")
        return
    # Bind must parse as a literal loopback IP (P4S-7). Replicate http.py's
    # validate_bind read-only so doctor flags the same startup refusal.
    bind = ht.get("bind", "127.0.0.1")
    try:
        from .gateway.http import validate_bind
        validate_bind(bind)
    except ValueError as exc:
        rep.add(FAIL, f"http bind {bind!r} is not a literal loopback IP — "
                      "the adapter refuses to start",
                f"set gateway.http.bind to 127.0.0.1 or ::1 ({exc})")
        return
    except Exception:
        # validate_bind import unexpectedly failed: do the check inline so the
        # doctor row is still honest about the literal-loopback discipline.
        try:
            addr = ipaddress.ip_address(bind)
            ok = addr.is_loopback
        except ValueError:
            ok = False
        if not ok:
            rep.add(FAIL, f"http bind {bind!r} is not a literal loopback IP — "
                          "the adapter refuses to start",
                    "set gateway.http.bind to 127.0.0.1 or ::1")
            return
    # Port sanity.
    try:
        port = int(ht.get("port", 8765))
    except (TypeError, ValueError):
        port = -1
    if not (1 <= port <= 65535):
        rep.add(FAIL, f"http port {ht.get('port')!r} is out of range",
                "set gateway.http.port to a value in 1..65535")
        return
    rep.add(OK, f"http enabled on {bind}:{port}, token resolvable "
                f"(principal: {ht.get('principal', 'http-operator')})")


# --------------------------------------------------------------------------- #
# Phase 4 (P4-T8): briefing delivery-target doctor check (P4S-15).
# --------------------------------------------------------------------------- #
def _briefing_target_resolves(cfg: dict, target: dict) -> tuple[bool, str]:
    """Return (ok, reason) for one briefing target.

    A push has no inbound message to assert ``is_private``, so each target must
    resolve to an ALREADY-allowlisted private identity on its surface (P4S-15):

      * telegram: a ``user_id`` that is a key in gateway.telegram.allowlist
        (a chat_id equal to an allowlisted user_id is the only private form);
      * email: an ``address`` (lowercased) that is a key in
        gateway.email.allowlist.

    Anything else — a group id, an unlisted chat, a list address, an unknown
    surface — is refused (deny-by-default). Prefer the sibling briefer's
    resolver when present; otherwise validate from CONFIG alone (this comment
    is the local fallback the spec permits).
    """
    if not isinstance(target, dict):
        return False, "target is not an object"
    surface = target.get("surface")
    gw = cfg.get("gateway") or {}
    if surface == "telegram":
        uid = target.get("user_id")
        if not uid:
            return False, "telegram target missing user_id"
        allow = (gw.get("telegram") or {}).get("allowlist") or {}
        if str(uid) not in {str(k) for k in allow}:
            return False, (f"telegram user_id {uid!r} is not in "
                           "gateway.telegram.allowlist (group/unlisted chats refused)")
        return True, ""
    if surface == "email":
        addr = (target.get("address") or "").strip().lower()
        if not addr:
            return False, "email target missing address"
        allow = (gw.get("email") or {}).get("allowlist") or {}
        if addr not in {str(k).strip().lower() for k in allow}:
            return False, (f"email address {addr!r} is not in "
                           "gateway.email.allowlist (list addresses refused)")
        return True, ""
    return False, f"unsupported briefing surface {surface!r} (telegram/email only)"


def _check_briefings(rep: "Report", cfg: dict) -> None:
    """Validate every configured briefing delivery target + the state file.

    Deny-by-default: no configured target => no delivery (nothing to report).
    Each target must resolve to an allowlisted private identity (P4S-15); an
    unresolvable target is a FAIL (config load AND doctor both refuse it). The
    persisted delivery-state file must be readable (corruption => no send +
    doctor flag, fail closed)."""
    briefings = cfg.get("briefings") or {}
    if not briefings:
        return  # deny-by-default; nothing configured -> nothing to check
    # Prefer the sibling's resolver if its module has landed by now.
    resolver = None
    try:
        from .service import briefer as _briefer  # type: ignore
        resolver = getattr(_briefer, "target_is_allowlisted_private", None)
    except Exception:
        resolver = None
    for inst, block in briefings.items():
        targets = (block or {}).get("targets") or []
        if not targets:
            rep.add(WARN, f"briefings[{inst}] has no delivery targets "
                          "(deny-by-default: no brief will be delivered)",
                    "add a {surface,user_id|address} target that resolves to an "
                    "allowlisted private identity")
            continue
        for target in targets:
            if resolver is not None:
                try:
                    ok = bool(resolver(cfg, target))
                    reason = "" if ok else "target is not an allowlisted private identity"
                except Exception as exc:  # resolver blew up -> fall back locally
                    ok, reason = _briefing_target_resolves(cfg, target)
                    if not ok:
                        reason += f" (briefer resolver error: {type(exc).__name__})"
            else:
                ok, reason = _briefing_target_resolves(cfg, target)
            if ok:
                desc = target.get("user_id") or target.get("address") or "?"
                rep.add(OK, f"briefings[{inst}]: target "
                            f"{target.get('surface')}:{desc} resolves to an "
                            "allowlisted private identity")
            else:
                rep.add(FAIL, f"briefings[{inst}]: target refused — {reason}",
                        "a briefing push must target an ALREADY-allowlisted "
                        "private identity (group ids / unlisted chats / list "
                        "addresses are refused; delivery is an export)")
    # State file readability (fail closed: corruption => no send + doctor flag).
    _check_briefing_state(rep)


def _check_briefing_state(rep: "Report") -> None:
    """Flag a corrupt/unreadable briefing delivery-state file (P4S-15).

    The state is keyed ``(instance, surface, drop_id)`` and lives in the profile
    dir (atomic write, P4S-20 naming). A missing file is normal (nothing
    delivered yet); an unreadable/corrupt one is a FAIL because the briefer
    fails closed (no send) on corruption."""
    try:
        state_path = config.profile_dir() / "briefing_delivery_state.json"
    except Exception:
        return
    if not state_path.exists():
        return  # nothing delivered yet -> not an error
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        rep.add(FAIL, f"briefing delivery-state file is unreadable/corrupt "
                      f"({type(exc).__name__}) — the briefer fails closed (no "
                      "send) until it is repaired",
                f"inspect or delete {state_path} (a missed brief beats a "
                "mis-sent one)")
        return
    if not isinstance(data, dict):
        rep.add(FAIL, "briefing delivery-state file is not a JSON object — the "
                      "briefer fails closed (no send) until it is repaired",
                f"inspect or delete {state_path}")


def _check_rc(root: Path) -> int:
    import subprocess
    try:
        proc = subprocess.run([sys.executable, str(root / "oracle"), "check"],
                              cwd=str(root), capture_output=True, text=True, timeout=120)
        return proc.returncode
    except Exception:
        return 1


def _probe_models(rep: Report, base_url: str) -> None:
    if not base_url:
        return
    url = base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=5):
            rep.add(OK, f"provider reachable: GET {url}")
    except (urllib.error.URLError, socket.timeout, OSError):
        rep.add(WARN, f"provider /models not reachable ({url})",
                "expected for some providers; verify base_url + network")


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="oracle doctor")
    ap.add_argument("instance", nargs="?")
    args = ap.parse_args(argv)
    rep = run(args.instance)
    print(rep.render())
    return 1 if rep.worst_is_fail() else 0


if __name__ == "__main__":
    raise SystemExit(main())
