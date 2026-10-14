"""可选离线Linux容器验收；只使用显式已缓存镜像和本测试fixture绑定。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DeploymentStateRuntimeTests(unittest.TestCase):
    def test_private_state_initializer_on_real_linux_filesystem(self):
        image = os.environ.get('DEPLOYMENT_LINUX_TEST_IMAGE')
        self.assertTrue(image, '设置已缓存 DEPLOYMENT_LINUX_TEST_IMAGE 才执行隔离Linux验收；缺失环境不能skip，不自动拉取镜像')
        docker = ['docker']
        if os.environ.get('DEPLOYMENT_DOCKER_CONTEXT'):
            docker += ['--context', os.environ['DEPLOYMENT_DOCKER_CONTEXT']]
        inspect = subprocess.run(docker + ['image', 'inspect', image, '--format', '{{.Id}}'], capture_output=True, text=True)
        self.assertEqual(0, inspect.returncode, '仅允许已缓存镜像')
        image_id = inspect.stdout.strip()
        self.assertRegex(image_id, r'^sha256:[a-f0-9]{64}$')
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp).resolve()
            for name in ('init', 'memory', 'source'): (root / name).mkdir()
            source = root / 'source'; (source / 'profiles').mkdir()
            nodes = b'{"fixture":"nodes"}\n'; deployment = b'{"fixture":"deployment"}\n'
            (source / 'nodes.json').write_bytes(nodes); (source / 'deployment.json').write_bytes(deployment)
            (source / 'profiles/fixture.json').write_text('{"id":"fixture"}')
            nd, dd = hashlib.sha256(nodes).hexdigest(), hashlib.sha256(deployment).hexdigest()
            args = ' '.join([nd, dd, 'fixture', 'one', 'fixture:' + hashlib.sha256((source / 'profiles/fixture.json').read_bytes()).hexdigest(), 'controller-home', 'one-work', 'one-connection'])
            shell = '''set -euo pipefail
cleanup() { chown -R OWNER:GROUP /init /memory; chmod -R u+rwX /init /memory; }
trap cleanup EXIT
mkdir -p /init/controller-home /init/one-work /init/one-connection /profiles/one
chown 0:0 /init/controller-home /init/one-work /init/one-connection /memory
chmod 755 /init/controller-home /init/one-work /init/one-connection /memory
bash /script ARGS
[[ "$(stat -c '%u:%g:%a:%h' /init/controller-home/ci-deployment/nodes.json)" == 1000:1000:600:1 ]]
[[ "$(stat -c '%u:%g:%a' /init/one-work)" == 1000:1000:700 ]]
[[ "$(stat -c '%u:%g:%a:%h' /memory/build.lock)" == 1000:1000:600:1 ]]
[[ "$(stat -c '%u:%g:%a' /profiles/one/projects)" == 0:1000:750 ]]
[[ "$(stat -c '%u:%g:%a' /init/controller-home/init.groovy.d)" == 1000:1000:700 ]]
su jenkins -s /bin/bash -c 'cp /source/nodes.json /init/controller-home/init.groovy.d/fixture.groovy'

[[ "$(stat -c '%u:%g:%a:%h' /profiles/one/projects/fixture.json)" == 0:1000:440:1 ]]
bash /script ARGS
before=$(sha256sum /init/controller-home/ci-deployment/nodes.json)
if bash /script WRONGARGS; then exit 91; fi
[[ "$before" == "$(sha256sum /init/controller-home/ci-deployment/nodes.json)" ]]
chmod 644 /init/controller-home/ci-deployment/nodes.json
if bash /script ARGS; then exit 92; fi
[[ "$(stat -c '%a' /init/controller-home/ci-deployment/nodes.json)" == 644 ]]
chmod 600 /init/controller-home/ci-deployment/nodes.json
printf 'unknown installed configuration' > /init/controller-home/ci-deployment/nodes.json
before=$(sha256sum /init/controller-home/ci-deployment/nodes.json)
if bash /script ARGS; then exit 93; fi
[[ "$before" == "$(sha256sum /init/controller-home/ci-deployment/nodes.json)" ]]
printf '%s' 'empty-init,idempotence,digest-reject,mode-reject,unknown-content-preserved passed'
'''.replace('OWNER', str(os.getuid())).replace('GROUP', str(os.getgid())).replace('WRONGARGS', '0' * 64 + ' ' + ' '.join(args.split()[1:])).replace('ARGS', args)
            cmd = docker + ['run', '--rm', '--network', 'none', '--cpus', '1', '--memory', '128m', '--user', '0:0',
                           '--tmpfs', '/init:rw,mode=0755', '--tmpfs', '/memory:rw,mode=0755', '--tmpfs', '/profiles:rw,mode=0755', '--entrypoint', '/bin/bash', '--mount', 'type=bind,src=' + str(ROOT / 'jenkins/remote/init-deployment-state.sh') + ',dst=/script,readonly']
            for name in ('source',):
                cmd += ['--mount', 'type=bind,src=' + str(root / name) + ',dst=/' + name + (',readonly' if name == 'source' else '')]
            r = subprocess.run(cmd + [image_id, '-c', shell], capture_output=True, text=True, timeout=60)
            self.assertEqual(0, r.returncode, r.stderr + r.stdout)
            self.assertIn('passed', r.stdout)


if __name__ == '__main__': unittest.main()
