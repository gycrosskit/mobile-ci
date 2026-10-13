#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INSTALL_ROOT="/usr/local/libexec/sxmqlive-jenkins-monitor"
CREDENTIAL_ROOT="/etc/sxmqlive-jenkins-monitor"
STATE_ROOT="/var/lib/sxmqlive-jenkins-monitor"
SERVICE_TARGET="/etc/systemd/system/sxmqlive-jenkins-health-monitor.service"
TIMER_TARGET="/etc/systemd/system/sxmqlive-jenkins-health-monitor.timer"
JENKINS_URL_VALUE="${JENKINS_MONITOR_URL:-http://127.0.0.1:18080}"

[[ "$(uname -s)" == "Linux" ]] || {
  echo "This installer only supports Linux." >&2
  exit 1
}
[[ "${EUID}" -eq 0 ]] || {
  echo "Run this installer with sudo so it can install systemd units and credentials." >&2
  exit 1
}
command -v systemctl >/dev/null 2>&1 || {
  echo "systemd is required." >&2
  exit 1
}
[[ "${JENKINS_URL_VALUE}" =~ ^https?://[^[:space:]]+$ ]] || {
  echo "JENKINS_MONITOR_URL must be an HTTP(S) URL without whitespace." >&2
  exit 2
}

required_file() {
  local name="$1" path="$2"
  [[ -s "${path}" ]] || {
    echo "Missing ${name}: ${path}" >&2
    exit 2
  }
}

required_file DINGTALK_WEBHOOK_FILE "${DINGTALK_WEBHOOK_FILE:-}"
required_file DINGTALK_SECRET_FILE "${DINGTALK_SECRET_FILE:-}"
required_file JENKINS_MONITOR_USER_FILE "${JENKINS_MONITOR_USER_FILE:-}"
required_file JENKINS_MONITOR_TOKEN_FILE "${JENKINS_MONITOR_TOKEN_FILE:-}"

install -d -o root -g root -m 755 "${INSTALL_ROOT}"
install -d -o root -g root -m 700 "${CREDENTIAL_ROOT}"
install -d -o root -g root -m 700 "${STATE_ROOT}"
install -o root -g root -m 755 \
  "${SCRIPT_DIR}/run-health-monitor-systemd.sh" \
  "${INSTALL_ROOT}/run-health-monitor-systemd.sh"
install -d -o root -g root -m 755 "${INSTALL_ROOT}/jenkins" "${INSTALL_ROOT}/delivery"
install -o root -g root -m 644 "${REPO_ROOT}/jenkins/__init__.py" "${INSTALL_ROOT}/jenkins/__init__.py"
install -o root -g root -m 644 "${REPO_ROOT}/jenkins/jenkins_health_monitor.py" "${INSTALL_ROOT}/jenkins/jenkins_health_monitor.py"
install -o root -g root -m 644 "${REPO_ROOT}/delivery/__init__.py" "${INSTALL_ROOT}/delivery/__init__.py"
install -o root -g root -m 644 "${REPO_ROOT}/delivery/delivery.py" "${INSTALL_ROOT}/delivery/delivery.py"
install -o root -g root -m 644 "${REPO_ROOT}/delivery/dingtalk_notify.py" "${INSTALL_ROOT}/delivery/dingtalk_notify.py"
install -o root -g root -m 600 "${DINGTALK_WEBHOOK_FILE}" "${CREDENTIAL_ROOT}/dingtalk-webhook"
install -o root -g root -m 600 "${DINGTALK_SECRET_FILE}" "${CREDENTIAL_ROOT}/dingtalk-secret"
install -o root -g root -m 600 "${JENKINS_MONITOR_USER_FILE}" "${CREDENTIAL_ROOT}/jenkins-user"
install -o root -g root -m 600 "${JENKINS_MONITOR_TOKEN_FILE}" "${CREDENTIAL_ROOT}/jenkins-token"

sed "s#__JENKINS_URL__#${JENKINS_URL_VALUE}#g" \
  "${SCRIPT_DIR}/systemd/sxmqlive-jenkins-health-monitor.service.template" \
  > "${SERVICE_TARGET}"
install -o root -g root -m 644 \
  "${SCRIPT_DIR}/systemd/sxmqlive-jenkins-health-monitor.timer" \
  "${TIMER_TARGET}"
systemd-analyze verify "${SERVICE_TARGET}" "${TIMER_TARGET}"
systemctl daemon-reload
systemctl enable --now sxmqlive-jenkins-health-monitor.timer
systemctl start sxmqlive-jenkins-health-monitor.service
systemctl is-active --quiet sxmqlive-jenkins-health-monitor.timer
service_result="$(systemctl show sxmqlive-jenkins-health-monitor.service --property=Result | sed -n 's/^Result=//p')"
[[ "${service_result}" == "success" ]]

echo "Jenkins systemd 健康监控已安装并完成首次检查。"
