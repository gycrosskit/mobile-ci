import os
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from jenkins import sync_jenkins_jobs as jobs
from jenkins import sync_jenkins_admin_jobs as admin


class CiRepositoryContractTest(unittest.TestCase):
    def test_job_rendering_rejects_missing_or_floating_automation_revision(self):
        for revision in ("", "master", "*/master", "a" * 39, "g" * 40):
            with self.subTest(revision=revision), patch.dict(os.environ, CI_AUTOMATION_COMMIT=revision):
                with self.assertRaises(jobs.JenkinsJobSyncError):
                    jobs.render_all()
                with self.assertRaises(jobs.JenkinsJobSyncError):
                    admin.render_all()

    def test_packaging_selectors_query_application_not_automation(self):
        with patch.dict(os.environ, CI_AUTOMATION_COMMIT="a" * 40):
            for spec, config in jobs.render_all().items():
                root = ET.fromstring(config)
                self.assertEqual("git@gitee.com:example/legacy-automation.git",
                                 root.findtext("definition/scm/userRemoteConfigs/hudson.plugins.git.UserRemoteConfig/url"))
                self.assertEqual("a" * 40, root.findtext("definition/scm/branches/hudson.plugins.git.BranchSpec/name"))
                scripts = root.findall(".//org.biouno.unochoice.CascadeChoiceParameter/script/secureScript/script")
                self.assertEqual(1, len(scripts), spec.name)
                self.assertIn("sxmqlive-android.git", scripts[0].text)
                self.assertNotIn("job.definition.scm", scripts[0].text)
                self.assertEqual("false", root.findtext("definition/lightweight"))

    def test_inline_maintenance_is_pinned_before_approval_hash_is_computed(self):
        with patch.dict(os.environ, CI_AUTOMATION_COMMIT="a" * 40):
            configs = admin.render_all()
        config = next(value for spec, value in configs.items() if spec.name == "sxmqlive-signing-maintenance")
        script = ET.fromstring(config).findtext("definition/script")
        self.assertIn("CI_AUTOMATION_COMMIT = '" + "a" * 40 + "'", script)
        self.assertNotIn("__CI_AUTOMATION_COMMIT__", config)
