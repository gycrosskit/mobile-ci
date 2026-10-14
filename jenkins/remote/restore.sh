#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /absolute/path/project-jenkins-migration-YYYYmmdd-HHMMSS.tar.gz"
  exit 2
fi

ARCHIVE_PATH="$1"
if [[ "${ARCHIVE_PATH}" != /* || ! -f "${ARCHIVE_PATH}" ]]; then
  echo "Migration bundle must be an existing absolute path: ${ARCHIVE_PATH}" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/compose-context.sh"
read_backup_resources
TEMP_DIR="$(mktemp -d)"
RESTORE_SUCCEEDED="false"
CREATED_VOLUME="false"
CREATED_OHOS_SIGNING_VOLUME="false"
CREATED_AGENT_WORK_VOLUME="false"
DELIVERY_READY="false"
MAC_RESTORE_DIR="${JENKINS_MAC_DELIVERY_RESTORE_DIR:-}"

cleanup() {
  if [[ "${CREATED_VOLUME}" == "true" && "${RESTORE_SUCCEEDED}" != "true" ]]; then
    docker volume rm "${VOLUME_NAME}" >/dev/null 2>&1 || true
  fi
  if [[ "${CREATED_OHOS_SIGNING_VOLUME}" == "true" && "${RESTORE_SUCCEEDED}" != "true" ]]; then
    docker volume rm "${OHOS_SIGNING_VOLUME_NAME}" >/dev/null 2>&1 || true
  fi
  if [[ "${CREATED_AGENT_WORK_VOLUME}" == "true" && "${RESTORE_SUCCEEDED}" != "true" ]]; then
    docker volume rm "${AGENT_WORK_VOLUME_NAME}" >/dev/null 2>&1 || true
  fi
  rm -rf "${TEMP_DIR}"
}
trap cleanup EXIT

if docker volume inspect "${VOLUME_NAME}" >/dev/null 2>&1; then
  echo "Refusing to overwrite existing Docker volume: ${VOLUME_NAME}" >&2
  echo "Restore only on a new host, or back up and remove the unused volume manually." >&2
  exit 1
fi

python3 - "${ARCHIVE_PATH}" <<'PY'
import re, sys, tarfile
allowed = {"jenkins-home.tar.gz", "dws-state.tar.gz", "ohos-signing.tar.gz", "manifest.sha256", "controller-backup-policy.json",
           "delivery-state-coverage.json", "linux-delivery-state.tar.gz", "mac-delivery-state.tar.gz"}
seen = set()
with tarfile.open(sys.argv[1], "r:gz") as archive:
    for member in archive:
        if not member.isfile() or member.name not in allowed or member.name in seen:
            raise SystemExit("Invalid migration bundle: unexpected, duplicate or linked outer entry.")
        if member.name == 'controller-backup-policy.json' and member.size > 4096:
            raise SystemExit('Invalid Controller backup policy: oversized metadata.')
        seen.add(member.name)
    if "manifest.sha256" not in seen or archive.getmember("manifest.sha256").size > 65536:
        raise SystemExit("Invalid checksum manifest: missing or oversized.")
    entries = set()
    manifest = archive.extractfile("manifest.sha256").read().decode("utf-8")
    for line in manifest.splitlines():
        match = re.fullmatch(r"[0-9a-fA-F]{64} [ *]([^\s]+)", line)
        if not match or match.group(1) not in seen - {"manifest.sha256"} or match.group(1) in entries:
            raise SystemExit("Invalid checksum manifest: unexpected, duplicate or unsafe path.")
        entries.add(match.group(1))
    if entries != seen - {"manifest.sha256"}:
        raise SystemExit("Invalid checksum manifest: coverage must match outer payload.")
PY
tar -xzf "${ARCHIVE_PATH}" -C "${TEMP_DIR}"
[[ -f "${TEMP_DIR}/jenkins-home.tar.gz" && -f "${TEMP_DIR}/manifest.sha256" ]] || {
  echo "Invalid migration bundle: required files are missing." >&2
  exit 1
}

if [[ -f "${TEMP_DIR}/ohos-signing.tar.gz" ]]; then
  if docker volume inspect "${OHOS_SIGNING_VOLUME_NAME}" >/dev/null 2>&1; then
    echo "Refusing to overwrite existing Docker volume: ${OHOS_SIGNING_VOLUME_NAME}" >&2
    exit 1
  fi
  if ! awk '$2 == "ohos-signing.tar.gz" {found=1} END {exit !found}' "${TEMP_DIR}/manifest.sha256"; then
    echo "Invalid migration bundle: OHOS signing archive is missing from checksum manifest." >&2
    exit 1
  fi
fi

(
  cd "${TEMP_DIR}"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum --check manifest.sha256
  else
    shasum -a 256 --check manifest.sha256
  fi
)

if [[ -f "${TEMP_DIR}/dws-state.tar.gz" ]]; then
  echo "Legacy DWS authentication archive verified and skipped; preserved in the original migration bundle." >&2
fi

# 回执与 coverage 必须先验完，不能在创建卷以后才发现恢复不完整或越界。
DELIVERY_READY="$(python3 - "${SCRIPT_DIR}" "${TEMP_DIR}" "${MAC_RESTORE_DIR}" "${BACKUP_PROJECT_ID}" "${LINUX_UID}" "${DELIVERY_RELATIVE}" <<'PY'
import json, os, stat, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parents[1]))
from delivery.delivery import inspect_state_archive, validate_state_coverage
root, output = Path(sys.argv[2]), sys.argv[3]
names = {line.split()[-1].lstrip("*") for line in (root / "manifest.sha256").read_text(encoding="utf-8").splitlines()}
policy = root / "controller-backup-policy.json"
if policy.exists():
    expected = {"version": 1, "scope": "controller_dr", "projectId": sys.argv[4],
                "linuxUid": int(sys.argv[5]), "receiptRelative": sys.argv[6]}
    if policy.name not in names or json.loads(policy.read_text(encoding="utf-8")) != expected:
        raise SystemExit("Controller disaster-recovery scope/project/owner differs from the selected trusted profile.")
elif sys.argv[4] != "sxmqlive" or int(sys.argv[5]) != 1000 or sys.argv[6] != "delivery-state":
    raise SystemExit("Legacy Controller disaster recovery is only accepted by the original sxmqlive profile.")
coverage_path = root / "delivery-state-coverage.json"
if not coverage_path.exists():
    if any((root / name).exists() for name in ("linux-delivery-state.tar.gz", "mac-delivery-state.tar.gz")):
        raise SystemExit("Delivery archives require checksummed coverage metadata.")
    print("false")
    raise SystemExit(0)
if coverage_path.name not in names:
    raise SystemExit("Delivery coverage is missing from the checksum manifest.")
coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
validate_state_coverage(coverage)
complete = True
for platform, expected_uid in (("linux", int(sys.argv[5])), ("mac", coverage["macSourceUid"])):
    name = platform + "-delivery-state.tar.gz"
    if coverage[platform] is None:
        complete = False
        if (root / name).exists():
            raise SystemExit("Delivery archive contradicts coverage metadata.")
        continue
    if name not in names or not (root / name).is_file():
        raise SystemExit("Delivery archive missing from the bundle/checksum manifest.")
    inspect_state_archive(root / name, expected_uid=expected_uid)
if coverage["mac"] is not None:
    path = Path(output)
    if not output or not path.is_absolute() or not path.is_dir() or path.resolve() != path or any(path.iterdir()):
        raise SystemExit("Mac receipt export requires JENKINS_MAC_DELIVERY_RESTORE_DIR: an existing absolute empty directory without symlinks.")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SystemExit("Mac receipt export directory must be current-user owned with mode 0700.")
print("true" if complete else "false")
PY
)"

if [[ -f "${TEMP_DIR}/linux-delivery-state.tar.gz" ]] \
  && docker volume inspect "${AGENT_WORK_VOLUME_NAME}" >/dev/null 2>&1; then
  echo "Refusing to overwrite existing Docker volume: ${AGENT_WORK_VOLUME_NAME}" >&2
  exit 1
fi

docker volume create "${VOLUME_NAME}" >/dev/null
CREATED_VOLUME="true"
docker run --rm --user root --entrypoint tar \
  --volume "${VOLUME_NAME}:/target" \
  --volume "${TEMP_DIR}:/backup:ro" \
  "${IMAGE_NAME}" \
  -C /target -xzf /backup/jenkins-home.tar.gz
docker run --rm --user root --entrypoint chown \
  --volume "${VOLUME_NAME}:/target" \
  "${IMAGE_NAME}" \
  -R "${LINUX_UID}:${LINUX_UID}" /target

if [[ -f "${TEMP_DIR}/ohos-signing.tar.gz" ]]; then
  docker volume create "${OHOS_SIGNING_VOLUME_NAME}" >/dev/null
  CREATED_OHOS_SIGNING_VOLUME="true"
  docker run --rm --user root --entrypoint tar \
    --volume "${OHOS_SIGNING_VOLUME_NAME}:/target" \
    --volume "${TEMP_DIR}:/backup:ro" \
    "${IMAGE_NAME}" \
    -C /target -xzf /backup/ohos-signing.tar.gz
  docker run --rm --user root --entrypoint chown \
    --volume "${OHOS_SIGNING_VOLUME_NAME}:/target" \
    "${IMAGE_NAME}" \
    -R "${LINUX_UID}:${LINUX_UID}" /target
  docker run --rm --user root --entrypoint bash \
    --volume "${OHOS_SIGNING_VOLUME_NAME}:/target" \
    "${IMAGE_NAME}" \
    -eu -c 'find /target -type d -exec chmod 700 {} +; find /target -type f -exec chmod 600 {} +'
fi

if [[ -f "${TEMP_DIR}/linux-delivery-state.tar.gz" ]]; then
  docker volume create "${AGENT_WORK_VOLUME_NAME}" >/dev/null
  CREATED_AGENT_WORK_VOLUME="true"
  docker run --rm --user root --entrypoint bash \
    --volume "${AGENT_WORK_VOLUME_NAME}:/target" \
    --volume "${TEMP_DIR}:/backup:ro" \
    "${IMAGE_NAME}" -eu -c 'umask 077; parent="$(dirname "/target/$1")"; mkdir -p "$parent"; chmod 700 "$parent"; tar -C "$parent" -xzf /backup/linux-delivery-state.tar.gz' restore "${DELIVERY_RELATIVE}"
  docker run --rm --user root --entrypoint chown \
    --volume "${AGENT_WORK_VOLUME_NAME}:/target" \
    "${IMAGE_NAME}" -R "${LINUX_UID}:${LINUX_UID}" /target
fi

if [[ -f "${TEMP_DIR}/mac-delivery-state.tar.gz" ]]; then
  python3 - "${TEMP_DIR}" "${MAC_RESTORE_DIR}" <<'PY'
import os, sys
from pathlib import Path
for name in ("mac-delivery-state.tar.gz", "delivery-state-coverage.json"):
    source, target = Path(sys.argv[1]) / name, Path(sys.argv[2]) / name
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(source.read_bytes())
PY
  echo "Mac receipts exported only; restore to the actual Mac service account and verify there before replay." >&2
fi
RESTORE_SUCCEEDED="true"

echo "Restored Jenkins home volume: ${VOLUME_NAME}"
if [[ "${CREATED_OHOS_SIGNING_VOLUME}" == "true" ]]; then
  echo "Restored private OHOS signing state volume: ${OHOS_SIGNING_VOLUME_NAME}"
fi
if [[ "${CREATED_AGENT_WORK_VOLUME}" == "true" ]]; then
  echo "Restored Linux delivery receipts only in Agent work volume: ${AGENT_WORK_VOLUME_NAME}"
fi
echo "Material volumes restored only; no service was started."
echo "The bundle does not restore private Compose/host tools. Mac receipts require separate controlled service-account recovery." >&2
echo "Next: with the same complete Compose manifest, run stack.sh check, start, then verify; build/import pinned images separately when needed."
if [[ "${DELIVERY_READY}" != "true" ]]; then
  echo "NOT READY: legacy/partial bundle has incomplete Linux/Mac state coverage; preserve/restore the corresponding snapshot before replay." >&2
  exit 3
fi
