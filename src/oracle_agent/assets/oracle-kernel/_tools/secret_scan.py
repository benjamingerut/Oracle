#!/usr/bin/env python3
"""Broadened, entropy-scored secret scanner (stdlib-only).

This module is the floor secrets enforcer: it recognises a wide catalogue of
provider tokens by pattern AND flags
high-Shannon-entropy blobs that look like keys regardless of provider.

Public API (interface_contracts: "secret_scan API"):
    scan_text(text: str) -> list[dict]   # each {pattern, line, offset[, entropy]}
    scan_tree(root: Path) -> list[dict]  # adds {file} to each finding

Required detections (interface_contracts):
    ghp_/gho_/ghu_/ghs_/github_pat_, glpat-, sk_live_/sk_test_/rk_,
    sk-ant-... (Anthropic), AIza[0-9A-Za-z_-]{35}, GCP service-account
    "private_key" JSON fields, postgres(ql)?://user:pass@, PEM headers,
    AKIA + 40-char AWS-secret heuristic, xox*, JWT, AccountKey= (Azure),
    npm_... tokens, Telegram bot tokens (NNN:AA...), generic
    password:/token:/api_key: assignments (digit-free for sensitive keys),
    plus Shannon-entropy blobs.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
# pattern catalogue
#
# Each entry: (pattern_name, compiled_regex). Patterns are intentionally
# anchored on the distinctive prefix/structure of each credential family so the
# named-pattern findings are precise; the entropy pass is the safety net for
# anything bespoke.
# --------------------------------------------------------------------------- #
_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    # GitHub personal/oauth/user/server tokens (ghp_, gho_, ghu_, ghs_)
    ("github_token", re.compile(r"\bgh[opus]_[A-Za-z0-9]{36,}\b")),
    # GitHub fine-grained PAT
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    # GitLab personal access token
    ("gitlab_pat", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    # Stripe live/test secret keys and restricted keys
    ("stripe_secret", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    # Anthropic API key (sk-ant-api03-...)
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}\b")),
    # Older / generic OpenAI-style sk- keys (must not overlap Anthropic or Stripe)
    ("sk_key", re.compile(r"\bsk-(?!ant-|live_|test_)[A-Za-z0-9]{20,}\b")),
    # Google API key
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    # GCP service-account JSON private key field
    (
        "gcp_service_account_key",
        re.compile(r'"private_key"\s*:\s*"-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    ),
    # Postgres / Postgresql connection string with inline user:pass
    (
        "postgres_url",
        re.compile(r"postgres(?:ql)?://[^\s:/@]+:[^\s:/@]+@[^\s/]+", re.IGNORECASE),
    ),
    # PEM private key header (RSA / EC / OPENSSH / generic PRIVATE KEY)
    (
        "pem_private_key",
        re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    ),
    # AWS access key id (the AKIA / ASIA prefix + 16 uppercase/num)
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # Slack tokens (xoxb-, xoxp-, xoxa-, xoxr-, xoxs-)
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # JSON Web Token (three base64url segments)
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    # Azure storage connection string AccountKey
    (
        "azure_storage_key",
        re.compile(r"\bAccountKey=[A-Za-z0-9+/]{20,}={0,2}\b"),
    ),
    # npm access / automation / publish tokens (npm_...)
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36,}\b")),
    # Telegram bot token (numeric-id:alphanumeric-secret, e.g. 123456:AABBccdd...)
    (
        "telegram_bot_token",
        re.compile(r"\b[0-9]{8,10}:AA[A-Za-z0-9_-]{33,}\b"),
    ),
]

# AWS *secret* access key heuristic: a 40-char base64-ish blob appearing near an
# AKIA id or an aws_secret assignment. We surface it as its own pattern.
# The gap between ``aws`` and ``secret|sk`` is bounded ({0,40}) -- real key
# names are short (aws_secret_access_key), and an unbounded lazy gap is
# quadratic on long alphanumeric lines, which is a hang on machine-generated
# content.
_AWS_SECRET = re.compile(
    r"(?i)aws[a-z0-9_]{0,40}?(?:secret|sk)[a-z0-9_]*\s*[\"'\s:=]+\s*[\"']?"
    r"([A-Za-z0-9/+=]{40})\b"
)
_AKIA_NEARBY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_RAW_40 = re.compile(r"\b[A-Za-z0-9/+=]{40}\b")

# Generic assignment style: password: / token: / api_key: / secret: / apikey =
# Group 2 captures an optional opening quote so the scanner can tell a config/env
# literal (token: "<literal>") from a source-code reference (token = m.group(0)).
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?token|"
    r"auth[_-]?token|client[_-]?secret|private[_-]?key)\b\s*[:=]\s*"
    r"(['\"]?)([^\s'\"]{6,})"
)

# Structural characters that mark a value as source code or markup rather than a
# literal credential (e.g. ``token = m.group(0)`` or ``token: <your-token>``).
# A colon inside the captured value means we matched a key-name list fragment
# (password:/token:/api_key:) rather than an assigned value; real credential
# values do not contain a bare colon.
_CODE_CHARS = frozenset("()[]{}<>:")

# Placeholder values we should NOT flag as a leaked assignment.
# Covers: shell/Jinja/Helm ${VAR}, {{VAR}}, angle-bracket templates <...>,
# repeated-char masks (xxx, ***), common placeholder words, and env-lookup
# references (os.environ, getenv, env(), process.env).
_PLACEHOLDER = re.compile(
    r"(?i)^(?:"
    r"\$\{?[a-z0-9_]+\}?"          # ${VAR} or $VAR
    r"|\{\{[^}]*\}\}"               # {{VAR}} Jinja/Helm interpolation
    r"|<[^>]+>"                     # <your-token> angle-bracket templates
    r"|x{3,}"                       # xxx...
    r"|\*{3,}"                      # ***...
    r"|changeme"
    r"|placeholder"
    r"|your[_-]?\w*"                # your_token, your-key, yourtoken, your_
    r"|example"
    r"|none"
    r"|null"
    r"|true"
    r"|false"
    r"|redacted"
    r"|\.{3,}"                      # ...
    r"|os\.environ"                 # os.environ['KEY']
    r"|(?:os\.)?getenv\b"           # getenv(...)
    r"|process\.env\b"              # Node.js process.env
    r"|env\(['\"]?[a-z0-9_]+['\"]?\)" # env('VAR')
    r")$"
)

# Sensitive key names for which a digit-free value of ≥ 8 chars is still
# flagged (the general heuristic requires a digit to suppress word-like prose;
# these names make the assignment unambiguous enough to drop that guard).
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)^(?:password|passwd|secret|token|api[_-]?key|apikey)$"
)

# Entropy scan tuning.
_ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{20,}")
_ENTROPY_THRESHOLD = 4.0  # bits/char; 4.0 catches dense base64-ish blobs
_ENTROPY_MIN_LEN = 24
# A real credential is one long UNBROKEN run of base64/hex characters. Paths
# (Meta.nosync/Autonomy/KILL-SWITCH), XML namespaces, lane names and prose
# joined by '/', '-', '_', '+' clear the raw entropy bar but break into many
# short word-runs -- so we additionally require a long contiguous alphanumeric
# run before treating a high-entropy token as a secret.
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")
_ENTROPY_MIN_RUN = 20
# Pure hex digests (sha1/sha256/md5 in .kernel-manifest.json, ledger hashes,
# git oids) are content addresses, not secrets -- exempt them outright.
_HEX_DIGEST = re.compile(r"(?:[0-9a-f]{32,}|[0-9A-F]{32,})\Z")
# A digest VALUE attached to a digest-named key (captured_sha256=..., oid=...).
# The kernel's own provenance lines carry these; they are content addresses,
# not credentials. The key name must say so -- ``apikey=<hex>`` still flags.
_KEYED_DIGEST = re.compile(
    r"(?i)[a-z0-9_\-]*(?:sha\d*|hash|digest|checksum|oid)[a-z0-9_\-]*="
    r"(?:[0-9a-f]{32,}|[0-9A-F]{32,})\Z"
)

# Per-call work budget. The scan is a session-ritual gate: it must be bounded
# even on pathological input. Lines beyond _MAX_LINE_CHARS are scanned only up
# to the cap (human-authored credentials live on human-scale lines; a
# multi-megabyte single line is machine-generated), and a file that has already
# produced _MAX_FINDINGS findings is maximally red -- further matches add no
# information, only memory.
_MAX_LINE_CHARS = 10_000
_MAX_FINDINGS = 1_000


def shannon_entropy(s: str) -> float:
    """Shannon entropy in bits per character."""
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    ent = 0.0
    for c in counts.values():
        p = c / n
        ent -= p * math.log2(p)
    return ent


def _is_wordy(s: str) -> bool:
    """True if the token looks like ordinary prose/hex-words rather than a key.

    A long run of a small alphabet (e.g. lowercase words joined by dashes) has
    lower entropy and tends to trip up naive scanners; the entropy threshold
    handles most of it, but we also skip tokens that are obviously a single
    English-ish word or a date-like / numeric-only string.
    """
    if s.isdigit():
        return True
    distinct = len(set(s))
    # Very low character diversity is almost never a random secret.
    if distinct <= 6:
        return True
    return False


def _longest_alnum_run(s: str) -> int:
    """Length of the longest unbroken [A-Za-z0-9] run in ``s``.

    Real credentials are a single long base64/hex run; filesystem paths, XML
    namespaces, routing-lane names and prose joined by ``/ - _ + .`` break into
    many short word-runs, which is how we tell them apart from secrets.
    """
    runs = _ALNUM_RUN.findall(s)
    return max((len(r) for r in runs), default=0)


def scan_text(text: str) -> list[dict]:
    """Scan a block of text and return a list of findings.

    Each finding is a dict with keys ``pattern``, ``line`` (1-based), ``offset``
    (0-based column within the line), ``match`` (the matched excerpt, truncated)
    and, for entropy findings, ``entropy``.
    """
    findings: list[dict] = []
    if not text:
        return findings

    lines = text.splitlines()
    # Hoisted out of the per-line loop: re-searching the WHOLE text once per
    # line is quadratic in file size (a real hang on large files).
    has_akia = _AKIA_NEARBY.search(text) is not None

    for lineno, line in enumerate(lines, start=1):
        if len(findings) >= _MAX_FINDINGS:
            break
        if len(line) > _MAX_LINE_CHARS:
            line = line[:_MAX_LINE_CHARS]
        # Entropy-vs-named-pattern overlap only matters within one line; index
        # where this line's findings start so the overlap check stays O(line).
        line_findings_start = len(findings)
        # 1) named provider patterns
        for name, pat in _PATTERNS:
            for m in pat.finditer(line):
                findings.append(
                    {
                        "pattern": name,
                        "line": lineno,
                        "offset": m.start(),
                        "match": _redact(m.group(0)),
                    }
                )

        # 2) AWS secret-key heuristic (assignment-anchored OR 40-char near AKIA)
        for m in _AWS_SECRET.finditer(line):
            findings.append(
                {
                    "pattern": "aws_secret_access_key",
                    "line": lineno,
                    "offset": m.start(1),
                    "match": _redact(m.group(1)),
                }
            )
        if has_akia:
            for m in _RAW_40.finditer(line):
                blob = m.group(0)
                if shannon_entropy(blob) >= 3.5 and not blob.isdigit():
                    findings.append(
                        {
                            "pattern": "aws_secret_access_key",
                            "line": lineno,
                            "offset": m.start(),
                            "match": _redact(blob),
                            "entropy": round(shannon_entropy(blob), 3),
                        }
                    )

        # 3) generic password:/token:/api_key: assignments.
        #    Quoted literals (config/env style: token: "<literal>") are treated
        #    as secrets. Unquoted values must look credential-like (contain a
        #    digit and not be a low-diversity word) so ordinary source code
        #    (token = m.group(0)) and docstring prose (api_key: assignments) do
        #    not trip the heuristic. Values carrying code/markup structure are
        #    never secrets; structured provider tokens are caught by _PATTERNS.
        #
        #    Tightened heuristic (K4): for highly sensitive key names
        #    (password, passwd, secret, token, api_key, apikey) a digit-free
        #    value of ≥ 8 chars is still flagged because the key name alone
        #    makes the assignment unambiguous — digits are not needed as a
        #    credibility signal. Placeholders and _CODE_CHARS still suppress.
        for m in _ASSIGNMENT.finditer(line):
            key_name = m.group(1)
            quote = m.group(2)
            value = m.group(3)
            if _PLACEHOLDER.match(value):
                continue
            if any(c in _CODE_CHARS for c in value):
                continue
            if not quote:
                if _is_wordy(value):
                    continue
                # Unquoted identifier-shaped values are source code passing a
                # variable through (api_key=api_key, api_key=resolved_key,...),
                # never a literal secret: real credentials are not lowercase
                # snake_case identifiers. Quoted literals are still flagged.
                bare = value.rstrip(",;)").strip()
                if bare == key_name:
                    continue
                if "_" in bare and bare.isidentifier() and bare == bare.lower():
                    continue
                # For highly sensitive key names: flag digit-free values ≥ 8
                # chars; for all others keep the original digit requirement.
                has_digit = any(ch.isdigit() for ch in value)
                sensitive = bool(_SENSITIVE_KEY_RE.match(key_name))
                if not has_digit and not (sensitive and len(value) >= 8):
                    continue
            findings.append(
                {
                    "pattern": "generic_assignment",
                    "line": lineno,
                    "offset": m.start(3),
                    "match": _redact(value),
                }
            )

        # 4) entropy safety net for bespoke high-entropy blobs
        for m in _ENTROPY_TOKEN.finditer(line):
            token = m.group(0)
            if len(token) < _ENTROPY_MIN_LEN:
                continue
            if _is_wordy(token):
                continue
            # Pure hex digests (manifest/ledger sha256, git oids) are content
            # addresses, not secrets.
            if _HEX_DIGEST.fullmatch(token) or _KEYED_DIGEST.fullmatch(token):
                continue
            # Secrets are a long contiguous base64/hex run; paths, XML
            # namespaces, lane names and punctuation-joined prose are not.
            if _longest_alnum_run(token) < _ENTROPY_MIN_RUN:
                continue
            ent = shannon_entropy(token)
            if ent >= _ENTROPY_THRESHOLD:
                if _already_reported(
                    findings[line_findings_start:], lineno, m.start()
                ):
                    continue
                findings.append(
                    {
                        "pattern": "high_entropy_blob",
                        "line": lineno,
                        "offset": m.start(),
                        "match": _redact(token),
                        "entropy": round(ent, 3),
                    }
                )

    return _dedupe(findings)


def _already_reported(findings: list[dict], line: int, offset: int) -> bool:
    """Avoid double-reporting an entropy hit that overlaps a named-pattern hit."""
    for f in findings:
        if f["line"] == line and abs(f["offset"] - offset) <= 4:
            return True
    return False


def _dedupe(findings: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for f in findings:
        key = (f["pattern"], f["line"], f["offset"])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    out.sort(key=lambda f: (f["line"], f["offset"]))
    return out


def _redact(s: str, keep: int = 4) -> str:
    """Return a short, non-leaking excerpt of the matched secret."""
    s = s.strip()
    if len(s) <= keep:
        return s[:1] + "***"
    return s[:keep] + "***" + f"({len(s)} chars)"


# --------------------------------------------------------------------------- #
# tree scanning
# --------------------------------------------------------------------------- #
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
_SKIP_SUFFIXES = {
    # images / fonts / archives / misc binary
    ".png", ".jpg", ".jpeg", ".gif", ".heic", ".ico", ".woff", ".woff2",
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".bz2", ".xz", ".bin",
    # databases / columnar / serialized data
    ".db", ".sqlite", ".sqlite3", ".duckdb", ".parquet", ".feather",
    ".arrow", ".pkl", ".pickle", ".npy", ".npz",
    # office formats (zip containers read-as-text decode to regex-hostile noise)
    ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt",
    # audio / video
    ".mp3", ".mp4", ".mov", ".avi", ".wav",
}
_MAX_BYTES = 5 * 1024 * 1024

# Binary sniff: read a small head and decide whether the file is text at all.
# Decoding a binary file with errors="ignore" produces high-entropy garbage
# that floods the regexes -- the suffix list above is the fast path, this is
# the suffix-independent guard.
_SNIFF_BYTES = 8192
# C0 control bytes that never appear in real text (NUL et al.), i.e. everything
# below 0x20 except TAB/LF/CR/FF/ESC, plus DEL.
_NONTEXT_BYTES = bytes(sorted(set(range(0x20)) - {0x09, 0x0A, 0x0C, 0x0D, 0x1B})) + b"\x7f"
_NONTEXT_RATIO = 0.30


def looks_binary(head: bytes) -> bool:
    """True if a file head is binary content (NUL byte or dense control bytes).

    UTF-8 multibyte text is NOT binary by this test: bytes >= 0x80 are left
    alone; only NULs and C0 control density mark a file as non-text. (UTF-16
    files trip the NUL check -- preferable to scanning them as mojibake.)
    """
    if not head:
        return False
    if b"\x00" in head:
        return True
    nontext = sum(head.count(b) for b in _NONTEXT_BYTES)
    return (nontext / len(head)) > _NONTEXT_RATIO


def is_binary_file(p: Path) -> bool:
    """Sniff the first _SNIFF_BYTES of ``p``; unreadable files count as binary."""
    try:
        with p.open("rb") as fh:
            return looks_binary(fh.read(_SNIFF_BYTES))
    except OSError:
        return True


def iter_files(root: Path, exclude=None):
    """Yield files under ``root`` depth-first, pruning _SKIP_DIRS in place.

    Unlike ``rglob``, this never descends into skipped directories and never
    materializes the whole tree in memory -- both matter on data-heavy oracles
    with multi-gigabyte ``.nosync`` exports. Order is deterministic (sorted
    per directory).

    ``exclude`` (optional) is a callable taking a root-relative POSIX path
    (directory or file) and returning True to skip it; an excluded directory
    is pruned, so its subtree is never walked. The predicate is the caller's
    policy -- this module stays mechanism-only.
    """
    root_s = str(root)
    for dirpath, dirnames, filenames in os.walk(root):
        keep: list[str] = []
        for d in sorted(dirnames):
            if d in _SKIP_DIRS:
                continue
            if exclude is not None:
                rel_d = os.path.relpath(os.path.join(dirpath, d), root_s).replace(os.sep, "/")
                if exclude(rel_d):
                    continue
            keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            if exclude is not None:
                rel_f = os.path.relpath(os.path.join(dirpath, name), root_s).replace(os.sep, "/")
                if exclude(rel_f):
                    continue
            p = Path(dirpath) / name
            if p.is_file():
                yield p


def scan_tree(root: Path, exclude=None) -> list[dict]:
    """Scan every text-ish file under ``root``; return findings with ``file``.

    Binary (by suffix AND by content sniff), oversized files and common
    vendored directories are skipped. The ``.env``-style ignore is
    intentionally NOT applied here -- the scanner's job is to find secrets
    wherever they are; callers decide policy. ``exclude`` is the caller's
    policy hook (see ``iter_files``); it is ignored when ``root`` is a single
    explicitly named file.
    """
    root = Path(root)
    results: list[dict] = []
    if root.is_file():
        files = iter([root])
        base = root.parent
    else:
        files = iter_files(root, exclude=exclude)
        base = root
    for p in files:
        if p.suffix.lower() in _SKIP_SUFFIXES:
            continue
        try:
            if p.stat().st_size > _MAX_BYTES:
                continue
            if is_binary_file(p):
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for f in scan_text(text):
            try:
                rel = str(p.relative_to(base))
            except ValueError:
                rel = str(p)
            f = dict(f)
            f["file"] = rel
            results.append(f)
    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Broadened secret scanner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_scan = sub.add_parser("scan", help="scan a file or directory tree")
    p_scan.add_argument("path")
    p_scan.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    target = Path(args.path)
    findings = scan_tree(target)
    if args.json:
        print(json.dumps(findings, indent=2, ensure_ascii=False))
    else:
        for f in findings:
            loc = f.get("file", str(target))
            print(
                f"{loc}:{f['line']}:{f['offset']}: {f['pattern']}"
                + (f" (entropy={f['entropy']})" if "entropy" in f else "")
            )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
