"""统一 Linux 构建与隔离签名维护 Agent 的部署边界静态契约；实际节点/SDK 仍由部署验收。"""
import unittest
import json
import shutil
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "jenkins/remote"


class OhosAgentTest(unittest.TestCase):
    def test_overlay_extends_existing_build_agent_and_keeps_maintenance_isolated(self):
        source = (ROOT / "compose.ohos.yaml").read_text()
        controller = source.split("  controller:\n", 1)[1].split("  android-agent:\n", 1)[0]
        for value in ("OHOS_MAINTENANCE_AGENT_SECRET_FILE", "jenkins_ohos_maintenance_secret:/run/jenkins-ohos-maintenance-bootstrap",
                      "05-configure-ohos-agent.groovy:ro"):
            self.assertIn(value, controller)
        for value in ("build:", "image:", "ports:", "command:", "jenkins_home:"):
            self.assertNotIn(value, controller)
        agent = source.split("  android-agent:\n", 1)[1].split("  ohos-maintenance:\n", 1)[0]
        for value in ("ohos_gradle_cache:/home/jenkins/.harmony-gradle",
                      "HARMONY_GRADLE_USER_HOME: /home/jenkins/.harmony-gradle", "context: ./ohos-agent", "OHOS_CLT_HOST_DIR",
                      ":/opt/harmony-clt:ro", "HARMONY_GRADLE_HEAP_MB: \"4096\"",
                      "HARMONY_NODE_HEAP_MB: \"1024\""):
            self.assertIn(value, agent)
        for value in ("ohos_signing", "SIGNING_MAINTENANCE_ROOT", "jenkins_ohos_maintenance", "ohos_maintenance_gradle_cache", "jenkins_ohos_agent_secret",
                      "jenkins_ohos_agent_work", "\n      GRADLE_USER_HOME:", "NODE_OPTIONS:"):
            self.assertNotIn(value, agent)
        maintenance = source.split("  ohos-maintenance:\n", 1)[1].split("\nvolumes:\n", 1)[0]
        for value in ("JENKINS_AGENT_NAME: signing-maintenance", "ohos_signing:/home/jenkins/signing",
                      "container_name: sxmqlive-jenkins-ohos-maintenance-agent",
                      "SIGNING_MAINTENANCE_ROOT: /home/jenkins/signing", "source: jenkins_ohos_maintenance_work",
                      "jenkins_ohos_maintenance_secret:/run/jenkins-ohos-maintenance-bootstrap:ro",
                      "ohos_maintenance_gradle_cache:/home/jenkins/.gradle", "mem_limit: 1g"):
            self.assertIn(value, maintenance)
        self.assertNotIn("jenkins_ohos_agent_secret:", maintenance)
        self.assertNotIn("source: jenkins_ohos_agent_work", maintenance)
        self.assertEqual(2, source.count("image: sxmqlive-jenkins-ohos-agent:clt26-jdk21"))
        self.assertNotIn("dws_state", source)
        self.assertNotIn("  ohos-agent:\n", source)
        self.assertNotIn("OHOS_AGENT_NAME:", controller)
        self.assertNotIn("OHOS_AGENT_SECRET_FILE:", controller)
        self.assertNotIn("pr-agent:", source)
        self.assertEqual(6, source.count("install -d -o 1000 -g 1000 -m 700"))

    def test_merged_compose_reuses_build_identity_and_isolates_caches_and_signing(self):
        if shutil.which("docker-compose"):
            compose = ["docker-compose"]
        elif shutil.which("docker") and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
            compose = ["docker", "compose"]
        else:
            self.skipTest("需要已有 Docker Compose；不自动安装")
        result = subprocess.run([*compose, "--env-file", os.devnull,
                                 "-f", str(ROOT / "compose.yaml"), "-f", str(ROOT / "compose.ohos.yaml"),
                                 "config", "--format", "json"],
                                env={**os.environ, "OHOS_CLT_HOST_DIR": "/tmp/verified-harmony-clt",
                                     "OHOS_AGENT_BASE_IMAGE": "example/android-agent:reviewed",
                                     "JENKINS_AGENT_NAME": "linux-build",
                                     "JENKINS_AGENT_LABELS": "android-builder ohos-linux linux-build"},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        services = json.loads(result.stdout)["services"]
        self.assertNotIn("ohos-agent", services)
        build = services["android-agent"]
        self.assertEqual("sxmqlive-jenkins-android-agent", build["container_name"])
        self.assertEqual("linux-build", build["environment"]["JENKINS_AGENT_NAME"])
        self.assertEqual(str(ROOT / "ohos-agent"), build["build"]["context"])
        self.assertEqual(6 * 1024**3, int(build["mem_limit"]))
        self.assertEqual(14 * 1024**3, int(build["memswap_limit"]))
        mounts = {item["target"]: item["source"] for item in build["volumes"]}
        self.assertEqual("jenkins_agent_work", mounts["/home/jenkins/agent"])
        self.assertEqual("jenkins_agent_secret", mounts["/run/jenkins-agent-bootstrap"])
        self.assertEqual("gradle_cache", mounts["/home/jenkins/.gradle"])
        self.assertEqual("ohos_gradle_cache", mounts["/home/jenkins/.harmony-gradle"])
        self.assertNotIn("/home/jenkins/.dws-state", mounts)
        self.assertEqual("linux_build_memory", mounts["/home/jenkins/linux-build-memory"])
        self.assertNotIn("/home/jenkins/signing", mounts)
        self.assertNotIn("GRADLE_USER_HOME", build["environment"])
        self.assertNotIn("KONAN_DATA_DIR", build["environment"])
        controller = services["controller"]["environment"]
        self.assertEqual("linux-build", controller["ANDROID_AGENT_NAME"])
        self.assertNotIn("OHOS_AGENT_NAME", controller)
        self.assertEqual("pr-linux", services["pr-agent"]["environment"]["JENKINS_AGENT_NAME"])
        self.assertEqual("signing-maintenance", services["ohos-maintenance"]["environment"]["JENKINS_AGENT_NAME"])
        pipeline = (ROOT.parent / "Jenkinsfile.ohos.test").read_text()
        for value in ("agent { label 'linux-build' }", "GRADLE_USER_HOME = '/home/jenkins/.harmony-gradle'",
                      "KONAN_DATA_DIR = '/home/jenkins/.harmony-gradle/konan'"):
            self.assertIn(value, pipeline)

    def test_public_ohos_image_and_explicit_unified_android_compatibility(self):
        source = (ROOT / "ohos-agent/Dockerfile").read_text()
        self.assertIn("eclipse-temurin:21.0.11_10-jdk-jammy@sha256:", source)
        self.assertIn("USER 1000:1000", source)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/start-ohos-agent"]', source)
        self.assertNotIn("sdkmanager", source)
        self.assertNotIn("dws", source.lower())
        compat = (ROOT / "ohos-agent/Dockerfile.compat").read_text()
        self.assertIn("FROM ${OHOS_AGENT_BASE_IMAGE}", compat)
        self.assertNotIn("ARG OHOS_AGENT_BASE_IMAGE=", compat)
        overlay = (ROOT / 'compose.ohos.yaml').read_text()
        self.assertIn('dockerfile: Dockerfile.compat', overlay)
        self.assertIn('${OHOS_AGENT_BASE_IMAGE:?', overlay)

    def test_agent_validates_tools_and_private_state_before_connecting(self):
        source = (ROOT / "ohos-agent/start-ohos-agent.sh").read_text()
        self.assertIn("for executable in java python3 openssl shasum curl unzip git", source)
        self.assertIn("java.specification.version = 21", source)
        self.assertIn("stat.S_IMODE(info.st_mode) != 0o700", source)
        self.assertIn("stat.S_IMODE(info.st_mode) & 0o077", source)
        self.assertIn('info.st_uid != os.getuid()', source)
        self.assertIn('path.is_symlink()', source)
        self.assertLess(source.index('SIGNING_MAINTENANCE_ROOT'), source.index('exec java'))
        self.assertIn('-secret "@${SECRET_FILE}"', source)
        self.assertNotIn('$(cat', source)

    def test_bootstrap_is_exclusive_and_preserves_existing_nodes_on_conflict(self):
        source = (ROOT / "init.groovy.d/05-configure-ohos-agent.groovy").read_text()
        self.assertIn("node.getRemoteFS() != remoteFs", source)
        self.assertIn("throw new IllegalStateException", source)
        self.assertIn("agent.remoteFs, '1', Node.Mode.EXCLUSIVE", source)
        self.assertIn("node.setNumExecutors(1)", source)
        self.assertIn("node.setLabelString(agent.label)", source)
        self.assertNotIn("OHOS_AGENT_NAME", source)
        self.assertIn("label: 'signing-maintenance'", source)
        self.assertIn("OHOS_MAINTENANCE_AGENT_SECRET_FILE", source)
        self.assertIn("rwx------", source)
        self.assertIn("rw-------", source)
        self.assertIn("else if (agent.changed)", source)
        self.assertIn("agent.changed = node != null && (node.getNumExecutors() != 1", source)
        self.assertIn("!computer.isIdle()", source)
        self.assertIn("refuse configuration changes", source)
        self.assertLess(source.index("!computer.isIdle()"), source.index("new DumbSlave("))
        self.assertIn("agent.name != 'signing-maintenance'", source)
        self.assertIn("jenkins.getNode('ohos-maintenance') != null", source)
        self.assertLess(source.index("migrate the legacy"), source.index("def node = jenkins.getNode"))
        self.assertNotIn("removeNode", source)
        self.assertNotIn("setAuthorization", source)
        self.assertNotIn("getItem", source)
        self.assertNotIn("println(secret", source)

    def test_startup_requires_signing_volume_only_for_maintenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tools = root / "tools"
            work = root / "work"
            cache = root / "cache"
            signing = root / "signing"
            harmony_cache = root / "harmony-cache"
            for directory in (tools, work, cache, signing, harmony_cache):
                directory.mkdir(mode=0o700)
            node = root / "clt/tool/node/bin/node"
            node.parent.mkdir(parents=True)
            node.write_text("#!/bin/sh\nexit 0\n")
            node.chmod(0o700)
            jar = root / "clt/sdk/default/openharmony/toolchains/lib/hap-sign-tool.jar"
            jar.parent.mkdir(parents=True)
            jar.touch()
            secret = root / "agent.secret"
            secret.write_text("test-only-agent-secret")
            secret.chmod(0o600)
            commands = {
                "java": '#!/bin/bash\nif [[ "$1" == -XshowSettings:properties ]]; then echo "java.specification.version = 21"; else echo connected; fi\n',
                "curl": '#!/bin/bash\nprintf jar > "${@: -1}"\n',
                "openssl": "#!/bin/sh\nexit 0\n",
                "shasum": "#!/bin/sh\nexit 0\n",
                "unzip": "#!/bin/sh\nexit 0\n",
                "git": "#!/bin/sh\nexit 0\n",
                "flock": "#!/bin/sh\nexit 0\n",
            }
            for name, content in commands.items():
                command = tools / name
                command.write_text(content)
                command.chmod(0o700)
            (tools / "python3").symlink_to(sys.executable)
            environment = dict(os.environ, PATH=f"{tools}:{os.environ['PATH']}",
                               JENKINS_AGENT_WORKDIR=str(work), GRADLE_USER_HOME=str(cache),
                               JENKINS_AGENT_NAME="linux-build", HARMONY_GRADLE_USER_HOME=str(harmony_cache),
                               JENKINS_AGENT_SECRET_FILE=str(secret), HARMONY_CLT_HOME=str(root / "clt"))
            environment.pop("SIGNING_MAINTENANCE_ROOT", None)
            lock_directory = root / 'memory-lock'
            lock_directory.mkdir(mode=0o700)
            lock = lock_directory / 'build.lock'
            lock.touch(mode=0o600)
            environment['CI_LINUX_MEMORY_LOCK'] = str(lock)
            script = root / 'start-ohos-agent.sh'
            preflight = root / 'fixture-preflight.py'
            preflight.write_text('print("fixture preflight called")\n')
            script.write_text((ROOT / 'ohos-agent/start-ohos-agent.sh').read_text().replace(
                '/home/jenkins/linux-build-memory', str(lock_directory)).replace(
                '/usr/local/lib/ohos/clt.py', str(preflight)))
            ordinary = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(0, ordinary.returncode, ordinary.stderr)
            self.assertIn("connected", ordinary.stdout)
            environment["SIGNING_MAINTENANCE_ROOT"] = str(signing)
            environment['JENKINS_AGENT_NAME'] = 'signing-maintenance'
            environment.pop('CI_LINUX_MEMORY_LOCK')
            maintenance = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(0, maintenance.returncode, maintenance.stderr)
            signing.chmod(0o755)
            rejected = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(0, rejected.returncode)
            self.assertIn("mode 0700", rejected.stderr)
            self.assertNotIn("connected", rejected.stdout)


if __name__ == "__main__":
    unittest.main()
