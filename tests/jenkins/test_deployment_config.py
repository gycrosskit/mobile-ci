import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from deployment_config import (DeploymentConfigError, derived_nodes, job_agent_bindings, load_deployment,
                               render_compose, render_native, serialized, validate_deployment, write_render, preflight_agent)


class DeploymentConfigTests(unittest.TestCase):
    def setUp(self):
        self.m = json.loads((ROOT / 'examples/deployment/linux-with-remote-mac.json').read_text())

    def rejected(self, mutate):
        m = copy.deepcopy(self.m)
        mutate(m)
        with self.assertRaises((DeploymentConfigError, ValueError)):
            validate_deployment(m)

    def test_linux_and_remote_native_mac_independent_templates(self):
        m = validate_deployment(self.m)
        c = render_compose(m)
        self.assertEqual({'controller', 'android-one', 'pr-one', 'state-init'}, set(c['services']))
        self.assertEqual('0:0', c['services']['state-init']['user'])
        for name in ('controller', 'android-one', 'pr-one'):
            service = c['services'][name]
            self.assertEqual('1000:1000', service['user'])
            self.assertNotIn('build', service)
            self.assertTrue(service['image'].startswith('sha256:'))
        pr_mounts = c['services']['pr-one']['volumes']
        self.assertFalse(any('signing' in v['target'] or 'dws' in v['target'] for v in pr_mounts))
        android_sdk = [v for v in pr_mounts if v['target'] == '/opt/android-sdk'][0]
        self.assertTrue(android_sdk['read_only'])
        nodes = derived_nodes(m)
        init = c['services']['state-init']['command']
        self.assertEqual(hashlib.sha256(serialized(nodes).encode()).hexdigest(), init[0])
        self.assertEqual(hashlib.sha256(serialized(m).encode()).hexdigest(), init[1])
        mac = m['agents'][-1]
        native = render_native(mac, m['controller']['url'])
        plist = plistlib.loads(native['plist'].encode())
        self.assertEqual(mac['service']['user'], plist['UserName'])
        self.assertEqual(mac['service']['home'], plist['EnvironmentVariables']['HOME'])
        self.assertEqual(mac['sdks']['java'], plist['ProgramArguments'][0])
        self.assertEqual(mac['sdks']['java'][:-len('/bin/java')], plist['EnvironmentVariables']['JAVA_HOME'])
        self.assertTrue(plist['EnvironmentVariables']['PATH'].startswith(mac['sdks']['java'][:-len('/java')] + ':'))
        self.assertNotIn('GroupName', plist)
        self.assertEqual('@' + mac['paths']['secret'] + '/agent.secret', plist['ProgramArguments'][plist['ProgramArguments'].index('-secret') + 1])
        self.assertNotIn('sh', plist['ProgramArguments'])
        self.assertIn(mac['id'], plist['Label'])

    def test_unknown_keys_shell_templates_urls_and_inline_secret_rejected(self):
        changes = [lambda m: m.update(hook='curl attacker'), lambda m: m['agents'][0].update(command='touch /tmp/x'),
                   lambda m: m['agents'][0]['sdks'].update(customTemplate='http://attacker/a'),
                   lambda m: m['agents'][0].update(secret='connection-token'),
                   lambda m: m['agents'][0]['paths'].update(work='/home/jenkins/$(id)'),
                   lambda m: m['controller'].update(url='https://user:secret@ci.example.invalid'),
                   lambda m: m['controller'].update(url='http://remote.example.invalid'),
                   lambda m: m['controller'].update(url=['secret']), lambda m: m.update(primaryProjectId='unknown')]

        for change in changes:
            with self.subTest(change=change): self.rejected(change)

    def test_duplicates_root_path_traversal_and_cross_agent_homes_rejected(self):
        changes = [lambda m: m['agents'].append(copy.deepcopy(m['agents'][0])),
                   lambda m: m['agents'][1].update(name=m['agents'][0]['name']),
                   lambda m: m['agents'][0].update(name='.'), lambda m: m['agents'][0].update(name='..'),
                   lambda m: m['agents'][1].update(secretRef=m['agents'][0]['secretRef']),
                   lambda m: m['agents'][0]['service'].update(uid=0),
                   lambda m: m['agents'][0]['service'].update(uid=True),
                   lambda m: m['agents'][0]['paths'].update(work='/home/jenkins/../secret'),
                   lambda m: m['agents'][0]['paths'].update(work='/tmp'),
                   lambda m: m['agents'][0]['paths'].update(cache=m['agents'][0]['paths']['work']),
                   lambda m: m['agents'][0]['labels'].append(m['agents'][0]['labels'][0])]
        for change in changes:
            with self.subTest(change=change): self.rejected(change)
        duplicate_mac = copy.deepcopy(self.m['agents'][-1])
        duplicate_mac.update(id='mac-ios-two', name='mac-ios-two', secretRef='ios-two-connection')
        self.rejected(lambda m: m['agents'].append(duplicate_mac))
        duplicate_mac['service']['home'] += '-two'
        duplicate_mac['paths'] = {k: v.replace('/ios-one/', '/ios-one-two/') for k, v in duplicate_mac['paths'].items()}
        duplicate_mac['sdks']['remoting'] = duplicate_mac['sdks']['remoting'].replace('/ios-one/', '/ios-one-two/')
        self.rejected(lambda m: m['agents'].append(duplicate_mac))

    def test_incompatible_capabilities_resources_secret_and_profile_refs_rejected(self):
        changes = [lambda m: m['agents'][0]['capabilities'].append('ios'),
                   lambda m: m['agents'][-1]['capabilities'].append('harmony'),
                   lambda m: m['agents'][0]['resources'].update(executors=2),
                   lambda m: m['controller']['resources'].update(executors=1),
                   lambda m: m['controller']['service'].update(home='/srv/custom-jenkins-home'),
                   lambda m: m['agents'][0]['resources'].update(memoryGiB=True),
                   lambda m: m['agents'][0]['resources'].update(cpu=0),
                   lambda m: m['agents'][0].update(imageId='jenkins/agent:latest'),
                   lambda m: m['profiles']['example-mobile'].update(android_test='missing-agent'),
                   lambda m: m['profiles']['example-mobile'].update(shell='pr-one'),
                   lambda m: m['agents'][-1].update(remotingSha256='unverified'),
                   lambda m: m['agents'][-1]['sdks'].update(java='/usr/bin/not-java'),
                   lambda m: m['agents'][-1]['expectedVersions'].update(ruby='latest'),
                   lambda m: m['agents'][0]['sdks'].pop('android')]
        for change in changes:
            with self.subTest(change=change): self.rejected(change)

    def test_pr_privileged_label_and_job_binding_rejected(self):
        self.rejected(lambda m: m['agents'][1]['labels'].append('android-builder'))
        self.rejected(lambda m: m['agents'][1]['labels'].append('mac-build'))
        self.rejected(lambda m: m['profiles']['example-mobile'].update(android_release='pr-one'))
        self.rejected(lambda m: m['profiles']['example-mobile'].update(android_test='mac-ios-one'))
        self.rejected(lambda m: m['agents'][0]['labels'].clear())

    def test_secret_volumes_cannot_alias_controller_other_agents_or_reserved_services(self):
        for secret in ('controller-home', 'memory-lock', 'android-one-work', 'android-one-cache',
                       'android-one-runtime', 'android-one-profiles', 'pr-one-work', 'pr-one-profiles'):
            with self.subTest(secret=secret):
                self.rejected(lambda m: m['agents'][1].update(secretRef=secret))
        for key in ('controller', 'agent'):
            with self.subTest(service=key):
                self.rejected(lambda m: (m['controller'] if key == 'controller' else m['agents'][0]).update(id='state-init'))

    def test_automatic_node_self_labels_preserve_pr_and_delivery_isolation(self):
        self.rejected(lambda m: m['agents'][0].update(name='pr-builder'))
        self.rejected(lambda m: m['agents'][1].update(name='android-builder'))
        self.m['profiles']['example-mobile']['ios_test'] = 'mac-ios-one'
        self.rejected(lambda m: m['agents'][-1].update(name='ios-pr-builder'))
        self.m['profiles']['example-mobile'].pop('ios_test')
        self.m['agents'][1]['name'] = 'pr-builder'
        self.m['agents'][1]['labels'] = ['ci-tools']
        project = {'id': 'example-mobile', 'jobs': {key: {} for key in self.m['profiles']['example-mobile']}}
        self.assertEqual(['pr-one'], job_agent_bindings(validate_deployment(self.m), project)['ci_pr']['eligibleAgentIds'])

    def test_multiple_equivalent_build_agents_are_supported_as_label_pool(self):
        second = copy.deepcopy(self.m['agents'][0])
        second.update(id='android-two', name='android-two', secretRef='android-two-connection')
        self.m['agents'].append(second)
        m = validate_deployment(self.m)
        project = {'id': 'example-mobile', 'jobs': {key: {} for key in m['profiles']['example-mobile']}}
        bindings = job_agent_bindings(m, project)
        self.assertEqual(['android-one', 'android-two'], bindings['android_test']['eligibleAgentIds'])
        with self.assertRaises(DeploymentConfigError):
            job_agent_bindings(m, {'id': 'example-mobile', 'jobs': {'app_pr': {}}})

    def test_maintenance_requires_existing_harmony_entrypoint_contract(self):
        node = copy.deepcopy(self.m['agents'][0])
        node.update(id='maintenance-one', name='signing-maintenance', secretRef='maintenance-connection',
                    capabilities=['maintenance'], labels=['signing-maintenance'])
        node['sdks'] = {}
        node['paths']['signing'] = '/home/jenkins/signing'
        self.m['agents'].append(node)
        with self.assertRaisesRegex(DeploymentConfigError, 'maintenance-only'):
            validate_deployment(self.m)
        node['capabilities'].append('harmony')
        node['sdks']['harmony'] = '/opt/harmony-clt'
        valid = validate_deployment(self.m)
        compose = render_compose(valid)
        environment = compose['services']['maintenance-one']['environment']
        self.assertEqual('/opt/harmony-clt', environment['HARMONY_CLT_HOME'])
        self.assertEqual('/home/jenkins/signing', environment['SIGNING_MAINTENANCE_ROOT'])
        self.assertNotIn('CI_LINUX_MEMORY_LOCK', environment)

    def test_native_pr_primary_rejected_until_linux_pipeline_contract_is_ported(self):
        node = self.m['agents'][-1]
        node['capabilities'] = ['ci', 'android']
        node['labels'] = ['pr-builder']
        node['sdks']['android'] = node['service']['home'] + '/android-sdk'
        self.m['profiles']['example-mobile']['ci_pr'] = node['id']
        with self.assertRaisesRegex(DeploymentConfigError, 'PR主Pipeline'):
            validate_deployment(self.m)

    def test_signing_roles_match_reviewed_node_root_and_exclusive_scheduler_label(self):
        node = copy.deepcopy(self.m['agents'][0])
        node.update(id='maintenance-one', name='dedicated-signing', secretRef='maintenance-connection',
                    capabilities=['maintenance', 'harmony'], labels=['signing-maintenance'])
        node['sdks']['harmony'] = '/opt/harmony-clt'
        node['paths']['signing'] = '/home/jenkins/signing'
        self.m['agents'].append(node)
        for role in ('signing_maintenance', 'signing_backup'):
            with self.subTest(role=role):
                self.m['profiles']['example-mobile'][role] = node['id']
                manifest = validate_deployment(self.m)
                project = {'id': 'example-mobile', 'jobs': {key: {} for key in manifest['profiles']['example-mobile']},
                           'signingBackup': {'node': node['name'], 'root': node['paths']['signing']}}
                binding = job_agent_bindings(manifest, project)[role]
                self.assertEqual(node['name'], binding['label'])
                self.assertEqual([node['id']], binding['eligibleAgentIds'])
                for policy in ({}, {'node': 'other-signing', 'root': '/home/jenkins/signing'},
                               {'node': node['name'], 'root': '/home/jenkins/other-signing'}):
                    with self.assertRaisesRegex(DeploymentConfigError, '受信profile政策'):
                        job_agent_bindings(manifest, project | {'signingBackup': policy})
                manifest['agents'][0]['labels'].append(node['name'])
                with self.assertRaisesRegex(DeploymentConfigError, '不能同时调度'):
                    job_agent_bindings(manifest, project)
                self.m['profiles']['example-mobile'].pop(role)

    def test_ios_delivery_pool_cannot_share_pr_child_labels(self):
        for role in ('ios_test', 'ios_release'):
            for direct in (True, False):
                with self.subTest(role=role, directlyBound=direct):
                    manifest = copy.deepcopy(self.m)
                    manifest['profiles']['example-mobile'][role] = 'mac-ios-one'
                    if direct:
                        manifest['agents'][-1]['labels'].append('ios-pr-builder')
                    else:
                        node = copy.deepcopy(manifest['agents'][-1])
                        node.update(id='ios-pr-two', name='ios-pr-two', secretRef='ios-pr-two-connection')
                        node['service'].update(user='ci_ios_two', uid=503, home='/private/var/lib/mobile-ci/ios-two')
                        node['paths'] = {k: v.replace('/ios-one/', '/ios-two/') for k, v in node['paths'].items()}
                        node['sdks']['remoting'] = node['sdks']['remoting'].replace('/ios-one/', '/ios-two/')
                        node['labels'].append('ios-pr-builder')
                        manifest['agents'].append(node)
                    with self.assertRaisesRegex(DeploymentConfigError, 'iOS交付标签pool'):
                        validate_deployment(manifest)

    def test_linux_pr_binding_rejects_native_mac_in_same_label_pool(self):
        node = self.m['agents'][-1]
        node['capabilities'] = ['ci', 'android']
        node['labels'] = ['pr-builder']
        node['sdks'].pop('xcode')
        node['sdks']['android'] = node['service']['home'] + '/android-sdk'
        node['expectedVersions'] = {'python': '3.13.7'}
        for role in ('app_pr', 'ci_pr'):
            with self.subTest(role=role):
                self.m['profiles']['example-mobile'][role] = 'pr-one'
                with self.assertRaisesRegex(DeploymentConfigError, 'PR主Pipeline标签pool'):
                    validate_deployment(self.m)
                self.m['profiles']['example-mobile'].pop(role)
        node['labels'] = ['native-ci-tools']
        self.m['profiles']['example-mobile']['ci_pr'] = 'pr-one'
        self.assertEqual(['pr-one'], job_agent_bindings(validate_deployment(self.m),
            {'id': 'example-mobile', 'jobs': {role: {} for role in self.m['profiles']['example-mobile']}})['ci_pr']['eligibleAgentIds'])

    def test_private_bounded_single_link_loader_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp).resolve(); root.chmod(0o700)
            p = root / 'instance.json'; p.write_text(serialized(self.m)); p.chmod(0o600)
            self.assertEqual(self.m, load_deployment(str(p), private=True))
            p.chmod(0o644)
            with self.assertRaises(DeploymentConfigError): load_deployment(str(p), private=True)
            p.chmod(0o600); p.write_text('{"version":1,"version":1}')
            with self.assertRaises(DeploymentConfigError): load_deployment(str(p))
            p.write_text(' ' * 65537)
            with self.assertRaises(ValueError): load_deployment(str(p))
            p.write_text(serialized(self.m)); os.link(p, root / 'hardlink')
            with self.assertRaises(ValueError): load_deployment(str(p), private=True)

    def test_render_private_artifacts_no_overwrite_and_installed_profiles_required(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp).resolve(); root.chmod(0o700)
            self.m['controller']['stateRoot'] = str(root)
            profile = {'id': 'example-mobile', 'jobs': {key: {} for key in self.m['profiles']['example-mobile']}}
            with patch('deployment_config.load_project', return_value=profile):
                result = write_render(self.m, root)
                self.assertIn('nodes.json', result['artifacts'])
                for name in result['artifacts']:
                    self.assertEqual(0o600, (root / name).stat().st_mode & 0o777)
                self.assertEqual(render_compose(self.m), json.loads((root / 'compose.json').read_text()))
                with self.assertRaises(DeploymentConfigError): write_render(self.m, root)
                profile['testOnly'] = True
                with self.assertRaises(DeploymentConfigError): write_render(self.m, root)

    def test_fixed_example_plan_has_explicit_offline_test_only_boundary(self):
        sample = ROOT / 'examples/deployment/linux.json'
        result = subprocess.run([sys.executable, str(ROOT / 'deployment_config.py'), 'plan', str(sample), '--example'], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        plan = json.loads(result.stdout)
        self.assertTrue(plan['testOnly']); self.assertTrue(plan['offline']); self.assertFalse(plan['trustedProfilesVerified'])
        self.assertFalse(plan['applySupported'])
        for flags in ([], ['--example', '--private'], ['--example', '--output-dir', '/tmp']):
            r = subprocess.run([sys.executable, str(ROOT / 'deployment_config.py'), 'plan', str(sample), *flags], capture_output=True, text=True)
            self.assertNotEqual(0, r.returncode)

    def test_native_preflight_rejects_unknown_compose_and_wrong_actual_host(self):
        for aid in ('unknown', 'android-one'):
            with self.assertRaises(DeploymentConfigError): preflight_agent(self.m, aid)
        with patch('deployment_config.host_platform.system', return_value='Linux'), patch('deployment_config.host_platform.machine', return_value='x86_64'), patch('deployment_config.toolchain_preflight') as probe:
            with self.assertRaises(DeploymentConfigError): preflight_agent(self.m, 'mac-ios-one')
            probe.assert_not_called()

    def test_selected_native_preflight_uses_only_bound_paths_hash_and_expected_versions(self):
        import types
        agent = self.m['agents'][-1]
        account = types.SimpleNamespace(pw_name=agent['service']['user'], pw_dir=agent['service']['home'], pw_gid=agent['service']['gid'])
        fake_stat = types.SimpleNamespace(st_uid=agent['service']['uid'], st_mode=0o700)
        with patch('deployment_config.host_platform.system', return_value='Darwin'), patch('deployment_config.host_platform.machine', return_value='arm64'), patch('deployment_config.os.getuid', return_value=agent['service']['uid']), patch('deployment_config.os.getgid', return_value=agent['service']['gid']), patch('deployment_config.pwd.getpwuid', return_value=account), patch('deployment_config.Path.is_dir', return_value=True), patch('deployment_config.Path.resolve', lambda p: p), patch('deployment_config.Path.stat', return_value=fake_stat), patch('deployment_config.toolchain_preflight', return_value={'scope':'runtime-preflight-only'}) as probe:
            result = preflight_agent(self.m, agent['id'])
            probe.assert_called_once_with('ios', sdk_root=None, java_bin=agent['sdks']['java'], remoting_jar=agent['sdks']['remoting'], remoting_sha256=agent['remotingSha256'], expected_versions=agent['expectedVersions'])
            self.assertFalse(result['productionTouched'])
            self.assertFalse(result['profileReferencesVerified'])

    def test_no_apply_cli_and_no_untrusted_file_output(self):
        sample = ROOT / 'examples/deployment/linux.json'
        for args in (['apply', str(sample)], ['render', str(sample), '--output-dir', '/tmp']):
            r = subprocess.run([sys.executable, str(ROOT / 'deployment_config.py'), *args], capture_output=True, text=True)
            self.assertNotEqual(0, r.returncode)
        r = subprocess.run([sys.executable, str(ROOT / 'deployment_config.py'), 'validate', str(sample)], capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stderr)


if __name__ == '__main__': unittest.main()
