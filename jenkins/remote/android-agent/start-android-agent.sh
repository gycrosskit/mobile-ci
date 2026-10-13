#!/usr/bin/env bash
set -euo pipefail

JENKINS_URL="${JENKINS_URL:-http://controller:8080}"
AGENT_NAME="${JENKINS_AGENT_NAME:-linux-build}"
AGENT_WORK_DIR="${JENKINS_AGENT_WORKDIR:-/home/jenkins/agent}"
SECRET_FILE="${JENKINS_AGENT_SECRET_FILE:-/run/jenkins-agent-bootstrap/agent.secret}"
AGENT_JAR="${AGENT_WORK_DIR}/agent.jar"

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

mkdir -p "${AGENT_WORK_DIR}"

until [[ -s "${SECRET_FILE}" ]]; do
  echo "Waiting for Jenkins to provision the Android agent secret..."
  sleep 2
done

until curl --fail --silent --show-error --location \
  "${JENKINS_URL}/jnlpJars/agent.jar" \
  --output "${AGENT_JAR}.tmp"; do
  echo "Waiting for Jenkins agent.jar..."
  sleep 5
done
mv "${AGENT_JAR}.tmp" "${AGENT_JAR}"

exec java -Xms128m -Xmx512m -jar "${AGENT_JAR}" \
  -url "${JENKINS_URL}" \
  -secret "@${SECRET_FILE}" \
  -name "${AGENT_NAME}" \
  -webSocket \
  -workDir "${AGENT_WORK_DIR}"
