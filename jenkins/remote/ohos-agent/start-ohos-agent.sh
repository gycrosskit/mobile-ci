#!/usr/bin/env bash
set -euo pipefail
umask 077

JENKINS_URL="${JENKINS_URL:-http://controller:8080}"
AGENT_NAME="${JENKINS_AGENT_NAME:-linux-build}"
AGENT_WORK_DIR="${JENKINS_AGENT_WORKDIR:-/home/jenkins/agent}"
SECRET_FILE="${JENKINS_AGENT_SECRET_FILE:-/run/jenkins-agent-bootstrap/agent.secret}"
AGENT_JAR="${AGENT_WORK_DIR}/agent.jar"
: "${HARMONY_CLT_HOME:?Missing frozen Linux CLT}"
[[ "${AGENT_NAME}" == linux-build || "${AGENT_NAME}" == signing-maintenance ]] || {
  echo 'Only unified Linux build or signing maintenance agents are supported' >&2; exit 1;
}

if [[ "${AGENT_NAME}" == signing-maintenance ]]; then
  : "${GRADLE_USER_HOME:?Missing isolated maintenance cache}"
  [[ -z "${CI_LINUX_MEMORY_LOCK:-}" ]] || { echo 'Maintenance must not mount the build memory lock' >&2; exit 1; }
else
: "${HARMONY_GRADLE_USER_HOME:?Missing isolated Harmony cache}"
command -v flock >/dev/null || { echo 'Missing tool: flock' >&2; exit 1; }
[[ "${CI_LINUX_MEMORY_LOCK:-}" == /home/jenkins/linux-build-memory/build.lock ]] || {
  echo 'CI_LINUX_MEMORY_LOCK must use the dedicated shared volume' >&2; exit 1;
}
python3 - "${CI_LINUX_MEMORY_LOCK}" <<'MEMORY_LOCK_PY'
import os
import stat
import sys
from pathlib import Path
lock = Path(sys.argv[1])
for path, mode, kind in ((lock.parent, 0o700, 'directory'), (lock, 0o600, 'file')):
    if path.is_symlink() or path.resolve() != path or not (path.is_dir() if kind == 'directory' else path.is_file()):
        sys.exit('Shared memory lock requires private regular state without symlinks')
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode:
        sys.exit('Shared memory lock requires current-user ownership and mode 0700/0600')
MEMORY_LOCK_PY
fi

for executable in java python3 openssl shasum curl unzip git; do
  command -v "${executable}" >/dev/null || { echo "Missing tool: ${executable}" >&2; exit 1; }
done
java -XshowSettings:properties -version 2>&1 | grep -Eq 'java.specification.version = 21$'
test -x "${HARMONY_CLT_HOME}/tool/node/bin/node"
test -f "${HARMONY_CLT_HOME}/sdk/default/openharmony/toolchains/lib/hap-sign-tool.jar"
python3 /usr/local/lib/ohos/clt.py check "${HARMONY_CLT_HOME}" --runtime

private_dirs=("${AGENT_WORK_DIR}" "${GRADLE_USER_HOME:-/home/jenkins/.gradle}")
if [[ "${AGENT_NAME}" == linux-build ]]; then
  private_dirs+=("${HARMONY_GRADLE_USER_HOME}")
fi
if [[ -n "${SIGNING_MAINTENANCE_ROOT:-}" ]]; then
  private_dirs+=("${SIGNING_MAINTENANCE_ROOT}")
fi
python3 - "${private_dirs[@]}" <<'PY'
import os
import stat
import sys
from pathlib import Path
for value in sys.argv[1:]:
    path = Path(value)
    if path.is_symlink() or not path.is_dir():
        sys.exit("Agent volumes must be pre-provisioned private directories")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        sys.exit("Agent volumes require current-user ownership and mode 0700")
PY

until [[ -s "${SECRET_FILE}" ]]; do
  echo 'Waiting for the dedicated OHOS agent secret...'
  sleep 2
done
python3 - "${SECRET_FILE}" <<'PY'
import os
import stat
import sys
from pathlib import Path
path = Path(sys.argv[1])
if path.is_symlink() or not path.is_file():
    sys.exit("OHOS agent secret must be a regular file")
info = path.stat()
if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
    sys.exit("OHOS agent secret requires current-user ownership and mode 0600")
PY

until curl --fail --silent --show-error --location \
    "${JENKINS_URL}/jnlpJars/agent.jar" --output "${AGENT_JAR}.tmp"; do
  echo 'Waiting for Jenkins agent.jar...'
  sleep 5
done
mv "${AGENT_JAR}.tmp" "${AGENT_JAR}"
exec java -Xms128m -Xmx512m -jar "${AGENT_JAR}" \
  -url "${JENKINS_URL}" -secret "@${SECRET_FILE}" -name "${AGENT_NAME}" \
  -webSocket -workDir "${AGENT_WORK_DIR}"
