#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${SCRIPT_DIR}/runtime"
JENKINS_URL="${JENKINS_URL:-http://127.0.0.1:18080}"
AGENT_NAME="${JENKINS_AGENT_NAME:-mac-build}"
AGENT_WORK_DIR="${JENKINS_AGENT_WORK_DIR:-${HOME}/JenkinsAgent}"
SECRET_FILE="${JENKINS_AGENT_SECRET_FILE:-${RUNTIME_DIR}/agent.secret}"
AGENT_JAR="${RUNTIME_DIR}/agent.jar"
AGENT_ARGUMENTS="${RUNTIME_DIR}/agent.args"

write_java_argument() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"\n' "${value}"
}

if [[ ! -s "${SECRET_FILE}" ]]; then
  echo "Missing agent secret file: ${SECRET_FILE}" >&2
  exit 1
fi

mkdir -p "${RUNTIME_DIR}" "${AGENT_WORK_DIR}"
chmod 700 "${RUNTIME_DIR}" "${AGENT_WORK_DIR}"
curl --fail --silent --show-error --location \
  "${JENKINS_URL}/jnlpJars/agent.jar" \
  --output "${AGENT_JAR}"

umask 077
{
  printf '%s\n' '-jar'
  write_java_argument "${AGENT_JAR}"
  printf '%s\n' '-url'
  write_java_argument "${JENKINS_URL}"
  printf '%s\n' '-secret'
  write_java_argument "$(<"${SECRET_FILE}")"
  printf '%s\n' '-name'
  write_java_argument "${AGENT_NAME}"
  printf '%s\n' '-webSocket' '-workDir'
  write_java_argument "${AGENT_WORK_DIR}"
} > "${AGENT_ARGUMENTS}"

# Java argument file keeps the connection Secret out of the process command line.
exec java "@${AGENT_ARGUMENTS}"
