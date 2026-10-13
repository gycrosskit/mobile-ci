#!/usr/bin/env bash
set -euo pipefail

MIN_CPU_COUNT="${MIN_CPU_COUNT:-4}"
MIN_MEMORY_GIB="${MIN_MEMORY_GIB:-12}"
MIN_DISK_GIB="${MIN_DISK_GIB:-60}"

fail() {
  echo "Host check failed: $*" >&2
  exit 1
}

[[ "$(uname -s)" == "Linux" ]] || fail "the full migration target must be Linux"
case "$(uname -m)" in
  x86_64|amd64) ;;
  *) fail "Android command-line tools require an x86_64 host; found $(uname -m)" ;;
esac

command -v docker >/dev/null 2>&1 || fail "Docker Engine is not installed"
docker info >/dev/null 2>&1 || fail "the current user cannot access Docker Engine"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is not installed"

CPU_COUNT="$(getconf _NPROCESSORS_ONLN)"
MEMORY_KIB="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
MEMORY_GIB="$(((MEMORY_KIB + 1024 * 1024 - 1) / 1024 / 1024))"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISK_KIB="$(df -Pk "${SCRIPT_DIR}" | awk 'NR == 2 {print $4}')"
DISK_GIB="$((DISK_KIB / 1024 / 1024))"

if [[ "${ALLOW_UNDERSIZED_HOST:-false}" != "true" ]]; then
  ((CPU_COUNT >= MIN_CPU_COUNT)) || fail "need at least ${MIN_CPU_COUNT} CPU cores; found ${CPU_COUNT}"
  ((MEMORY_GIB >= MIN_MEMORY_GIB)) || fail "need at least ${MIN_MEMORY_GIB} GiB memory; found ${MEMORY_GIB} GiB"
  ((DISK_GIB >= MIN_DISK_GIB)) || fail "need at least ${MIN_DISK_GIB} GiB free disk; found ${DISK_GIB} GiB"
fi

echo "Host ready: ${CPU_COUNT} CPU, ${MEMORY_GIB} GiB memory, ${DISK_GIB} GiB free disk."
