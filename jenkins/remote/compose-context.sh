#!/usr/bin/env bash
# 三个运维入口共用解析；显式私有清单与可迁移模板不能混用。
if [[ -n "${JENKINS_COMPOSE_FILE:-}" ]]; then
  COMPOSE_FILES=("${JENKINS_COMPOSE_FILE}")
elif [[ -n "${COMPOSE_FILE:-}" ]]; then
  IFS="${COMPOSE_PATH_SEPARATOR:-:}" read -r -a COMPOSE_FILES <<< "${COMPOSE_FILE}"
else
  COMPOSE_FILES=("${SCRIPT_DIR}/compose.yaml" "${SCRIPT_DIR}/compose.ohos.yaml")
fi

if [[ -n "${JENKINS_COMPOSE_FILE:-}${COMPOSE_FILE:-}" ]]; then
  python3 - "${COMPOSE_FILES[@]}" <<'PY'
import os
import stat
import sys
from pathlib import Path

for value in sys.argv[1:]:
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.resolve() != path:
        raise SystemExit("Compose manifest must be an existing absolute path without symlinks.")
    info, parent = path.stat(), path.parent.stat()
    if info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit("Private Compose manifest must be owned by the current user with mode 0600.")
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise SystemExit("Private Compose manifest directory must be owned by the current user with mode 0700.")
PY
fi

COMPOSE=(docker compose --project-directory "${SCRIPT_DIR}")
for compose_path in "${COMPOSE_FILES[@]}"; do
  COMPOSE+=(-f "${compose_path}")
done
CI_CONTEXT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
if [[ -n "${JENKINS_DEPLOYMENT_FILE:-}" ]]; then
  [[ -n "${JENKINS_COMPOSE_FILE:-}" && -z "${COMPOSE_FILE:-}" ]] || {
    echo "Deployment requires its explicit generated JENKINS_COMPOSE_FILE without overlays." >&2; exit 1;
  }
  role_names="$(python3 "${SCRIPT_DIR}/deployment-runtime.py" context "${JENKINS_DEPLOYMENT_FILE}" "${JENKINS_COMPOSE_FILE}")" || exit 1
  read -r -a SERVICES <<< "${role_names}"
  CONTROLLER_SERVICE="${SERVICES[0]}"
  ANDROID_SERVICE=""; PR_SERVICE=""; MAINTENANCE_SERVICE=""
else
role_names="$(python3 - "${CI_CONTEXT_ROOT}" "${JENKINS_CI_PROJECT:-sxmqlive}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from project_config import load_project
profile = load_project(sys.argv[2])
print(' '.join(profile['runtime']['roles'][role] for role in ('controller', 'android', 'pr', 'maintenance')))
PY
)" || { echo "Trusted project roles are unavailable." >&2; exit 1; }
read -r -a SERVICES <<< "${role_names}"
CONTROLLER_SERVICE="${SERVICES[0]}"
ANDROID_SERVICE="${SERVICES[1]}"
PR_SERVICE="${SERVICES[2]}"
MAINTENANCE_SERVICE="${SERVICES[3]}"

fi

read_backup_resources() {
  local resource_line
  [[ -z "${JENKINS_DEPLOYMENT_FILE:-}" ]] || { echo "Deployment backups require a separately reviewed resource plan." >&2; return 1; }
  check_compose_services
  resource_line="$("${COMPOSE[@]}" config --format json 2>/dev/null | python3 "${SCRIPT_DIR}/backup_resources.py")" || return 1
  read -r VOLUME_NAME OHOS_SIGNING_VOLUME_NAME AGENT_WORK_VOLUME_NAME \
    CONTAINER_NAME AGENT_CONTAINER_NAME OHOS_AGENT_CONTAINER_NAME LINUX_UID DELIVERY_RELATIVE \
    IMAGE_NAME BACKUP_PROJECT_ID <<< "${resource_line}"
}

check_compose_services() {
  local configured service
  configured="$("${COMPOSE[@]}" config --services 2>/dev/null)" || {
    echo "Compose configuration failed; check the selected manifest and required host paths privately." >&2
    return 1
  }
  for service in "${SERVICES[@]}"; do
    if ! printf '%s\n' "${configured}" | grep -qx -- "${service}"; then
      echo "Incomplete Compose manifest: missing ${service}." >&2
      return 1
    fi
  done
}
