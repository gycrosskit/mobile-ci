"""真实文件验证接口升级、入口缺失和文档链接的拒绝行为。"""

import json
import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jenkins import check_repository_contract as contract_checker
import project_config

from jenkins.check_repository_contract import APP_TOOLS, CI_ROOT, application_layout, check_markdown_links


class RepositoryContractTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def write(self, relative, content=""):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def application(self, tools="ci/scripts", tests="ci/tests", manifest=True):
        for relative in ("gradlew", "ci/brands.json", tests + "/test_manifest_contract.py",
                         *(tools + "/" + name for name in APP_TOOLS)):
            self.write(relative)
        contract = {"version": 1, "requiredCiCapabilities": ["app-layout-v1", "repository-docs-v1"],
                    "toolDir": tools, "testDir": tests,
                    "requiredAppFiles": [tools + "/check_repository_docs.py", tests + "/test_repository_docs.py"]}
        if manifest:
            self.write(contract["requiredAppFiles"][0])
            self.write(contract["requiredAppFiles"][1])
            self.write("ci/contract.json", json.dumps(contract))
        return contract

    def test_modern_layout_requires_docs_entrypoints_and_declared_files(self):
        self.application()
        self.assertEqual({"toolDir": "ci/scripts", "testDir": "ci/tests", "docsCheck": True,
                          "contractVersion": 1, "projectId":"sxmqlive", "platforms":["android","harmony","ios"],
                          "checks":{"android":["affected_gradle_tests.py"], "ios":["affected_ios_host_builds.py","test_ios_bridge_logic.sh"], "harmony":["affected_ohos_contracts.py"]}}, application_layout(self.root))
        (self.root / "ci/scripts/check_repository_docs.py").unlink()
        with self.assertRaisesRegex(ValueError, "check_repository_docs"):
            application_layout(self.root)

    def test_android_only_profile_does_not_require_ios_or_harmony_inputs(self):
        self.application()
        profile = copy.deepcopy(project_config.load_project())
        profile["id"] = "other-mobile"
        profile["platforms"] = {"android": profile["platforms"]["android"]}
        for tool in ("affected_ios_host_builds.py", "test_ios_bridge_logic.sh", "affected_ohos_contracts.py"):
            (self.root / "ci/scripts" / tool).unlink()
        layout = application_layout(self.root, profile=profile)
        self.assertEqual(["android"], layout["platforms"])
        self.assertEqual({"android":["affected_gradle_tests.py"]}, layout["checks"])
        (self.root / "ci/scripts/affected_gradle_tests.py").unlink()
        with self.assertRaisesRegex(ValueError, "affected_gradle_tests"):
            application_layout(self.root, profile=profile)

    def test_trusted_profile_baseline_preserves_implicit_checks_and_operational_identity(self):
        profiles = self.root / "projects"; profiles.mkdir()
        original = project_config.load_project()
        path = profiles / "sxmqlive.json"
        path.write_text(json.dumps(original))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "trusted profile"], check=True)
        base = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        mutations = []
        disabled = copy.deepcopy(original); disabled["contract"]["requiredTools"] = {"android":[]}; mutations.append(disabled)
        moved = copy.deepcopy(original); moved["contract"]["testDir"] = "other-tests"; mutations.append(moved)
        foreign = copy.deepcopy(original); foreign["credentials"]["scm"] = "other-credential"; mutations.append(foreign)
        node = copy.deepcopy(original); node["portal"]["macNode"] = "other-mac"; mutations.append(node)
        with patch.object(contract_checker, "CI_ROOT", self.root), patch.object(project_config, "PROJECTS_DIR", profiles):
            contract_checker.check_project_baseline(base, "sxmqlive")
            for changed in mutations:
                path.write_text(json.dumps(changed))
                with self.subTest(change=changed), self.assertRaises(project_config.ProjectConfigError):
                    contract_checker.check_project_baseline(base, "sxmqlive")
            path.unlink()
            with self.assertRaises(OSError):
                contract_checker.check_project_baseline(base, "sxmqlive")

    def test_historical_layouts_are_explicit_and_missing_entry_never_skips(self):
        for tools, tests in (("tools", "tests/tools"), ("ci/scripts", "ci/tests")):
            with self.subTest(tools=tools):
                self.application(tools, tests, manifest=False)
                layout = application_layout(self.root)
                self.assertEqual(tools, layout["toolDir"])
                self.assertEqual(tests, layout["testDir"])
                self.assertFalse(layout["docsCheck"])
                self.assertEqual("legacy", layout["contractVersion"])
        (self.root / "ci/scripts/affected_ohos_contracts.py").unlink()
        with self.assertRaisesRegex(ValueError, "affected_ohos_contracts"):
            application_layout(self.root)

    def test_unknown_version_capability_or_directory_fails_before_consumption(self):
        contract = self.application()
        for field, value in (("version", 2), ("version", True), ("requiredCiCapabilities", ["new-interface-v2"]),
                             ("requiredCiCapabilities", "app-layout-v1"), ("toolDir", "../outside"), ("toolDir", []),
                             ("requiredAppFiles", ["missing.py"]), ("requiredAppFiles", ["../../outside"])):
            with self.subTest(field=field, value=value):
                self.write("ci/contract.json", json.dumps({**contract, field: value}))
                with self.assertRaises(ValueError):
                    application_layout(self.root)
        self.write("ci/contract.json", "invalid")
        with self.assertRaises(ValueError):
            application_layout(self.root)

    def test_markdown_links_support_relative_encoded_images_references_and_fences(self):
        self.write("docs/中文 文件.md")
        self.write("docs/image.svg")
        self.write("README.md", '[正文](<docs/中文 文件.md> "文档")\n![图](docs/image.svg)\n[编码](docs/%E4%B8%AD%E6%96%87%20%E6%96%87%E4%BB%B6.md#章节)\n'
                   "[外网](https://example.invalid/no-check)\n[本页](#章节)\n`[示例](missing.md)`\n"
                   "```text\n[代码](missing.md)\n```\n[正文引用][guide]\n[guide]: docs/中文%20文件.md\n")
        self.assertEqual(1, check_markdown_links(self.root, ["README.md"]))
        (self.root / "docs/image.svg").unlink()
        with self.assertRaisesRegex(ValueError, r"README.md:2:.*image.svg"):
            check_markdown_links(self.root, ["README.md"])

    def test_deleted_markdown_and_nested_fence_examples_are_not_live_links(self):
        self.write("README.md", "````markdown\n```python\n[示例](missing.md)\n```\n````\n[正文](target.md)\n")
        self.write("target.md")
        check_markdown_links(self.root, ["README.md", "deleted.md"])
        (self.root / "target.md").unlink()
        with self.assertRaisesRegex(ValueError, r"README.md:6:.*target.md"):
            check_markdown_links(self.root, ["README.md"])

    def test_git_baseline_allows_first_manifest_but_rejects_removal_and_capability_downgrade(self):
        self.application(manifest=False)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "--", "ci", "gradlew"], check=True)
        command = ["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                   "commit", "-q", "-m"]
        subprocess.run(command + ["legacy application"], check=True)
        legacy = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        self.assertEqual("legacy", application_layout(self.root, legacy)["contractVersion"])
        contract = self.application()
        self.assertTrue(application_layout(self.root, legacy)["docsCheck"])
        subprocess.run(["git", "-C", str(self.root), "add", "--", "ci"], check=True)
        subprocess.run(command + ["declare interface"], check=True)
        modern = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        (self.root / "ci/contract.json").unlink()
        with self.assertRaisesRegex(ValueError, "禁止删除"):
            application_layout(self.root, modern)
        self.write("ci/contract.json", json.dumps({**contract, "requiredCiCapabilities": ["app-layout-v1"]}))
        with self.assertRaisesRegex(ValueError, "不能移除"):
            application_layout(self.root, modern)
        self.write("ci/contract.json", json.dumps({**contract, "requiredAppFiles":[]}))
        with self.assertRaisesRegex(ValueError, "不能移除.*必需文件"):
            application_layout(self.root, modern)
        self.write("ci/contract.json", json.dumps(contract))
        self.assertTrue(application_layout(self.root, modern)["docsCheck"])
        with self.assertRaises(subprocess.CalledProcessError):
            application_layout(self.root, "missing-base")

    def test_cli_reports_two_actual_commits_and_rejects_incompatible_application(self):
        contract = self.application()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-q", "--allow-empty", "-m", "application fixture"], check=True)
        command = [sys.executable, str(CI_ROOT / "jenkins/check_repository_contract.py"), "--app-root", str(self.root)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        for field, root in (("appSha", self.root), ("ciSha", CI_ROOT)):
            self.assertEqual(subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(), report[field])
        self.assertTrue(report["docsCheck"])
        self.assertTrue(report["appDirty"])
        self.assertIsInstance(report["ciDirty"], bool)
        subprocess.run(["git", "-C", str(self.root), "add", "--", "ci", "gradlew"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-q", "-m", "checked interface"], check=True)
        clean = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, clean.returncode, clean.stderr)
        self.assertFalse(json.loads(clean.stdout)["appDirty"])
        self.write("ci/contract.json", json.dumps({**contract, "version": 2}))
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn("先升级可信 CI", result.stderr)
        self.assertEqual("", result.stdout)


if __name__ == "__main__":
    unittest.main()
