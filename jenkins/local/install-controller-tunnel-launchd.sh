#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${JENKINS_TUNNEL_INSTALL_ROOT:-${HOME}/Library/Application Support/sxmqlive-jenkins-controller-tunnel}"
RUNTIME_DIR="${INSTALL_ROOT}/runtime"
LOG_DIR="${INSTALL_ROOT}/logs"
PLIST_SOURCE="${SCRIPT_DIR}/com.sxmqlive.jenkins-controller-tunnel.plist.template"
PLIST_TARGET="${HOME}/Library/LaunchAgents/com.sxmqlive.jenkins-controller-tunnel.plist"
SSH_CONFIG="${RUNTIME_DIR}/ssh_config"
SSH_KEY_TARGET="${RUNTIME_DIR}/id_controller_tunnel"
KNOWN_HOSTS_TARGET="${RUNTIME_DIR}/known_hosts"

SSH_HOST="${JENKINS_CONTROLLER_SSH_HOST:-}"
SSH_USER="${JENKINS_CONTROLLER_SSH_USER:-}"
SSH_PORT="${JENKINS_CONTROLLER_SSH_PORT:-22}"
SSH_KEY_SOURCE="${JENKINS_CONTROLLER_SSH_KEY_FILE:-}"
KNOWN_HOSTS_SOURCE="${JENKINS_CONTROLLER_KNOWN_HOSTS_FILE:-}"
LOCAL_PORT="${JENKINS_CONTROLLER_LOCAL_PORT:-18080}"
REMOTE_PORT="${JENKINS_CONTROLLER_REMOTE_PORT:-18080}"

require_absolute_file() {
  local label="$1" path="$2"
  [[ "${path}" == /* && -f "${path}" ]] || {
    echo "${label} must be an existing absolute file: ${path:-<empty>}" >&2
    exit 2
  }
}

validate_port() {
  local label="$1" value="$2" minimum="$3" numeric
  [[ "${value}" =~ ^[0-9]+$ ]] || {
    echo "${label} must be a numeric TCP port." >&2
    exit 2
  }
  numeric="$((10#${value}))"
  ((numeric >= minimum && numeric <= 65535)) || {
    echo "${label} must be between ${minimum} and 65535." >&2
    exit 2
  }
}

[[ "$(uname -s)" == "Darwin" ]] || {
  echo "The Jenkins Controller SSH tunnel installer only supports macOS." >&2
  exit 1
}
[[ "${SSH_HOST}" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || {
  echo "JENKINS_CONTROLLER_SSH_HOST is invalid." >&2
  exit 2
}
[[ "${SSH_USER}" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || {
  echo "JENKINS_CONTROLLER_SSH_USER is invalid." >&2
  exit 2
}
validate_port "JENKINS_CONTROLLER_SSH_PORT" "${SSH_PORT}" 1
validate_port "JENKINS_CONTROLLER_LOCAL_PORT" "${LOCAL_PORT}" 1024
validate_port "JENKINS_CONTROLLER_REMOTE_PORT" "${REMOTE_PORT}" 1
require_absolute_file "JENKINS_CONTROLLER_SSH_KEY_FILE" "${SSH_KEY_SOURCE}"
require_absolute_file "JENKINS_CONTROLLER_KNOWN_HOSTS_FILE" "${KNOWN_HOSTS_SOURCE}"
for command_name in ssh ssh-keygen launchctl plutil lsof curl python3; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "Missing tunnel tool: ${command_name}" >&2
    exit 1
  }
done

HOST_KEY_QUERY="${SSH_HOST}"
if [[ "${SSH_PORT}" != "22" ]]; then
  HOST_KEY_QUERY="[${SSH_HOST}]:${SSH_PORT}"
fi
ssh-keygen -F "${HOST_KEY_QUERY}" -f "${KNOWN_HOSTS_SOURCE}" >/dev/null || {
  echo "The verified known_hosts file does not contain ${HOST_KEY_QUERY}." >&2
  exit 1
}

launchctl bootout "gui/$(id -u)" "${PLIST_TARGET}" >/dev/null 2>&1 || true
for _ in {1..5}; do
  if ! lsof -nP -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if lsof -nP -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Local TCP port ${LOCAL_PORT} is already in use; refusing to install the tunnel." >&2
  exit 1
fi

install -d -m 700 "${INSTALL_ROOT}" "${RUNTIME_DIR}" "${LOG_DIR}" "$(dirname "${PLIST_TARGET}")"
install -m 600 "${SSH_KEY_SOURCE}" "${SSH_KEY_TARGET}"
install -m 600 "${KNOWN_HOSTS_SOURCE}" "${KNOWN_HOSTS_TARGET}"
umask 077
cat > "${SSH_CONFIG}" <<EOF
Host jenkins-controller
  HostName ${SSH_HOST}
  User ${SSH_USER}
  Port ${SSH_PORT}
  IdentityFile "${SSH_KEY_TARGET}"
  IdentitiesOnly yes
  BatchMode yes
  PasswordAuthentication no
  KbdInteractiveAuthentication no
  StrictHostKeyChecking yes
  UserKnownHostsFile "${KNOWN_HOSTS_TARGET}"
  UpdateHostKeys no
  ExitOnForwardFailure yes
  ConnectTimeout 10
  ServerAliveInterval 30
  ServerAliveCountMax 3
  TCPKeepAlive yes
  ForwardAgent no
  RequestTTY no
  LocalForward 127.0.0.1:${LOCAL_PORT} 127.0.0.1:${REMOTE_PORT}
EOF
chmod 600 "${SSH_CONFIG}"

python3 - "${PLIST_SOURCE}" "${PLIST_TARGET}" "${SSH_CONFIG}" "${LOG_DIR}" <<'PY'
import pathlib
import sys
from xml.sax.saxutils import escape

source, target, ssh_config, log_dir = map(pathlib.Path, sys.argv[1:])
content = source.read_text(encoding="utf-8")
content = content.replace("__SSH_CONFIG__", escape(str(ssh_config)))
content = content.replace("__LOG_DIR__", escape(str(log_dir)))
target.write_text(content, encoding="utf-8")
PY
chmod 644 "${PLIST_TARGET}"
plutil -lint "${PLIST_TARGET}" >/dev/null
launchctl bootstrap "gui/$(id -u)" "${PLIST_TARGET}"

JENKINS_LOCAL_URL="http://127.0.0.1:${LOCAL_PORT}"
for _ in {1..15}; do
  if curl --fail --silent --max-time 2 "${JENKINS_LOCAL_URL}/login" >/dev/null; then
    echo "Persistent Jenkins Controller tunnel is ready: ${JENKINS_LOCAL_URL}"
    exit 0
  fi
  sleep 2
done

echo "Tunnel did not reach Jenkins within 30 seconds. Check ${LOG_DIR}/stderr.log." >&2
exit 1
