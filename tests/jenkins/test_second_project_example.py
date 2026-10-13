"""公开示例必须在无私有应用/凭据的临时双仓中完整运行，缺工具必须失败。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tests.jenkins.test_pr_validation import groovy_classpath

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/second-project"


class SecondProjectExampleTest(unittest.TestCase):
    def test_public_example_runs_real_entrypoints_without_private_application(self):
        classpath = groovy_classpath()
        self.assertTrue(classpath, "需要已安装 Groovy；本验收不允许 skip")
        environment = dict(os.environ, GROOVY_TEST_CLASSPATH=classpath,
                           APP_SOURCE_ROOT="/nonexistent-private-app", CI_PROJECT_ID="untrusted",
                           JOB_NAME="production/foreign", PGYER_API_KEY="must-not-be-consumed",
                           DINGTALK_WEBHOOK="must-not-be-consumed")
        result = subprocess.run([sys.executable, str(EXAMPLE / "validate.py")], env=environment,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("PASS: second-project", result.stdout)
        self.assertIn("SDK build: NOT executed; APK is explicitly synthetic", result.stdout)
        self.assertIn("Vendor calls: 0; production credential reads: 0; Jenkins deployment: 0.", result.stdout)

    def test_missing_groovy_is_an_explicit_failure(self):
        result = subprocess.run([sys.executable, str(EXAMPLE / "validate.py")],
                                env=dict(os.environ, GROOVY_TEST_CLASSPATH=""),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(2, result.returncode)
        self.assertIn("缺少 GROOVY_TEST_CLASSPATH", result.stderr)
        self.assertNotIn("PASS:", result.stdout)

    def test_example_is_android_only_and_has_no_real_signing_material(self):
        profile = json.loads((EXAMPLE / "project.json").read_text())
        self.assertTrue(profile["testOnly"])
        self.assertEqual(["android"], list(profile["platforms"]))
        self.assertEqual("main", profile["repositories"]["application"]["defaultBranch"])
        self.assertEqual("gradle", profile["platforms"]["android"]["adapter"])
        brands = json.loads((EXAMPLE / "application/ci/brands.json").read_text())
        self.assertEqual("0" * 64, brands["example"]["android"]["signing"]["certificateSha256"])
        self.assertEqual([], list(EXAMPLE.rglob("*.keystore")))
        self.assertEqual([], list(EXAMPLE.rglob("*.jks")))


if __name__ == "__main__":
    unittest.main()
