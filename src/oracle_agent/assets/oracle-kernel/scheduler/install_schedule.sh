#!/usr/bin/env bash
# install_schedule.sh -- render + (optionally) enable the headless loop schedule.
#
# The oracle can run its due loops between sessions via a headless harness. That
# is the HIGHEST-blast-radius capability in the kernel, so this installer is
# conservative by construction:
#
#   * By DEFAULT it only RENDERS the platform template (launchd plist on macOS,
#     cron line on Linux) with the oracle's codename + absolute root and PRINTS
#     the exact actions it WOULD take. It changes nothing.
#   * It ENABLES the schedule (loads the launchd agent / installs the crontab
#     line UNCOMMENTED) ONLY when the admin passes the explicit --enable flag.
#     There is no implicit/default enable path.
#   * --disable unloads the launchd agent / removes the crontab line.
#   * Even when enabled, the schedule is inert until autonomy is turned ON in
#     Meta.nosync/Autonomy/autonomy.yml, because harness.py --once checks the
#     kill-switch and autonomy gate first.
#
# Usage:
#   install_schedule.sh [--root DIR] [--codename NAME] [--python BIN]
#                       [--enable | --disable] [--dry-run]
#
# Stdlib/posix only; no third-party tools required.

set -euo pipefail

# --------------------------------------------------------------------------- #
# locate self / defaults
# --------------------------------------------------------------------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# scheduler/ lives directly under the oracle kernel root.
DEFAULT_ROOT="$(cd "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"

ORACLE_ROOT="${DEFAULT_ROOT}"
CODENAME=""
PYTHON_BIN=""
MODE="render"   # render | enable | disable
DRY_RUN=0

# --------------------------------------------------------------------------- #
# args
# --------------------------------------------------------------------------- #
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root)      ORACLE_ROOT="$2"; shift 2 ;;
    --codename)  CODENAME="$2"; shift 2 ;;
    --python)    PYTHON_BIN="$2"; shift 2 ;;
    --enable)    MODE="enable"; shift ;;
    --disable)   MODE="disable"; shift ;;
    --dry-run)   DRY_RUN=1; shift ;;
    -h|--help)
      grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'
      exit 0
      ;;
    *)
      echo "install_schedule.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

# Absolutize root.
ORACLE_ROOT="$(cd "${ORACLE_ROOT}" >/dev/null 2>&1 && pwd)"

# Resolve python.
if [ -z "${PYTHON_BIN}" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    PYTHON_BIN="/usr/bin/python3"
  fi
fi

# Derive codename from oracle.yml if not supplied.
if [ -z "${CODENAME}" ]; then
  if [ -f "${ORACLE_ROOT}/oracle.yml" ]; then
    CODENAME="$(grep -E '^[[:space:]]*codename:' "${ORACLE_ROOT}/oracle.yml" \
      | head -n1 | sed -E 's/.*codename:[[:space:]]*//; s/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//' || true)"
  fi
fi
[ -z "${CODENAME}" ] && CODENAME="oracle"
# lowercase codename for labels/filenames
CODENAME_LC="$(printf '%s' "${CODENAME}" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')"
[ -z "${CODENAME_LC}" ] && CODENAME_LC="oracle"

OS="$(uname -s 2>/dev/null || echo unknown)"

PLIST_TEMPLATE="${SCRIPT_DIR}/com.oracle.loops.plist.template"
CRON_TEMPLATE="${SCRIPT_DIR}/oracle-loops.cron.template"
RENDER_DIR="${ORACLE_ROOT}/tmp.nosync"
mkdir -p "${RENDER_DIR}"

LAUNCHD_LABEL="com.oracle.${CODENAME_LC}.loops"
LAUNCHD_TARGET="${HOME}/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"
RENDERED_PLIST="${RENDER_DIR}/${LAUNCHD_LABEL}.plist"
RENDERED_CRON="${RENDER_DIR}/oracle-loops.${CODENAME_LC}.cron"

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
render() {
  # render <template> <out> : substitute placeholders. Uses awk so we never
  # depend on a particular sed dialect for the path values (which contain '/').
  local tmpl="$1" out="$2"
  awk \
    -v codename="${CODENAME_LC}" \
    -v root="${ORACLE_ROOT}" \
    -v py="${PYTHON_BIN}" \
    '{
       gsub(/\{\{CODENAME\}\}/, codename);
       gsub(/\{\{ORACLE_ROOT\}\}/, root);
       gsub(/\{\{PYTHON\}\}/, py);
       print;
     }' "${tmpl}" > "${out}"
}

say() { printf '%s\n' "$*"; }

# --------------------------------------------------------------------------- #
# always render + report
# --------------------------------------------------------------------------- #
say "=== oracle schedule installer ==="
say "codename     : ${CODENAME_LC}"
say "oracle root  : ${ORACLE_ROOT}"
say "python       : ${PYTHON_BIN}"
say "platform     : ${OS}"
say "mode         : ${MODE}$([ "${DRY_RUN}" -eq 1 ] && echo ' (dry-run)')"
say ""

if [ "${OS}" = "Darwin" ]; then
  if [ ! -f "${PLIST_TEMPLATE}" ]; then
    echo "missing template: ${PLIST_TEMPLATE}" >&2
    exit 1
  fi
  render "${PLIST_TEMPLATE}" "${RENDERED_PLIST}"
  say "rendered launchd plist -> ${RENDERED_PLIST}"
  say "(DISABLED by default: RunAtLoad=false, Disabled=true)"
