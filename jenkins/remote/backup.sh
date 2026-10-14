#!/usr/bin/env bash
set -euo pipefail
umask 077
export PYTHONIOENCODING=utf-8

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/compose-context.sh"
read_backup_resources
BACKUP_DIR="${JENKINS_BACKUP_DIR:-${SCRIPT_DIR}/backups}"
DELIVERY_TOOL="${SCRIPT_DIR}/../../delivery/delivery.py"
STAMP="$(date '+%Y%m%d-%H%M%S')"
BUNDLE_NAME="${BACKUP_PROJECT_ID}-jenkins-migration-${STAMP}-$$.tar.gz"
TEMP_DIR="$(mktemp -d)"
PARTIAL_BUNDLE=""
LOCK_DIR=""
CONTROLLER_WAS_RUNNING="false"
AGENT_WAS_RUNNING="false"
OHOS_SIGNING_FROM_VOLUME="false"
OHOS_AGENT_WAS_RUNNING="false"
LINUX_STATE_FROM_VOLUME="false"
LINUX_STATE_REQUIRED="false"
MAC_STATE_UID=""

resume_services() {
  if [[ "${CONTROLLER_WAS_RUNNING}" == "true" && "${JENKINS_KEEP_STOPPED:-false}" != "true" ]]; then
    docker start "${CONTAINER_NAME}" >/dev/null
    CONTROLLER_WAS_RUNNING="false"
  fi
  if [[ "${AGENT_WAS_RUNNING}" == "true" && "${JENKINS_KEEP_STOPPED:-false}" != "true" ]]; then
    docker start "${AGENT_CONTAINER_NAME}" >/dev/null
    AGENT_WAS_RUNNING="false"
  fi
  if [[ "${OHOS_AGENT_WAS_RUNNING}" == "true" && "${JENKINS_KEEP_STOPPED:-false}" != "true" ]]; then
    docker start "${OHOS_AGENT_CONTAINER_NAME}" >/dev/null
    OHOS_AGENT_WAS_RUNNING="false"
  fi
}
cleanup() {
  rm -rf "${TEMP_DIR}"
  [[ -z "${PARTIAL_BUNDLE}" ]] || rm -f "${PARTIAL_BUNDLE}"
  resume_services
  [[ -z "${LOCK_DIR}" ]] || rmdir "${LOCK_DIR}"
}
trap cleanup EXIT

docker volume inspect "${VOLUME_NAME}" >/dev/null 2>&1 || {
  echo "Missing Docker volume: ${VOLUME_NAME}" >&2
  exit 1
}

