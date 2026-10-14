#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/compose-context.sh"

check_selected_host() {
  if [[ -n "${JENKINS_DEPLOYMENT_FILE:-}" ]]; then
    python3 "${SCRIPT_DIR}/deployment-runtime.py" check "${JENKINS_DEPLOYMENT_FILE}" "${JENKINS_COMPOSE_FILE}"
  else
    bash "${SCRIPT_DIR}/check-host.sh"
  fi
}

usage() {
  echo "Usage: $0 {check|build|start|stop|restart|status|logs|verify}"
}

case "${1:-}" in
  check)
    check_selected_host
    check_compose_services
    ;;
  build)
    [[ -z "${JENKINS_DEPLOYMENT_FILE:-}" ]] || { echo "Build reviewed images separately with toolchains.py build-args; deployment consumes exact installed image IDs." >&2; exit 2; }
    check_selected_host
    check_compose_services
    "${COMPOSE[@]}" build "${CONTROLLER_SERVICE}" "${ANDROID_SERVICE}" "${PR_SERVICE}"
    ;;
  start)
    check_selected_host
    check_compose_services
    "${COMPOSE[@]}" up --detach --no-build "${SERVICES[@]}"
    ;;
  stop)
    check_compose_services
    "${COMPOSE[@]}" stop
    ;;
  restart)
    check_compose_services
    "${COMPOSE[@]}" restart "${SERVICES[@]}"
    ;;
  status)
    check_compose_services
    "${COMPOSE[@]}" ps
    ;;
  logs)
    check_compose_services
    "${COMPOSE[@]}" logs --follow --tail=200
    ;;
  verify)
    bash "${SCRIPT_DIR}/verify.sh"
    ;;
  *)
    usage
    exit 2
    ;;
esac
