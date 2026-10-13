import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from project import TOOL_ROOT
from signing import signing_maintenance as module


class SigningMaintenancePipelineTest(unittest.TestCase):
    def test_both_platforms_use_fixed_linux_maintenance_node_and_root(self):
        pipeline = (TOOL_ROOT / "jenkins/Jenkinsfile.signing.maintenance").read_text()
        for value in ("agent { label 'signing-maintenance' }", "nodeName != 'signing-maintenance'",
                      "def expectedNode = 'signing-maintenance'", "def expectedRoot = '/home/jenkins/signing'",
                      "def path = '/home/jenkins/signing'", "def signingRoot = '/home/jenkins/signing'"):
            self.assertIn(value, pipeline)
        for value in ("android-mac", "ios-mac", "ohos-maintenance", "/Users/guoyang/"):
            self.assertNotIn(value, pipeline)

    def test_credential_cas_entries_share_provider_monitor(self):
        pipeline = (TOOL_ROOT / "jenkins/Jenkinsfile.signing.maintenance").read_text()
        for source in (pipeline, module.credentials.REFRESH_GROOVY):
            self.assertIn("CredentialsProvider.lookupStores(", source)
            self.assertIn("it.context == provider", source)
            self.assertNotIn("SystemCredentialsProvider", source)
            self.assertIn("synchronized (provider)", source)
            self.assertNotIn("synchronized (store)", source)

    def test_controller_helper_executes_cas_authority_and_rollback_with_groovy_stubs(self):
        classpath = os.environ.get("GROOVY_TEST_CLASSPATH")
        if not classpath:
            cache = Path(os.environ.get("GRADLE_USER_HOME", Path.home() / ".gradle")) / "wrapper/dists"
            for core in sorted(cache.glob("*/*/*/lib/groovy-[0-9]*.jar")):
                json_jar = core.with_name(core.name.replace("groovy-", "groovy-json-"))
                if json_jar.is_file():
                    classpath = str(core) + os.pathsep + str(json_jar)
                    break
        if not classpath or not shutil.which("java"):
            self.skipTest("需要已有 Groovy/JDK；可设置 GROOVY_TEST_CLASSPATH，不自动下载")
        with TemporaryDirectory() as temporary:
            from signing import ios_jenkins_credentials
            scripts = [ios_jenkins_credentials.GROOVY, module.credentials.REFRESH_GROOVY]
            paths = [Path(temporary) / (str(index) + ".groovy") for index in range(len(scripts))]
            for path, source in zip(paths, scripts):
                path.write_text(source)
            result = subprocess.run(["java", "-cp", classpath, "groovy.ui.GroovyMain",
                                     str(TOOL_ROOT / "tests/jenkins/fixtures/signing_maintenance_controller.groovy"),
                                     str(TOOL_ROOT / "jenkins/Jenkinsfile.signing.maintenance"),
                                     str(TOOL_ROOT / "jenkins/remote/init.groovy.d/05-configure-ohos-agent.groovy"),
                                     str(TOOL_ROOT / "jenkins/remote/init.groovy.d/01-configure-android-agent.groovy"),
                                     *[str(path) for path in paths]],
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("52 behavioral checks passed", result.stdout)

    def test_dual_checkout_keeps_application_context_and_pins_automation(self):
        pipeline = (TOOL_ROOT / "jenkins/Jenkinsfile.signing.maintenance").read_text()
        self.assertIn("CI_AUTOMATION_COMMIT = '__CI_AUTOMATION_COMMIT__'", pipeline)
        self.assertIn("env.CI_AUTOMATION_COMMIT ==~ /[0-9a-f]{40}/", pipeline)
        self.assertIn("branches: [[name: env.CI_AUTOMATION_COMMIT]]", pipeline)
        self.assertIn("branches: [[name: 'refs/heads/master']]", pipeline)
        self.assertIn('APP_SOURCE_ROOT = "${WORKSPACE}/source"', pipeline)
        self.assertIn('PYTHONPATH = "${WORKSPACE}/automation"', pipeline)
        self.assertEqual(3, pipeline.count("dir('source')"))
        self.assertIn('env.NODE_NAME, env.APP_SOURCE_ROOT, target.brand', pipeline)
        self.assertNotIn('env.NODE_NAME, env.WORKSPACE, target.brand', pipeline)
        self.assertNotIn('ci/scripts/', pipeline)
