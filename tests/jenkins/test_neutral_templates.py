"""中性模板必须确定性生成旧生产快照，不允许残留占位符进入 Job。"""
import os
import shutil
import json
import re
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import project_config
from jenkins import generate_jenkins_job_templates as generator
from jenkins import sync_jenkins_jobs as jobs
from jenkins import sync_jenkins_admin_jobs as admin
from tests.jenkins.test_project_configuration import second_project
from tests.jenkins.test_pr_validation import groovy_classpath

ROOT = Path(__file__).resolve().parents[2]


class NeutralTemplatesTest(unittest.TestCase):
    def test_production_templates_and_inline_scripts_stay_byte_compatible(self):
        profile = project_config.load_project()
        for path in (ROOT / 'jenkins/templates').rglob('*'):
            if not path.is_file():
                continue
            if path.name == 'Jenkinsfile.project-pr-validation':
                continue
            relative = path.relative_to(ROOT / 'jenkins/templates')
            role = 'ci_pr' if path.name == 'Jenkinsfile.ci-pr-validation' else None
            if path.name == 'Jenkinsfile.pr-validation':
                role = 'app_pr'
            if relative.parts[0] == 'pipelines':
                snapshot = ROOT / 'jenkins' / path.name
            else:
                snapshot = ROOT / 'jenkins' / relative
            with self.subTest(template=str(relative)):
                self.assertEqual(snapshot.read_bytes(), project_config.render_project_text(path.read_text(), profile, role).encode())
        with patch.dict(os.environ, CI_AUTOMATION_COMMIT='a' * 40):
            self.assertEqual(jobs.render_all(), jobs.render_all(profile=profile))
            self.assertEqual(admin.render_all(), admin.render_all(profile))

    def test_renderer_never_reads_company_marker_file_or_performs_text_replacement(self):
        profile = second_project()
        with patch.object(project_config, '_read_static_json', side_effect=AssertionError('company markers read')):
            source = '{{CI_APP_SSH}} {{CI_CI_SSH}} {{CI_PROJECT_ID}}'
            self.assertEqual(profile['repositories']['application']['ssh'] + ' ' + profile['repositories']['automation']['ssh'] + ' ' + profile['id'],
                             project_config.render_project_text(source, profile))
            self.assertEqual('ordinary fixed text', project_config.render_project_text('ordinary fixed text', profile))

    def test_generator_checks_and_repairs_legacy_controller_snapshots_from_neutral_source(self):
        profile = project_config.load_project()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / 'jenkins/templates', root / 'jenkins/templates')
            shutil.copytree(ROOT / 'jenkins/local/admin', root / 'jenkins/local/admin')
            shutil.copytree(ROOT / 'jenkins/selectors', root / 'jenkins/selectors')
            for path in (ROOT / 'jenkins/templates/pipelines').iterdir():
                if path.name == 'Jenkinsfile.project-pr-validation':
                    continue
                shutil.copyfile(ROOT / 'jenkins' / path.name, root / 'jenkins' / path.name)
            target = root / 'jenkins/Jenkinsfile.ci-pr-validation'
            target.write_text(target.read_text().replace("CI_PROJECT_ID = 'sxmqlive'", "CI_PROJECT_ID = '{{CI_PROJECT_ID}}'"))
            damaged = target.read_bytes()
            with patch.object(generator, 'REPO_ROOT', root):
                self.assertEqual(['jenkins/Jenkinsfile.ci-pr-validation'], generator.synchronize_compatibility_snapshots(profile, True))
                self.assertEqual(damaged, target.read_bytes())
                self.assertEqual(['jenkins/Jenkinsfile.ci-pr-validation'], generator.synchronize_compatibility_snapshots(profile, False))
                self.assertEqual([], generator.synchronize_compatibility_snapshots(profile, True))
                self.assertEqual((ROOT / 'jenkins/Jenkinsfile.ci-pr-validation').read_bytes(), target.read_bytes())

    def test_unknown_malformed_unbound_and_expression_placeholders_fail_closed(self):
        profile = second_project()
        for value in ('{{CI_NO_SUCH_FIELD}}', '{{CI_PROJECT_ID', '{{CI_project_id}}', '{{CI_JOB_SIGNING_BACKUP}}', '{{CI_repositories.application.ssh}}'):
            with self.subTest(value=value), self.assertRaises(project_config.ProjectConfigError):
                project_config.render_project_text(value, profile)

    def test_untrusted_value_cannot_inject_groovy_or_xml(self):
        for field, value in (('id', "bad';error('x')"), ('id', '<unsafe>')):
            profile = second_project(); profile[field] = value
            with self.assertRaises(project_config.ProjectConfigError):
                project_config.render_project_text('{{CI_PROJECT_ID}}', profile)

    def test_unrendered_project_placeholder_cannot_be_pinned_or_deployed(self):
        with patch.dict(os.environ, CI_AUTOMATION_COMMIT='a' * 40):
            with self.assertRaisesRegex(jobs.JenkinsJobSyncError, '未渲染'):
                jobs.pin_ci_config('{{CI_PROJECT_ID}} __CI_AUTOMATION_COMMIT__')
        # ef513 的旧 Controller 只读取此路径；保留已渲染生产文本让自动晋级仍有安全输入。
        source = (ROOT / 'jenkins/Jenkinsfile.ci-pr-validation').read_text()
        self.assertNotIn('{{CI_', source)
        self.assertIn("CI_PROJECT_ID = 'sxmqlive'", source)
        self.assertIn("CI_AUTOMATION_COMMIT = '__CI_AUTOMATION_COMMIT__'", source)

    def test_actual_project_app_gate_rejects_wrong_ci_repository_checkout_and_sha(self):
        profile = second_project()
        commit = 'a' * 40
        with patch.dict(os.environ, CI_AUTOMATION_COMMIT=commit):
            config = next(text for spec, text in admin.render_all(profile).items() if spec.role == 'app_pr')
        definition = ET.fromstring(config).find('definition')
        self.assertEqual('org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition', definition.get('class'))
        self.assertEqual('true', definition.findtext('sandbox'))
        pipeline = definition.findtext('script')
        self.assertNotIn('checkout(changelog: false, poll: false, scm: scm)', pipeline)
        self.assertNotIn('{{CI_', pipeline)
        guard = re.search(r"(?s)if \(!\(env.CI_AUTOMATION_COMMIT.*?error '受信 CI 仓库或实际提交不匹配' \}", pipeline).group()
        script = 'import groovy.json.JsonSlurperClassic\n'
        repo = profile['repositories']['automation']['ssh']
        for checkout_url, checkout_sha, actual_sha, accepted in ((repo, commit, commit, True), ('git@gitee.com:foreign/ci.git', commit, commit, False),
                                                                 (repo, 'b' * 40, commit, False), (repo, commit, 'b' * 40, False)):
            environment = {'CI_AUTOMATION_COMMIT': commit, 'CI_REPOSITORY_URL': repo}
            script += '{ ->\n def env = new JsonSlurperClassic().parseText(' + json.dumps(json.dumps(environment)) + ')\n'
            script += "def checkout = { args -> assert args.changelog == false && args.poll == false; assert args.scm.branches == [[name:env.CI_AUTOMATION_COMMIT]]; assert args.scm.userRemoteConfigs == [[credentialsId:'" + profile['credentials']['scm'] + "',url:env.CI_REPOSITORY_URL]]; return " + json.dumps({'GIT_URL':checkout_url,'GIT_COMMIT':checkout_sha}).replace('{', '[').replace('}', ']') + ' }\n'
            script += 'def sh = { args -> ' + json.dumps(actual_sha) + " }; def error = { message -> throw new IllegalArgumentException(message) }; boolean accepted = false\ntry {\n" + guard + '\naccepted = true\n} catch (IllegalArgumentException ignored) {}\nassert accepted == ' + str(accepted).lower() + '\n}();\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixed-checkout.groovy'; path.write_text(script)
            result = subprocess.run(['java','-cp',groovy_classpath(),'groovy.ui.GroovyMain',str(path)],capture_output=True,text=True,timeout=30)
            self.assertEqual(0,result.returncode,result.stderr+result.stdout)

    def test_second_project_regenerates_selectors_with_exact_approval(self):
        profile = second_project()
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(0, generator.synchronize(Path('/not-read'), False, profile, directory))
            self.assertEqual(0, generator.synchronize(Path('/not-read'), True, profile, directory))
            for path in Path(directory).glob('*.xml.template'):
                self.assertNotIn('{{CI_', path.read_text())
                root = ET.fromstring(path.read_text())
                script = root.findtext('.//secureScript/script')
                self.assertIn('job.checkPermission(hudson.model.Item.BUILD)', script)
                self.assertIn(profile['credentials']['scm'], script)
                self.assertNotIn('shanxi-hanning-network', script)


if __name__ == '__main__':
    unittest.main()
