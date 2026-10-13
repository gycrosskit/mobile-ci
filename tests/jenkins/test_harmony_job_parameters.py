"""用已有 Groovy 执行真实参数段和 Tag selector；替身校验应用 SCM 输入。"""

import os
from pathlib import Path
import shutil
import subprocess
import unittest

from project_config import load_project

ROOT = Path(__file__).resolve().parents[2]


class HarmonyJobParametersTest(unittest.TestCase):
    def test_actual_parameters_and_active_choices_with_groovy_stubs(self):
        classpath = os.environ.get("GROOVY_TEST_CLASSPATH")
        if not classpath:
            cache = Path(os.environ.get("GRADLE_USER_HOME", Path.home() / ".gradle")) / "wrapper/dists"
            for core in sorted(cache.glob("*/*/*/lib/groovy-[0-9]*.jar")):
                classpath = str(core)
                break
        if not classpath or not shutil.which("java"):
            self.skipTest("需要已有 Groovy/JDK，不自动下载；真实 Jenkins 插件兼容另行验收")
        profile = load_project()
        result = subprocess.run(["java", "-Dci.test.applicationSsh=" + profile["repositories"]["application"]["ssh"],
                                 "-Dci.test.scmCredential=" + profile["credentials"]["scm"], "-cp", classpath, "groovy.ui.GroovyMain",
                                 str(Path(__file__).with_suffix(".groovy")),
                                 str(ROOT / "jenkins/Jenkinsfile.ohos.test"),
                                 str(ROOT / "jenkins/local/ohos-release-job.xml.template")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("Harmony Job parameters self-check passed", result.stdout)
        self.assertIn("Harmony formal Tag selector self-check passed", result.stdout)
