"""公开包只包含有摘要的源码；排除私有状态，任何拒绝均不得覆盖外部文件。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from jenkins import export_public_source as exporter
from tests.jenkins.test_pr_validation import groovy_classpath

ROOT = Path(__file__).resolve().parents[2]


class PublicSourceExportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="public-source-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        self.output = self.base / "output.tar.gz"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        for name in ("LICENSE", "NOTICE", "THIRD_PARTY.md", "SECURITY.md", "examples/second-project/project.json"):
            self.put(name, "{}\n" if name.endswith(".json") else "public fixture\n")

    def put(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return path

    def assert_rejected(self):
        with self.assertRaises(ValueError):
            exporter.export(self.output, root=self.root)
        self.assertFalse(self.output.exists())

    def test_excludes_private_states_and_verifies_every_manifest_hash(self):
        excluded = ["projects/sxmqlive.json", "projects/another-private.json", "AGENTS.md", ".env",
                    "signing/resources/apple-intermediate-certificates/AppleWWDRCAG3.pem",
                    "build/cache.txt", "state/runtime/private.txt", "signing/backups/private.txt",
                    "__pycache__/cached.pyc", "docs/audits/image-license-audit.json",
                    "docs/audits/image-license-followup.json"]
        for name in excluded:
            self.put(name, "EXCLUDED TEST MATERIAL\n")
        for name in exporter.RUNBOOKS:
            self.put(name, "PRIVATE TEST HOST DETAILS\n")
        self.put("docs/部署.md", "generic deployment\n")
        self.put("docs/运行验收.md", "generic validation\n")
        for name in exporter.PRIVATE_DOCUMENTS:
            self.put(name, "PRIVATE TEST DEPLOYMENT EVIDENCE\n")
        for name, heading in exporter.PUBLIC_SECTIONS.items():
            self.put(name, "Public instructions\n\n" + heading + "\nPRIVATE TEST RUN LINKS\n")
        self.put("project.py", "print('fixture')\n")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", "."], check=True)
        # 新源码文件也必须包含，避免只导出已提交快照而漏掉接入入口。
        self.put("untracked.py", "print('new fixture')\n")
        result = exporter.export(self.output, root=self.root)
        with tarfile.open(self.output, "r:gz") as archive:
            contents = {member.name: archive.extractfile(member).read() for member in archive.getmembers()}
            self.assertTrue(all(member.isfile() for member in archive.getmembers()))
        for name in excluded:
            self.assertNotIn(name, contents)
        self.assertFalse(any(name.startswith(".git/") for name in contents))
        self.assertIn("untracked.py", contents)
        for name in exporter.RUNBOOKS:
            self.assertEqual(exporter.NEUTRAL_RUNBOOK.encode(), contents[name])
            self.assertNotIn(b"PRIVATE TEST HOST DETAILS", contents[name])
        for name in exporter.PRIVATE_DOCUMENTS:
            self.assertEqual(exporter.NEUTRAL_DOCUMENT.encode(), contents[name])
        for name in exporter.PUBLIC_SECTIONS:
            self.assertNotIn(b"PRIVATE TEST RUN LINKS", contents[name])
        origin = json.loads(contents["SOURCE-ORIGIN.json"])
        self.assertFalse(origin["gitHistoryIncluded"])
        self.assertTrue(origin["workingTreeSnapshot"])
        self.assertIsNone(origin["sourceCommit"])
        self.assertEqual(hashlib.sha256(b"PRIVATE TEST DEPLOYMENT EVIDENCE\n").hexdigest(),
                         origin["sourceFileSha256"]["docs/发布流程.md"])
        manifest = json.loads(contents.pop("SOURCE-MANIFEST.json"))
        self.assertEqual(set(contents), set(manifest))
        for name, data in contents.items():
            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest[name], name)
        self.assertEqual(len(contents), result["files"])
        self.assertEqual(hashlib.sha256(self.output.read_bytes()).hexdigest(), result["sha256"])
        self.assertEqual(0, result["companyProfiles"])
        self.assertFalse(result["gitHistory"])
        self.assertFalse(result["published"])

    def test_dotenv_variants_are_excluded(self):
        for name in (".env.local", ".env.production", "config/.env.test"):
            self.put(name, "FAKE_TOKEN=not-a-credential\n")
        exporter.export(self.output, root=self.root)
        with tarfile.open(self.output, "r:gz") as archive:
            self.assertFalse(any(Path(name).name.startswith(".env.") for name in archive.getnames()))

    def test_output_parent_symlink_is_rejected(self):
        destination = self.base / "destination"
        destination.mkdir()
        link = self.base / "linked-destination"
        link.symlink_to(destination, target_is_directory=True)
        self.output = link / "output.tar.gz"
        self.assert_rejected()
        self.assertEqual([], list(destination.iterdir()))

    def test_source_symlink_and_dangling_symlink_are_rejected(self):
        target = self.base / "outside.txt"
        target.write_text("EXTERNAL FIXTURE\n")
        for target_path in (target, self.base / "missing-target"):
            with self.subTest(target=target_path.name):
                link = self.root / "linked-source.py"
                link.symlink_to(target_path)
                try:
                    self.assert_rejected()
                finally:
                    link.unlink()

    def test_source_fifo_is_rejected_without_blocking(self):
        path = self.put("source.pipe", "tracked regular fixture\n")
        subprocess.run(["git", "-C", str(self.root), "add", "source.pipe"], check=True)
        path.unlink()
        os.mkfifo(path)
        self.assert_rejected()

    def test_synthetic_private_key_binary_and_oversize_material_are_rejected(self):
        samples = [("material.pem", b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 64 + b"\n-----END PRIVATE KEY-----\n"),
                   ("artifact.jar", b"not-a-real-jar"), ("material.bin", b"binary\x00fixture\xff"),
                   ("oversized.txt", b"x" * (4 * 1024 * 1024 + 1))]
        for name, content in samples:
            with self.subTest(name=name):
                path = self.put(name, content)
                try:
                    self.assert_rejected()
                finally:
                    path.unlink()
                    self.output.unlink(missing_ok=True)

    def test_existing_output_is_preserved(self):
        self.output.write_bytes(b"existing external fixture")
        with self.assertRaises(ValueError):
            exporter.export(self.output, root=self.root)
        self.assertEqual(b"existing external fixture", self.output.read_bytes())

    def test_competing_output_created_during_exclusive_open_is_preserved(self):
        original_open = Path.open
        output = self.output
        def competing_open(path, mode="r", *args, **kwargs):
            if path == output and mode == "xb":
                with original_open(output, "wb") as stream:
                    stream.write(b"competing external fixture")
                raise FileExistsError(str(output))
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, "open", competing_open):
            with self.assertRaises((OSError, ValueError)):
                exporter.export(output, root=self.root)
        self.assertTrue(output.exists(), "拒绝独占创建后删除了竞争者文件")
        self.assertEqual(b"competing external fixture", output.read_bytes())

    def test_failed_archive_write_removes_only_its_own_partial_output(self):
        with patch.object(exporter.tarfile, "open", side_effect=RuntimeError("fixture write failure")):
            with self.assertRaises(RuntimeError):
                exporter.export(self.output, root=self.root)
        self.assertFalse(self.output.exists())

    def test_existing_source_manifest_cannot_corrupt_generated_manifest(self):
        self.put("SOURCE-MANIFEST.json", "{\"old\": \"fixture\"}\n")
        try:
            exporter.export(self.output, root=self.root)
        except ValueError:
            self.assertFalse(self.output.exists())
            return
        with tarfile.open(self.output, "r:gz") as archive:
            manifest = json.loads(archive.extractfile("SOURCE-MANIFEST.json").read())
            self.assertNotIn("SOURCE-MANIFEST.json", manifest)
            for name, digest in manifest.items():
                self.assertEqual(digest, hashlib.sha256(archive.extractfile(name).read()).hexdigest(), name)

    def test_exported_package_runs_second_project_without_company_profile(self):
        classpath = groovy_classpath()
        self.assertTrue(classpath, "公开包自足验收需要已有 Groovy，不允许 skip")
        exporter.export(self.output, root=ROOT)
        extracted = self.base / "extracted"
        extracted.mkdir()
        with tarfile.open(self.output, "r:gz") as archive:
            for member in archive.getmembers():
                self.assertTrue(member.isfile())
                self.assertFalse(Path(member.name).is_absolute())
                self.assertNotIn("..", Path(member.name).parts)
                archive.extract(member, path=extracted)
        self.assertFalse((extracted / "projects").exists())
        absolute_classpath = os.pathsep.join(str(Path(item).resolve()) for item in classpath.split(os.pathsep))
        result = subprocess.run([sys.executable, str(extracted / "examples/second-project/validate.py")], cwd=extracted,
                                env=dict(os.environ, GROOVY_TEST_CLASSPATH=absolute_classpath, APP_SOURCE_ROOT="/missing-private-app"),
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("PASS: second-project", result.stdout)
        self.assertIn("Vendor calls: 0", result.stdout)


if __name__ == "__main__":
    unittest.main()
