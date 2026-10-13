import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2] / 'jenkins/remote'
LOCK_DIRECTORY = '/home/jenkins/linux-build-memory'


class LinuxMemoryVolumeTest(unittest.TestCase):
    def test_build_and_pr_nodes_share_only_dedicated_nocopy_volume(self):
        base = (ROOT / 'compose.yaml').read_text()
        overlay = (ROOT / 'compose.ohos.yaml').read_text()
        for source, service, end in ((base, 'android-agent', 'pr-agent'), (base, 'pr-agent', None)):
            section = source.split('  ' + service + ':\n', 1)[1]
            section = section.split('  ' + end + ':\n', 1)[0] if end else section.split('\nvolumes:', 1)[0]
            self.assertIn('CI_LINUX_MEMORY_LOCK: ' + LOCK_DIRECTORY + '/build.lock', section)
            self.assertIn('source: linux_build_memory\n        target: ' + LOCK_DIRECTORY + '\n        volume:\n          nocopy: true', section)
        for source in (base, overlay):
            self.assertIn('linux_build_memory:\n    name: sxmqlive_linux_build_memory', source)
            self.assertIn('source: linux_build_memory\n        target: /linux-build-memory\n        volume:\n          nocopy: true', source)
            self.assertIn('install -d -o 1000 -g 1000 -m 700 /linux-build-memory', source)
        maintenance = overlay.split('  ohos-maintenance:\n', 1)[1].split('\nvolumes:', 1)[0]
        self.assertNotIn('linux_build_memory', maintenance)
        self.assertNotIn('CI_LINUX_MEMORY_LOCK', maintenance)

    def test_both_initializers_preserve_existing_lock_inode_and_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            command_dir = root / 'commands'
            command_dir.mkdir()
            (command_dir / 'chown').write_text('#!/bin/sh\nexit 0\n')
            (command_dir / 'stat').write_text('#!' + sys.executable + '\nimport os,sys\nprint("1000:" + oct(os.stat(sys.argv[-1]).st_mode & 0o777)[2:])\n')
            for command in command_dir.iterdir():
                command.chmod(0o700)
            directory = root / 'shared'
            directory.mkdir(mode=0o700)
            lock = directory / 'build.lock'
            env = {**os.environ, 'PATH': str(command_dir) + os.pathsep + os.environ['PATH']}
            for filename in ('compose.yaml', 'compose.ohos.yaml'):
                source = (ROOT / filename).read_text()
                init = 'test ! -L /linux-build-memory/build.lock' + source.split('&& test ! -L /linux-build-memory/build.lock', 1)[1].split('    volumes:', 1)[0]
                command = ' '.join(line.strip() for line in init.splitlines()).replace('/linux-build-memory', str(directory))
                if lock.exists():
                    inode = lock.stat().st_ino
                    lock.write_text('held-lock-inode')
                result = subprocess.run(['sh', '-c', command], env=env, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(0o600, lock.stat().st_mode & 0o777)
                if filename == 'compose.ohos.yaml':
                    self.assertEqual(inode, lock.stat().st_ino)
                    self.assertEqual('held-lock-inode', lock.read_text())
                lock.chmod(0o644)
                self.assertNotEqual(0, subprocess.run(['sh', '-c', command], env=env, capture_output=True).returncode)
                lock.chmod(0o600)

    def test_launchers_reject_invalid_lock_permissions_symlinks_and_missing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve() / 'shared'
            directory.mkdir(mode=0o700)
            lock = directory / 'build.lock'
            for script in ('android-agent/start-android-agent.sh', 'ohos-agent/start-ohos-agent.sh'):
                source = (ROOT / script).read_text()
                self.assertIn('command -v flock', source)
                self.assertIn('== ' + LOCK_DIRECTORY + '/build.lock', source)
                self.assertIn('exec java -Xms128m -Xmx512m', source)
                validator = source.split("<<'MEMORY_LOCK_PY'\n", 1)[1].split('\nMEMORY_LOCK_PY', 1)[0]
                def validate():
                    return subprocess.run([sys.executable, '-c', validator, str(lock)], capture_output=True, text=True)
                lock.unlink(missing_ok=True)
                self.assertNotEqual(0, validate().returncode)
                lock.touch(mode=0o600)
                self.assertEqual(0, validate().returncode)
                lock.chmod(0o644)
                self.assertNotEqual(0, validate().returncode)
                lock.chmod(0o600)
                directory.chmod(0o755)
                self.assertNotEqual(0, validate().returncode)
                directory.chmod(0o700)
                lock.unlink()
                lock.symlink_to(directory / 'other')
                self.assertNotEqual(0, validate().returncode)
                lock.unlink()


if __name__ == '__main__':
    unittest.main()
