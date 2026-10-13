import os
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from jenkins import sync_jenkins_admin_jobs as module
from project_config import load_project


class SyncJenkinsAdminJobsTest(unittest.TestCase):
    def setUp(self):
        patched = patch.dict(os.environ, CI_AUTOMATION_COMMIT="a" * 40)
        patched.start()
        self.addCleanup(patched.stop)

    def test_selected_job_never_inspects_or_applies_other_jobs(self):
        for command in ("check", "apply"):
            with self.subTest(command=command), patch.object(module.shared, "ensure_controller_running"), \
                    patch.object(module, "inspect", return_value=([], {})) as inspect, \
                    patch.object(module, "apply") as apply, \
                    patch.object(module, "signing_script_approval_hash") as signing_hash:
                self.assertEqual(0, module.main([command, "--job", "sxmqlive-wiki-navigation"]))
                called = inspect if command == "check" else apply
                configs = called.call_args.args[1]
                self.assertEqual(["sxmqlive-wiki-navigation"], [spec.name for spec in configs])
                signing_hash.assert_not_called()
                (apply if command == "check" else inspect).assert_not_called()

    def test_renders_distinct_admin_jobs(self):
        configs = module.render_all()
        self.assertEqual({"sxmqlive-signing-maintenance", "sxmqlive-signing-backup", "sxmqlive-pr-validation", "sxmqlive-ci-pr-validation", "Jenkins 失败告警验收", "sxmqlive-wiki-navigation"},
                         {spec.name for spec in configs})
        self.assertNotIn("iOS 测试设备管理", [spec.name for spec in configs])
        for spec, source in configs.items():
            root = ElementTree.fromstring(source)
            self.assertTrue((root.findtext("displayName") or "").strip())
            if spec.name in ("sxmqlive-signing-maintenance", "sxmqlive-signing-backup"):
                self.assertEqual("org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition", root.find("definition").get("class"))
                self.assertEqual("false", root.findtext("definition/sandbox"))
                self.assertEqual(module.shared.pin_ci_config((module.REPO_ROOT / spec.script_path).read_text()), root.findtext("definition/script"))
            elif spec.name == "sxmqlive-ci-pr-validation":
                self.assertEqual("org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition", root.find("definition").get("class"))
                self.assertEqual("true", root.findtext("definition/sandbox"))
                self.assertEqual(module.shared.pin_ci_config((module.REPO_ROOT / spec.script_path).read_text()), root.findtext("definition/script"))
                self.assertEqual([], root.findall(".//scm"))
            else:
                self.assertEqual(spec.script_path, root.findtext("definition/scriptPath"))

    def test_signing_inline_templates_pin_both_automation_checkouts(self):
        configs = module.render_all()
        for spec, config in configs.items():
            if spec.name not in ("sxmqlive-signing-maintenance", "sxmqlive-signing-backup"):
                continue
            with self.subTest(job=spec.name):
                source = ElementTree.fromstring(config).findtext("definition/script")
                self.assertIn("CI_AUTOMATION_COMMIT = '" + "a" * 40 + "'", source)
                self.assertNotIn("__CI_AUTOMATION_COMMIT__", source)
                self.assertIn("branches: [[name: env.CI_AUTOMATION_COMMIT]]", source)
                self.assertIn(load_project()["repositories"]["automation"]["ssh"], source)
                self.assertIn(load_project()["repositories"]["application"]["ssh"], source)
                self.assertIn("load(env.CI_TOOL_ROOT + '/jenkins/signing-backup.groovy')", source)
                module.assert_signing_authorization(spec, config)

    def test_backup_is_admin_only_and_uses_existing_maintenance_node(self):
        spec = next(item for item in module.JOB_SPECS if item.name == "sxmqlive-signing-backup")
        config = module.render_all()[spec]
        root = ElementTree.fromstring(config)
        self.assertEqual("运维", spec.folder)
        self.assertEqual("H/10 * * * *", root.findtext("triggers/hudson.triggers.TimerTrigger/spec"))
        source = root.findtext("definition/script")
        self.assertIn("agent { label 'signing-maintenance' }", source)
        self.assertIn("refs/heads/master", source)
        self.assertIn("backup.publish()", source)
        module.assert_signing_authorization(spec, config)
        with self.assertRaises(module.shared.JenkinsJobSyncError):
            module.assert_signing_authorization(spec, "<flow-definition/>")
        current = ElementTree.fromstring(config)
        self.assertEqual(module.signing_script_approval_hash(config), module.signing_script_approval_hash(
            ElementTree.tostring(current, encoding="unicode")))

    def test_maintenance_is_flat_in_mobile_apps_and_root_admin_jobs_stay_put(self):
        specs = {spec.name: spec for spec in module.JOB_SPECS}
        signing = specs["sxmqlive-signing-maintenance"]
        self.assertEqual("mobile-apps", signing.folder)
        self.assertEqual("签名与设备维护", ElementTree.fromstring(module.render_all()[signing]).findtext("displayName"))
        self.assertEqual("/var/jenkins_home/jobs/mobile-apps/jobs/sxmqlive-signing-maintenance/config.xml",
                         module._job_path(signing))
        self.assertEqual("", specs["sxmqlive-pr-validation"].folder)
        self.assertEqual("运维", specs["Jenkins 失败告警验收"].folder)
        with patch.object(module.shared, "read_container_file", return_value=None) as read:
            with self.assertRaisesRegex(module.shared.JenkinsJobSyncError, "mobile-apps"):
                module.inspect("controller", {signing: module.render_all()[signing]})
        read.assert_called_once_with("controller", "/var/jenkins_home/jobs/mobile-apps/config.xml")

    def test_busy_controller_after_staging_preserves_jobs_and_skips_restart(self):
        spec = next(item for item in module.JOB_SPECS if item.name == "sxmqlive-pr-validation")
        commands = []
        with (
            TemporaryDirectory() as temporary,
            patch.object(module.shared, "ensure_controller_running"),
            patch.object(module, "inspect", return_value=([spec], {spec: "<flow-definition/>"})),
            patch.object(module.shared, "_backup_configs"),
            patch.object(module.shared, "assert_controller_idle", side_effect=[
                None, module.shared.JenkinsJobSyncError("busy"),
            ]) as idle,
            patch.object(module.shared, "_docker", side_effect=lambda args, **kwargs: commands.append(args)),
            patch.object(module.shared, "_wait_until_ready") as wait,
        ):
            with self.assertRaisesRegex(module.shared.JenkinsJobSyncError, "busy"):
                module.apply("controller", {spec: "<flow-definition/>"}, Path(temporary), 10)
        self.assertEqual(2, idle.call_count)
        self.assertTrue(any(command[0] == "cp" for command in commands))
        self.assertFalse(any("mv" in command or command[0] == "restart" for command in commands))
        self.assertTrue(any("rm" in command for command in commands))
        wait.assert_not_called()

    def test_all_configs_are_staged_before_idle_check_and_restart(self):
        specs = tuple(spec for spec in module.JOB_SPECS if spec.name in ("Jenkins 失败告警验收", "sxmqlive-pr-validation"))
        events = []
        with (
            TemporaryDirectory() as temporary,
            patch.object(module.shared, "ensure_controller_running"),
            patch.object(module, "inspect", side_effect=[
                (list(specs), {spec: "<flow-definition/>" for spec in specs}), ([], {}),
            ]),
            patch.object(module.shared, "_backup_configs"),
            patch.object(module.shared, "assert_controller_idle", side_effect=lambda container: events.append("idle")),
            patch.object(module.shared, "_docker", side_effect=lambda args, **kwargs: events.append(args)),
            patch.object(module.shared, "_wait_until_ready"),
        ):
            module.apply("controller", {spec: "<flow-definition/>" for spec in specs}, Path(temporary), 10)
        self.assertEqual(2, events.count("idle"))
        final_idle = len(events) - 1 - events[::-1].index("idle")
        copies = [index for index, event in enumerate(events) if isinstance(event, list) and event[0] == "cp"]
        moves = [index for index, event in enumerate(events) if isinstance(event, list) and "mv" in event]
        self.assertEqual(2, len(copies))
        self.assertEqual(2, len(moves))
        self.assertLess(max(copies), final_idle)
        self.assertLess(final_idle, min(moves))
        ownership = [event[event.index("jenkins:jenkins") + 1:] for event in events
                     if isinstance(event, list) and "chown" in event]
        for spec in specs:
            job_dir = str(Path(module._job_path(spec)).parent)
            self.assertIn([job_dir, job_dir + "/config.xml.new"], ownership)
        self.assertEqual(["restart", "controller"], events[-1])

    def test_apply_preserves_existing_job_acl(self):
        spec = next(item for item in module.JOB_SPECS if item.name == "sxmqlive-pr-validation")
        current = '<flow-definition><properties><hudson.security.AuthorizationMatrixProperty><permission>hudson.model.Item.Build:admin</permission><inheritanceStrategy class="NonInheritingStrategy"/></hudson.security.AuthorizationMatrixProperty></properties></flow-definition>'
        uploaded = []
        def docker(args, **kwargs):
            if args[0] == "cp":
                uploaded.append(Path(args[1]).read_text())
        with (TemporaryDirectory() as temporary,
              patch.object(module.shared, "ensure_controller_running"),
              patch.object(module, "inspect", side_effect=[([spec], {spec: current}), ([], {})]),
              patch.object(module.shared, "_backup_configs"),
              patch.object(module.shared, "assert_controller_idle"),
              patch.object(module.shared, "_docker", side_effect=docker),
              patch.object(module.shared, "_wait_until_ready")):
            module.apply("controller", {spec: module.render_all()[spec]}, Path(temporary), 10)
        self.assertEqual(1, len(uploaded))
        acl = ElementTree.fromstring(uploaded[0]).find("properties/hudson.security.AuthorizationMatrixProperty")
        self.assertEqual("hudson.model.Item.Build:admin", acl.findtext("permission"))
        self.assertEqual("NonInheritingStrategy", acl.find("inheritanceStrategy").get("class"))

    def test_apply_preserves_wiki_enabled_state_in_both_directions_without_acl(self):
        spec = next(item for item in module.JOB_SPECS if item.name == "sxmqlive-wiki-navigation")
        for disabled in ("true", "false", None):
            with self.subTest(disabled=disabled):
                desired_root = ElementTree.fromstring(module.render_all()[spec])
                desired_root.find("disabled").text = "false" if disabled == "true" else "true"
                desired = ElementTree.tostring(desired_root, encoding="unicode")
                current_root = ElementTree.fromstring(desired)
                if disabled is None:
                    current_root.remove(current_root.find("disabled"))
                else:
                    current_root.find("disabled").text = disabled
                current = ElementTree.tostring(current_root, encoding="unicode")
                self.assertTrue(module.configs_equivalent(spec, desired, current))
                current_root.find("definition/scm/branches/hudson.plugins.git.BranchSpec/name").text = "b" * 40
                state = {"xml": ElementTree.tostring(current_root, encoding="unicode")}

                def read(container, path):
                    return '<com.cloudbees.hudson.plugins.folder.Folder/>' if path == module._folder_path(spec.folder) else state["xml"]

                def docker(args, **kwargs):
                    if args[0] == "cp":
                        state["staged"] = Path(args[1]).read_text()
                    elif "mv" in args:
                        state["xml"] = state["staged"]

                with TemporaryDirectory() as directory, \
                        patch.object(module.shared, "ensure_controller_running"), \
                        patch.object(module.shared, "read_container_file", side_effect=read), \
                        patch.object(module.shared, "assert_controller_idle"), \
                        patch.object(module.shared, "_docker", side_effect=docker) as docker_mock, \
                        patch.object(module.shared, "_wait_until_ready"):
                    module.apply("controller", {spec: desired}, Path(directory), 10)
                    self.assertEqual([], module.inspect("controller", {spec: desired})[0])
                    docker_mock.reset_mock()
                    module.apply("controller", {spec: desired}, Path(directory), 10)
                    docker_mock.assert_not_called()
                staged = ElementTree.fromstring(state["staged"])
                self.assertEqual(disabled, staged.findtext("disabled"))
                self.assertIsNone(staged.find("properties/hudson.security.AuthorizationMatrixProperty"))
                self.assertEqual("a" * 40, staged.findtext("definition/scm/branches/hudson.plugins.git.BranchSpec/name"))

    def test_signing_existing_acl_is_rejected_without_overwriting_or_restart(self):
        spec = next(item for item in module.JOB_SPECS if item.name == "sxmqlive-signing-maintenance")
        for existing in ("<flow-definition/>",
                         '<flow-definition><properties><hudson.security.AuthorizationMatrixProperty>'
                         '<permission>hudson.model.Item.Build:ordinary</permission>'
                         '<inheritanceStrategy class="org.jenkinsci.plugins.matrixauth.inheritance.NonInheritingStrategy"/>'
                         '</hudson.security.AuthorizationMatrixProperty></properties></flow-definition>'):
            with self.subTest(existing=existing), TemporaryDirectory() as temporary, \
                    patch.object(module.shared, "ensure_controller_running"), \
                    patch.object(module, "inspect", return_value=([spec], {spec: existing})), \
                    patch.object(module.shared, "_docker") as docker:
                with self.assertRaisesRegex(module.shared.JenkinsJobSyncError, "拒绝改写现存 ACL"):
                    module.apply("controller", {spec: module.render_all()[spec]}, Path(temporary), 10)
                docker.assert_not_called()

    def test_signing_template_loads_master_and_masks_device_input(self):
        spec = next(item for item in module.JOB_SPECS if item.name == "sxmqlive-signing-maintenance")
        root = ElementTree.fromstring(module.render_all()[spec])
        self.assertIn("branches: [[name: 'refs/heads/master']]", root.findtext("definition/script"))
        self.assertEqual("false", root.findtext("definition/sandbox"))
        self.assertIsNone(root.find("definition/scm"))
        parameters = root.find("properties/hudson.model.ParametersDefinitionProperty/parameterDefinitions")
        by_name = {item.findtext("name"): item for item in parameters}
        self.assertEqual("hudson.model.PasswordParameterDefinition", by_name["DEVICE_UDID"].tag)
        self.assertEqual("false", by_name["NOTIFY"].findtext("defaultValue"))
        self.assertEqual("plan", by_name["MODE"].findtext("choices/a/string"))
        source = root.findtext("definition/script")
        self.assertLess(source.index("env.NODE_NAME != expectedNode"), source.index("withCredentials(bindings)"))
        self.assertIn("env.SIGNING_MAINTENANCE_ROOT != expectedRoot", source)
        self.assertIn('--brands "$TARGET_BRANDS" --state-root "$SIGNING_MAINTENANCE_ROOT"', source)

        acl = root.find("properties/hudson.security.AuthorizationMatrixProperty")
        self.assertEqual([], acl.findall("permission"))
        self.assertEqual("org.jenkinsci.plugins.matrixauth.inheritance.NonInheritingStrategy",
                         acl.find("inheritanceStrategy").get("class"))

    def test_inline_script_hash_uses_jenkins_groovy_language_prefix(self):
        import hashlib
        spec = next(item for item in module.JOB_SPECS if item.name == "sxmqlive-signing-maintenance")
        config = module.render_all()[spec]
        source = module.shared.pin_ci_config((module.REPO_ROOT / spec.script_path).read_text())
        self.assertEqual("SHA512:" + hashlib.sha512(("groovy:" + source).encode()).hexdigest(),
                         module.signing_script_approval_hash(config))
        root = ElementTree.fromstring(config)
        root.find("definition/script").text += "\n"
        changed = ElementTree.tostring(root, encoding="unicode")
        self.assertNotEqual(module.signing_script_approval_hash(config), module.signing_script_approval_hash(changed))
        self.assertNotIn("approveSignature", source)
        self.assertNotIn("headers-command", source)
        self.assertNotIn("readFile", source)

    def test_gitee_bootstrap_requires_plugin_token_credential(self):
        source = Path(
            "jenkins/controller/init.groovy.d/02-configure-gitee.groovy"
        ).read_text(encoding="utf-8")
        self.assertIn("import com.gitee.jenkins.connection.GiteeApiToken", source)
        self.assertIn("apiToken instanceof GiteeApiToken", source)
        self.assertIn("webhookSecret instanceof StringCredentials", source)
        self.assertIn("job.getTriggers().values().find", source)
        self.assertNotIn("job.getTrigger(GiteePushTrigger.class)", source)
        self.assertIn("['app_pr','ci_pr']", source)
        self.assertNotIn("codex-api-key", source)


if __name__ == "__main__":
    unittest.main()