HOME_PATH="$(docker volume inspect --format '{{.Mountpoint}}' "${VOLUME_NAME}")"
[[ "${HOME_PATH}" == /* ]] || { echo "Invalid Jenkins Home mountpoint." >&2; exit 1; }
HOME_PATH="$(cd "${HOME_PATH}" && pwd -P)"
BACKUP_DIR="$(python3 -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve())' "${BACKUP_DIR}")"
case "${BACKUP_DIR}/" in
  "${HOME_PATH}/"*) echo "Backup output must be outside Jenkins Home." >&2; exit 1 ;;
esac
[[ -f "${SCRIPT_DIR}/jenkins-home-excludes.txt" ]] || { echo "Missing Home exclusion policy." >&2; exit 1; }
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
if ! mkdir "${BACKUP_DIR}/.backup.lock"; then
  echo "Another backup owns this directory; refusing concurrent backup." >&2
  exit 1
fi
LOCK_DIR="${BACKUP_DIR}/.backup.lock"
PARTIAL_BUNDLE="${BACKUP_DIR}/.${BUNDLE_NAME}.partial"

# 已登记的 Home 不能把丢失回执当成首次运行，不依赖项目 Job/节点命名。
[[ ! -d "${HOME_PATH}/jobs" && ! -d "${HOME_PATH}/nodes" ]] || LINUX_STATE_REQUIRED="true"
if docker volume inspect "${AGENT_WORK_VOLUME_NAME}" >/dev/null 2>&1; then
  LINUX_STATE_FROM_VOLUME="true"
elif [[ "${LINUX_STATE_REQUIRED}" == "true" ]]; then
  echo "Registered Linux delivery state volume is missing; refusing incomplete migration." >&2
  exit 1
fi

if [[ -n "${JENKINS_MAC_DELIVERY_STATE_ARCHIVE:-}" ]]; then
  python3 "${DELIVERY_TOOL}" check-state-archive "${JENKINS_MAC_DELIVERY_STATE_ARCHIVE}" \
    --private --outside="${HOME_PATH}" > "${TEMP_DIR}/mac-state.json"
  MAC_STATE_UID="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["uid"])' "${TEMP_DIR}/mac-state.json")"
  cp "${JENKINS_MAC_DELIVERY_STATE_ARCHIVE}" "${TEMP_DIR}/mac-delivery-state.tar.gz"
  chmod 600 "${TEMP_DIR}/mac-delivery-state.tar.gz"
  # 再核对复制结果，输入被替换时不能将另一份回执放入恢复包。
  python3 "${DELIVERY_TOOL}" check-state-archive "${TEMP_DIR}/mac-delivery-state.tar.gz" \
    --uid="${MAC_STATE_UID}" > "${TEMP_DIR}/mac-copy.json"
  python3 -c 'import json, sys; a=json.load(open(sys.argv[1], encoding="utf-8")); b=json.load(open(sys.argv[2], encoding="utf-8")); sys.exit(a != b)' \
    "${TEMP_DIR}/mac-state.json" "${TEMP_DIR}/mac-copy.json"
fi

if docker volume inspect "${OHOS_SIGNING_VOLUME_NAME}" >/dev/null 2>&1; then
  OHOS_SIGNING_FROM_VOLUME="true"
  if [[ "$(docker inspect --format '{{.State.Running}}' "${OHOS_AGENT_CONTAINER_NAME}" 2>/dev/null || true)" == "true" ]]; then
    OHOS_AGENT_WAS_RUNNING="true"
    docker stop "${OHOS_AGENT_CONTAINER_NAME}" >/dev/null
  fi
fi

if [[ "$(docker inspect --format '{{.State.Running}}' "${AGENT_CONTAINER_NAME}" 2>/dev/null || true)" == "true" ]]; then
  AGENT_WAS_RUNNING="true"
  docker stop "${AGENT_CONTAINER_NAME}" >/dev/null
fi

if [[ "${LINUX_STATE_FROM_VOLUME}" == "true" ]]; then
  docker run --rm --user root --entrypoint bash \
    --volume "${AGENT_WORK_VOLUME_NAME}:/source:ro" \
    --volume "${TEMP_DIR}:/backup" "${IMAGE_NAME}" -eu -c '
      state="/source/$1"; uid="$2"
      cursor="$state"
      while [ "$cursor" != /source ]; do test ! -L "$cursor"; cursor="$(dirname "$cursor")"; done
      test -d "$state" && test "$(stat -c "%u:%a" "$state")" = "$uid:700"
      test -z "$(find "$state" -mindepth 1 -maxdepth 1 \( ! -type f -o ! -uid "$uid" -o ! -perm 0600 -o -links +1 -o -size +1M \) -print -quit)"
      test "$(find "$state" -mindepth 1 -maxdepth 1 -printf . | wc -c)" -le 10000
      test "$(du -sb "$state" | cut -f1)" -le 67108864
      tar -C "$(dirname "$state")" -czf /backup/linux-delivery-state.tar.gz delivery-state
    ' backup "${DELIVERY_RELATIVE}" "${LINUX_UID}"
  docker run --rm --user root --entrypoint chown \
    --volume "${TEMP_DIR}:/backup" "${IMAGE_NAME}" \
    "$(id -u):$(id -g)" /backup/linux-delivery-state.tar.gz
  chmod 600 "${TEMP_DIR}/linux-delivery-state.tar.gz"
  python3 "${DELIVERY_TOOL}" check-state-archive "${TEMP_DIR}/linux-delivery-state.tar.gz" \
    --uid="${LINUX_UID}" > "${TEMP_DIR}/linux-state.json"
fi

if [[ "$(docker inspect --format '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null || true)" == "true" ]]; then
  CONTROLLER_WAS_RUNNING="true"
  docker stop "${CONTAINER_NAME}" >/dev/null
fi

python3 - "${TEMP_DIR}" "${MAC_STATE_UID}" "${BACKUP_PROJECT_ID}" "${LINUX_UID}" "${DELIVERY_RELATIVE}" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
value = {"version": 1, "linux": "linux-delivery-state.tar.gz" if (root / "linux-delivery-state.tar.gz").is_file() else None,
         "mac": "mac-delivery-state.tar.gz" if sys.argv[2] else None, "macSourceUid": int(sys.argv[2]) if sys.argv[2] else None}
(root / "delivery-state-coverage.json").write_text(json.dumps(value) + "\n", encoding="utf-8")
(root / "controller-backup-policy.json").write_text(json.dumps({"version": 1, "scope": "controller_dr",
    "projectId": sys.argv[3], "linuxUid": int(sys.argv[4]), "receiptRelative": sys.argv[5]}) + "\n", encoding="utf-8")
if not value["linux"] or not value["mac"]:
    print("Partial migration: delivery receipts do not cover both Linux and Mac; restore cannot resume delivery.")
PY

docker run --rm --user root --entrypoint tar \
  --volume "${VOLUME_NAME}:/source:ro" \
  --volume "${TEMP_DIR}:/backup" \
  --volume "${SCRIPT_DIR}/jenkins-home-excludes.txt:/home-excludes.txt:ro" \
  "${IMAGE_NAME}" \
  --anchored --exclude-from=/home-excludes.txt -C /source -czf /backup/jenkins-home.tar.gz .
docker run --rm --user root --entrypoint chown \
  --volume "${TEMP_DIR}:/backup" \
  "${IMAGE_NAME}" \
  "$(id -u):$(id -g)" /backup/jenkins-home.tar.gz

if [[ "${OHOS_SIGNING_FROM_VOLUME}" == "true" ]]; then
  # 仅迁移签名状态；Agent Secret 位于独立 bootstrap 卷，由新宿主重新生成。
  docker run --rm --user root --entrypoint tar \
    --volume "${OHOS_SIGNING_VOLUME_NAME}:/source:ro" \
    --volume "${TEMP_DIR}:/backup" \
    "${IMAGE_NAME}" \
    -C /source -czf /backup/ohos-signing.tar.gz .
  docker run --rm --user root --entrypoint chown \
    --volume "${TEMP_DIR}:/backup" \
    "${IMAGE_NAME}" \
    "$(id -u):$(id -g)" /backup/ohos-signing.tar.gz
  chmod 600 "${TEMP_DIR}/ohos-signing.tar.gz"
fi

(
  cd "${TEMP_DIR}"
  ARCHIVES=(jenkins-home.tar.gz delivery-state-coverage.json controller-backup-policy.json)
  if [[ -f ohos-signing.tar.gz ]]; then
    ARCHIVES+=(ohos-signing.tar.gz)
  fi
  for state in linux-delivery-state.tar.gz mac-delivery-state.tar.gz; do
    [[ ! -f "${state}" ]] || ARCHIVES+=("${state}")
  done
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${ARCHIVES[@]}" > manifest.sha256
  else
    shasum -a 256 "${ARCHIVES[@]}" > manifest.sha256
  fi
  tar -czf "${PARTIAL_BUNDLE}" "${ARCHIVES[@]}" manifest.sha256
  gzip -t "${PARTIAL_BUNDLE}"
  mkdir verify
  tar -xzf "${PARTIAL_BUNDLE}" -C verify
  cd verify
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum --check manifest.sha256
  else
    shasum -a 256 --check manifest.sha256
  fi
  for archive in "${ARCHIVES[@]}"; do
    [[ "${archive}" != *.tar.gz ]] || gzip -t "${archive}"
  done
)

chmod 600 "${PARTIAL_BUNDLE}"
# 同文件系统原子发布，校验或服务恢复失败时不清理已有回滚包。
ln "${PARTIAL_BUNDLE}" "${BACKUP_DIR}/${BUNDLE_NAME}"
rm "${PARTIAL_BUNDLE}"
PARTIAL_BUNDLE=""
resume_services
python3 - "${BACKUP_DIR}" "${BUNDLE_NAME}" "${BACKUP_PROJECT_ID}" <<'PY'
import pathlib, re, sys
root = pathlib.Path(sys.argv[1])
current = root / sys.argv[2]
pattern = re.compile(re.escape(sys.argv[3]) + r'-jenkins-migration-\d{8}-\d{6}(?:-\d+)?\.tar\.gz')
older = sorted((p for p in root.iterdir() if pattern.fullmatch(p.name)
                and p != current and not p.is_symlink() and p.is_file()),
               key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
for obsolete in older[1:]:
    obsolete.unlink()
    print('Removed obsolete rollback bundle: ' + str(obsolete))
PY
echo "Sensitive migration bundle: ${BACKUP_DIR}/${BUNDLE_NAME}"
