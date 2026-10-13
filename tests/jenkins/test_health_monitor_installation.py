import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class HealthMonitorInstallationTest(unittest.TestCase):
    def test_remote_installed_file_list_imports_without_source_checkout_or_pythonpath(self):
        source = (ROOT / 'jenkins/remote/install-health-monitor-systemd.sh').read_text()
        # 使用安装器实际文件清单；只复制源码，不运行 systemd、root 安装或真实监控。
        pairs = re.findall(r'install -o root -g root -m 644 "\$\{REPO_ROOT\}/([^"\n]+)" "\$\{INSTALL_ROOT\}/([^"\n]+)"', source)
        with tempfile.TemporaryDirectory() as temporary:
            installed = Path(temporary).resolve()
            for origin, target in pairs:
                destination = installed / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / origin, destination)
            result = subprocess.run([sys.executable, '-I', str(installed / 'jenkins/jenkins_health_monitor.py'), '--help'],
                cwd=installed, env={key:value for key,value in os.environ.items() if key != 'PYTHONPATH'},
                capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('--state-file', result.stdout)
            (installed / 'delivery/delivery.py').unlink()
            negative = subprocess.run([sys.executable, '-I', str(installed / 'jenkins/jenkins_health_monitor.py'), '--help'],
                cwd=installed, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(0, negative.returncode)
            self.assertIn("No module named 'delivery.delivery'", negative.stderr)


if __name__ == '__main__':
    unittest.main()
