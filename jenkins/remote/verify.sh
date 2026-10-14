#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/compose-context.sh"
check_compose_services
if [[ -n "${JENKINS_DEPLOYMENT_FILE:-}" ]]; then
  exec python3 "${SCRIPT_DIR}/deployment-runtime.py" verify "${JENKINS_DEPLOYMENT_FILE}" "${JENKINS_COMPOSE_FILE}"
fi

# 配置和 inspect 只在内存比较，不输出带凭据的配置或工具日志。
python3 - "${CI_CONTEXT_ROOT}" "${JENKINS_CI_PROJECT:-sxmqlive}" "${COMPOSE[@]}" <<'PY'
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from project_config import load_project
profile = load_project(sys.argv[2])
roles = profile['runtime']['roles']
compose = sys.argv[3:]
services = tuple(roles.values())
white_env = {
    "DELIVERY_STATE_ROOT", "CI_LINUX_MEMORY_LOCK", "ANDROID_SDK_ROOT",
    "HARMONY_CLT_HOME", "HARMONY_GRADLE_USER_HOME", "SIGNING_MAINTENANCE_ROOT",
    "JAVA_OPTS", "GRADLE_USER_HOME", "DWS_CONFIG_DIR", "JENKINS_URL",
    "JENKINS_AGENT_NAME", "JENKINS_AGENT_WORKDIR", "JENKINS_AGENT_SECRET_FILE",
    "ANDROID_AGENT_NAME", "ANDROID_AGENT_LABELS", "ANDROID_AGENT_REMOTE_FS",
    "PR_AGENT_NAME", "PR_AGENT_LABELS", "PR_AGENT_REMOTE_FS",
    "OHOS_MAINTENANCE_AGENT_NAME", "OHOS_MAINTENANCE_AGENT_REMOTE_FS",
}


