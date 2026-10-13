"""解析公开参考 YAML，防止 PR 检查入口悄悄变成特权执行入口。"""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples/github-actions"
ACTION_PINS = {
    "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
    "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/setup-java@c5195efecf7bdfc987ee8bae7a71cb8b11521c00",
}
CORE_CHECK = "python3 jenkins/run_public_checks.py"


def read_workflow(path):
    if not shutil.which("ruby"):
        raise RuntimeError("GitHub reference YAML checks require existing Ruby/Psych")
    result = subprocess.run(
        ["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.safe_load(ARGF.read))", str(path)],
        capture_output=True, text=True, timeout=10, check=True,
    )
    return json.loads(result.stdout)


class GitHubWorkflowExamplesTest(unittest.TestCase):
    def setUp(self):
        self.caller = read_workflow(EXAMPLES / "check.yml")
        self.reusable = read_workflow(EXAMPLES / "reusable-check.yml")

    def assert_safe(self, workflow, *, reusable=False):
        self.assertEqual({"contents": "read"}, workflow["permissions"])
        self.assertEqual({"workflow_call"} if reusable else {"pull_request", "workflow_dispatch"}, set(workflow["on"]))
        self.assertNotIn("secrets.", json.dumps(workflow))
        for job in workflow["jobs"].values():
            self.assertEqual({"contents": "read"}, job.get("permissions", workflow["permissions"]))
            self.assertEqual({}, job.get("secrets", {}))
            self.assertNotIn("environment", job)
            if "uses" in job:
                self.assertEqual("./.github/workflows/reusable-check.yml", job["uses"])
                self.assertNotIn("runs-on", job)
                continue
            self.assertEqual("ubuntu-24.04", job["runs-on"])
            self.assertNotIn("container", job)
            for step in job["steps"]:
                if "uses" in step:
                    self.assertIn(step["uses"], ACTION_PINS)
                if "run" in step:
                    self.assertNotIn("${{", step["run"])

    def test_parse_triggers_typed_input_and_explicit_zero_secrets(self):
        self.assert_safe(self.caller)
        self.assert_safe(self.reusable, reusable=True)
        contract = self.reusable["on"]["workflow_call"]
        self.assertEqual({}, contract["secrets"])
        self.assertEqual({"project-id"}, set(contract["inputs"]))
        self.assertEqual("string", contract["inputs"]["project-id"]["type"])
        self.assertEqual("example-mobile", contract["inputs"]["project-id"]["default"])
        self.assertEqual({"project-id": "example-mobile"}, self.caller["jobs"]["check"]["with"])
        self.assertEqual({}, self.caller["jobs"]["check"]["secrets"])

    def test_commands_and_tool_locks_reach_existing_public_checks(self):
        steps = self.reusable["jobs"]["public-check"]["steps"]
        commands = [step["run"] for step in steps if "run" in step]
        self.assertEqual(3, len(commands))
        self.assertEqual('test "$EXAMPLE_PROJECT_ID" = example-mobile', commands[0])
        self.assertEqual(CORE_CHECK, commands[2])
        self.assertTrue((ROOT / "examples/second-project/validate.py").is_file())
        self.assertTrue((ROOT / "jenkins/run_public_checks.py").is_file())
        runtime = commands[1]
        self.assertIn("set -euo pipefail", runtime)
        self.assertIn("groovy-all/2.4.21/groovy-all-2.4.21.jar", runtime)
        self.assertIn("de65260cf2070442e99882f2f3d72e7531725c1e6a257446cc0cea525c607bd0", runtime)
        self.assertIn("sha256sum --check -", runtime)
        self.assertIn('chmod 0444 "$GROOVY_TEST_CLASSPATH"', runtime)
        actions = {step["uses"].split("@")[0]: step for step in steps if "uses" in step}
        self.assertEqual(False, actions["actions/checkout"]["with"]["persist-credentials"])
        self.assertEqual("3.11.9", actions["actions/setup-python"]["with"]["python-version"])
        self.assertEqual("21.0.11+10", actions["actions/setup-java"]["with"]["java-version"])

    def test_installed_tool_workflows_match_reviewed_examples(self):
        for name in ("check.yml", "reusable-check.yml"):
            self.assertEqual((EXAMPLES / name).read_bytes(), (ROOT / ".github/workflows" / name).read_bytes())

    def test_reject_privileged_trigger_permissions_runner_or_secret_inheritance(self):
        mutations = []
        bad = copy.deepcopy(self.reusable); bad["permissions"]["contents"] = "write"; mutations.append(bad)
        bad = copy.deepcopy(self.reusable); bad["on"] = {"pull_request_target": {}}; mutations.append(bad)
        bad = copy.deepcopy(self.reusable); bad["jobs"]["public-check"]["runs-on"] = ["self-hosted", "linux"]; mutations.append(bad)
        bad = copy.deepcopy(self.reusable); bad["jobs"]["public-check"]["permissions"] = {"id-token": "write"}; mutations.append(bad)
        bad = copy.deepcopy(self.reusable); bad["jobs"]["public-check"]["secrets"] = "inherit"; mutations.append(bad)
        for workflow in mutations:
            with self.subTest(workflow=workflow), self.assertRaises(AssertionError):
                self.assert_safe(workflow, reusable=True)

    def test_reject_floating_dynamic_actions_and_interpolated_shell(self):
        for use in ("actions/checkout@v4", "actions/checkout@main", "${{ inputs.action }}", "./app-action"):
            workflow = copy.deepcopy(self.reusable)
            workflow["jobs"]["public-check"]["steps"][1]["uses"] = use
            with self.subTest(use=use), self.assertRaises(AssertionError):
                self.assert_safe(workflow, reusable=True)
        workflow = copy.deepcopy(self.reusable)
        workflow["jobs"]["public-check"]["steps"][0]["run"] = "${{ inputs.project-id }}"
        with self.assertRaises(AssertionError):
            self.assert_safe(workflow, reusable=True)

    def test_project_input_is_quoted_data_and_rejects_shell_payload(self):
        step = self.reusable["jobs"]["public-check"]["steps"][0]
        self.assertEqual({"EXAMPLE_PROJECT_ID": "${{ inputs.project-id }}"}, step["env"])
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist"
            for value in ("example-mobile", "other-mobile", f"$(touch {marker})"):
                result = subprocess.run(
                    ["bash", "-eu", "-c", step["run"]],
                    env=dict(os.environ, EXAMPLE_PROJECT_ID=value), capture_output=True, timeout=10,
                )
                self.assertEqual(0 if value == "example-mobile" else 1, result.returncode)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
