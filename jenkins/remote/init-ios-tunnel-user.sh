#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: sudo $0 /absolute/path/id_jenkins_tunnel.pub" >&2
  exit 2
fi

PUBLIC_KEY_PATH="$1"
TUNNEL_USER="${JENKINS_TUNNEL_USER:-jenkins-tunnel}"
CONTROLLER_PORT="${JENKINS_TUNNEL_CONTROLLER_PORT:-18080}"

[[ "$(uname -s)" == "Linux" ]] || {
  echo "The iOS tunnel account must be initialized on Linux." >&2
  exit 1
}
[[ "$(id -u)" == "0" ]] || {
  echo "Run this script with sudo/root." >&2
  exit 1
}
[[ "${PUBLIC_KEY_PATH}" == /* && -f "${PUBLIC_KEY_PATH}" ]] || {
  echo "Public key must be an existing absolute file: ${PUBLIC_KEY_PATH}" >&2
  exit 2
}
[[ "${TUNNEL_USER}" =~ ^[a-z_][a-z0-9_-]*$ ]] || {
  echo "JENKINS_TUNNEL_USER is invalid." >&2
  exit 2
}
[[ "${CONTROLLER_PORT}" =~ ^[0-9]+$ ]] \
  && ((10#${CONTROLLER_PORT} >= 1 && 10#${CONTROLLER_PORT} <= 65535)) || {
    echo "JENKINS_TUNNEL_CONTROLLER_PORT must be between 1 and 65535." >&2
    exit 2
  }
for command_name in getent install readlink ssh-keygen useradd; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "Missing server command: ${command_name}" >&2
    exit 1
  }
done

[[ "$(awk 'NF { count += 1 } END { print count + 0 }' "${PUBLIC_KEY_PATH}")" == "1" ]] || {
  echo "The tunnel public key file must contain exactly one key." >&2
  exit 2
}
ssh-keygen -l -f "${PUBLIC_KEY_PATH}" >/dev/null || {
  echo "Invalid SSH public key." >&2
  exit 2
}
PUBLIC_KEY_LINE="$(awk 'NF { print; exit }' "${PUBLIC_KEY_PATH}")"
read -r KEY_TYPE KEY_VALUE _ <<< "${PUBLIC_KEY_LINE}"
[[ "${KEY_TYPE}" == "ssh-ed25519" && -n "${KEY_VALUE}" ]] || {
  echo "The tunnel key must be an Ed25519 public key." >&2
  exit 2
}

NOLOGIN_SHELL="$(command -v nologin 2>/dev/null || true)"
[[ -n "${NOLOGIN_SHELL}" ]] || {
  echo "The Linux host does not provide a nologin shell." >&2
  exit 1
}
if ! id "${TUNNEL_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell "${NOLOGIN_SHELL}" "${TUNNEL_USER}"
fi

IFS=: read -r _ _ _ _ _ USER_HOME USER_SHELL < <(getent passwd "${TUNNEL_USER}")
[[ -n "${USER_HOME}" \
  && "$(readlink -f "${USER_SHELL}")" == "$(readlink -f "${NOLOGIN_SHELL}")" ]] || {
  echo "Existing ${TUNNEL_USER} account is not a dedicated nologin account." >&2
  exit 1
}
USER_GROUP="$(id -gn "${TUNNEL_USER}")"
SSH_DIR="${USER_HOME}/.ssh"
AUTHORIZED_KEYS="${SSH_DIR}/authorized_keys"
AUTHORIZED_LINE="restrict,port-forwarding,permitopen=\"127.0.0.1:${CONTROLLER_PORT}\" ${KEY_TYPE} ${KEY_VALUE}"

if [[ -s "${AUTHORIZED_KEYS}" ]]; then
  if [[ "$(<"${AUTHORIZED_KEYS}")" == "${AUTHORIZED_LINE}" ]]; then
    echo "Restricted iOS tunnel account is already configured: ${TUNNEL_USER}"
    exit 0
  fi
  echo "Refusing to overwrite existing authorized_keys for ${TUNNEL_USER}." >&2
  exit 1
fi

install -d -m 700 -o "${TUNNEL_USER}" -g "${USER_GROUP}" "${SSH_DIR}"
TEMP_KEY_FILE="$(mktemp)"
cleanup() {
  rm -f "${TEMP_KEY_FILE}"
}
trap cleanup EXIT
printf '%s\n' "${AUTHORIZED_LINE}" > "${TEMP_KEY_FILE}"
install -m 600 -o "${TUNNEL_USER}" -g "${USER_GROUP}" \
  "${TEMP_KEY_FILE}" "${AUTHORIZED_KEYS}"

echo "Restricted iOS tunnel account configured: ${TUNNEL_USER}"
echo "Allowed forwarding destination: 127.0.0.1:${CONTROLLER_PORT}"
