#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 reserved-domain.ngrok-free.app allowed-google-email@example.com"
  exit 2
fi

DOMAIN="$1"
ALLOWED_EMAIL="$2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${SCRIPT_DIR}/runtime"
ENV_FILE="${SCRIPT_DIR}/.env"

[[ "${DOMAIN}" =~ ^[a-z0-9-]+\.ngrok-free\.app$ ]] || {
  echo "Only a reserved ngrok-free.app domain is accepted." >&2
  exit 2
}
[[ "${ALLOWED_EMAIL}" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || {
  echo "Invalid Google account email." >&2
  exit 2
}

if [[ -z "${NGROK_AUTHTOKEN:-}" ]]; then
  read -r -s -p "ngrok Authtoken: " NGROK_AUTHTOKEN
  echo
fi
[[ "${NGROK_AUTHTOKEN}" =~ ^[A-Za-z0-9_-]+$ ]] || {
  echo "Invalid ngrok Authtoken format." >&2
  exit 2
}

umask 077
mkdir -p "${RUNTIME_DIR}"

sed "s/__ALLOWED_EMAIL__/${ALLOWED_EMAIL}/g" \
  "${SCRIPT_DIR}/ngrok-policy.yml.template" \
  > "${RUNTIME_DIR}/ngrok-policy.yml"

printf '%s\n' \
  'version: "3"' \
  'agent:' \
  "  authtoken: ${NGROK_AUTHTOKEN}" \
  > "${RUNTIME_DIR}/ngrok.yml"

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${SCRIPT_DIR}/.env.example" "${ENV_FILE}"
fi

update_env_value() {
  local key="$1"
  local value="$2"
  awk -F= -v key="${key}" -v value="${value}" '
  BEGIN {updated = 0}
  $1 == key {print key "=" value; updated = 1; next}
  {print}
  END {if (!updated) print key "=" value}
' "${ENV_FILE}" > "${ENV_FILE}.tmp"
  mv "${ENV_FILE}.tmp" "${ENV_FILE}"
}

update_env_value NGROK_DOMAIN "${DOMAIN}"
update_env_value NGROK_UID "$(id -u)"
update_env_value NGROK_GID "$(id -g)"
chmod 600 "${ENV_FILE}" "${RUNTIME_DIR}/ngrok.yml" "${RUNTIME_DIR}/ngrok-policy.yml"

echo "ngrok runtime configured: Google OAuth allowlist for ${ALLOWED_EMAIL}, with secret-protected /project/sxmqlive-pr-validation exempted."
