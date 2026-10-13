import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from project import SOURCE_ROOT, TOOL_ROOT
from signing import signing_devices, signing_maintenance


class RepositoryBoundariesTest(unittest.TestCase):
    def test_signing_cli_runs_from_application_directory(self):
        env = os.environ | {"APP_SOURCE_ROOT": str(SOURCE_ROOT), "PYTHONPATH": str(TOOL_ROOT)}
        for name in ("android_signing", "ios_devices", "ios_signing_setup", "ios_verify_signature",
                     "harmony_provisioning", "harmony_jenkins_credentials", "ios_jenkins_credentials",
                     "signing_inventory", "signing_maintenance"):
            with self.subTest(name=name):
                result = subprocess.run([sys.executable, str(TOOL_ROOT / "signing" / (name + ".py")), "--help"],
                                        cwd=SOURCE_ROOT, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(0, result.returncode, result.stderr)

    def test_persistent_state_rejects_both_repository_roots(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"WORKSPACE": directory}):
            for root in (SOURCE_ROOT, TOOL_ROOT):
                with self.subTest(root=root), patch.object(signing_devices, "_private"), \
                        patch.object(signing_maintenance, "private_dir"):
                    with self.assertRaisesRegex(ValueError, "workspace/仓库"):
                        signing_maintenance.team_directory(root, "ios", "fixture")
                    with self.assertRaises(signing_devices.DeviceError):
                        signing_devices._directory(root)
