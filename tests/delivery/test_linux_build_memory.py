"""共享内存门禁行为；Mac 使用真实内核 flock 适配器，Linux 使用系统 flock。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXED_LOCK = "/home/jenkins/linux-build-memory/build.lock"


class LinuxBuildMemoryTest(unittest.TestCase):
    def test_serialization_failure_release_arguments_and_exit_status(self):
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="linux-memory-test-", dir=build) as temporary:
            root = Path(temporary).resolve()
            lock = root / "build.lock"
            lock.touch(mode=0o600)
            script = root / "with-memory.sh"
            # 仅替换生产固定挂载路径，让测试不触正在运行的共享锁 inode。
            script.write_text((ROOT / "delivery/with_linux_build_memory.sh").read_text().replace(FIXED_LOCK, str(lock)))
            environment = dict(os.environ, CI_LINUX_MEMORY_LOCK=str(lock))
            if not shutil.which("flock"):
                adapter = root / "flock"
                adapter.write_text("#!" + sys.executable + "\nimport fcntl, sys\nassert sys.argv[1:] == ['--exclusive', '9']\nfcntl.flock(9, fcntl.LOCK_EX)\n")
                adapter.chmod(0o700)
                environment["PATH"] = str(root) + os.pathsep + environment["PATH"]
            self.assertEqual(72, subprocess.run(["bash", str(script), "true"], env={**environment, "CI_LINUX_MEMORY_LOCK": ""}, capture_output=True).returncode)
            events, release, arguments = root / "events", root / "release", root / "arguments.json"
            command = root / "command.py"
            command.write_text("""import json, os, sys, time
from pathlib import Path
root, label = Path(sys.argv[1]), sys.argv[2]
try: os.close(9)
except OSError: pass
with (root / 'events').open('a') as out: out.write(label + ':enter\\n')
if label == 'first':
    (root / 'arguments.json').write_text(json.dumps(sys.argv[3:]))
    deadline = time.monotonic() + 5
    while not (root / 'release').exists():
        if time.monotonic() > deadline: sys.exit(90)
        time.sleep(0.01)
with (root / 'events').open('a') as out: out.write(label + ':exit\\n')
sys.exit(37 if label == 'first' else 0)
""")
            unusual = ["argument with spaces", "--literal-option", "$(no-command); *"]
            first = subprocess.Popen(["bash", str(script), sys.executable, str(command), str(root), "first", *unusual], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(lambda: first.kill() if first.poll() is None else None)
            deadline = time.monotonic() + 3
            while not events.exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            second = subprocess.Popen(["bash", str(script), sys.executable, str(command), str(root), "second"], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(lambda: second.kill() if second.poll() is None else None)
            time.sleep(0.15)
            self.assertIsNone(second.poll())
            self.assertEqual(["first:enter"], events.read_text().splitlines())
            release.touch()
            first_output = first.communicate(timeout=5)
            second_output = second.communicate(timeout=5)
            self.assertEqual(37, first.returncode, first_output)
            self.assertEqual(0, second.returncode, second_output)
            self.assertEqual(["first:enter", "first:exit", "second:enter", "second:exit"], events.read_text().splitlines())
            self.assertEqual(unusual, json.loads(arguments.read_text()))
            killed = subprocess.run(["bash", str(script), sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"], env=environment, capture_output=True, timeout=5)
            self.assertEqual(143, killed.returncode)
            self.assertEqual(0, subprocess.run(["bash", str(script), "true"], env=environment, capture_output=True, timeout=5).returncode)
            lock.unlink()
            lock.symlink_to(arguments)
            denied = subprocess.run(["bash", str(script), "true"], env=environment, capture_output=True, timeout=5)
            self.assertEqual(1, denied.returncode)
            self.assertIn(b"without symlinks", denied.stderr)
            self.assertNotIn(unusual[0].encode(), denied.stderr)
            lock.unlink()
            missing = subprocess.run(["bash", str(script), "true"], env=environment, capture_output=True, timeout=5)
            self.assertNotEqual(0, missing.returncode)
            os.mkfifo(lock, 0o600)
            fifo = subprocess.run(["bash", str(script), "true"], env=environment, capture_output=True, timeout=5)
            self.assertEqual(1, fifo.returncode)


if __name__ == "__main__":
    unittest.main()
