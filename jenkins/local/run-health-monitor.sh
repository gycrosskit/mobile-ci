#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="__INSTALL_ROOT__"
RUNTIME_DIR="${INSTALL_ROOT}/runtime"

read_secret() {
  local path="$1"
  [[ -s "${path}" ]] || return 1
  tr -d '\r\n' < "${path}"
}

MONITOR_ENV=(
  "PATH=/usr/bin:/bin:/usr/sbin:/sbin"
  "PYTHONPATH=${INSTALL_ROOT}"
  "DINGTALK_WEBHOOK=$(read_secret "${RUNTIME_DIR}/dingtalk-webhook")"
  "DINGTALK_SECRET=$(read_secret "${RUNTIME_DIR}/dingtalk-secret")"
)
if [[ -s "${RUNTIME_DIR}/jenkins-user" && -s "${RUNTIME_DIR}/jenkins-token" ]]; then
  MONITOR_ENV+=(
    "JENKINS_MONITOR_USER=$(read_secret "${RUNTIME_DIR}/jenkins-user")"
    "JENKINS_MONITOR_TOKEN=$(read_secret "${RUNTIME_DIR}/jenkins-token")"
  )
fi

exec /usr/bin/env -i "${MONITOR_ENV[@]}" /usr/bin/python3 \
  "${INSTALL_ROOT}/jenkins/jenkins_health_monitor.py" \
  --url="__JENKINS_URL__" \
  --state-file="${RUNTIME_DIR}/state.json" \
  --repeat-minutes=60
