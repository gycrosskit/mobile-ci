import os
import hashlib
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from project_config import load_project
from jenkins import sync_jenkins_jobs
from tests.jenkins.test_pr_validation import groovy_classpath


REPO_ROOT = Path(__file__).resolve().parents[2]


class SyncJenkinsJobsTest(unittest.TestCase):
    def setUp(self):
        self.profile = load_project()
        patched = patch.dict(os.environ, CI_AUTOMATION_COMMIT="a" * 40)
        patched.start()
        self.addCleanup(patched.stop)

    def test_selectors_use_exact_script_approvals_without_global_query_permissions(self):
        approval = (REPO_ROOT / "jenkins/controller/init.groovy.d/03-configure-script-approvals.groovy").read_text()
        managed = approval.split('// BEGIN MANAGED SELECTOR HASHES\n')[1].split('// END MANAGED SELECTOR HASHES')[0]
        approved = set(re.findall(r"'SHA512:[0-9a-f]{128}'", managed))
        expected = set()
        for spec, source in sync_jenkins_jobs.render_all().items():
            with self.subTest(job=spec.name):
                selector = ElementTree.fromstring(source).find(".//org.biouno.unochoice.CascadeChoiceParameter")
                script = selector.findtext("script/secureScript/script")
                self.assertEqual("false", selector.findtext("script/secureScript/sandbox"))
                self.assertEqual("true", selector.findtext("script/secureFallbackScript/sandbox"))
                self.assertIn(self.profile["repositories"]["application"]["ssh"], script)
                self.assertIn("'" + self.profile["credentials"]["scm"] + "'", script)
                self.assertIn("[], null, null, []", script)
                self.assertNotIn("new hudson.plugins.git.BranchSpec", script)
                self.assertIn('job.checkPermission(hudson.model.Item.BUILD)', script)
                self.assertIn('SafeHtmlExtendedMarkupFormatter.INSTANCE.translate', script)
                expected.add("'SHA512:" + hashlib.sha512(('groovy:' + script).encode()).hexdigest() + "'")
        self.assertEqual(6, len(approved))
        self.assertEqual(expected, approved)
        self.assertIn('approval.setApprovedSignatures(', approval)
        self.assertEqual([
            'new groovy.json.JsonSlurperClassic',
            'method groovy.json.JsonSlurperClassic parseText java.lang.String',
        ], re.findall(r"approveSignature\('([^']+)'\)", approval))
        self.assertNotIn("approveAll", approval)

    def test_changed_job_repository_credentials_or_code_are_not_approved(self):
        approval = (REPO_ROOT / 'jenkins/controller/init.groovy.d/03-configure-script-approvals.groovy').read_text()
        approved = set(re.findall(r'SHA512:[0-9a-f]{128}', approval))
        for spec, source in sync_jenkins_jobs.render_all().items():
            script = ElementTree.fromstring(source).findtext('.//org.biouno.unochoice.CascadeChoiceParameter/script/secureScript/script')
            changes = {
                'job': script.replace(spec.full_name, 'private/other-job'),
                'repository': script.replace(self.profile['repositories']['application']['ssh'], 'git@gitee.com:example/other-private.git'),
                'credential': script.replace("'" + self.profile["credentials"]["scm"] + "'", "'other-credential'"),
                'code': script + '\nreturn Jenkins.get()',
            }
            for field, changed in changes.items():
                with self.subTest(job=spec.name, changed=field):
                    self.assertNotEqual(script, changed)
                    self.assertNotIn('SHA512:' + hashlib.sha512(('groovy:' + changed).encode()).hexdigest(), approved)

    def test_approval_migration_revokes_only_selector_signatures_and_is_idempotent(self):
        classpath = groovy_classpath()
        self.assertTrue(classpath and shutil.which('java'), '审批迁移验证必须具备已有 Groovy/JDK')
        policy = (REPO_ROOT / 'jenkins/controller/init.groovy.d/03-configure-script-approvals.groovy').read_text()
        guard = next(line for line in policy.splitlines(keepends=True) if 'assert !approval.aclApprovedSignatures.any' in line)
        revoke = next(line for line in policy.splitlines(keepends=True) if 'approval.setApprovedSignatures(' in line)
        cases = {
            'reviewed': (policy, None),
            'guard removed': (policy.replace(guard, ''), 'Expected extra ACL approval to block migration'),
            'guard too late': (policy.replace(guard, '').replace(revoke, revoke + guard),
                               'Blocked migration changed global approvals'),
        }
        with tempfile.TemporaryDirectory() as directory:
            for case, (source, failure) in cases.items():
                with self.subTest(case=case):
                    path = Path(directory) / 'approvals.groovy'
                    path.write_text(source)
                    result = subprocess.run(['java', '-cp', classpath, 'groovy.ui.GroovyMain',
                                             str(REPO_ROOT / 'tests/jenkins/fixtures/selector_approvals.groovy'), str(path)],
                                            capture_output=True, text=True, timeout=30)
                    if failure:
                        self.assertNotEqual(0, result.returncode)
                        self.assertIn(failure, result.stderr)
                    else:
                        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
                        self.assertIn('Selector approval migration passed', result.stdout)

    def test_delivery_defaults_and_optional_controls_are_preserved(self):
        approval = (REPO_ROOT / "jenkins/controller/init.groovy.d/03-configure-script-approvals.groovy").read_text()
        for spec, source in sync_jenkins_jobs.render_all().items():
            with self.subTest(job=spec.name):
                root = ElementTree.fromstring(source)
                parameters = {node.findtext("name"): node for node in root.find(".//parameterDefinitions")}
                self.assertNotIn("DRY_RUN", parameters)
                self.assertNotIn("NOTIFY", parameters)
                self.assertEqual("hudson.model.BooleanParameterDefinition", parameters["UPLOAD_AND_NOTIFY"].tag)
                self.assertEqual("true", parameters["UPLOAD_AND_NOTIFY"].findtext("defaultValue"))
                if spec.platform == "harmony":
                    self.assertNotIn("UPDATE_DESCRIPTION", parameters)
                    self.assertNotIn("可选设置", parameters)
                    continue
                self.assertEqual("", parameters["UPDATE_DESCRIPTION"].findtext("defaultValue"))
                self.assertEqual("", parameters["WORK_ITEM_TYPE"].findtext("choices/a/string"))
                for name in ("WORK_ITEM_ID", "WORK_ITEM_URL"):
                    self.assertEqual("", parameters[name].findtext("defaultValue"))
                optional = parameters["可选设置"]
                self.assertEqual("true", optional.findtext("omitValueField"))
                self.assertEqual("", optional.findtext("referencedParameters"))
                script = optional.findtext("script/secureScript/script")
                digest = hashlib.sha512(("groovy:" + script).encode()).hexdigest()
                self.assertIn("approveScript('SHA512:" + digest + "')", approval)
                # 审批只覆盖静态 HTML/CSS，不能随参数值拼入可执行内容。
                self.assertTrue(script.startswith("return '''<details"))
                self.assertTrue(script.endswith("</style>'''"))
                self.assertNotIn("<script", script)
                self.assertNotIn("${", script)
                self.assertEqual(
                    ["UPDATE_DESCRIPTION", "WORK_ITEM_TYPE", "WORK_ITEM_ID", "WORK_ITEM_URL"],
                    re.findall(r'\[value="([A-Z_]+)"\]', script),
                )
                self.assertIn('<details id="sxmqlive-optional-settings">', script)

    def test_defines_all_platform_test_and_formal_jobs(self):
        self.assertEqual(
            [
                "sxmqlive-android-test",
                "sxmqlive-android-formal-release",
                "sxmqlive-ios-test",
                "sxmqlive-ios-formal-release",
                "sxmqlive-ohos-test",
                "sxmqlive-ohos-formal-release",
            ],
            [spec.name for spec in sync_jenkins_jobs.JOB_SPECS],
        )

    def test_rendered_configs_bind_all_three_job_name_locations(self):
        configs = sync_jenkins_jobs.render_all()

        self.assertEqual(6, len(configs))
        for spec, source in configs.items():
            self.assertNotIn("__JOB_NAME__", source)
            self.assertNotIn("-next", source)
            self.assertEqual(3, source.count(spec.name))
            root = ElementTree.fromstring(source)
            self.assertEqual(spec.name, root.findtext(".//projectName"))
            self.assertEqual(spec.full_name, root.findtext(".//projectFullName"))
            self.assertIn(
                "def jobName = '{}'".format(spec.full_name),
                root.findtext(".//secureScript/script"),
            )

    def test_sync_reads_and_stages_only_mobile_apps_jobs(self):
        spec = sync_jenkins_jobs.JOB_SPECS[0]
        desired = sync_jenkins_jobs.render_all()[spec]
        folder = '<com.cloudbees.hudson.plugins.folder.Folder/>'
        with patch.object(sync_jenkins_jobs, "read_container_file", side_effect=[folder, desired]) as read:
            self.assertEqual([], sync_jenkins_jobs.inspect_jobs("controller", {spec: desired})[0])
        self.assertEqual([("controller", "/var/jenkins_home/jobs/mobile-apps/config.xml"),
                          ("controller", "/var/jenkins_home/jobs/mobile-apps/jobs/" + spec.name + "/config.xml")],
                         [call.args for call in read.call_args_list])
        with patch.object(sync_jenkins_jobs, "_docker") as docker:
            target = sync_jenkins_jobs._stage_config("controller", spec, Path("/tmp/job.xml"))
        self.assertEqual("/var/jenkins_home/jobs/mobile-apps/jobs/" + spec.name + "/config.xml.codex-job-sync-new", target)
        self.assertFalse(any("/var/jenkins_home/jobs/" + spec.name in str(call.args) for call in docker.call_args_list))
        for missing in (None, '<flow-definition/>'):
            with self.subTest(folder=missing), patch.object(sync_jenkins_jobs, "read_container_file", return_value=missing) as read:
                with self.assertRaisesRegex(sync_jenkins_jobs.JenkinsJobSyncError, "缺少 mobile-apps Folder"):
                    sync_jenkins_jobs.inspect_jobs("controller", {spec: desired})
                self.assertEqual(1, read.call_count)

    def test_harmony_job_uses_fixed_master_and_existing_brand_branch_selector(self):
        spec = next(spec for spec in sync_jenkins_jobs.JOB_SPECS if spec.name == 'sxmqlive-ohos-test')
        root = ElementTree.fromstring(sync_jenkins_jobs.render_all()[spec])
        self.assertEqual('鸿蒙 QA 测试包', root.findtext('displayName'))
        build_type = next(node for node in root.findall('.//parameterDefinitions/*') if node.findtext('name') == 'BUILD_TYPE')
        self.assertEqual(['QA', 'Debug'], [node.text for node in build_type.findall('choices/a/string')])
        self.assertEqual('a' * 40, root.findtext('definition/scm/branches/hudson.plugins.git.BranchSpec/name'))
        self.assertEqual('jenkins/Jenkinsfile.ohos.test', root.findtext('definition/scriptPath'))
        selector = root.find('.//org.biouno.unochoice.CascadeChoiceParameter')
        self.assertEqual('BRAND,BRANCH_TYPE', selector.findtext('referencedParameters'))
        self.assertEqual('false', selector.findtext('script/secureScript/sandbox'))
        script = selector.findtext('script/secureScript/script')
        for fragment in ('"feature/${brand}/"', '"dev/${brand}"', '"release/${brand}-"'):
            self.assertIn(fragment, script)
        self.assertIn("def allowedBrands = ['cihang']", script)

    def test_harmony_formal_job_uses_brand_tag_and_fixed_release(self):
        spec = next(spec for spec in sync_jenkins_jobs.JOB_SPECS if spec.name == 'sxmqlive-ohos-formal-release')
        root = ElementTree.fromstring(sync_jenkins_jobs.render_all()[spec])
        parameters = {node.findtext('name'): node for node in root.find('.//parameterDefinitions')}
        self.assertEqual('鸿蒙 Release 正式包', root.findtext('displayName'))
        self.assertEqual(['Release'], [node.text for node in parameters['BUILD_TYPE'].findall('choices/a/string')])
        self.assertNotIn('BRANCH_TYPE', parameters)
        self.assertNotIn('GIT_BRANCH', parameters)
        self.assertEqual('a' * 40, root.findtext('definition/scm/branches/hudson.plugins.git.BranchSpec/name'))
        self.assertEqual('jenkins/Jenkinsfile.ohos.test', root.findtext('definition/scriptPath'))
        selector = parameters['GIT_TAG']
        self.assertEqual('BRAND', selector.findtext('referencedParameters'))
        self.assertEqual('false', selector.findtext('script/secureScript/sandbox'))
        script = selector.findtext('script/secureScript/script')
        self.assertIn("def allowedBrands = ['cihang']", script)
        self.assertIn('"${brand}-ohos-v"', script)
        self.assertIn("'PT_TAG'", script)

    def test_render_rejects_missing_job_name_placeholder(self):
        spec = sync_jenkins_jobs.JOB_SPECS[0]
        template = (
            sync_jenkins_jobs.TEMPLATE_DIR / spec.template_name
        ).read_text(encoding="utf-8")

        with self.assertRaises(sync_jenkins_jobs.JenkinsJobSyncError):
            sync_jenkins_jobs.render_job_config(
                template.replace("__JOB_NAME__", spec.name, 1),
                spec,
            )

    def test_equivalence_ignores_only_jenkins_runtime_metadata(self):
        spec = sync_jenkins_jobs.JOB_SPECS[0]
        desired = sync_jenkins_jobs.render_all()[spec]
        root = ElementTree.fromstring(desired)
        root.set("plugin", "workflow-job@runtime")
        actions = root.find("actions")
        ElementTree.SubElement(actions, "runtimeAction").set("plugin", "runtime@1")
        cache = root.find(".//parameters[@class='linked-hash-map']")
        entry = ElementTree.SubElement(cache, "entry")
        ElementTree.SubElement(entry, "string").text = "BRAND"
        ElementTree.SubElement(entry, "string").text = "kangmin"
        properties = root.find("properties")
        ElementTree.SubElement(
            properties,
            "org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty",
        )
        current = ElementTree.tostring(root, encoding="unicode")

        self.assertTrue(
            sync_jenkins_jobs.configs_equivalent(desired, current, spec.name)
        )

    def test_equivalence_detects_temporary_next_job_name(self):
        spec = sync_jenkins_jobs.JOB_SPECS[0]
        desired = sync_jenkins_jobs.render_all()[spec]
        current = desired.replace(spec.name, "{}-next".format(spec.name))

        self.assertFalse(
            sync_jenkins_jobs.configs_equivalent(desired, current, spec.name)
        )

    def test_parameter_sync_preserves_current_authorization_in_staged_xml(self):
        spec = sync_jenkins_jobs.JOB_SPECS[0]
        desired = sync_jenkins_jobs.render_all()[spec]
        root = ElementTree.fromstring(desired)
        auth = ElementTree.SubElement(root.find("properties"), "hudson.security.AuthorizationMatrixProperty")
        ElementTree.SubElement(auth, "permission").text = "hudson.model.Item.Build:release-team"
        ElementTree.SubElement(auth, "inheritanceStrategy", {"class": "org.jenkinsci.plugins.matrixauth.inheritance.InheritParentStrategy"})
        root.find("properties").remove(auth)
        root.find("properties").insert(0, auth)
        current = ElementTree.tostring(root, encoding="unicode")
        self.assertTrue(sync_jenkins_jobs.configs_equivalent(desired, current, spec.name))
        next(node for node in root.findall(".//parameterDefinitions/*") if node.findtext("name") == "UI_ENGINE").find("choices/a/string").text = "kuikly"
        current = ElementTree.tostring(root, encoding="unicode")
        self.assertFalse(sync_jenkins_jobs.configs_equivalent(desired, current, spec.name))

        def check_stage(container, selected_spec, path):
            staged = ElementTree.parse(path).getroot()
            self.assertEqual(ElementTree.tostring(auth), ElementTree.tostring(staged.find("properties/" + auth.tag)))
            self.assertEqual("cmp", next(node for node in staged.findall(".//parameterDefinitions/*") if node.findtext("name") == "UI_ENGINE").findtext("choices/a/string"))
            return "/tmp/staged-config.xml"

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(sync_jenkins_jobs, "ensure_controller_running"), \
                patch.object(sync_jenkins_jobs, "inspect_jobs", side_effect=[([spec], {spec: current}), ([], {})]), \
                patch.object(sync_jenkins_jobs, "assert_controller_idle"), \
                patch.object(sync_jenkins_jobs, "_stage_config", side_effect=check_stage), \
                patch.object(sync_jenkins_jobs, "_docker"), \
                patch.object(sync_jenkins_jobs, "_wait_until_ready"):
            sync_jenkins_jobs.apply_configs("controller", Path(directory), {spec: desired}, 10)

    def test_parameter_sync_preserves_enabled_and_disabled_jobs_with_or_without_acl(self):
        spec = sync_jenkins_jobs.JOB_SPECS[0]
        for disabled in ("true", "false", None):
            for with_acl in (True, False):
                with self.subTest(disabled=disabled, with_acl=with_acl):
                    desired_root = ElementTree.fromstring(sync_jenkins_jobs.render_all()[spec])
                    desired_root.find("disabled").text = "false" if disabled == "true" else "true"
                    desired = ElementTree.tostring(desired_root, encoding="unicode")
                    current_root = ElementTree.fromstring(desired)
                    if disabled is None:
                        current_root.remove(current_root.find("disabled"))
                    else:
                        current_root.find("disabled").text = disabled
                    if with_acl:
                        acl = ElementTree.SubElement(current_root.find("properties"), "hudson.security.AuthorizationMatrixProperty")
                        ElementTree.SubElement(acl, "permission").text = "hudson.model.Item.Build:release-team"
                    current = ElementTree.tostring(current_root, encoding="unicode")
                    self.assertTrue(sync_jenkins_jobs.configs_equivalent(desired, current, spec.name))
                    current_root.find("definition/scm/branches/hudson.plugins.git.BranchSpec/name").text = "b" * 40
                    state = {"xml": ElementTree.tostring(current_root, encoding="unicode")}

                    def read(container, path):
                        return '<com.cloudbees.hudson.plugins.folder.Folder/>' if path.endswith("/mobile-apps/config.xml") else state["xml"]

                    def docker(args, **kwargs):
                        if args[0] == "cp":
                            state["staged"] = Path(args[1]).read_text()
                        elif "mv" in args:
                            state["xml"] = state["staged"]

                    with tempfile.TemporaryDirectory() as directory, \
                            patch.object(sync_jenkins_jobs, "ensure_controller_running"), \
                            patch.object(sync_jenkins_jobs, "read_container_file", side_effect=read), \
                            patch.object(sync_jenkins_jobs, "assert_controller_idle"), \
                            patch.object(sync_jenkins_jobs, "_docker", side_effect=docker), \
                            patch.object(sync_jenkins_jobs, "_wait_until_ready"):
                        sync_jenkins_jobs.apply_configs("controller", Path(directory), {spec: desired}, 10)
                        self.assertEqual([], sync_jenkins_jobs.inspect_jobs("controller", {spec: desired})[0])
                    staged = ElementTree.fromstring(state["staged"])
                    self.assertEqual(disabled, staged.findtext("disabled"))
                    self.assertEqual(with_acl, staged.find("properties/hudson.security.AuthorizationMatrixProperty") is not None)
                    self.assertEqual("a" * 40, staged.findtext("definition/scm/branches/hudson.plugins.git.BranchSpec/name"))

    def test_new_job_keeps_template_enabled_state(self):
        for disabled in ("true", "false"):
            desired = "<flow-definition><disabled>" + disabled + "</disabled></flow-definition>"
            self.assertEqual(desired, sync_jenkins_jobs.preserve_job_authorization(desired, None, "new-job"))

    def test_equivalence_accepts_jenkins_omitting_scm_browser(self):
        spec = sync_jenkins_jobs.JOB_SPECS[0]
        desired = sync_jenkins_jobs.render_all()[spec]
        root = ElementTree.fromstring(desired)
        scm = root.find("definition/scm")
        scm.remove(scm.find("browser"))
        current = ElementTree.tostring(root, encoding="unicode")

        self.assertTrue(
            sync_jenkins_jobs.configs_equivalent(desired, current, spec.name)
        )

    def test_equivalence_detects_changed_scm_browser(self):
        spec = sync_jenkins_jobs.JOB_SPECS[0]
        desired = sync_jenkins_jobs.render_all()[spec]
        root = ElementTree.fromstring(desired)
        root.find("definition/scm/browser/url").text = "https://example.invalid/repo"
        current = ElementTree.tostring(root, encoding="unicode")

        self.assertFalse(
            sync_jenkins_jobs.configs_equivalent(desired, current, spec.name)
        )

    def test_equivalence_does_not_ignore_other_omitted_scm_browsers(self):
        spec = sync_jenkins_jobs.JOB_SPECS[0]
        root = ElementTree.fromstring(sync_jenkins_jobs.render_all()[spec])
        scm = root.find("definition/scm")
        browser = scm.find("browser")
        browser.set("class", "hudson.plugins.git.browser.GitLab")
        desired = ElementTree.tostring(root, encoding="unicode")
        scm.remove(browser)
        current = ElementTree.tostring(root, encoding="unicode")

        self.assertFalse(
            sync_jenkins_jobs.configs_equivalent(desired, current, spec.name)
        )

    def test_render_command_writes_importable_xml(self):
        configs = sync_jenkins_jobs.render_all()
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            sync_jenkins_jobs.render_to_directory(output_dir, configs)

            self.assertEqual(
                {"{}.xml".format(spec.name) for spec in sync_jenkins_jobs.JOB_SPECS},
                {path.name for path in output_dir.iterdir()},
            )
            for path in output_dir.iterdir():
                self.assertEqual("flow-definition", ElementTree.parse(path).getroot().tag)


if __name__ == "__main__":
    unittest.main()
