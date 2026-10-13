#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/compose-context.sh"

usage() {
  echo "Usage: $0 {check|build|start|stop|restart|status|logs|verify}"
}

case "${1:-}" in
  check)
    bash "${SCRIPT_DIR}/check-host.sh"
    check_compose_services
    ;;
  build)
    bash "${SCRIPT_DIR}/check-host.sh"
    check_compose_services
    "${COMPOSE[@]}" build "${CONTROLLER_SERVICE}" "${ANDROID_SERVICE}" "${PR_SERVICE}"
    ;;
  start)
    bash "${SCRIPT_DIR}/check-host.sh"
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
