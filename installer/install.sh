#!/bin/sh
# Oracle installer (SPEC S9). POSIX sh, no sudo, nothing system-wide.
#
#   sh install.sh                      # clone from GIT_URL (set below or env)
#   sh install.sh --from-dir /path     # install from a local checkout
#
# Layout: ~/.oracle/app (source), ~/.oracle/venv (env), ~/.local/bin/oracle.
# Idempotent: re-running updates the checkout and reinstalls.

set -eu

ORACLE_HOME="${ORACLE_HOME:-$HOME/.oracle}"
APP_DIR="$ORACLE_HOME/app"
VENV_DIR="$ORACLE_HOME/venv"
BIN_DIR="${ORACLE_BIN:-$HOME/.local/bin}"
GIT_URL="${ORACLE_GIT_URL:-}"
FROM_DIR=""

COPY_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --from-dir) FROM_DIR="$2"; shift 2 ;;
    --git-url)  GIT_URL="$2"; shift 2 ;;
    --copy-only) COPY_ONLY=1; shift ;;   # CI: stop after source copy + integrity check
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# 1. python3 >= 3.10. Plain `python3` on a stock Mac is often Apple's 3.9,
#    while a newer versioned interpreter sits unlinked on PATH — so probe the
#    versioned names too. ORACLE_PYTHON overrides everything. (--copy-only
#    stages source without touching python, so the probe is skipped there.)
PY=""
[ "$COPY_ONLY" = "1" ] || for cand in "${ORACLE_PYTHON:-}" python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
  [ -n "$cand" ] || continue
  p="$(command -v "$cand" 2>/dev/null || true)"
  [ -n "$p" ] || continue
  if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY="$p"
    break
  fi
done
if [ "$COPY_ONLY" != "1" ]; then
  [ -n "$PY" ] || {
    echo "FATAL: no Python 3.10+ found (tried python3 and python3.14..python3.10)." >&2
    echo "       Install Python 3.10+ (e.g. 'brew install python3') or point" >&2
    echo "       ORACLE_PYTHON at an interpreter and re-run." >&2
    exit 1
  }
  echo "==> using $PY ($("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))"
fi

mkdir -p "$ORACLE_HOME" && chmod 700 "$ORACLE_HOME"

# 2. obtain source
if [ -n "$FROM_DIR" ]; then
  echo "==> copying source from $FROM_DIR"
  rm -rf "$APP_DIR"
  mkdir -p "$APP_DIR"
  # tar --exclude matches a name ANYWHERE in the tree (and bsdtar/GNU tar
  # anchoring semantics differ), and the kernel template legitimately ships a
  # tmp.nosync/ directory that spawned oracles require. So tar only excludes
  # names that are safe to drop at any depth; the repo's top-level tmp.nosync
  # scratch dir is pruned AFTER the copy, anchored by explicit path.
  (cd "$FROM_DIR" && tar -cf - --exclude .git \
      --exclude .pytest_cache --exclude __pycache__ .) | (cd "$APP_DIR" && tar -xf -)
  rm -rf "$APP_DIR/tmp.nosync"
elif [ -d "$APP_DIR/.git" ]; then
  echo "==> updating existing checkout"
  git -C "$APP_DIR" pull --ff-only
elif [ -n "$GIT_URL" ]; then
  echo "==> cloning $GIT_URL"
  git clone "$GIT_URL" "$APP_DIR"
else
  echo "FATAL: no source. Use --from-dir PATH or set ORACLE_GIT_URL." >&2
  exit 2
fi

# 3. integrity: the copied app must carry a complete kernel template, or every
#    oracle spawned from this install fails its own post-spawn audit. Fail HERE
#    with an actionable message instead.
KERNEL_ASSET="$APP_DIR/src/oracle_agent/assets/oracle-kernel"
for sentinel in oracle.yml tmp.nosync/_CONTEXT.md _tools/setup_audit.py; do
  [ -f "$KERNEL_ASSET/$sentinel" ] || {
    echo "FATAL: installed source is missing kernel file: $sentinel" >&2
    echo "       The copy/checkout under $APP_DIR is incomplete; spawned oracles" >&2
    echo "       would fail their post-spawn audit. Re-run the installer from a" >&2
    echo "       clean checkout." >&2
    exit 1
  }
done

if [ "$COPY_ONLY" = "1" ]; then
  echo "copy-only: source staged and kernel template verified at $APP_DIR"
  exit 0
fi

# 4. venv + install (zero runtime deps; pip just wires the entry point)
[ -d "$VENV_DIR" ] || "$PY" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV_DIR/bin/pip" install --quiet "$APP_DIR"

# 5. expose the command
mkdir -p "$BIN_DIR"
ln -sf "$VENV_DIR/bin/oracle" "$BIN_DIR/oracle"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "NOTE: add $BIN_DIR to your PATH to use 'oracle' directly." ;;
esac

# 6. doctor
echo "==> oracle doctor"
"$VENV_DIR/bin/oracle" doctor || true

echo ""
echo "Installed. Next: oracle setup"
