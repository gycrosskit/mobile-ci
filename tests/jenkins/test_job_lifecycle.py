import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse
import xml.etree.ElementTree as ET

from jenkins import job_lifecycle as lifecycle
from tests.jenkins.test_project_configuration import second_project
from project_config import ProjectConfigError, validate_project
import project_config


class JobLifecycleTest(unittest.TestCase):
    def setUp(self):
        fixture = json.loads((lifecycle.ROOT / 'tests/fixtures/legacy-project.json').read_text())
        with patch.object(project_config, 'load_project', return_value=fixture):
            self.profile = second_project()
        self.profile.pop('testOnly')
        self.profile['jobs'] = {'android_test': self.profile['jobs']['android_test']}
        self.name = self.profile['jobs']['android_test']['name']
        self.configs = {'Example Folder': '<com.cloudbees.hudson.plugins.folder.Folder/>'}
        self.events = []
        self.quiet = False
        self.queue = []
        self.running = None
        self.approved = []
        self.history = {}
        self.failure = None
        self.corrupt = False
        self.concurrent = None
        self.redirect = False
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, code, value):
                self.send_response(code)
                self.end_headers()
                self.wfile.write((json.dumps(value) if isinstance(value, dict) else value).encode())

            def parsed(self):
                parsed = urllib.parse.urlsplit(self.path)
                parts = [urllib.parse.unquote(part) for part in parsed.path.split('/') if part]
                names = []
                while parts and parts[0] == 'job':
                    names.append(parts[1]); parts = parts[2:]
                return '/'.join(names), '/'.join(parts), urllib.parse.parse_qs(parsed.query)

            def do_GET(self):
                test.events.append(('GET', self.path))
                if test.redirect:
                    self.send_response(302)
                    self.send_header('Location', 'http://127.0.0.1:1/credential-leak')
                    self.end_headers()
                    return
                if self.headers.get('Authorization') != 'Basic dGVzdDp0b2tlbg==':
                    self.respond(401, 'private error token body'); return
                name, route, _ = self.parsed()
                if route == 'config.xml':
                    if test.concurrent == name:
                        test.configs[name] += '\n'; test.concurrent = None
                    self.respond(200, test.configs[name]) if name in test.configs else self.respond(404, '')
                elif route == 'queue/api/json':
                    self.respond(200, {'items': test.queue})
                elif route == 'computer/api/json':
                    self.respond(200, {'computer': [{'executors': [{'currentExecutable': test.running}], 'oneOffExecutors': []}]})
                elif route == 'api/json':
                    if name:
                        if name in test.configs and ET.fromstring(test.configs[name]).tag == 'flow-definition':
                            self.respond(200, {'fullName': name, 'nextBuildNumber': test.history.get(name, 1), 'upstreamProjects': [], 'downstreamProjects': []})
                        else:
                            self.respond(200, {'jobs': [{'name': n.split('/')[-1], 'fullName': n, '_class': 'org.jenkinsci.plugins.workflow.job.WorkflowJob'} for n in test.configs if n.rpartition('/')[0] == name]})
                    elif self.path.endswith('tree=quietingDown'):
                        self.respond(200, {'quietingDown': test.quiet})
                    else:
                        self.respond(200, {'jobs': [{'name': n, 'fullName': n, '_class': 'com.cloudbees.hudson.plugins.folder.Folder' if ET.fromstring(xml).tag.endswith('.Folder') else 'org.jenkinsci.plugins.workflow.job.WorkflowJob'} for n, xml in test.configs.items() if '/' not in n]})
                else:
                    self.respond(404, '')

            def do_POST(self):
                name, route, query = self.parsed()
                source = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode()
                test.events.append(('POST', self.path))
                if test.failure == route:
                    self.respond(503, 'private error token body'); return
                if route == 'scriptText':
                    if (self.headers.get('Content-Type') != 'application/x-www-form-urlencoded'
                            or urllib.parse.parse_qs(source) != {'script': [lifecycle.APPROVAL_READBACK]}):
                        self.respond(400, ''); return
                    self.respond(200, {'approvedScriptHashes': test.approved}); return
                if route == 'quietDown':
                    test.quiet = True
                elif route == 'cancelQuietDown':
                    test.quiet = False
                elif route == 'createItem':
                    name = name + '/' + query['name'][0] if name else query['name'][0]
                    if name in test.configs:
                        self.respond(409, ''); return
                    test.configs[name] = source
                elif route == 'config.xml':
                    test.configs[name] = source
                elif route == 'doDelete':
                    test.configs.pop(name)
                else:
                    self.respond(404, ''); return
                if test.corrupt and route in ('config.xml', 'createItem'):
                    test.configs[name] = source.replace('<disabled>true</disabled>', '<disabled>false</disabled>')
                self.respond(200, '')

            def do_DELETE(self):
                name, route, _ = self.parsed()
                test.events.append(('DELETE', self.path))
                if route or name not in test.configs:
                    self.respond(404, ''); return
                test.configs.pop(name)
                self.respond(204, '')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = lifecycle.JenkinsAPI('http://127.0.0.1:' + str(self.server.server_port), 'test', 'token')
        self.directory = tempfile.TemporaryDirectory()
        self.directory.name = str(Path(self.directory.name).resolve())
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.patch = patch.dict(os.environ, CI_AUTOMATION_COMMIT='a' * 40)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def posts(self):
        return [path for method, path in self.events if method == 'POST' and path != '/scriptText']

    def plan_apply(self, **kwargs):
        plan = lifecycle.make_plan(self.api, self.profile, **kwargs)
        lifecycle.apply_plan(self.api, self.profile, plan, Path(self.directory.name) / 'backups')
        return plan

    def test_single_fixed_capability_creates_disabled_and_second_plan_is_idempotent(self):
        plan = self.plan_apply()
        self.assertEqual([self.name], [action['job'] for action in plan['actions']])
        self.assertTrue(lifecycle.disabled(self.configs[self.name]))
        self.assertEqual(('example-mobile', 'android_test', self.name), lifecycle.owner(self.configs[self.name]))
        self.assertFalse(self.quiet)
        self.assertEqual([], lifecycle.make_plan(self.api, self.profile)['actions'])
        self.assertFalse(any('restart' in path or 'build' in path or 'approve' in path for path in self.posts()))
        backup = next((Path(self.directory.name) / 'backups').iterdir())
        self.assertEqual(0o700, backup.stat().st_mode & 0o777)
        self.assertEqual(0o600, (backup / 'before.json').stat().st_mode & 0o777)
        self.assertTrue((backup / 'verified.json').exists())

    def test_enabled_true_requires_exact_readback_approvals_then_disable_and_preserve_acl(self):
        self.plan_apply()
        root = ET.fromstring(self.configs[self.name])
        acl = ET.SubElement(root.find('properties'), 'hudson.security.AuthorizationMatrixProperty')
        ET.SubElement(acl, 'permission').text = 'hudson.model.Item.Read:operator'
        self.configs[self.name] = ET.tostring(root, encoding='unicode')
        self.profile['jobs']['android_test']['enabled'] = True
        with self.assertRaises(lifecycle.Error):
            lifecycle.make_plan(self.api, self.profile)
        hashes = []
        for node in root.iter():
            if node.findtext('sandbox') == 'false' and node.find('script') is not None:
                hashes.append('SHA512:' + lifecycle.hashlib.sha512(('groovy:' + node.findtext('script')).encode()).hexdigest())
        self.approved = hashes
        self.plan_apply()
        self.assertFalse(lifecycle.disabled(self.configs[self.name]))
        self.assertIn('hudson.model.Item.Read:operator', self.configs[self.name])
        self.profile['jobs']['android_test']['enabled'] = False
        self.plan_apply()
        self.assertTrue(lifecycle.disabled(self.configs[self.name]))

    def test_missing_role_never_deletes_and_explicit_delete_needs_exact_confirmation(self):
        self.plan_apply()
        self.profile['jobs'] = {}
        self.assertEqual([], lifecycle.make_plan(self.api, self.profile)['actions'])
        plan = lifecycle.make_plan(self.api, self.profile, delete_jobs=[self.name])
        with self.assertRaises(lifecycle.Error):
            lifecycle.apply_plan(self.api, self.profile, plan, Path(self.directory.name) / 'backup')
        lifecycle.apply_plan(self.api, self.profile, plan, Path(self.directory.name) / 'backup', [self.name])
        self.assertNotIn(self.name, self.configs)
        self.assertEqual([], lifecycle.make_plan(self.api, self.profile, delete_jobs=[self.name])['actions'])

    def test_delete_rejects_build_history_dynamic_references_and_unknown_owner(self):
        self.plan_apply(); self.profile['jobs'] = {}
        self.history[self.name] = 2
        with self.assertRaises(lifecycle.Error): lifecycle.make_plan(self.api, self.profile, [self.name])
        self.history.clear()
        self.configs['Other'] = '<flow-definition><definition><script>build job: "other"</script></definition></flow-definition>'
        with self.assertRaises(lifecycle.Error): lifecycle.make_plan(self.api, self.profile, [self.name])
        self.configs.pop('Other')
        self.configs[self.name] = lifecycle.mark(self.configs[self.name], 'other-project', 'android_test', self.name)
        with self.assertRaises(lifecycle.Error): lifecycle.make_plan(self.api, self.profile, [self.name])

    def test_delete_rejects_named_and_scm_downstream_reference_and_enabled_target(self):
        self.plan_apply(); self.profile['jobs'] = {}
        target = self.configs[self.name]
        for other in ('<project><projects>' + self.name + '</projects></project>',
                      '<flow-definition><definition><scriptPath>Jenkinsfile</scriptPath></definition></flow-definition>'):
            self.configs['Other'] = other
            with self.assertRaises(lifecycle.Error): lifecycle.make_plan(self.api, self.profile, [self.name])
        self.configs.pop('Other')
        self.configs[self.name] = lifecycle.with_disabled(target, False)
        with self.assertRaises(lifecycle.Error): lifecycle.make_plan(self.api, self.profile, [self.name])

    def test_unowned_or_foreign_job_and_adoption_of_arbitrary_pipeline_are_rejected(self):
        source = lifecycle.with_disabled(lifecycle.desired_configs(self.profile)[self.name][2], True)
        self.configs[self.name] = source
        with self.assertRaises(lifecycle.Error): lifecycle.make_plan(self.api, self.profile)
        plan = lifecycle.make_plan(self.api, self.profile, adopt_jobs=[self.name])
        self.assertEqual('update', plan['actions'][0]['operation'])
        self.configs[self.name] = source.replace('jenkins/Jenkinsfile.test', 'evil/Jenkinsfile')
        with self.assertRaises(lifecycle.Error): lifecycle.make_plan(self.api, self.profile, adopt_jobs=[self.name])
        self.configs[self.name] = lifecycle.mark(source, 'foreign', 'android_test', self.name)
        with self.assertRaises(lifecycle.Error): lifecycle.make_plan(self.api, self.profile, adopt_jobs=[self.name])

    def test_tampered_plan_changed_controller_config_or_profile_never_writes(self):
        plan = lifecycle.make_plan(self.api, self.profile)
        for mutate in (lambda p: p['actions'][0].update(xml='<evil/>'),
                       lambda p: p.update(controller='https://other.example/'),
                       lambda p: p.update(ciCommit='b' * 40)):
            changed = copy.deepcopy(plan); mutate(changed)
            with self.assertRaises(lifecycle.Error): lifecycle.apply_plan(self.api, self.profile, changed, self.directory.name)
        self.configs[self.name] = '<flow-definition/>'
        with self.assertRaises(lifecycle.Error): lifecycle.apply_plan(self.api, self.profile, plan, self.directory.name)
        self.assertEqual([], self.posts())

    def test_running_queue_testonly_refuse_before_any_post(self):
        plan = lifecycle.make_plan(self.api, self.profile)
        self.queue = [{'id': 1}]
        with self.assertRaises(lifecycle.Error): lifecycle.apply_plan(self.api, self.profile, plan, self.directory.name)
        self.queue = []; self.running = {'url': 'http://example/build/1'}
        with self.assertRaises(lifecycle.Error): lifecycle.apply_plan(self.api, self.profile, plan, self.directory.name)
        self.running = None; self.profile['testOnly'] = True
        with self.assertRaises(lifecycle.Error): lifecycle.apply_plan(self.api, self.profile, plan, self.directory.name)
        self.assertEqual([], self.posts())

    def test_preexisting_quietdown_is_not_cancelled_and_unknown_write_never_retried(self):
        self.quiet = True
        self.plan_apply()
        self.assertTrue(self.quiet)
        self.assertNotIn('/cancelQuietDown', self.posts())
        self.profile['jobs']['android_test']['enabled'] = False
        self.configs[self.name] = lifecycle.with_disabled(self.configs[self.name], False)
        plan = lifecycle.make_plan(self.api, self.profile)
        self.failure = 'config.xml'
        with self.assertRaises(lifecycle.Error) as error:
            lifecycle.apply_plan(self.api, self.profile, plan, Path(self.directory.name) / 'failure')
        self.assertNotIn('token', str(error.exception))
        self.assertEqual(1, sum(path.endswith('/config.xml') for path in self.posts()))
        self.assertTrue(self.quiet)

    def test_readback_corruption_keeps_quietdown_and_recovery_record(self):
        plan = lifecycle.make_plan(self.api, self.profile)
        self.corrupt = True
        with self.assertRaises(lifecycle.Error): lifecycle.apply_plan(self.api, self.profile, plan, Path(self.directory.name) / 'backup')
        self.assertTrue(self.quiet)
        record = next((Path(self.directory.name) / 'backup').iterdir())
        self.assertTrue((record / 'before.json').exists())
        self.assertFalse((record / 'verified.json').exists())

    def test_http_credentials_and_private_files_failclosed(self):
        for url in ('http://remote.example', 'https://test:token@example.com', 'https://example.com/?token=x', 'https://example.com/#x'):
            with self.assertRaises(lifecycle.Error): lifecycle.JenkinsAPI(url, 'test', 'token')
        path = Path(self.directory.name) / 'token'
        lifecycle.private_write(path, 'token')
        self.assertEqual('token', lifecycle.private_read(path, 4096))
        path.chmod(0o644)
        with self.assertRaises(lifecycle.Error): lifecycle.private_read(path, 4096)
        path.chmod(0o600)
        link = Path(self.directory.name) / 'link'; link.symlink_to(path)
        with self.assertRaises(lifecycle.Error): lifecycle.private_read(link, 4096)
        os.link(path, Path(self.directory.name) / 'hardlink')
        with self.assertRaises(lifecycle.Error): lifecycle.private_read(path, 4096)
        bad = lifecycle.JenkinsAPI(self.api.url, 'test', 'wrong')
        with self.assertRaises(lifecycle.Error) as error: bad.config(self.name)
        self.assertNotIn('token body', str(error.exception))

    def test_actual_http_redirect_is_rejected_without_following_or_writing(self):
        self.redirect = True
        with self.assertRaises(lifecycle.Error):
            self.api.config(self.name)
        self.assertEqual(1, len(self.events))
        self.assertEqual([], self.posts())

    def test_removed_approval_and_state_change_before_apply_refuse_all_mutations(self):
        self.plan_apply()
        self.profile['jobs']['android_test']['enabled'] = True
        root = ET.fromstring(self.configs[self.name])
        self.approved = ['SHA512:' + lifecycle.hashlib.sha512(('groovy:' + node.findtext('script')).encode()).hexdigest()
                         for node in root.iter() if node.findtext('sandbox') == 'false' and node.find('script') is not None]
        plan = lifecycle.make_plan(self.api, self.profile)
        prior = list(self.posts())
        self.approved.clear()
        with self.assertRaises(lifecycle.Error): lifecycle.apply_plan(self.api, self.profile, plan, self.directory.name)
        self.assertEqual(prior, self.posts())
        self.profile['jobs']['android_test']['enabled'] = False
        self.configs[self.name] = lifecycle.with_disabled(self.configs[self.name], False)
        plan = lifecycle.make_plan(self.api, self.profile)
        self.concurrent = self.name
        with self.assertRaises(lifecycle.Error): lifecycle.apply_plan(self.api, self.profile, plan, self.directory.name)
        self.assertEqual(prior, self.posts())

    def test_capability_state_fixed_pipeline_and_platform_guards(self):
        validate_project(self.profile)
        for change in (lambda p: p['jobs']['android_test'].update(enabled='false'),
                       lambda p: p['jobs']['android_test'].update(enabled=1),
                       lambda p: p['jobs']['android_test'].update(pipeline='evil'),
                       lambda p: p['jobs']['android_test'].update(shell='echo hi'),
                       lambda p: p['jobs'].update(ios_test={'name': 'ios', 'pipeline': 'jenkins/Jenkinsfile.ios.test'})):
            profile = copy.deepcopy(self.profile); change(profile)
            with self.assertRaises(ProjectConfigError): validate_project(profile)

    def test_delete_rejects_executable_consumers_without_literal_target_or_build_keyword(self):
        self.plan_apply()
        self.profile['jobs'] = {}
        prior = list(self.posts())
        consumers = [
            '<flow-definition><definition><script>jenkins.model.Jenkins.instance.getAllItems(hudson.model.Job).each { it.scheduleBuild2(0) }</script></definition></flow-definition>',
            '<project><builders><hudson.tasks.Shell><command>curl "$JENKINS_URL"</command></hudson.tasks.Shell></builders></project>',
        ]
        for config in consumers:
            with self.subTest(config=config):
                self.configs['Other'] = config
                with self.assertRaises(lifecycle.Error):
                    lifecycle.make_plan(self.api, self.profile, [self.name])
                self.assertIn(self.name, self.configs)
                self.assertEqual(prior, self.posts())

    def test_invalid_backup_destination_does_not_enter_quietdown_or_write_jobs(self):
        plan = lifecycle.make_plan(self.api, self.profile)
        backup = Path(self.directory.name) / 'not-a-directory'
        backup.write_text('preserve this file')
        with self.assertRaises(OSError):
            lifecycle.apply_plan(self.api, self.profile, plan, backup)
        self.assertFalse(self.quiet)
        self.assertEqual([], self.posts())
        self.assertNotIn(self.name, self.configs)
        self.assertEqual('preserve this file', backup.read_text())

    def test_verify_rejects_tampered_xml_and_recomputed_hash_against_trusted_pipeline(self):
        plan = self.plan_apply()
        action = copy.deepcopy(plan['actions'][0])
        self.configs[self.name] = self.configs[self.name].replace('jenkins/Jenkinsfile.test', 'arbitrary/Jenkinsfile')
        action['xml'] = self.configs[self.name]
        prior = list(self.posts())
        with self.assertRaises(lifecycle.Error):
            lifecycle.verify_action(self.api, self.profile, action)
        action['after'] = lifecycle.digest(action['xml'])
        with self.assertRaises(lifecycle.Error):
            lifecycle.verify_action(self.api, self.profile, action)
        self.assertEqual(prior, self.posts())

    def test_multiple_adoptions_verify_each_target_without_scanning_unadopted_jobs(self):
        self.profile['jobs']['android_release'] = {
            'name': 'Example Folder/android-release', 'pipeline': 'jenkins/Jenkinsfile.release',
        }
        for name, (_, _, source) in lifecycle.desired_configs(self.profile).items():
            self.configs[name] = lifecycle.with_disabled(source, True)
        names = [job['name'] for job in self.profile['jobs'].values()]
        plan = lifecycle.make_plan(self.api, self.profile, adopt_jobs=names)
        lifecycle.apply_plan(self.api, self.profile, plan, Path(self.directory.name) / 'backup')
        self.assertFalse(self.quiet)
        for role, job in self.profile['jobs'].items():
            self.assertEqual(('example-mobile', role, job['name']), lifecycle.owner(self.configs[job['name']]))
        self.assertEqual([], lifecycle.make_plan(self.api, self.profile)['actions'])


if __name__ == '__main__':
    unittest.main()
