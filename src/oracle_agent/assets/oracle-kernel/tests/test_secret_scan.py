#!/usr/bin/env python3
"""Tests for the broadened secret scanner (secret_scan.py).

Asserts that EVERY required credential format is detected with file:line:offset
semantics, that a high-entropy blob is flagged, and that benign prose is clean.
Self-contained: only depends on secret_scan.py + stdlib.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import secret_scan  # noqa: E402


def _patterns(text: str) -> set[str]:
    return {f["pattern"] for f in secret_scan.scan_text(text)}


def _has(text: str, pattern: str) -> bool:
    return pattern in _patterns(text)


# --------------------------------------------------------------------------- #
# one case per required format
# --------------------------------------------------------------------------- #
def test_github_classic_token():
    text = "token = ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    assert _has(text, "github_token")


def test_github_oauth_user_server_prefixes():
    for pre in ("gho_", "ghu_", "ghs_"):
        text = pre + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        assert _has(text, "github_token"), pre


def test_github_fine_grained_pat():
    text = "GH=github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"
    assert _has(text, "github_pat")


def test_gitlab_pat():
    text = "GITLAB_TOKEN=glpat-ABCDefgh1234IJKLmnop"
    assert _has(text, "gitlab_pat")


def test_stripe_live_test_restricted():
    assert _has("sk_live_" + "A" * 24, "stripe_secret")
    assert _has("sk_test_" + "B" * 24, "stripe_secret")
    assert _has("rk_live_" + "C" * 24, "stripe_secret")


def test_google_api_key():
    text = "AIza" + "Bb1Cc2Dd3Ee4Ff5Gg6Hh7Ii8Jj9Kk0Ll1Mm"  # AIza + 35
    # ensure exactly 35 trailing chars
    body = "A" * 35
    text = "key=AIza" + body
    assert _has(text, "google_api_key")


def test_postgres_url_with_credentials():
    text = "DATABASE_URL=postgresql://admin:s3cr3tP4ss@db.internal:5432/app"
    assert _has(text, "postgres_url")
    text2 = "postgres://user:pw@localhost/db"
    assert _has(text2, "postgres_url")


def test_pem_private_key_header():
    for kind in (
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "-----BEGIN EC PRIVATE KEY-----",
    ):
        assert _has(kind, "pem_private_key"), kind


def test_aws_access_key_id():
    text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
    assert _has(text, "aws_access_key_id")


def test_aws_secret_access_key_heuristic():
    # 40-char secret via assignment anchor.
    text = 'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
    assert _has(text, "aws_secret_access_key")
    # 40-char blob in proximity to an AKIA id.
    text2 = (
        "AKIAIOSFODNN7EXAMPLE\n"
        "secret line: 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\n"
    )
    assert _has(text2, "aws_secret_access_key")


def test_slack_token():
    for pre in ("xoxb-", "xoxp-", "xoxa-", "xoxr-", "xoxs-"):
        text = pre + "123456789012-abcdefghijklmnop"
        assert _has(text, "slack_token"), pre


def test_jwt():
    text = (
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    assert _has(text, "jwt")


def test_generic_assignments():
    assert _has("password: hunter2hunter2", "generic_assignment")
    assert _has('token: "abc123def456ghi"', "generic_assignment")
    assert _has("api_key = 9f8e7d6c5b4a3210", "generic_assignment")


def test_generic_assignment_ignores_placeholders():
    # Placeholders / env-var refs should not be flagged as leaks.
    assert not _has("password: ${DB_PASSWORD}", "generic_assignment")
    assert not _has("token: <your-token-here>", "generic_assignment")
    assert not _has("api_key: CHANGEME", "generic_assignment")


# --------------------------------------------------------------------------- #
# entropy + cleanliness
# --------------------------------------------------------------------------- #
def test_high_entropy_blob_flagged():
    blob = "Zx9Qw3Er7Ty1Ui5Op2As8Df4Gh6Jk0Lm3Nb7Vc"  # dense mixed-case+digits
    findings = secret_scan.scan_text("value=" + blob)
    assert any(f["pattern"] == "high_entropy_blob" for f in findings) or any(
        "entropy" in f for f in findings
    )


def test_benign_document_is_clean():
    text = (
        "# Project README\n\n"
        "This oracle ingests company material and produces findings.\n"
        "The quarterly revenue grew by twelve percent year over year.\n"
        "See the operations lane for the latest workproduct registry.\n"
        "Contact the admin to request an export approval.\n"
        "version: 1.0.0  status: active  cadence: weekly\n"
    )
    findings = secret_scan.scan_text(text)
    assert findings == [], findings


def test_findings_carry_line_and_offset():
    text = "line one is clean\nsecret here: ghp_" + "Z" * 36 + "\nlast line\n"
    findings = secret_scan.scan_text(text)
    gh = [f for f in findings if f["pattern"] == "github_token"]
    assert gh
    assert gh[0]["line"] == 2
    assert isinstance(gh[0]["offset"], int)
    assert gh[0]["offset"] >= 0


def test_scan_text_redacts_match():
    text = "ghp_" + "A" * 36
    findings = secret_scan.scan_text(text)
    gh = [f for f in findings if f["pattern"] == "github_token"][0]
    # Excerpt must not contain the full secret.
    assert text not in gh["match"]
    assert "***" in gh["match"]


# --------------------------------------------------------------------------- #
# tree scanning
# --------------------------------------------------------------------------- #
def test_scan_tree_reports_file(tmp_path: Path):
    secrets_file = tmp_path / "config" / "creds.env"
    secrets_file.parent.mkdir(parents=True)
    secrets_file.write_text("GITHUB=ghp_" + "Q" * 36 + "\n", encoding="utf-8")
    clean_file = tmp_path / "README.md"
    clean_file.write_text("Nothing secret here, just docs.\n", encoding="utf-8")

    results = secret_scan.scan_tree(tmp_path)
    files = {r["file"] for r in results}
    assert any("creds.env" in f for f in files)
    assert all("README.md" not in f for f in files)


def test_scan_tree_skips_binary_and_git(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        "token: ghp_" + "Z" * 36 + "\n", encoding="utf-8"
    )
    img = tmp_path / "logo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"ghp_" + b"Z" * 36)
    results = secret_scan.scan_tree(tmp_path)
    files = {r.get("file", "") for r in results}
    assert all(".git" not in f for f in files)
    assert all("logo.png" not in f for f in files)


def test_cli_scan_returns_nonzero_on_hit(tmp_path: Path, capsys):
    f = tmp_path / "leak.txt"
    f.write_text("api token = glpat-ABCDefgh1234IJKLmnop\n", encoding="utf-8")
    rc = secret_scan.main(["scan", str(tmp_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "gitlab_pat" in out


def test_cli_scan_returns_zero_when_clean(tmp_path: Path):
    f = tmp_path / "ok.txt"
    f.write_text("just some ordinary documentation text\n", encoding="utf-8")
    rc = secret_scan.main(["scan", str(tmp_path)])
    assert rc == 0


# --------------------------------------------------------------------------- #
# binary awareness + bounded scanning (data-heavy-oracle regression: a tree of
# office/columnar deliverables decoded as text fed garbage to the regexes and
# hung the gate)
# --------------------------------------------------------------------------- #
def test_scan_tree_skips_office_and_columnar_suffixes(tmp_path: Path):
    payload = b"PK\x03\x04" + b"\x00\x01" * 64 + b"ghp_" + b"Z" * 36
    for name in ("deck.pptx", "model.xlsx", "doc.docx", "export.parquet", "cache.duckdb"):
        (tmp_path / name).write_bytes(payload)
    assert secret_scan.scan_tree(tmp_path) == []


def test_scan_tree_sniffs_binary_with_unknown_suffix(tmp_path: Path):
    """A NUL-carrying blob is skipped by content sniff, suffix notwithstanding."""
    blob = tmp_path / "export.dat"
    blob.write_bytes(b"\x00\x01\x02\x03" * 256 + b"ghp_" + b"Z" * 36)
    assert secret_scan.scan_tree(tmp_path) == []


def test_looks_binary_classification():
    # UTF-8 multibyte text is text, not binary.
    assert not secret_scan.looks_binary("résumé — naïve café ✓\nplain\n".encode("utf-8"))
    assert not secret_scan.looks_binary(b"ordinary ascii with\ttabs\nand newlines\n")
    assert not secret_scan.looks_binary(b"")
    # NUL byte or dense control bytes mean binary.
    assert secret_scan.looks_binary(b"abc\x00def")
    assert secret_scan.looks_binary(bytes(range(1, 9)) * 32)


def test_secret_in_text_still_found_next_to_binary(tmp_path: Path):
    """The binary guard must not weaken detection in real text files."""
    (tmp_path / "blob.dat").write_bytes(b"\x00" * 128)
    (tmp_path / "note.md").write_text(
        "creds: ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n", encoding="utf-8"
    )
    results = secret_scan.scan_tree(tmp_path)
    assert {r["file"] for r in results} == {"note.md"}


def test_scan_tree_exclude_predicate_prunes_subtree(tmp_path: Path):
    """The caller-supplied exclude predicate skips files AND prunes excluded
    directories so the walk never enters them."""
    secret = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n"
    (tmp_path / "raw" / "deep").mkdir(parents=True)
    (tmp_path / "raw" / "deep" / "x.txt").write_text(secret, encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "y.txt").write_text(secret, encoding="utf-8")

    excluded = lambda rel: rel == "raw" or rel.startswith("raw/")  # noqa: E731
    results = secret_scan.scan_tree(tmp_path, exclude=excluded)
    assert {r["file"] for r in results} == {"docs/y.txt"}


def test_long_machine_generated_line_is_bounded():
    """A multi-megabyte single alnum line must scan in bounded time (the old
    unbounded aws-gap pattern was quadratic here -- an effective hang)."""
    import time

    line = "aws0" * 500_000  # 2 MB, one line, all alphanumeric
    t0 = time.monotonic()
    secret_scan.scan_text(line)
    assert time.monotonic() - t0 < 5.0


def test_akia_proximity_check_is_not_quadratic():
    """The whole-text AKIA search must run once per call, not once per line."""
    import time

    text = "AKIAIOSFODNN7EXAMPLE\n" + ("an ordinary line of prose text\n" * 20_000)
    t0 = time.monotonic()
    secret_scan.scan_text(text)
    assert time.monotonic() - t0 < 5.0


def test_overlong_line_scans_only_the_head():
    """Lines beyond the per-line cap are truncated for scanning: a credential
    in the head is found, one parked past the cap is the documented trade-off."""
    head_secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    tail_secret = "glpat-ABCDefgh1234IJKLmnop"
    line = head_secret + " " + ("x" * (secret_scan._MAX_LINE_CHARS + 100)) + " " + tail_secret
    pats = {f["pattern"] for f in secret_scan.scan_text(line)}
    assert "github_token" in pats
    assert "gitlab_pat" not in pats


def test_findings_are_capped_per_call():
    """A pathological file cannot accumulate unbounded findings (memory guard);
    the gate is already red at one finding."""
    text = "\n".join(
        f"token: x9k{i}q7p2w5z8r4t6y1u3o0m" for i in range(secret_scan._MAX_FINDINGS * 2)
    )
    findings = secret_scan.scan_text(text)
    assert 0 < len(findings) <= secret_scan._MAX_FINDINGS


def test_keyed_hex_digest_is_not_a_secret():
    """The kernel's own provenance lines (captured_sha256=<hex>) must not flag."""
    from secret_scan import scan_text

    line = (
        "provenance: Ingested from _INPUT/x.txt via manual; "
        "captured_sha256=7cc7375f69e03b768bd6271f9c84ac175617c02a9a257e479f4038d09393fc7e."
    )
    assert scan_text(line) == []


def test_keyed_hex_with_secret_key_name_still_flags():
    """A 64-hex value on a credential-named key keeps flagging."""
    from secret_scan import scan_text

    line = "apikey=7cc7375f69e03b768bd6271f9c84ac175617c02a9a257e479f4038d09393fc7e"
    findings = scan_text(line)
    assert findings, "credential-shaped keyed hex must still be reported"
