import json
import os
import shutil
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from project import SOURCE_ROOT


ROOT = Path(__file__).resolve().parents[2]


class CiExecutionContractsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        application = Path(self.temporary.name).resolve()
        (application / "gradlew").touch()
        environment = patch.dict(os.environ, APP_SOURCE_ROOT=str(application), CI_TOOL_ROOT=str(ROOT))
        environment.start(); self.addCleanup(environment.stop)

    def test_verified_android_build_only_runs_target_lint_and_assemble(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            scripts = repo / "delivery"
            scripts.mkdir(parents=True)
            shutil.copy(ROOT / "delivery/android_ci.sh", scripts)
            capture = repo / "arguments.txt"
            (repo / "ci").mkdir()
            (repo / "ci/brands.json").write_text("{}")
            (repo / "gradlew").write_text('printf "%s\\n" "$@" >> "$CAPTURE"\n')
            for engine, module in (("cmp", "app"), ("kuikly", "androidKuiklyApp")):
                for pipeline, build in (("test", "Qa"), ("release", "Release")):
                    with self.subTest(pipeline=pipeline, ui_engine=engine):
                        capture.write_text("")
                        result = subprocess.run(
                            ["bash", str(scripts / "android_ci.sh"), "verified-build", "kangmin", pipeline],
                            env=dict(os.environ, CAPTURE=str(capture), CI_UI_ENGINE=engine, APP_SOURCE_ROOT=str(repo), CI_TOOL_ROOT=str(ROOT)), capture_output=True, text=True,
                        )
                        self.assertEqual(0, result.returncode, result.stderr)
                        arguments = capture.read_text().splitlines()
                        self.assertEqual(1, arguments.count("--profile"))
                        self.assertIn(f":{module}:lintKangmin{build}", arguments)
                        self.assertIn(f":{module}:assembleKangmin{build}", arguments)
                        self.assertIn("-PcmpBrand=kangmin", arguments)
                        self.assertFalse(any("UnitTest" in task for task in arguments))

    def test_android_engine_defaults_overrides_and_mapping_directory(self):
        script = str(ROOT / "delivery/android_ci.sh")
        environment = dict(os.environ)
        environment.pop("CI_UI_ENGINE", None)
        for pipeline, arguments, engine, expected in (
            ("test", [], None, "app/build/outputs/mapping/kangminQa"),
            ("test", [], "kuikly", "androidKuiklyApp/build/outputs/mapping/kangminQa"),
            ("test", ["cmp"], "kuikly", "app/build/outputs/mapping/kangminQa"),
            ("release", [], None, "app/build/outputs/mapping/kangminRelease"),
            ("release", [], "kuikly", "androidKuiklyApp/build/outputs/mapping/kangminRelease"),
        ):
            with self.subTest(pipeline=pipeline, arguments=arguments, ui_engine=engine):
                selected_env = dict(environment, **({"CI_UI_ENGINE": engine} if engine is not None else {}))
                result = subprocess.run(["bash", script, "mapping-dir", "kangmin", pipeline] + arguments,
                                        env=selected_env, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(expected, result.stdout.strip())
        for engine in ("", "unknown"):
            result = subprocess.run(["bash", script, "mapping-dir", "kangmin", "test"],
                                    env=dict(environment, CI_UI_ENGINE=engine), capture_output=True, text=True)
            self.assertEqual(2, result.returncode)
            self.assertIn("UI 引擎", result.stderr)

    def test_python_release_entrypoints_run_directly_outside_both_checkouts(self):
        environment = dict(os.environ, APP_SOURCE_ROOT=str(SOURCE_ROOT))
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as temporary:
            for name in ("release.py", "ios_release.py", "harmony_release.py", "changelog.py"):
                with self.subTest(script=name):
                    result = subprocess.run([sys.executable, str(ROOT / "delivery" / name), "--help"],
                                            cwd=temporary, env=environment, capture_output=True, text=True)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn("usage:", result.stdout)

    def test_shell_entrypoints_reject_a_ci_checkout_as_application_source(self):
        for name, arguments in (("android_ci.sh", ["verified-build", "kangmin", "test"]),
                                ("ios_ci.sh", ["preflight"])):
            with self.subTest(script=name):
                result = subprocess.run(["bash", str(ROOT / "delivery" / name), *arguments],
                                        env=dict(os.environ, APP_SOURCE_ROOT=str(ROOT)),
                                        capture_output=True, text=True)
                self.assertEqual(2, result.returncode)
                self.assertIn("APP_SOURCE_ROOT", result.stderr)

    def test_android_gate_runs_ci_and_application_contracts_in_their_own_checkouts(self):
        for package in ("ci.tests", "tests.tools"):
            with self.subTest(package=package), tempfile.TemporaryDirectory() as temporary:
                app = Path(temporary)
                (app / "gradlew").touch()
                (app / "ci").mkdir()
                (app / "ci/brands.json").write_text("{}")
                test_dir = app / package.replace(".", "/")
                test_dir.mkdir(parents=True)
                (test_dir / "test_manifest_contract.py").touch()
                if package == "ci.tests":
                    (app / "tests/tools").mkdir(parents=True)
                    (app / "tests/tools/test_manifest_contract.py").touch()
                tools = app / "bin"
                tools.mkdir()
                capture = app / "invocations.jsonl"
                python = tools / "python3"
                python.write_text(
                    '#!' + sys.executable + '\n'
                    'import json, os, sys\n'
                    'if sys.argv[1].endswith("project.py"):\n'
                    '    os.execv(' + repr(sys.executable) + ', [' + repr(sys.executable) + '] + sys.argv[1:])\n'
                    'with open(os.environ["CAPTURE"], "a") as out:\n'
                    '    out.write(json.dumps({"cwd":os.getcwd(), "args":sys.argv[1:],'
                    '"pythonpath":os.environ["PYTHONPATH"]}) + "\\n")\n'
                )
                python.chmod(0o755)
                result = subprocess.run(["bash", str(ROOT / "delivery/android_ci.sh"),
                                         "quality-gate", "kangmin", "test"],
                                        env=dict(os.environ, APP_SOURCE_ROOT=str(app),
                                                 PATH=str(tools) + os.pathsep + os.environ["PATH"],
                                                 CAPTURE=str(capture)), capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                invocations = [json.loads(line) for line in capture.read_text().splitlines()]
                self.assertEqual(2, len(invocations))
                self.assertEqual(str(ROOT.resolve()), invocations[0]["cwd"])
                self.assertEqual(str(app.resolve()), invocations[1]["cwd"])
                self.assertEqual(["tests.delivery.test_ci_execution_contracts", "tests.delivery.test_release"],
                                 invocations[0]["args"][-2:])
                self.assertEqual([package + ".test_manifest_contract", package + ".test_r8_configuration"],
                                 invocations[1]["args"][-2:])
                self.assertEqual(app.resolve(), Path(invocations[1]["pythonpath"].split(os.pathsep)[0]).resolve())

    def test_fastlane_runner_selects_pinned_version_and_explicit_gem_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ruby = directory / "ruby"
            ruby.write_text(
                '#!/usr/bin/env python3\nimport json, os, sys\n'
                'if sys.argv[1] == "-rrbconfig": print("4.0.0")\n'
                'else: print(json.dumps({"args":sys.argv[1:], "home":os.environ["GEM_HOME"],'
                '"path":os.environ["GEM_PATH"], "cwd":os.getcwd()}))\n'
            )
            ruby.chmod(0o755)
            result = subprocess.run(
                ["bash", str(ROOT / "delivery/fastlane.sh"), "--version"],
                env=dict(os.environ, FASTLANE_RUBY=str(ruby), FASTLANE_GEM_HOME=str(directory / "gems")),
                capture_output=True, text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            invocation = json.loads(result.stdout)
            self.assertEqual((ROOT / "delivery/.fastlane-version").read_text().strip(), invocation["args"][-2])
            self.assertEqual("--version", invocation["args"][-1])
            self.assertEqual(str(directory / "gems"), invocation["home"])
            self.assertEqual(invocation["home"], invocation["path"])
            self.assertEqual(str(ROOT / "delivery"), invocation["cwd"])


if __name__ == "__main__":
    unittest.main()
