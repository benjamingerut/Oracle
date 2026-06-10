#!/usr/bin/env bash
# load-env.sh — source the oracle's local secrets into the environment.
#
# It sources .env.nosync with `set -a` so every assignment is exported, and NEVER echoes a
# value — it only prints an error to stderr if the file is missing. Secrets stay
# in .env.nosync (git-ignored, backup-excluded); this script just loads them.
#
# Usage:  source ./load-env.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$ROOT/.env.nosync" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env.nosync"
  set +a
else
  echo "No .env.nosync found at $ROOT/.env.nosync" >&2
  exit 1
fi