else
  if [ ! -f "${CRON_TEMPLATE}" ]; then
    echo "missing template: ${CRON_TEMPLATE}" >&2
    exit 1
  fi
  render "${CRON_TEMPLATE}" "${RENDERED_CRON}"
  say "rendered cron file -> ${RENDERED_CRON}"
  say "(DISABLED by default: the schedule line is commented out)"
fi
say ""

# --------------------------------------------------------------------------- #
# render-only (default): print intended actions, change NOTHING
# --------------------------------------------------------------------------- #
if [ "${MODE}" = "render" ]; then
  say "Render-only mode. No schedule was installed or enabled."
  say "To ENABLE (admin action required), re-run with --enable. Intended actions:"
  if [ "${OS}" = "Darwin" ]; then
    say "  cp '${RENDERED_PLIST}' '${LAUNCHD_TARGET}'   # with Disabled=false, RunAtLoad=true"
    say "  launchctl unload '${LAUNCHD_TARGET}' 2>/dev/null || true"
    say "  launchctl load '${LAUNCHD_TARGET}'"
  else
    say "  install the UNCOMMENTED schedule line from '${RENDERED_CRON}' into the user crontab"
  fi
  say ""
  say "Reminder: even once enabled, loops stay inert until autonomy is turned ON"
  say "in ${ORACLE_ROOT}/Meta.nosync/Autonomy/autonomy.yml."
  exit 0
fi

# --------------------------------------------------------------------------- #
# enable (explicit admin confirm)
# --------------------------------------------------------------------------- #
if [ "${MODE}" = "enable" ]; then
  if [ "${OS}" = "Darwin" ]; then
    # Flip Disabled/RunAtLoad to active in a SEPARATE enabled copy. The plist
    # has the <key> and its <true/>/<false/> value on separate lines, so we use
    # an awk state machine: when we see the RunAtLoad/Disabled key, flip the
    # boolean on the NEXT value line to the enabled polarity (RunAtLoad->true,
    # Disabled->false). This is robust to the multi-line plist layout.
    ENABLED_PLIST="${RENDER_DIR}/${LAUNCHD_LABEL}.enabled.plist"
    awk '
      /<key>RunAtLoad<\/key>/ { print; flip="run"; next }
      /<key>Disabled<\/key>/  { print; flip="dis"; next }
      flip=="run" && /<(true|false)\/>/ { sub(/<(true|false)\/>/, "<true/>"); flip=""; print; next }
      flip=="dis" && /<(true|false)\/>/ { sub(/<(true|false)\/>/, "<false/>"); flip=""; print; next }
      { print }
    ' "${RENDERED_PLIST}" > "${ENABLED_PLIST}" || cp "${RENDERED_PLIST}" "${ENABLED_PLIST}"
    say "Will install + load launchd agent:"
    say "  ${ENABLED_PLIST} -> ${LAUNCHD_TARGET}"
    if [ "${DRY_RUN}" -eq 1 ]; then
      say "(dry-run) not copying or loading."
      exit 0
    fi
    mkdir -p "$(dirname "${LAUNCHD_TARGET}")"
    cp "${ENABLED_PLIST}" "${LAUNCHD_TARGET}"
    launchctl unload "${LAUNCHD_TARGET}" 2>/dev/null || true
    launchctl load "${LAUNCHD_TARGET}"
    say "launchd agent loaded: ${LAUNCHD_LABEL}"
  else
    ACTIVE_LINE="$(grep -E '^#0 ' "${RENDERED_CRON}" | head -n1 | sed -E 's/^#//')"
    if [ -z "${ACTIVE_LINE}" ]; then
      echo "could not find the schedule line in ${RENDERED_CRON}" >&2
      exit 1
    fi
    say "Will add this crontab line for the current user:"
    say "  ${ACTIVE_LINE}"
    if [ "${DRY_RUN}" -eq 1 ]; then
      say "(dry-run) not modifying crontab."
      exit 0
    fi
    ( crontab -l 2>/dev/null | grep -vF "harness.py --once" || true; printf '%s\n' "${ACTIVE_LINE}" ) | crontab -
    say "crontab line installed."
  fi
  say ""
  say "Enabled. Loops remain inert until autonomy is ON in autonomy.yml."
  exit 0
fi

# --------------------------------------------------------------------------- #
# disable
# --------------------------------------------------------------------------- #
if [ "${MODE}" = "disable" ]; then
  if [ "${OS}" = "Darwin" ]; then
    say "Will unload + remove launchd agent: ${LAUNCHD_TARGET}"
    if [ "${DRY_RUN}" -eq 1 ]; then say "(dry-run) not changing anything."; exit 0; fi
    launchctl unload "${LAUNCHD_TARGET}" 2>/dev/null || true
    rm -f "${LAUNCHD_TARGET}"
    say "launchd agent removed."
  else
    say "Will remove the harness crontab line for the current user."
    if [ "${DRY_RUN}" -eq 1 ]; then say "(dry-run) not changing crontab."; exit 0; fi
    ( crontab -l 2>/dev/null | grep -vF "harness.py --once" || true ) | crontab -
    say "crontab line removed."
  fi
  exit 0
fi

echo "install_schedule.sh: nothing to do" >&2
exit 2
