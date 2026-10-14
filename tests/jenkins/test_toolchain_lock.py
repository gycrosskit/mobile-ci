import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import toolchains

ROOT = Path(__file__).resolve().parents[2]


class ToolchainLockTest(unittest.TestCase):
    def test_reviewed_lock_and_generated_build_args_match_executable_consumers(self):
        lock = toolchains.load_toolchain_lock()
        self.assertEqual(toolchains.lock_digest(lock), toolchains.check_repository_pins(lock))
        self.assertIn('@sha256:', toolchains.docker_build_args('controller')['JENKINS_CONTROLLER_IMAGE'])
        candidate = copy.deepcopy(lock)
        candidate['components']['androidNDK']['version'] = '27.0.11718014'
        self.assertEqual('27.0.11718014', toolchains.docker_build_args('android', candidate)['ANDROID_NDK_VERSION'])
        self.assertNotEqual(toolchains.lock_digest(lock), toolchains.lock_digest(candidate))

    def test_unknown_os_arch_and_components_are_rejected(self):
        for component, system, arch in [('harmonyCLT', 'darwin', 'arm64'), ('androidNDK', 'linux', 'arm64'),
                                        ('xcode', 'linux', 'amd64'), ('unknown', 'linux', 'amd64')]:
            with self.subTest(component=component, system=system, arch=arch), self.assertRaises(ValueError):
                toolchains.component_for(component, system, arch)

    def test_malformed_schema_digest_or_image_is_rejected(self):
        for field, value in [('schemaVersion', True), ('sha256', 'not-a-digest'), ('imageRef', 'jenkins/jenkins:latest'),
                             ('platforms', [['linux/amd64']]), ('pinScope', []), ('version', '21; curl bad'), ('url', 'https://secret@dl.google.com/archive.zip'), ('url', 'https://dl.google.com/archive.zip?token=secret')]:
            lock = toolchains.load_toolchain_lock()
            if field == 'schemaVersion':
                lock[field] = value
            else:
                lock['components']['controllerImage'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                toolchains.validate_toolchain_lock(lock)
        for malformed in [None, [], {'schemaVersion': 1, 'components': []}, {'schemaVersion': 1, 'components': {}}]:
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                toolchains.validate_toolchain_lock(malformed)
        lock = toolchains.load_toolchain_lock()
        lock['components']['controllerImage']['sha256'] = 'a' * 64
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            toolchains.validate_toolchain_lock(lock)

    def test_duplicate_json_and_symlink_lock_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / 'lock.json'
            path.write_text('{"schemaVersion":1,"schemaVersion":1,"components":{}}')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                toolchains.load_toolchain_lock(path)
            link = Path(temporary).resolve() / 'link.json'
            link.symlink_to(ROOT / 'toolchains.lock.json')
            with self.assertRaises(OSError):
                toolchains.load_toolchain_lock(link)

    def test_compatibility_file_drift_stops_lock_check(self):
        lock = toolchains.load_toolchain_lock()
        lock['components']['fastlane']['version'] = '2.239.0'
        with self.assertRaisesRegex(ValueError, 'Compatibility version file'):
            toolchains.check_repository_pins(lock)

    def test_clt_bundled_version_drift_stops_lock_check(self):
        lock = toolchains.load_toolchain_lock()
        lock['components']['harmonyNode']['version'] = '25.0.0'
        with self.assertRaisesRegex(ValueError, 'Bundled CLT component'):
            toolchains.check_repository_pins(lock)

    def test_cli_outputs_only_locked_values_and_rejects_missing_version(self):
        command = [sys.executable, str(ROOT / 'toolchains.py')]
        result = subprocess.run(command + ['build-args', 'android'], capture_output=True, text=True, check=True)
        self.assertEqual('35.0.0', json.loads(result.stdout)['ANDROID_BUILD_TOOLS_VERSION'])
        result = subprocess.run(command + ['version', 'ruby'], capture_output=True, text=True)
        self.assertEqual(1, result.returncode)
        self.assertIn('no exact/required version', result.stderr)
        result = subprocess.run(command + ['preflight', 'pr', '--java-bin', '/missing/java'], capture_output=True, text=True)
        self.assertEqual(1, result.returncode)
        self.assertIn('Toolchain validation failed', result.stderr)

    def test_agent_remoting_digest_and_unfrozen_dependency_drift_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            jar = Path(temporary) / 'remoting.jar'
            jar.write_bytes(b'reviewed test jar')
            sha256 = hashlib.sha256(jar.read_bytes()).hexdigest()
            with patch.object(toolchains.platform, 'system', return_value='Darwin'), \
                 patch.object(toolchains.platform, 'machine', return_value='arm64'), \
                 patch.object(toolchains, '_run', side_effect=lambda command, *args: 'Python ' + toolchains.platform.python_version()
                              if command == ['python3', '--version'] else 'version "21.0.11"'):
                with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                    toolchains.preflight('pr', remoting_jar=jar, remoting_sha256='a' * 64)
                with self.assertRaisesRegex(ValueError, 'expected version mismatch: python'):
                    toolchains.preflight('pr', remoting_jar=jar, remoting_sha256=sha256,
                                          expected_versions={'python': '0.0.1'})
                result = toolchains.preflight('pr', remoting_jar=jar, remoting_sha256=sha256,
                                              expected_versions={'python': toolchains.platform.python_version()})
                self.assertEqual('runtime-preflight-only', result['scope'])
                self.assertEqual(sha256, result['observations']['remotingSha256'])
                with self.assertRaisesRegex(ValueError, 'Unsupported OS/architecture for harmonyCLT'):
                    toolchains.preflight('ohos', clt_root=temporary, remoting_jar=jar, remoting_sha256=sha256)

    def test_python_expected_version_uses_target_path_instead_of_probe_interpreter(self):
        with tempfile.TemporaryDirectory() as temporary:
            jar = Path(temporary) / 'remoting.jar'
            jar.write_bytes(b'reviewed test jar')
            sha256 = hashlib.sha256(jar.read_bytes()).hexdigest()
            for actual, accepted in [('3.9.6', False), ('3.13.5', True)]:
                def run(command, *args):
                    return 'Python ' + actual if command == ['python3', '--version'] else 'version "21.0.11"'
                with self.subTest(actual=actual), patch.object(toolchains.platform, 'python_version', return_value='3.13.5'), \
                     patch.object(toolchains, '_run', side_effect=run) as probe:
                    if accepted:
                        result = toolchains.preflight('pr', remoting_jar=jar, remoting_sha256=sha256,
                                                      expected_versions={'python': '3.13.5'})
                        self.assertEqual('3.13.5', result['observations']['python'])
                        self.assertEqual('3.13.5', result['observations']['probeInterpreter'])
                    else:
                        with self.assertRaisesRegex(ValueError, 'expected version mismatch: python'):
                            toolchains.preflight('pr', remoting_jar=jar, remoting_sha256=sha256,
                                                 expected_versions={'python': '3.13.5'})
                    self.assertEqual(['python3', '--version'], probe.call_args_list[0].args[0])

    def test_mac_installer_reads_lock_through_cli(self):
        result = subprocess.run(['bash', str(ROOT / 'jenkins/local/install-pr-agent-launchdaemon.sh'), '--help'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        for script in ['jenkins/local/bootstrap-mac-agent.sh', 'jenkins/local/install-pr-agent-launchdaemon.sh']:
            subprocess.run(['bash', '-n', str(ROOT / script)], check=True)

    def test_admin_expected_versions_reject_unknown_components_or_ranges(self):
        for values in [{'python': '>=3.9'}, {'node': '24.14.1'}, {'ruby': True}, []]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                toolchains.validate_expected_versions(values)
            with self.subTest(preflight=values), self.assertRaises(ValueError):
                toolchains.preflight('pr', expected_versions=values)


if __name__ == '__main__':
    unittest.main()
