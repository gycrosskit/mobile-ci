#!/usr/bin/env bash
set -euo pipefail

CREDENTIALS_ROOT="${CREDENTIALS_DIRECTORY:?systemd credentials are unavailable}"
STATE_ROOT="${STATE_DIRECTORY:?systemd state directory is unavailable}"
INSTALL_ROOT="/usr/local/libexec/sxmqlive-jenkins-monitor"

read_secret() {
  local path="$1"
  [[ -s "${path}" ]] || {
    echo "Missing health-monitor credential: ${path}" >&2
    exit 2
  }
  tr -d '\r\n' < "${path}"
}

MONITOR_ENV=(
  "PATH=/usr/bin:/bin:/usr/sbin:/sbin"
  "PYTHONPATH=${INSTALL_ROOT}"
  "PYTHONIOENCODING=UTF-8"
  "PYTHONUTF8=1"
  "DINGTALK_WEBHOOK=$(read_secret "${CREDENTIALS_ROOT}/dingtalk-webhook")"
  "DINGTALK_SECRET=$(read_secret "${CREDENTIALS_ROOT}/dingtalk-secret")"
  "JENKINS_MONITOR_USER=$(read_secret "${CREDENTIALS_ROOT}/jenkins-user")"
  "JENKINS_MONITOR_TOKEN=$(read_secret "${CREDENTIALS_ROOT}/jenkins-token")"
)

exec /usr/bin/env -i "${MONITOR_ENV[@]}" /usr/bin/python3 \
  "${INSTALL_ROOT}/jenkins/jenkins_health_monitor.py" \
  --url="${JENKINS_MONITOR_URL:-http://127.0.0.1:18080}" \
  --state-file="${STATE_ROOT}/state.json" \
  --repeat-minutes=60
