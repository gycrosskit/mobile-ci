#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/compose-context.sh"
check_compose_services

usage() {
  echo "Usage: $0 {version|status|login|verify|import /absolute/path/dws-auth.tar.gz}"
}

prepare_image() {
  bash "${SCRIPT_DIR}/check-host.sh"
  "${COMPOSE[@]}" build "${ANDROID_SERVICE}"
}

run_dws() {
  "${COMPOSE[@]}" run --rm --no-deps --entrypoint /usr/local/bin/dws "${ANDROID_SERVICE}" "$@"
}

verify_auth() {
  run_dws auth status --format json \
    | python3 -c 'import json, sys; s = json.load(sys.stdin); sys.exit(0 if all(s.get(k) is True for k in ("authenticated", "token_valid", "refresh_token_valid")) else 1)'
  echo "DWS login, access token, and refresh token are valid."
}

case "${1:-}" in
  version)
    prepare_image
    run_dws version
    ;;
  status)
    prepare_image
    run_dws auth status
    ;;
  login)
    prepare_image
    run_dws auth login --device
    run_dws auth status
    ;;
  verify)
    prepare_image
    run_dws version
    verify_auth
    ;;
  import)
    AUTH_BUNDLE="${2:-}"
    if [[ "${AUTH_BUNDLE}" != /* || ! -f "${AUTH_BUNDLE}" ]]; then
      echo "DWS auth bundle must be an existing absolute path: ${AUTH_BUNDLE}" >&2
      exit 2
    fi
    prepare_image
    "${COMPOSE[@]}" run --rm --no-deps \
      --volume "${AUTH_BUNDLE}:/run/dws-auth/import.tar.gz:ro" \
      --entrypoint /usr/local/bin/dws \
      "${ANDROID_SERVICE}" auth import \
      --input /run/dws-auth/import.tar.gz
    run_dws auth status
    ;;
  *)
    usage
    exit 2
    ;;
esac
