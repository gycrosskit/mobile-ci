import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('deployment_runtime', ROOT / 'jenkins/remote/deployment-runtime.py')
runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)
from deployment_config import DeploymentConfigError, render_compose, serialized


class DeploymentRemoteRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.m = json.loads((ROOT / 'examples/deployment/linux.json').read_text())
        self.config = render_compose(self.m)

    def test_generated_output_is_the_only_accepted_runtime_configuration(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp).resolve(); root.chmod(0o700)
            self.m['controller']['stateRoot'] = str(root)
            manifest, compose = root / 'deployment.json', root / 'compose.json'
            manifest.write_text(serialized(self.m)); manifest.chmod(0o600)
            config = render_compose(self.m)
            compose.write_text(serialized(config)); compose.chmod(0o600)
            with patch.object(runtime, 'verify_profiles'):
                self.assertEqual((self.m, config), runtime.select(str(manifest), str(compose)))
                config['services']['pr-one']['volumes'].append({'type': 'bind', 'source': '/private/secret', 'target': '/private/secret'})
                compose.write_text(serialized(config))
                with self.assertRaises(DeploymentConfigError): runtime.select(str(manifest), str(compose))
                compose.write_text(serialized(render_compose(self.m))); compose.chmod(0o644)
                with self.assertRaises(DeploymentConfigError): runtime.select(str(manifest), str(compose))

    def fake_snapshot(self):
        snapshots = {}
        for sid in ('controller', 'android-one', 'pr-one'):
            s = self.config['services'][sid]
            snapshots[sid] = {'State': {'Running': True, 'Health': {'Status': 'healthy'}}, 'Image': s['image'],
                'Config': {'User': s['user'], 'Env': [k + '=' + str(v) for k,v in s['environment'].items()]},
                'HostConfig': {'Memory': int(s['mem_limit'][:-1]) * 1024**3, 'NanoCpus': s['cpus'] * 10**9},
                'Mounts': [{'Type': v['type'], 'Destination': v['target'], 'RW': not v.get('read_only', False),
                            'Name': self.config['volumes'][v['source']]['name'] if v['type'] == 'volume' else '',
                            'Source': v['source'] if v['type'] == 'bind' else ''} for v in s['volumes']]}
        return snapshots

    def verify_fixture(self, snapshots):
        by_name = {a['name']: a for a in self.m['agents']}
        def command(args):
            if args[-3:-1] == ['ps', '--quiet']: return args[-1]
            if args[:2] == ['docker', 'inspect']: return json.dumps([snapshots[args[-1]]])
            if args[:2] == ['docker', 'exec']:
                name = args[-1].split('/')[-2]; a = by_name[name]
                return '<slave><numExecutors>1</numExecutors><mode>EXCLUSIVE</mode><remoteFS>' + a['paths']['work'] + '</remoteFS><label>' + ' '.join(a['labels']) + '</label></slave>'
            raise AssertionError(args)
        with patch.object(runtime, 'run', side_effect=command): runtime.verify(self.m, self.config, ['docker','compose'])

    def test_resource_identity_mount_sdk_and_environment_drift_rejected(self):
        self.verify_fixture(self.fake_snapshot())
        mutations = [lambda f: f['pr-one']['HostConfig'].update(Memory=1),
                     lambda f: f['pr-one']['HostConfig'].update(NanoCpus=1),
                     lambda f: f['pr-one']['Config'].update(User='0:0'),
                     lambda f: f['pr-one']['Config'].update(Env=[]),
                     lambda f: f['pr-one'].update(Image='sha256:'+'0'*64),
                     lambda f: f['pr-one']['Mounts'].append({'Destination':'/signing','Type':'bind','RW':True}),
                     lambda f: f['pr-one']['Mounts'][-1].update(RW=True),
                     lambda f: f['controller']['State']['Health'].update(Status='unhealthy')]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                f=self.fake_snapshot();mutation(f)
                with self.assertRaises(DeploymentConfigError): self.verify_fixture(f)

    def test_runtime_host_and_docker_vm_are_independently_checked(self):
        with patch.object(runtime.platform, 'system', return_value='Darwin'), patch.object(runtime.platform, 'machine', return_value='arm64'), patch.object(runtime, 'run') as run:
            with self.assertRaises(DeploymentConfigError): runtime.check_host(self.m, self.config)
            run.assert_not_called()
        with patch.object(runtime.platform, 'system', return_value='Linux'), patch.object(runtime.platform, 'machine', return_value='x86_64'), patch.object(runtime, 'run', side_effect=['ready', json.dumps({'OSType':'linux','Architecture':'arm64','NCPU':64,'MemTotal':100*1024**3})]):
            with self.assertRaises(DeploymentConfigError): runtime.check_host(self.m, self.config)