def run(args, stage, input=None):
    try:
        result = subprocess.run(args, input=input.encode("utf-8") if input is not None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise SystemExit("Infrastructure verification failed: " + stage)
    if result.returncode:
        raise SystemExit("Infrastructure verification failed: " + stage)
    return result.stdout.decode("utf-8")


def require(condition, message):
    if not condition:
        raise SystemExit("Infrastructure verification failed: " + message)


config = json.loads(run(compose + ["config", "--format", "json"], "render Compose"))
ids = {}
actual_envs = {}
for service in services:
    desired = config["services"][service]
    cid = run(compose + ["ps", "--quiet", service], service + " container").strip()
    require(bool(cid) and "\n" not in cid, service + " must have exactly one container")
    ids[service] = cid
    actual = json.loads(run(["docker", "inspect", cid], service + " inspect"))[0]
    require(actual["Config"].get("Labels", {}).get("com.docker.compose.project.config_files")
            == ",".join(compose[i + 1] for i, value in enumerate(compose) if value == "-f"),
            service + " was started from a different Compose manifest")
    require(actual["State"].get("Running"), service + " is not running")
    if service == roles['controller'] or desired.get("healthcheck"):
        require(actual["State"].get("Health", {}).get("Status") == "healthy", service + " is not healthy")
    if desired.get("image"):
        image = json.loads(run(["docker", "image", "inspect", desired["image"]], service + " image"))[0]
        require(actual["Image"] == image["Id"], service + " image differs from selected manifest")
    env = dict(value.split("=", 1) for value in actual["Config"].get("Env", []) if "=" in value)
    actual_envs[service] = env
    for key in white_env & set(desired.get("environment", {})):
        require(env.get(key) == str(desired["environment"][key]), service + " environment drift: " + key)
    mounts = {mount["Destination"]: mount for mount in actual["Mounts"]}
    for volume in desired.get("volumes", []):
        target, kind, source = volume["target"], volume["type"], volume.get("source")
        require(target in mounts, service + " missing mount: " + target)
        mount = mounts[target]
        require(mount["Type"] == kind and mount["RW"] == (not volume.get("read_only", False)),
                service + " mount access differs: " + target)
        if kind == "volume" and source:
            expected = config.get("volumes", {}).get(source, {}).get("name", config["name"] + "_" + source)
            require(mount.get("Name") == expected, service + " volume differs: " + target)
        elif kind == "bind":
            path = Path(source)
            require(path.is_absolute() and path.exists() and path.resolve() == path,
                    service + " bind source missing or uses symlinks: " + target)
            require(mount["Source"] == source, service + " bind source differs: " + target)
    if service == roles['pr']:
        require(not any("dws" in (target + mount.get("Name", ""))
                        or "signing" in (target + mount.get("Name", ""))
                        for target, mount in mounts.items()),
                "PR Agent must not mount DWS or signing state")
    if service == roles['android']:
        root = env.get("DELIVERY_STATE_ROOT", "")
        work = env.get("JENKINS_AGENT_WORKDIR", "")
        require(bool(work) and root.startswith(work.rstrip("/") + "/")
                and work in mounts and mounts[work]["Type"] == "volume",
                "delivery receipts must use the persistent Agent work volume")

common = r'''
  java -version 2>&1 | grep -Eq 'version "21[.]'
  python3 --version >/dev/null
  git --version >/dev/null
  test -w "${JENKINS_AGENT_WORKDIR}"
  test -w /home/jenkins/.gradle
  test -s "${JENKINS_AGENT_SECRET_FILE}"
'''
android = r'''
  sdkmanager --version >/dev/null
  test -d "${ANDROID_SDK_ROOT}/platforms/android-36"
  test -d "${ANDROID_SDK_ROOT}/build-tools/35.0.0"
  test -d "${ANDROID_SDK_ROOT}/ndk/25.1.8937393"
  test ! -L "${CI_LINUX_MEMORY_LOCK}"
  test "$(stat -c '%u:%a' "${CI_LINUX_MEMORY_LOCK}")" = "$(id -u):600"
'''
harmony = r'''
  test -x "${HARMONY_CLT_HOME}/tool/node/bin/node"
  test -s "${HARMONY_CLT_HOME}/sdk/default/openharmony/toolchains/lib/hap-sign-tool.jar"
'''
receipts = r'''
  python3 - <<'PYROOT'
import os, stat
from pathlib import Path
root = Path(os.environ["DELIVERY_STATE_ROOT"])
if not root.is_absolute() or not root.is_dir() or root.resolve() != root or "workspace" in root.parts:
    raise SystemExit(1)
info = root.stat()
if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
    raise SystemExit(1)
PYROOT
'''
maintenance = r'''
  command -v openssl >/dev/null
  test -z "${CI_LINUX_MEMORY_LOCK:-}"
  test ! -L "${SIGNING_MAINTENANCE_ROOT}"
  test -d "${SIGNING_MAINTENANCE_ROOT}"
  test "$(stat -c '%u:%a' "${SIGNING_MAINTENANCE_ROOT}")" = "$(id -u):700"
'''
for service, checks in ((roles['android'], android + harmony + receipts + '\n dws version | grep -F v1.0.61'),
                        (roles['pr'], android), (roles['maintenance'], harmony + maintenance)):
    run(compose + ["exec", "--no-TTY", service, "bash", "-euc", common + checks], service + " tools/state permissions")
    run(compose + ["exec", "--no-TTY", service, "python3", "-"], service + " Controller connection", input=r'''
from pathlib import Path
for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines()[1:]:
            fields = line.split()
            if int(fields[2].rsplit(":", 1)[1], 16) == 8080 and fields[3] == "01":
                raise SystemExit(0)
raise SystemExit(1)
''')

controller_env = config["services"][roles['controller']].get("environment", {})
for service, prefix, fallback, labels in ((roles['android'], "ANDROID", "linux-build", "android-builder ohos-linux linux-build"),
                                         (roles['pr'], "PR", "pr-linux", "pr-builder"),
                                         (roles['maintenance'], "OHOS_MAINTENANCE", "signing-maintenance", "signing-maintenance")):
    name = controller_env.get(prefix + "_AGENT_NAME", fallback)
    require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+", name)), "invalid Agent name")
    xml = run(["docker", "exec", ids[roles['controller']], "cat", "/var/jenkins_home/nodes/" + name + "/config.xml"], name + " Node configuration")
    node = ET.fromstring(xml)
    expected_labels = controller_env.get(prefix + "_AGENT_LABELS", labels)
    expected_fs = controller_env.get(prefix + "_AGENT_REMOTE_FS", "/home/jenkins/agent")
    require(node.findtext("numExecutors") == "1" and node.findtext("mode") == "EXCLUSIVE"
            and set(node.findtext("label", "").split()) == set(expected_labels.split())
            and node.findtext("remoteFS") == expected_fs, name + " Node configuration drift")
    for entry in node.findall(".//hudson.slaves.EnvironmentVariablesNodeProperty//entry"):
        values = entry.findall("string")
        if len(values) == 2 and values[0].text in white_env:
            require(values[1].text == actual_envs[service].get(values[0].text),
                    name + " Node environment overrides container: " + values[0].text)
print("Complete Linux Compose, four services, Node configuration, tools, mounts and receipt-root permissions passed read-only verification.")
print("Receipt-root permissions do not prove historical delivery receipts were restored; check the receipt backup before replay.")
PY
