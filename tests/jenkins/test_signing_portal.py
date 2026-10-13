import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class SigningPortalTest(unittest.TestCase):
    def test_request_validation_and_partial_progress(self):
        classpath = os.environ.get("GROOVY_TEST_CLASSPATH")
        if not classpath:
            cache = Path(os.environ.get("GRADLE_USER_HOME", Path.home() / ".gradle")) / "wrapper/dists"
            for core in sorted(cache.glob("*/*/*/lib/groovy-[0-9]*.jar")):
                json_jar = core.with_name(core.name.replace("groovy-", "groovy-json-"))
                if json_jar.is_file():
                    classpath = str(core) + os.pathsep + str(json_jar)
                    break
        if not classpath or not shutil.which("java"):
            self.skipTest("需要已有 Groovy/JDK，不自动下载")
        result = subprocess.run(
            ["java", "-Dci.project.fixture=" + str(ROOT / "tests/fixtures/legacy-project.json"), "-cp", classpath, "groovy.ui.GroovyMain",
             str(ROOT / "tests/jenkins/groovy/signing_portal_stubs.groovy"),
             str(ROOT / "jenkins/controller/init.groovy.d/05-signing-portal.groovy"),
             str(ROOT / "tests/jenkins/groovy/signing_portal_test.groovy")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn('"ok":true', result.stdout)

    def test_frontend_behavior(self):
        if not shutil.which("node"):
            self.skipTest("需要已有 Node，不自动下载")
        result = subprocess.run(["node", "--test", str(ROOT / "tests/jenkins/test_signing_portal_ui.cjs")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
