"""默认发行目标不继承 SDK/DWS 层；历史私有构建仍须显式选择。"""
import json
import os
from pathlib import Path
import subprocess
import shutil
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SdkFreeImageTest(unittest.TestCase):
    def test_default_image_cannot_copy_or_inherit_private_payload(self):
        source = (ROOT / 'jenkins/remote/android-agent/Dockerfile').read_text()
        runtime, private = source.split('FROM runtime AS private-sdk')
        private, default = private.split('FROM runtime AS sdk-free')
        self.assertIn('FROM ${JENKINS_AGENT_IMAGE} AS runtime', runtime)
        self.assertIn('RUN --mount=type=tmpfs,target=/tmp', runtime)
        for forbidden in ('sdkmanager', 'dl.google.com/android', 'releases/download/v${DWS_VERSION}', '/usr/local/bin/dws', 'COPY --from='):
            self.assertNotIn(forbidden, runtime)
            self.assertNotIn(forbidden, default)
        self.assertNotIn('dws', source.lower())
        self.assertIn('sdkmanager', private)
        self.assertIn('sha256sum --check -', private)
        self.assertIn('USER jenkins', private)
        self.assertEqual('', default.strip())

    def test_legacy_compose_selects_private_target_but_harmony_overlay_replaces_it(self):
        compose = (ROOT / 'jenkins/remote/compose.yaml').read_text()
        self.assertEqual(2, compose.count('target: private-sdk'))
        command = ['docker-compose'] if shutil.which('docker-compose') else ['docker', 'compose']
        result = subprocess.run([*command, '--env-file', os.devnull,
                                 '-f', str(ROOT / 'jenkins/remote/compose.yaml'),
                                 '-f', str(ROOT / 'jenkins/remote/compose.ohos.yaml'), 'config', '--format', 'json'],
                                env={**os.environ, 'OHOS_CLT_HOST_DIR': '/tmp/verified-clt',
                                     'OHOS_AGENT_BASE_IMAGE': 'example/android-agent:reviewed'},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        services = json.loads(result.stdout)['services']
        self.assertEqual('unified-runtime', services['android-agent']['build']['target'])
        self.assertEqual('private-sdk', services['pr-agent']['build']['target'])
        compat = (ROOT / 'jenkins/remote/ohos-agent/Dockerfile.compat').read_text()
        self.assertIn('FROM ${OHOS_AGENT_BASE_IMAGE} AS unified-runtime', compat)


if __name__ == '__main__':
    unittest.main()
