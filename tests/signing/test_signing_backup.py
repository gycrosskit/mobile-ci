import shutil
import subprocess
import unittest
from pathlib import Path


class SigningBackupTest(unittest.TestCase):
    def test_authenticated_backup(self):
        if not shutil.which('node'):
            self.skipTest('需要已有 Node，不自动下载')
        result = subprocess.run(['node', '--test', str(Path(__file__).with_suffix('.cjs'))], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
