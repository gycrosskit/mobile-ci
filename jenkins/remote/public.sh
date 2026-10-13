#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
source "${SCRIPT_DIR}/compose-context.sh"
if [[ -z "${JENKINS_COMPOSE_FILE:-}${COMPOSE_FILE:-}" ]]; then
  COMPOSE+=(-f "${SCRIPT_DIR}/compose.ngrok.yaml")
fi
COMPOSE+=(--profile public)
check_compose_services

usage() {
  echo "Usage: $0 {start|stop|status|logs}"
}

require_runtime() {
  if [[ -n "${JENKINS_COMPOSE_FILE:-}${COMPOSE_FILE:-}" ]]; then
    "${COMPOSE[@]}" config --services 2>/dev/null | grep -qx ngrok || {
      echo "The selected complete private Compose manifest must include ngrok." >&2; return 1;
    }
    return
  fi
  [[ -f "${ENV_FILE}" ]] || {
    echo "Missing ${ENV_FILE}; run init-ngrok.sh first." >&2
    exit 1
  }
  [[ -s "${SCRIPT_DIR}/runtime/ngrok.yml" ]] || {
    echo "Missing protected ngrok Authtoken config; run init-ngrok.sh first." >&2
    exit 1
  }
  [[ -s "${SCRIPT_DIR}/runtime/ngrok-policy.yml" ]] || {
    echo "Missing protected ngrok traffic policy; run init-ngrok.sh first." >&2
    exit 1
  }
  chmod 600 "${ENV_FILE}" "${SCRIPT_DIR}/runtime/ngrok.yml" \
    "${SCRIPT_DIR}/runtime/ngrok-policy.yml"
}

case "${1:-}" in
  start)
    require_runtime
    bash "${SCRIPT_DIR}/check-host.sh"
    "${COMPOSE[@]}" config --quiet
    "${COMPOSE[@]}" up --detach --no-build "${SERVICES[@]}" ngrok
    ;;
  stop)
    "${COMPOSE[@]}" stop ngrok
    ;;
  status)
    "${COMPOSE[@]}" ps
    ;;
  logs)
    "${COMPOSE[@]}" logs --follow --tail=200 ngrok
    ;;
  *)
    usage
    exit 2
    ;;
esac
