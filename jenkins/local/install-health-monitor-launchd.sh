#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INSTALL_ROOT="${JENKINS_MONITOR_INSTALL_ROOT:-${HOME}/Library/Application Support/sxmqlive-jenkins-monitor}"
RUNTIME_DIR="${INSTALL_ROOT}/runtime"
LOG_DIR="${INSTALL_ROOT}/logs"
PLIST_TARGET="${HOME}/Library/LaunchAgents/com.sxmqlive.jenkins-health-monitor.plist"
JENKINS_URL_VALUE="${JENKINS_MONITOR_URL:-http://127.0.0.1:18080}"

required_file() {
  local name="$1" path="$2"
  [[ -s "${path}" ]] || {
    echo "Missing ${name}: ${path}" >&2
    exit 2
  }
}

required_file DINGTALK_WEBHOOK_FILE "${DINGTALK_WEBHOOK_FILE:-}"
required_file DINGTALK_SECRET_FILE "${DINGTALK_SECRET_FILE:-}"
install -d -m 700 "${INSTALL_ROOT}" "${RUNTIME_DIR}" "${LOG_DIR}" "$(dirname "${PLIST_TARGET}")"
install -m 755 "${SCRIPT_DIR}/run-health-monitor.sh" "${INSTALL_ROOT}/run-health-monitor.sh.template"
install -d -m 700 "${INSTALL_ROOT}/jenkins" "${INSTALL_ROOT}/delivery"
install -m 600 "${REPO_ROOT}/jenkins/__init__.py" "${INSTALL_ROOT}/jenkins/__init__.py"
install -m 600 "${REPO_ROOT}/jenkins/jenkins_health_monitor.py" "${INSTALL_ROOT}/jenkins/jenkins_health_monitor.py"
install -m 600 "${REPO_ROOT}/delivery/__init__.py" "${INSTALL_ROOT}/delivery/__init__.py"
install -m 600 "${REPO_ROOT}/delivery/delivery.py" "${INSTALL_ROOT}/delivery/delivery.py"
install -m 600 "${REPO_ROOT}/delivery/dingtalk_notify.py" "${INSTALL_ROOT}/delivery/dingtalk_notify.py"
install -m 600 "${DINGTALK_WEBHOOK_FILE}" "${RUNTIME_DIR}/dingtalk-webhook"
install -m 600 "${DINGTALK_SECRET_FILE}" "${RUNTIME_DIR}/dingtalk-secret"
if [[ -n "${JENKINS_MONITOR_USER_FILE:-}" || -n "${JENKINS_MONITOR_TOKEN_FILE:-}" ]]; then
  required_file JENKINS_MONITOR_USER_FILE "${JENKINS_MONITOR_USER_FILE:-}"
  required_file JENKINS_MONITOR_TOKEN_FILE "${JENKINS_MONITOR_TOKEN_FILE:-}"
  install -m 600 "${JENKINS_MONITOR_USER_FILE}" "${RUNTIME_DIR}/jenkins-user"
  install -m 600 "${JENKINS_MONITOR_TOKEN_FILE}" "${RUNTIME_DIR}/jenkins-token"
fi
sed -e "s#__INSTALL_ROOT__#${INSTALL_ROOT}#g" \
    -e "s#__JENKINS_URL__#${JENKINS_URL_VALUE}#g" \
    "${INSTALL_ROOT}/run-health-monitor.sh.template" > "${INSTALL_ROOT}/run-health-monitor.sh"
chmod 700 "${INSTALL_ROOT}/run-health-monitor.sh"
sed -e "s#__INSTALL_ROOT__#${INSTALL_ROOT}#g" \
    -e "s#__LOG_DIR__#${LOG_DIR}#g" \
    "${SCRIPT_DIR}/com.sxmqlive.jenkins-health-monitor.plist.template" > "${PLIST_TARGET}"
plutil -lint "${PLIST_TARGET}" >/dev/null
launchctl bootout "gui/$(id -u)" "${PLIST_TARGET}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "${PLIST_TARGET}"
echo "Jenkins 外部健康监控已安装：${PLIST_TARGET}"
