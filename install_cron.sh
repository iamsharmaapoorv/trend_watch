#!/usr/bin/env bash
# Installs (or refreshes) only the trend_watch block in the user's crontab.
# Every other line is left exactly as it is. A backup of the old crontab is kept.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
CRON_BEGIN="# BEGIN trend_watch managed cron"
CRON_END="# END trend_watch managed cron"
RUN="cd ${SCRIPT_DIR} && set -a && . ${ENV_FILE} && set +a && flock -n ${SCRIPT_DIR}/.trend_watch.lock ${PYTHON_BIN} trend_watch.py"

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "python3 not found in PATH and PYTHON_BIN was not provided." >&2
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy .env.example to .env and fill in Telegram credentials first." >&2
  exit 1
fi

# Times are UTC. Every 5 minutes, one minute after each 5m candle closes, 08:31-15:26 IST
# (runs outside 09:20-15:30 IST exit immediately). Then one archive run after the close.
CRON_BLOCK=$(cat <<EOF
${CRON_BEGIN}
1-59/5 3-9 * * 1-5 ${RUN}
35 10 * * 1-5 ${RUN} --archive
${CRON_END}
EOF
)

EXISTING_CRONTAB="$(mktemp)"
FILTERED_CRONTAB="$(mktemp)"
trap 'rm -f "${EXISTING_CRONTAB}" "${FILTERED_CRONTAB}"' EXIT

if crontab -l > "${EXISTING_CRONTAB}" 2>/dev/null; then
  BACKUP="${SCRIPT_DIR}/crontab.backup.$(date -u +%Y%m%dT%H%M%SZ)"
  cp "${EXISTING_CRONTAB}" "${BACKUP}"
  echo "Previous crontab saved to ${BACKUP}"
  awk -v begin="${CRON_BEGIN}" -v end="${CRON_END}" '
    $0 == begin { skip=1; next }
    $0 == end { skip=0; next }
    !skip { print }
  ' "${EXISTING_CRONTAB}" > "${FILTERED_CRONTAB}"
else
  : > "${EXISTING_CRONTAB}"
  : > "${FILTERED_CRONTAB}"
fi

{
  cat "${FILTERED_CRONTAB}"
  if [[ -s "${FILTERED_CRONTAB}" ]]; then
    echo
  fi
  echo "${CRON_BLOCK}"
} | crontab -

echo "Changes to the crontab:"
diff "${EXISTING_CRONTAB}" <(crontab -l) || true
