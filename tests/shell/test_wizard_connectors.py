"""Wizard connector step + first-run experience (P7-T7 remainder, T8, T11).

These tests drive the wizard's optional "Connect knowledge sources?" step with
SCRIPTED input/getpass (monkeypatched) against a testkit-spawned root. They
cover:

  * the manifest is RENDERED from the catalog and VALIDATES against the schema
    (the kernel loads it without a ConnectorError);
  * the prompted scope allowlist lands in the manifest (and a blank answer
    leaves a bare default-deny key -- empty never means 'everything');
  * secrets land in <root>/.env.nosync at 0600 and NEVER in config.json;
  * the DRY-RUN plan is shown BEFORE any bytes move, then the first pull;
  * the per-root FLOCK is held across the pull + ingest (lock-spy);
  * a non-TTY run skips the step with a printed note;
  * the T7 scrubbed-env acceptance test: a kernel connector verb spawned with
    the shell's _scrubbed_env() still resolves its auth vars from the root's
    .env.nosync (proving the root-file path works where the process env cannot);
  * the first-run progress output shape (ingested / skipped / refused counts)
    and the doctor zero-sources warning clearing.

The live-pull path uses the LOCALFOLDER connector (a real registered connector
that reads a local folder -- no network), wired up by writing its manifest the
same way the wizard writes a remote one. Per the phase's shell-test discipline
every test runs on a testkit-spawned root (conftest ``spawned_root``).
"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from oracle_agent import config, doctor, wizard


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _register(root: Path, name: str = "main") -> None:
    cfg = config.load_config()
    cfg = config.register_instance(cfg, name, root)
    config.save_config(cfg)


def _clean_connectors(root: Path) -> None:
    """Remove any wizard-written / scaffold connector dirs so a session-scoped
    spawned_root reuse stays clean."""
    import shutil
    cdir = root / "Connectors"
    if cdir.is_dir():
        for sub in cdir.iterdir():
            if sub.is_dir() and (sub / f"{sub.name}.manifest.yaml").exists():
                shutil.rmtree(sub, ignore_errors=True)
    inp = root / "Workproduct.nosync" / "_INPUT"
    if inp.is_dir():
        for sub in inp.iterdir():
            if sub.is_dir() and sub.name in ("localfolder",):
                shutil.rmtree(sub, ignore_errors=True)
    env = root / ".env.nosync"
    if env.exists():
        env.unlink()


class _Script:
    """A line-feeding stdin double: ``_ask`` and the getpass-fallback both call
    ``.readline()`` on it, in order."""

    def __init__(self, lines: list[str]):
        self._buf = io.StringIO("".join(l if l.endswith("\n") else l + "\n"
                                        for l in lines))

    def readline(self) -> str:
        return self._buf.readline()


# --------------------------------------------------------------------------- #
# manifest render + validation (P7-T8)
# --------------------------------------------------------------------------- #
def test_rendered_manifest_validates_against_schema(profile, spawned_root):
    """A wizard-rendered manifest loads + schema-validates (kernel health loads
    it without a ConnectorError; lint stays green)."""
    _register(spawned_root, "main")
    try:
        text = wizard._render_manifest(
            "slack-export", sensitivity="internal",
            scope={"path": "/tmp/does-not-matter.zip", "channels": ["general"]},
        )
        d = spawned_root / "Connectors" / "slack-export"
        d.mkdir(parents=True, exist_ok=True)
        (d / "slack-export.manifest.yaml").write_text(text, encoding="utf-8")

        # lint must stay green (schema-valid manifest).
        proc = subprocess.run(
            [sys.executable, str(spawned_root / "oracle"), "lint"],
            cwd=str(spawned_root), capture_output=True, text=True, timeout=60,
        )
        assert "ORACLE LINT: PASS" in (proc.stdout + proc.stderr), proc.stdout

        # health loads it (status broken only because the zip is absent -- NOT a
        # load/schema error, which would surface as rc 2 / ConnectorError).
        rc, out, err = wizard._kernel(
            spawned_root, ["connector", "--json", "health", "slack-export"])
        assert rc != 2, f"health hit a ConnectorError (manifest failed to load): {err}"
        rep = json.loads(out)
        assert rep["connector"] == "slack-export"
    finally:
        _clean_connectors(spawned_root)


def test_render_empty_allowlist_is_bare_default_deny():
    """A blank allowlist answer renders a BARE key (default-deny). Empty never
    means 'everything' (I4 / P7S-13)."""
    text = wizard._render_manifest("gdrive", sensitivity="internal",
                                   scope={"folder_ids": []})
    # bare key, no list items.
    assert "\n  folder_ids:\n" in text
    assert "    - " not in text.split("folder_ids:")[1].split("default_sensitivity")[0]


def test_render_allowlist_lands_in_manifest():
    text = wizard._render_manifest("notion", sensitivity="internal",
                                   scope={"page_ids": ["abc", "def"], "database_ids": []})
    assert "  page_ids:\n    - abc\n    - def\n" in text
    # the empty database_ids is bare default-deny.
    assert "  database_ids:\n" in text


# --------------------------------------------------------------------------- #
# secrets -> .env.nosync, never config.json (P7-T7/T8)
# --------------------------------------------------------------------------- #
def test_secrets_land_in_env_nosync_0600_not_config(profile, spawned_root, monkeypatch):
    """Scripted setup: secrets land in <root>/.env.nosync at 0600, never in
    config.json."""
    _register(spawned_root, "main")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    out = io.StringIO()
    # pick notion (one secret NOTION_TOKEN), provide token, DECLINE the pull.
    script = _Script([
        "y",                       # connect a source?
        "notion",                  # pick
        "page-root-1",             # page_ids
        "",                        # database_ids (blank -> default-deny)
        "secret-notion-token-xyz", # NOTION_TOKEN (getpass fallback reads stdin)
        "n",                       # proceed with first pull? -> no
    ])
    try:
        wizard.connector_step(spawned_root, "main", stream_in=script, stream_out=out)

        env_file = spawned_root / ".env.nosync"
        assert env_file.exists(), out.getvalue()
        body = env_file.read_text()
        assert "NOTION_TOKEN=secret-notion-token-xyz" in body
        mode = stat.S_IMODE(env_file.stat().st_mode)
        assert mode == 0o600, oct(mode)

        # the secret must NEVER be in config.json.
        cfg_text = config.config_path().read_text() if config.config_path().exists() else ""
        assert "secret-notion-token-xyz" not in cfg_text
    finally:
        _clean_connectors(spawned_root)


# --------------------------------------------------------------------------- #
# dry-run plan shown BEFORE first real pull (P7-T8) + first-run output (P7-T11)
# --------------------------------------------------------------------------- #
def _wire_localfolder(root: Path, src: Path) -> None:
    """Write a localfolder manifest the way the wizard writes a remote one (the
    live-pull path with NO network)."""
    d = root / "Connectors" / "localfolder"
    d.mkdir(parents=True, exist_ok=True)
    (d / "localfolder.manifest.yaml").write_text(
        "id: localfolder\n"
        "system: Local folder\n"
        "status: active\n"
        "access_mode: folder\n"
        "locality: snapshot_local\n"
        "capture_tier: snapshot\n"
        "auth:\n  method: none\n"
        "permissions: read_only\n"
        "freshness:\n  class: snapshot\n  expected_decay_days: 7\n"
        "source:\n"
        f'  path: "{src}"\n'
        "  max_files: 50\n"
        "  default_sensitivity: internal\n",
        encoding="utf-8",
    )


def test_dry_run_then_first_pull_progress(profile, spawned_root, tmp_path):
    """The first pull + ingest streams progress counts and the corpus grows; the
    dry-run plan is reported before bytes move."""
    _register(spawned_root, "main")
    src = tmp_path / "src"
    src.mkdir()
    (src / "memo.txt").write_text("Quarterly revenue was strong.\n", encoding="utf-8")
    _wire_localfolder(spawned_root, src)
    out = io.StringIO()
    try:
        # exercise the dry-run + first-pull internals directly (no secrets needed
        # for localfolder), proving the dry-run-before-bytes + progress shape.
        rc, dout, _ = wizard._kernel(
            spawned_root, ["connector", "--json", "pull", "localfolder", "--dry-run"])
        plan = wizard._parse_pull_payload(dout)
        planned = wizard._pull_counts(plan["results"])
        assert planned["planned"] >= 1
        # NOTHING landed yet (dry-run moved no bytes).
        landed = spawned_root / "Workproduct.nosync" / "_INPUT" / "localfolder"
        assert not landed.exists() or not any(p.is_file() for p in landed.rglob("*"))

        # now the real first pull + ingest under the flock.
        wizard._first_pull_and_ingest(spawned_root, "main", "localfolder", stream_out=out)
        text = out.getvalue()
        assert "ingested=1" in text, text
        assert "skipped-out-of-scope=0" in text
        # corpus growth + review-provenance note (P7-T11).
        assert "ingested source(s)" in text
        assert "connector tag (localfolder)" in text

        # doctor zero-sources warning has cleared.
        n = doctor._count_real_sources(spawned_root)
        assert n >= 1
    finally:
        _clean_connectors(spawned_root)
        # remove any source records this test ingested (keep spawned_root clean)
        sdir = spawned_root / "Memory.nosync" / "Sources"
        if sdir.is_dir():
            for p in sdir.iterdir():
                if p.suffix == ".md" and not p.name.startswith("_"):
                    p.unlink()


# --------------------------------------------------------------------------- #
# flock held across pull + ingest (P7S-22)
# --------------------------------------------------------------------------- #
def test_flock_held_during_pull_and_ingest(profile, spawned_root, tmp_path, monkeypatch):
    """The per-root flock is acquired and held across the first pull + ingest."""
    _register(spawned_root, "main")
    src = tmp_path / "src2"
    src.mkdir()
    (src / "doc.txt").write_text("hello flock\n", encoding="utf-8")
    _wire_localfolder(spawned_root, src)

    import oracle_agent.service.scheduler as sched

    events = {"acquired": 0, "released": 0, "kernel_calls_inside_lock": 0,
              "in_lock": False}
    real_lock = sched.root_lock

    import contextlib

    @contextlib.contextmanager
    def spy_lock(name, **kw):
        events["acquired"] += 1
        events["in_lock"] = True
        with real_lock(name, **kw):
            yield
        events["in_lock"] = False
        events["released"] += 1

    real_kernel = wizard._kernel

    def spy_kernel(root, argv, **kw):
        if events["in_lock"]:
            events["kernel_calls_inside_lock"] += 1
        return real_kernel(root, argv, **kw)

    monkeypatch.setattr(sched, "root_lock", spy_lock)
    monkeypatch.setattr(wizard, "_kernel", spy_kernel)

    out = io.StringIO()
    try:
        wizard._first_pull_and_ingest(spawned_root, "main", "localfolder", stream_out=out)
        assert events["acquired"] == 1, out.getvalue()
        assert events["released"] == 1
        # both the pull AND the ingest ran while the lock was held.
        assert events["kernel_calls_inside_lock"] >= 2, events
    finally:
        _clean_connectors(spawned_root)
        sdir = spawned_root / "Memory.nosync" / "Sources"
        if sdir.is_dir():
            for p in sdir.iterdir():
                if p.suffix == ".md" and not p.name.startswith("_"):
                    p.unlink()


# --------------------------------------------------------------------------- #
# non-TTY skip (P7-T8 non-interactive-safe)
# --------------------------------------------------------------------------- #
def test_non_tty_blank_answer_declines_cleanly(profile, spawned_root, monkeypatch):
    """A non-TTY run whose 'connect?' answer is blank declines cleanly with the
    'no connector configured' note and writes no manifest (the realistic scripted
    headless path -- a blank stream feeds the default 'N')."""
    _register(spawned_root, "main")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    out = io.StringIO()
    script = _Script([""])  # blank -> default 'N'
    wizard.connector_step(spawned_root, "main", stream_in=script, stream_out=out)
    text = out.getvalue()
    assert "no connector configured" in text
    # no manifest was written.
    assert not (spawned_root / "Connectors" / "notion").exists()


def test_tty_guard_skips_when_scripted_and_non_tty_after_yes(profile, spawned_root, monkeypatch):
    """If the user answers 'y' but the run is non-TTY with no script stream, the
    step prints the interactive-terminal skip note."""
    _register(spawned_root, "main")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    # Patch _ask so the first (connect?) question returns 'y' but stream_in stays
    # None, hitting the TTY guard.
    real_ask = wizard._ask
    calls = {"n": 0}

    def fake_ask(prompt, default="", *, stream_in=None, stream_out=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return "y"
        return real_ask(prompt, default, stream_in=stream_in, stream_out=stream_out)

    monkeypatch.setattr(wizard, "_ask", fake_ask)
    out = io.StringIO()
    wizard.connector_step(spawned_root, "main", stream_in=None, stream_out=out)
    assert "needs an interactive terminal" in out.getvalue()


# --------------------------------------------------------------------------- #
# T7 scrubbed-env acceptance: kernel verb under _scrubbed_env resolves auth from
# the root's .env.nosync (where the process env cannot reach).
# --------------------------------------------------------------------------- #
def test_scrubbed_env_pull_resolves_auth_from_root_env_nosync(profile, spawned_root):
    """A kernel connector verb spawned with the shell's _scrubbed_env() still
    resolves its auth vars from <root>/.env.nosync -- proving the root-file path
    works where the process env cannot (the scrub drops *_TOKEN/_SECRET/etc).

    notion declares NOTION_TOKEN; its health reports 'unresolved auth vars' when
    the token is absent and stops reporting that once it lands in .env.nosync. We
    run health via _kernel (scrubbed env) so the ONLY place the var can come from
    is the root file.
    """
    _register(spawned_root, "main")
    try:
        # render + write a notion manifest with an allowlist (so the only failure
        # left is auth resolution).
        text = wizard._render_manifest("notion", sensitivity="internal",
                                       scope={"page_ids": ["p1"], "database_ids": []})
        d = spawned_root / "Connectors" / "notion"
        d.mkdir(parents=True, exist_ok=True)
        (d / "notion.manifest.yaml").write_text(text, encoding="utf-8")

        # BEFORE: no .env.nosync -> health reports unresolved auth var.
        rc, before, _ = wizard._kernel(
            spawned_root, ["connector", "--json", "health", "notion"])
        rep_before = json.loads(before)
        notes_before = " ".join(str(n) for n in (rep_before.get("notes") or []))
        assert "NOTION_TOKEN" in notes_before and "unresolved" in notes_before.lower(), notes_before

        # Put a TOKEN-shaped secret (a *_TOKEN var the scrub WOULD strip from the
        # process env) into the ROOT's .env.nosync via the sanctioned writer.
        config.write_root_env_secret(spawned_root, "NOTION_TOKEN", "secret_ntn_value_123456")

        # ALSO export it into the parent process env to PROVE the scrub is what
        # forces resolution from the file (if the scrub didn't run, the env value
        # would mask the test). The scrub strips *_TOKEN, so only the file path
        # can satisfy resolution inside the kernel subprocess.
        os.environ["NOTION_TOKEN"] = "WRONG_FROM_PROCESS_ENV"
        try:
            rc, after, aerr = wizard._kernel(
                spawned_root, ["connector", "--json", "health", "notion"])
        finally:
            os.environ.pop("NOTION_TOKEN", None)

        rep_after = json.loads(after)
        notes_after = " ".join(str(n) for n in (rep_after.get("notes") or []))
        # the unresolved-auth note is gone now that the root file carries the var.
        assert "unresolved" not in notes_after.lower(), (
            f"auth still unresolved under scrubbed env: {notes_after}")
    finally:
        _clean_connectors(spawned_root)


def test_scrubbed_env_actually_drops_token_vars():
    """Guard: the env the wizard hands the kernel really does drop *_TOKEN vars,
    so the resolution-from-file test above is meaningful."""
    from oracle_agent.agentloop.verbtools import _scrubbed_env

    os.environ["SOME_FAKE_TOKEN"] = "x"
    try:
        env = _scrubbed_env()
        assert "SOME_FAKE_TOKEN" not in env
    finally:
        os.environ.pop("SOME_FAKE_TOKEN", None)