class DeploymentStackIntegrationTests(unittest.TestCase):
    def test_stack_consumes_rendered_services_and_rejects_tampered_overlays_before_docker(self):
        import shutil
        import subprocess
        import sys
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp).resolve(); root.chmod(0o700)
            remote = root / 'jenkins/remote'; remote.mkdir(parents=True)
            (root / 'jenkins/controller/init.groovy.d').mkdir(parents=True)
            for name in ('project_config.py', 'deployment_config.py', 'toolchains.py', 'toolchains.lock.json'):
                shutil.copyfile(ROOT / name, root / name)
            for name in ('compose-context.sh', 'stack.sh', 'deployment-runtime.py', 'init-deployment-state.sh'):
                shutil.copyfile(ROOT / 'jenkins/remote' / name, remote / name)
            # Hardware checks are covered independently; retain real manifest/profile/template selection here.
            helper = remote / 'deployment-runtime.py'
            helper.write_text(helper.read_text().replace('            check_host(m, config)', "            require(True, 'fixture host check')"))
            projects = root / 'projects'; projects.mkdir(mode=0o700)
            profile = json.loads((ROOT / 'examples/second-project/project.json').read_text()); profile.pop('testOnly')
            (projects / 'example-mobile.json').write_text(serialized(profile)); (projects / 'example-mobile.json').chmod(0o600)
            private = root / 'private'; private.mkdir(mode=0o700)
            m = json.loads((ROOT / 'examples/deployment/linux.json').read_text()); m['controller']['stateRoot'] = str(private)
            inputfile = private / 'input.json'; inputfile.write_text(serialized(m)); inputfile.chmod(0o600)
            r = subprocess.run([sys.executable, str(root / 'deployment_config.py'), 'render', str(inputfile), '--private', '--output-dir', str(private)], capture_output=True, text=True)
            self.assertEqual(0, r.returncode, r.stderr)
            config = json.loads((private / 'compose.json').read_text())
            log = root / 'docker.log'; docker = root / 'docker'
            docker.write_text('#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ["DOCKER_LOG"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\nif "--services" in sys.argv: print("controller\\nandroid-one\\npr-one\\nstate-init")\n')
            docker.chmod(0o755)
            env = dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], DOCKER_LOG=str(log),
                       JENKINS_DEPLOYMENT_FILE=str(private / 'deployment.json'), JENKINS_COMPOSE_FILE=str(private / 'compose.json'))
            env.pop('COMPOSE_FILE', None)
            r = subprocess.run(['bash', str(remote / 'stack.sh'), 'start'], env=env, capture_output=True, text=True)
            self.assertEqual(0, r.returncode, r.stderr)
            calls = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertEqual(['up','--detach','--no-build','controller','android-one','pr-one'], calls[-1][-6:])
            before = log.read_text()
            for action, extra in [('build', {}), ('status', {'COMPOSE_FILE':'arbitrary.yaml'})]:
                r = subprocess.run(['bash', str(remote / 'stack.sh'), action], env=dict(env, **extra), capture_output=True, text=True)
                self.assertNotEqual(0, r.returncode); self.assertEqual(before, log.read_text())
            config['services']['pr-one']['environment']['HOME'] = '/other-user'
            (private / 'compose.json').write_text(serialized(config))
            r = subprocess.run(['bash', str(remote / 'stack.sh'), 'start'], env=env, capture_output=True, text=True)
            self.assertNotEqual(0, r.returncode); self.assertEqual(before, log.read_text())


if __name__ == '__main__': unittest.main()
