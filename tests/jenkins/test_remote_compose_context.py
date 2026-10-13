import json
import io
import os
import shutil
import subprocess
import tempfile
import tarfile
import unittest
from pathlib import Path

REMOTE = Path(__file__).resolve().parents[2] / 'jenkins' / 'remote'
SERVICES = ('controller', 'android-agent', 'pr-agent', 'ohos-maintenance')


class RemoteComposeContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.remote = self.root / 'jenkins/remote'
        self.remote.mkdir(parents=True)
        shutil.copyfile(REMOTE.parents[1] / 'project_config.py', self.root / 'project_config.py')
        shutil.copytree(REMOTE.parents[1] / 'projects', self.root / 'projects')
        for name in ('compose-context.sh', 'stack.sh', 'verify.sh', 'restore.sh', 'backup_resources.py', 'dws.sh', 'public.sh'):
            shutil.copyfile(REMOTE / name, self.remote / name)
        (self.remote / 'check-host.sh').write_text('exit 0\n')
        for name in ('compose.yaml', 'compose.ohos.yaml'):
            (self.remote / name).write_text('services: {}\n')
        self.private = self.root / 'private'
        self.private.mkdir(mode=0o700)
        self.manifest = self.private / 'compose.json'
        self.manifest.write_text('{}')
        self.manifest.chmod(0o600)
        self.bind = self.root / 'source'
        self.bind.mkdir()
        config = {'name': 'test', 'volumes': {'work': {'name': 'agent-work'}}, 'services': {}}
        actual = {}
        for service in SERVICES:
            env = {'JENKINS_AGENT_WORKDIR': '/home/jenkins/agent', 'DELIVERY_STATE_ROOT': '/home/jenkins/agent/delivery-state'}
            desired = {'container_name': 'container-' + service, 'image': 'image-' + service, 'environment': env, 'healthcheck': {'test': ['CMD', 'true']},
                       'volumes': [{'type': 'volume', 'source': 'work', 'target': '/home/jenkins/agent'},
                                   {'type': 'bind', 'source': str(self.bind), 'target': '/source', 'read_only': True}]}
            config['services'][service] = desired
            actual[service] = {'State': {'Running': True, 'Health': {'Status': 'healthy'}},
                               'Image': 'id-image-' + service,
                               'Config': {'Env': [key + '=' + value for key, value in env.items()],
                                          'Labels': {'com.docker.compose.project.config_files': str(self.manifest)}},
                               'Mounts': [{'Type': 'volume', 'Name': 'agent-work', 'Destination': '/home/jenkins/agent', 'RW': True},
                                          {'Type': 'bind', 'Source': str(self.bind), 'Destination': '/source', 'RW': False}]}
        for service, target, name in (
            ('controller', '/var/jenkins_home', 'home'),
            ('android-agent', '/home/jenkins/.dws-state', 'dws'),
            ('ohos-maintenance', '/home/jenkins/signing', 'signing'),
        ):
            config['volumes'][name] = {'name': name}
            config['services'][service]['volumes'].append({'type': 'volume', 'source': name, 'target': target})
            actual[service]['Mounts'].append({'Type': 'volume', 'Name': name, 'Destination': target, 'RW': True})
        self.fixture = {'config': config, 'actual': actual}
        self.fixture_path = self.root / 'fixture.json'
        self.log = self.root / 'docker.log'
        self.docker = self.root / 'docker'
        self.docker.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['DOCKER_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
f = json.loads(Path(os.environ['DOCKER_FIXTURE']).read_text())
if args[0] == 'compose':
    i = 1
    while args[i] in ('--project-directory', '-f', '--profile'):
        i += 2
    cmd = args[i:]
    if cmd[0] == 'config':
        if '--services' in cmd:
            print('\\n'.join(f['config']['services']))
        elif '--format' in cmd:
            print(json.dumps(f['config']))
    elif cmd[0] == 'ps' and '--quiet' in cmd:
        print(cmd[-1])
    elif cmd[0] == 'exec' and cmd[2] == os.environ.get('FAIL_EXEC'):
        print('SECRET_SHOULD_NOT_LEAK', file=sys.stderr)
        sys.exit(12)
elif args[0] == 'inspect':
    print(json.dumps([f['actual'][args[-1]]]))
elif args[:2] == ['image', 'inspect']:
    print(json.dumps([{'Id': 'id-' + args[-1]}]))
elif args[0] == 'exec':
    name = args[-1].split('/')[-2]
    labels = {'linux-build': 'android-builder ohos-linux linux-build', 'pr-linux': 'pr-builder', 'signing-maintenance': 'signing-maintenance'}
    xml = '<slave><description>管理员维护节点</description><remoteFS>/home/jenkins/agent</remoteFS><numExecutors>' + os.environ.get('NODE_EXECUTORS', '1') + '</numExecutors><mode>EXCLUSIVE</mode><label>' + labels[name] + '</label></slave>'
    os.write(1, xml.encode('utf-8'))
elif args[:2] == ['volume', 'inspect']:
    sys.exit(1)
''', encoding='utf-8')
        self.docker.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                        DOCKER_LOG=str(self.log), DOCKER_FIXTURE=str(self.fixture_path), JENKINS_COMPOSE_FILE=str(self.manifest))
        self.env.pop('COMPOSE_FILE', None)

    def execute(self, script, *args, **env):
        self.fixture_path.write_text(json.dumps(self.fixture))
        return subprocess.run(['bash', str(self.remote / script), *args], env=dict(self.env, **env),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_start_uses_same_private_complete_manifest_without_build(self):
        result = self.execute('stack.sh', 'start')
        self.assertEqual(0, result.returncode, result.stderr)
        cmd = self.commands()[-1]
        self.assertIn(str(self.manifest), cmd)
        self.assertEqual(['up', '--detach', '--no-build', *SERVICES], cmd[-7:])
        self.assertNotIn('--build', cmd)
        self.assertEqual(0, self.execute('stack.sh', 'status').returncode)
        self.assertIn(str(self.manifest), self.commands()[-1])

    def test_portable_default_includes_both_templates(self):
        result = self.execute('stack.sh', 'status', JENKINS_COMPOSE_FILE='')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(str(self.remote / 'compose.yaml'), self.commands()[-1])
        self.assertIn(str(self.remote / 'compose.ohos.yaml'), self.commands()[-1])

    def test_build_is_explicit_and_separate(self):
        result = self.execute('stack.sh', 'build')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(['build', 'controller', 'android-agent', 'pr-agent'], self.commands()[-1][-4:])
        self.assertFalse(any('up' in cmd for cmd in self.commands()))

    def test_dws_and_public_use_the_same_complete_private_manifest(self):
        self.assertEqual(0, self.execute('dws.sh', 'version').returncode)
        self.assertIn(str(self.manifest), self.commands()[-1])
        self.assertEqual('android-agent', self.commands()[-1][-2])
        self.fixture['config']['services']['ngrok'] = {}
        result = self.execute('public.sh', 'start')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(str(self.manifest), self.commands()[-1])
        self.assertEqual(['up', '--detach', '--no-build', *SERVICES, 'ngrok'], self.commands()[-1][-8:])
        self.assertNotIn('--build', self.commands()[-1])

    def test_native_compose_file_list_and_jenkins_precedence(self):
        second = self.private / 'overlay.json'
        second.write_text('{}')
        second.chmod(0o600)
        result = self.execute('stack.sh', 'status', JENKINS_COMPOSE_FILE='', COMPOSE_FILE=str(self.manifest) + ':' + str(second))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(str(second), self.commands()[-1])
        result = self.execute('stack.sh', 'status', COMPOSE_FILE='relative.yaml')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn('relative.yaml', self.commands()[-1])

    def test_private_manifest_security_rejects_before_docker(self):
        for value in ('relative.json', str(self.private / 'missing.json')):
            result = self.execute('stack.sh', 'status', JENKINS_COMPOSE_FILE=value)
            self.assertNotEqual(0, result.returncode)
        self.manifest.chmod(0o644)
        self.assertNotEqual(0, self.execute('stack.sh', 'status').returncode)
        self.manifest.chmod(0o600)
        self.private.chmod(0o755)
        self.assertNotEqual(0, self.execute('stack.sh', 'status').returncode)
        self.private.chmod(0o700)
        link = self.private / 'link.json'
        link.symlink_to(self.manifest)
        self.assertNotEqual(0, self.execute('stack.sh', 'status', JENKINS_COMPOSE_FILE=str(link)).returncode)
        self.assertEqual([], self.commands())

    def test_incomplete_manifest_blocks_start(self):
        del self.fixture['config']['services']['pr-agent']
        result = self.execute('stack.sh', 'start')
        self.assertNotEqual(0, result.returncode)
        self.assertIn('missing pr-agent', result.stderr)
        self.assertFalse(any('up' in cmd for cmd in self.commands()))

    def test_full_verify_and_each_runtime_drift(self):
        self.assertEqual(0, self.execute('verify.sh').returncode)
        mutations = (
            lambda f: f['actual']['ohos-maintenance']['State'].update(Running=False),
            lambda f: f['actual']['android-agent'].update(Image='old-image'),
            lambda f: f['actual']['android-agent']['Config'].update(Env=['JENKINS_AGENT_WORKDIR=/home/jenkins/agent', 'DELIVERY_STATE_ROOT=/tmp/state']),
            lambda f: f['actual']['pr-agent']['Mounts'].pop(),
            lambda f: f['actual']['controller']['Config']['Labels'].update({'com.docker.compose.project.config_files': '/old/split.json'}),
            lambda f: f['actual']['pr-agent']['Mounts'].append({'Name': 'sxmqlive_dws_state', 'Destination': '/private', 'Type': 'volume', 'RW': True}),
        )
        original = json.dumps(self.fixture)
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.fixture = json.loads(original)
                mutate(self.fixture)
                self.assertNotEqual(0, self.execute('verify.sh').returncode)
        self.fixture = json.loads(original)
        for service in SERVICES[1:]:
            result = self.execute('verify.sh', FAIL_EXEC=service)
            self.assertNotEqual(0, result.returncode)
            self.assertNotIn('SECRET_SHOULD_NOT_LEAK', result.stderr + result.stdout)
        self.assertNotEqual(0, self.execute('verify.sh', NODE_EXECUTORS='2').returncode)

    def test_utf8_node_configuration_under_ascii_locale(self):
        result = self.execute('verify.sh', LC_ALL='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0')
        self.assertEqual(0, result.returncode, result.stderr)

    def test_controller_requires_healthy_even_when_manifest_omits_healthcheck(self):
        del self.fixture['config']['services']['controller']['healthcheck']
        self.assertEqual(0, self.execute('verify.sh').returncode)
        for status in ('unhealthy', 'starting', None):
            with self.subTest(status=status):
                state = self.fixture['actual']['controller']['State']
                state['Health'] = {'Status': status} if status is not None else {}
                result = self.execute('verify.sh')
                self.assertNotEqual(0, result.returncode)
                self.assertIn('controller is not healthy', result.stderr)

    def test_restore_rejects_manifest_with_different_material_volumes_before_writes(self):
        bundle = self.root / 'migration.tar.gz'
        bundle.write_bytes(b'not-read-before-manifest-check')
        self.fixture['config']['services']['controller']['volumes'].pop()
        result = self.execute('restore.sh', str(bundle))
        self.assertNotEqual(0, result.returncode)
        self.assertIn('灾备资源解析失败', result.stderr)
        self.assertTrue(all(str(self.manifest) in cmd for cmd in self.commands()))
        self.assertFalse(any('create' in cmd for cmd in self.commands()))

    def test_restore_rejects_outer_path_escape_before_extract_or_volume_creation(self):
        config = self.fixture['config']
        for service, target, name in (
            ('controller', '/var/jenkins_home', 'sxmqlive_jenkins_home'),
            ('android-agent', '/home/jenkins/.dws-state', 'sxmqlive_dws_state'),
            ('android-agent', '/home/jenkins/agent', 'sxmqlive_jenkins_agent_work'),
            ('ohos-maintenance', '/home/jenkins/signing', 'sxmqlive_ohos_signing'),
        ):
            config['volumes'][name] = {'name': name}
            config['services'][service]['volumes'] = [v for v in config['services'][service]['volumes'] if v['target'] != target]
            config['services'][service]['volumes'].append({'type': 'volume', 'source': name, 'target': target})
        bundle = self.root / 'malicious.tar.gz'
        with tarfile.open(bundle, 'w:gz') as archive:
            info = tarfile.TarInfo('../' + self.root.name + '/sentinel')
            info.size = 4
            archive.addfile(info, io.BytesIO(b'data'))
        result = self.execute('restore.sh', str(bundle))
        self.assertNotEqual(0, result.returncode)
        self.assertIn('Invalid migration bundle', result.stderr)
        self.assertFalse(any('create' in cmd for cmd in self.commands()))
        self.assertFalse((self.root / 'sentinel').exists())


if __name__ == '__main__':
    unittest.main()
