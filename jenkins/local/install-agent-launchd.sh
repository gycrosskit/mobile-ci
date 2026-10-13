#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 保留旧入口和 Secret 变量；mac-build 统一使用服务账号，不再安装个人 LaunchAgent。
JENKINS_PR_AGENT_SECRET_FILE="${JENKINS_PR_AGENT_SECRET_FILE:-${JENKINS_AGENT_SECRET_FILE:-}}" \
  exec bash "${SCRIPT_DIR}/install-pr-agent-launchdaemon.sh" install "$@"
