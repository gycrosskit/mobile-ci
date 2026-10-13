#!/usr/bin/env bash
set -euo pipefail

export PYTHONIOENCODING=UTF-8
export PYTHONUTF8=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MODE="${1:-}"

usage() {
  echo "Usage: $0 {mac-agents|remote-linux}" >&2
}

verify_job_configuration() {
  python3 "${REPO_ROOT}/jenkins/generate_jenkins_job_templates.py" --check
  python3 "${REPO_ROOT}/jenkins/sync_jenkins_jobs.py" check
  python3 "${REPO_ROOT}/jenkins/sync_jenkins_admin_jobs.py" check
}

verify_health_state() {
  local state_file="$1"
  python3 - "${state_file}" <<'PY'
import json
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit("Jenkins health-monitor state is missing: {}".format(path))
state = json.loads(path.read_text(encoding="utf-8"))
if state.get("status") != "healthy":
    raise SystemExit("Jenkins health monitor is not healthy: {}".format(state.get("summary")))
checked_at = int(state.get("checkedAt") or 0)
age = int(time.time()) - checked_at
if checked_at <= 0 or age < 0 or age > 600:
    raise SystemExit("Jenkins health-monitor state is stale: {} seconds".format(age))
print("Jenkins external health monitor passed: {}".format(state.get("summary") or "healthy"))
PY
}

case "${MODE}" in
  mac-agents)
    [[ "$(uname -s)" == "Darwin" ]] || {
      echo "mac-agents verification must run on macOS." >&2
      exit 1
    }
    bash "${SCRIPT_DIR}/local/install-pr-agent-launchdaemon.sh" check
    launchctl print "system/com.sxmqlive.jenkins-pr-agent" >/dev/null
    if launchctl print "gui/$(id -u)/com.sxmqlive.jenkins-agent" >/dev/null 2>&1; then
      echo "Legacy personal Mac Agent is still registered; disable it after backing up its state." >&2
      exit 1
    fi
    launchctl print "gui/$(id -u)/com.sxmqlive.jenkins-controller-tunnel" >/dev/null
    curl --fail --silent --show-error --max-time 5 \
      "${JENKINS_URL:-http://127.0.0.1:18080}/login" >/dev/null
    ;;
  remote-linux)
    [[ "$(uname -s)" == "Linux" ]] || {
      echo "remote-linux verification must run on Linux." >&2
      exit 1
    }
    bash "${SCRIPT_DIR}/remote/stack.sh" verify
    systemctl is-enabled --quiet sxmqlive-jenkins-health-monitor.timer
    systemctl is-active --quiet sxmqlive-jenkins-health-monitor.timer
    service_result="$(systemctl show sxmqlive-jenkins-health-monitor.service --property=Result | sed -n 's/^Result=//p')"
    [[ "${service_result}" == "success" ]]
    verify_health_state "/var/lib/sxmqlive-jenkins-monitor/state.json"
    LC_ALL=en_US.utf8 verify_job_configuration
    ;;
  *)
    usage
    exit 2
    ;;
esac

echo "Jenkins ${MODE} verification passed."
