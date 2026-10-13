import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "delivery/ci_timing.py"


class CiTimingTest(unittest.TestCase):
    def test_run_records_success_without_logging_command_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "timings.jsonl"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "run", "--output", str(output),
                 "--label", "build", "--", sys.executable, "-c", "pass # secret-value"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, result.returncode)
            self.assertNotIn("secret-value", result.stdout + result.stderr + output.read_text())
            record = json.loads(output.read_text().strip())
            self.assertEqual("build", record["label"])
            self.assertEqual(0, record["exit_code"])
            self.assertGreaterEqual(record["duration_ms"], 0)
            self.assertLessEqual(record["started_at"], record["finished_at"])

    def test_run_preserves_command_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "timings.jsonl"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "run", "--output", str(output),
                 "--label", "build", "--", sys.executable, "-c", "raise SystemExit(7)"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(7, result.returncode)
            self.assertEqual(7, json.loads(output.read_text())["exit_code"])

    def test_size_records_file_length_without_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "timings.jsonl"
            artifact = Path(temporary) / "private-name.apk"
            artifact.write_bytes(b"package")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "size", "--output", str(output),
                 "--label", "apk", "--path", str(artifact)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, result.returncode)
            self.assertNotIn(str(artifact), result.stdout + output.read_text())
            self.assertEqual(7, json.loads(output.read_text())["size_bytes"])

    def test_size_resolves_path_from_delivery_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "timings.jsonl"
            artifact = Path(temporary) / "app.apk"
            artifact.write_bytes(b"apk")
            context = Path(temporary) / "context.json"
            context.write_text(json.dumps({"apk_path": str(artifact)}))
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "size", "--output", str(output),
                 "--label", "apk", "--context", str(context), "--key", "apk_path"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, result.returncode)
            self.assertEqual(3, json.loads(output.read_text())["size_bytes"])
