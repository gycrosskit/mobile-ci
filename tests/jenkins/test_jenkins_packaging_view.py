import re
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from jenkins.sync_jenkins_jobs import JOB_SPECS


REPO_ROOT = Path(__file__).resolve().parents[2]


class JenkinsPackagingViewTest(unittest.TestCase):
    def test_view_adds_only_managed_jobs_without_resetting_user_settings(self):
        script = (REPO_ROOT / "jenkins/remote/init.groovy.d/06-configure-packaging-view.groovy").read_text()
        packaging = script.split("println('Configured packaging view", 1)[0]
        self.assertEqual([spec.full_name for spec in JOB_SPECS], re.findall(r"'(mobile-apps/sxmqlive-[a-z-]+)'", packaging))
        for fragment in ("getView('打包任务')", "!(view instanceof ListView)",
                         "throw new IllegalStateException", "new ListView('打包任务', jenkins)",
                         "item != null && !view.jobNames.contains(jobName)", "view.add(item)",
                         "getItemByFullName(jobName)", "view.setRecurse(true)", "view.save()"):
            self.assertIn(fragment, script)
        for fragment in ("removeView", "removeJob", ".clear(", "setJobNames", "setColumns",
                         "setJobFilters", "setIncludeRegex", "delete", "Credentials"):
            self.assertNotIn(fragment, script)
        for role in ('代码门禁', '管理员维护', '历史验收'):
            self.assertIn(role, script)
        for mutation in ('createProject', 'setDisabled', 'setDefinition', 'setPrimaryView'):
            self.assertNotIn(mutation, script)

    def test_view_preserves_existing_settings_with_groovy_stubs(self):
        classpath = os.environ.get("GROOVY_TEST_CLASSPATH")
        if not classpath:
            cache = Path(os.environ.get("GRADLE_USER_HOME", Path.home() / ".gradle")) / "wrapper/dists"
            for core in sorted(cache.glob("*/*/*/lib/groovy-[0-9]*.jar")):
                classpath = str(core)
                break
        if not classpath or not shutil.which("java"):
            self.skipTest("需要已有 Groovy/JDK，不自动下载")
        result = subprocess.run(["java", "-cp", classpath, "groovy.ui.GroovyMain",
                                 str(Path(__file__).with_suffix(".groovy")),
                                 str(REPO_ROOT / "jenkins/remote/init.groovy.d/06-configure-packaging-view.groovy")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("Packaging view self-check passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
