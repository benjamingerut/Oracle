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

while [ $# -gt 0 ]; do
  case "$1" in
    --from-dir) FROM_DIR="$2"; shift 2 ;;
    --git-url)  GIT_URL="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# 1. python3 >= 3.10
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "FATAL: python3 not found; install Python 3.10+" >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || {
  echo "FATAL: python3 is older than 3.10" >&2; exit 1; }

mkdir -p "$ORACLE_HOME" && chmod 700 "$ORACLE_HOME"

# 2. obtain source
if [ -n "$FROM_DIR" ]; then
  echo "==> copying source from $FROM_DIR"
  rm -rf "$APP_DIR"
  mkdir -p "$APP_DIR"
  (cd "$FROM_DIR" && tar -cf - --exclude .git --exclude tmp.nosync \
      --exclude .pytest_cache --exclude __pycache__ .) | (cd "$APP_DIR" && tar -xf -)
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

# 3. venv + install (zero runtime deps; pip just wires the entry point)
[ -d "$VENV_DIR" ] || "$PY" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV_DIR/bin/pip" install --quiet "$APP_DIR"

# 4. expose the command
mkdir -p "$BIN_DIR"
ln -sf "$VENV_DIR/bin/oracle" "$BIN_DIR/oracle"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "NOTE: add $BIN_DIR to your PATH to use 'oracle' directly." ;;
esac

# 5. doctor
echo "==> oracle doctor"
"$VENV_DIR/bin/oracle" doctor || true

echo ""
echo "Installed. Next: oracle setup"
