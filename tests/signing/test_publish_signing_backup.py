import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from signing import publish_signing_backup as backup


def run_git(directory, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost",
                           "-c", "commit.gpgsign=false", *args], cwd=directory, check=True,
                          env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()


class SigningBackupDestinationTest(unittest.TestCase):
    def test_destination_is_the_application_backup_branch(self):
        self.assertEqual("git@gitee.com:example/legacy-application.git", backup.REMOTE)
        self.assertEqual("backup/signing", backup.BRANCH)
        self.assertEqual("refs/heads/backup/signing", backup.REF)


class PublishSigningBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.remote.mkdir()
        run_git(self.remote, "init", "--bare", "--initial-branch=master")
        self.caller = self.root / "caller"
        self.caller.mkdir()
        run_git(self.caller, "init", "--initial-branch=master")
        (self.caller / "source.kt").write_text("project source")
        run_git(self.caller, "add", "source.kt")
        run_git(self.caller, "commit", "-m", "Existing project")
        run_git(self.caller, "push", str(self.remote), "master")
        self.master = run_git(self.remote, "rev-parse", "master")
        (self.caller / "unrelated.txt").write_text("preserved")
        run_git(self.caller, "add", "unrelated.txt")
        self.index_before = (self.caller / ".git/index").read_bytes()
        self.source = "a" * 40
        self.content = hashlib.sha256(b"whole high entropy archive").hexdigest()
        self.snapshot = self.root / "input.enc.json"
        self.raw = self.make_snapshot()
        self.snapshot.write_bytes(self.raw)
        self.addCleanup(patch.stopall)
        self.profile = json.loads(json.dumps(backup.DEFAULT_PROJECT))
        self.profile['repositories']['application']['ssh'] = str(self.remote)
        self.profile['signingBackup']['recipientSha256'] = 'b' * 64
        self.profile['signingBackup']['legacyRecipientSha256'] = 'b' * 64
        patch.object(backup, "load_project", lambda project_id='sxmqlive': self.profile).start()
        patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -i /unused-test-key",
                                "GIT_INDEX_FILE": str(self.caller / ".git/index")}).start()
        self.original_git = backup.git

    @staticmethod
    def make_snapshot():
        sealed = {"format": "sxmqlive-signing-backup-v2", "keyAlgorithm": "RSA-OAEP-SHA256",
                  "recipientSha256": "b" * 64}
        for key, size in (("wrappedKey", 384), ("nonce", 12), ("tag", 16), ("ciphertext", 512)):
            sealed[key] = base64.b64encode(os.urandom(size)).decode()
        return (json.dumps(sealed) + "\n").encode()

    def publish(self, content=None, path=None):
        return backup.publish(path or self.snapshot, content or self.content, self.source, 7)

    def latest(self):
        return json.loads(run_git(self.remote, "show", backup.BRANCH + ":latest.json"))

    def test_initial_orphan_second_snapshot_and_caller_index_preserved(self):
        first = self.publish()
        self.assertEqual("synced", first["status"])
        self.assertEqual(self.raw, subprocess.check_output([
            "git", "-C", str(self.remote), "show", backup.BRANCH + ":" + first["snapshot"]]))
        self.assertEqual("1", run_git(self.remote, "rev-list", "--count", backup.BRANCH))
        self.assertEqual(first["gitCommit"], run_git(self.remote, "rev-list", "--max-parents=0", backup.BRANCH))
        second = self.publish(hashlib.sha256(b"changed archive").hexdigest())
        self.assertEqual("synced", second["status"])
        files = run_git(self.remote, "ls-tree", "-r", "--name-only", backup.BRANCH).splitlines()
        self.assertEqual(3, len(files))
        self.assertIn(first["snapshot"], files)
        self.assertIn(second["snapshot"], files)
        self.assertNotIn("source.kt", files)
        self.assertEqual(self.master, run_git(self.remote, "rev-parse", "master"))
        self.assertEqual(self.index_before, (self.caller / ".git/index").read_bytes())

    def test_content_checksum_skips_reencrypted_identical_archive(self):
        first = self.publish()
        self.snapshot.write_bytes(self.make_snapshot())
        second = self.publish()
        self.assertEqual("unchanged", second["status"])
        self.assertEqual(first["gitCommit"], second["gitCommit"])
        self.assertEqual(first["ciphertextSha256"], second["ciphertextSha256"])
        self.assertEqual("1", run_git(self.remote, "rev-list", "--count", backup.BRANCH))

    def test_identical_content_for_a_different_recipient_appends_a_new_snapshot(self):
        first = self.publish()
        changed = json.loads(self.make_snapshot())
        changed['recipientSha256'] = 'c' * 64
        self.profile['signingBackup']['recipientSha256'] = 'c' * 64
        self.snapshot.write_text(json.dumps(changed))
        second = self.publish()
        self.assertEqual('synced', second['status'])
        self.assertNotEqual(first['gitCommit'], second['gitCommit'])
        self.assertEqual('c' * 64, self.latest()['recipientSha256'])
        self.assertEqual('2', run_git(self.remote, 'rev-list', '--count', backup.BRANCH))
        self.assertEqual(second['gitCommit'], self.publish()['gitCommit'])

    def test_unapproved_recipient_fails_before_git_or_network(self):
        self.profile['signingBackup']['recipientSha256'] = 'c' * 64
        with patch.object(backup, 'git') as git, self.assertRaisesRegex(ValueError, '接收人'):
            self.publish()
        git.assert_not_called()

    def test_second_project_has_separate_branch_and_scope_and_cannot_reuse_controller_history(self):
        original = self.publish()
        self.profile['id'] = 'example-mobile'
        self.profile['signingBackup'].update(scope='project_signing', branch='backup/example-signing', prefixes=['ios/test-team/materials/demo'])
        result = backup.publish(self.snapshot, self.content, self.source, 8, project_id='example-mobile')
        index = json.loads(run_git(self.remote, 'show', 'backup/example-signing:latest.json'))
        self.assertEqual(('example-mobile', 'project_signing'), (index['projectId'], index['scope']))
        self.assertEqual('backup/example-signing', result['branch'])
        self.assertEqual(original['gitCommit'], run_git(self.remote, 'rev-parse', 'backup/signing'))
        self.profile['signingBackup']['branch'] = 'backup/signing'
        with self.assertRaisesRegex(ValueError, '其他项目或范围'):
            backup.publish(self.snapshot, self.content, self.source, 8, project_id='example-mobile')
        self.assertEqual(original['gitCommit'], run_git(self.remote, 'rev-parse', 'backup/signing'))

    def test_legacy_index_is_not_relabelled_as_another_project_or_recipient(self):
        self.publish()
        value = self.latest()
        value.pop('projectId'); value.pop('scope')
        value['format'] = 'sxmqlive-signing-backup-index-v1'
        self.assertEqual(value, backup.validate_index(value, self.profile))
        self.profile['id'] = 'example-mobile'
        with self.assertRaisesRegex(ValueError, '旧索引'):
            backup.validate_index(value, self.profile)
        self.profile['id'] = 'sxmqlive'
        self.profile['signingBackup']['legacyRecipientSha256'] = 'c' * 64
        with self.assertRaisesRegex(ValueError, '旧索引'):
            backup.validate_index(value, self.profile)

    def test_recipient_recovery_is_self_contained_and_identical_bytes_skip(self):
        recovery = self.root / "recipient-private-key.enc.json"
        value = {key: value for key, value in json.loads(self.raw).items()
                 if key in {"nonce", "tag", "ciphertext"}}
        value["format"] = "sxmqlive-signing-backup-v1"
        recovery.write_text(json.dumps(value) + "\n")
        first = backup.publish(self.snapshot, self.content, self.source, 7, recovery)
        expected = "recipients/" + "b" * 64 + ".enc.json"
        self.assertEqual(expected, first["recipientRecovery"])
        self.assertEqual(expected, self.latest()["recipientRecovery"])
        restored = subprocess.check_output(["git", "-C", str(self.remote), "show",
                                             backup.BRANCH + ":" + expected])
        self.assertEqual(recovery.read_bytes(), restored)
        self.assertEqual(3, len(run_git(self.remote, "ls-tree", "-r", "--name-only", backup.BRANCH).splitlines()))
        second = backup.publish(self.snapshot, self.content, self.source, 7, recovery)
        self.assertEqual("unchanged", second["status"])
        self.assertEqual(first["gitCommit"], second["gitCommit"])

    def test_recipient_recovery_cannot_be_replaced_even_for_duplicate_content(self):
        recovery = self.root / "recipient-private-key.enc.json"
        value = {key: value for key, value in json.loads(self.raw).items()
                 if key in {"nonce", "tag", "ciphertext"}}
        value["format"] = "sxmqlive-signing-backup-v1"
        recovery.write_text(json.dumps(value) + "\n")
        first = backup.publish(self.snapshot, self.content, self.source, 7, recovery)
        original = recovery.read_bytes()
        value["nonce"] = base64.b64encode(os.urandom(12)).decode()
        recovery.write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(ValueError, "不得替换"):
            backup.publish(self.snapshot, self.content, self.source, 7, recovery)
        self.assertEqual(first["gitCommit"], run_git(self.remote, "rev-parse", backup.BRANCH))
        self.assertEqual(original, subprocess.check_output([
            "git", "-C", str(self.remote), "show", backup.BRANCH + ":" + first["recipientRecovery"]]))
        value["secret"] = "plaintext"
        with self.assertRaises(ValueError):
            backup.encrypted_snapshot(json.dumps(value).encode(), recovery=True)

    def test_plaintext_unknown_fields_duplicate_fields_and_invalid_sizes_rejected(self):
        for field, value in (("format", "plain"), ("privateKey", "secret"),
                             ("nonce", base64.b64encode(b"short").decode()),
                             ("wrappedKey", base64.b64encode(b"x" * 256).decode()),
                             ("ciphertext", "not base64")):
            with self.subTest(field=field):
                sealed = json.loads(self.raw)
                sealed[field] = value
                with self.assertRaises(ValueError):
                    backup.encrypted_snapshot(json.dumps(sealed).encode())
        with self.assertRaises(ValueError):
            backup.encrypted_snapshot(self.raw[:-2] + b',"format":"sxmqlive-signing-backup-v2"}')

    def test_ssh_cannot_disable_host_verification_or_override_known_hosts(self):
        for command in ("", "ssh -o StrictHostKeyChecking=no", "ssh -o UserKnownHostsFile=/dev/null",
                        "ssh -F /untrusted/config"):
            with self.subTest(command=command), patch.dict(os.environ, {"GIT_SSH_COMMAND": command}):
                with self.assertRaises(ValueError):
                    backup.git_environment()
        environment = backup.git_environment()
        self.assertNotIn("GIT_INDEX_FILE", environment)
        self.assertIn("StrictHostKeyChecking=yes", environment["GIT_SSH_COMMAND"])

    def test_existing_project_history_rejected_without_push(self):
        run_git(self.remote, "branch", backup.BRANCH, "master")
        with self.assertRaisesRegex(ValueError, "非备份文件"):
            self.publish()
        self.assertEqual(self.master, run_git(self.remote, "rev-parse", backup.BRANCH))

    def test_remote_index_path_and_checksum_are_verified_before_skip(self):
        first = self.publish()
        clone = self.root / "tamper"
        run_git(self.root, "clone", "--branch", backup.BRANCH, str(self.remote), str(clone))
        latest = self.latest()
        latest["ciphertextSha256"] = "0" * 64
        (clone / "latest.json").write_text(json.dumps(latest))
        run_git(clone, "add", "latest.json")
        run_git(clone, "commit", "-m", "Invalid index")
        run_git(clone, "push", "origin", backup.BRANCH)
        with self.assertRaisesRegex(ValueError, "密文校验"):
            self.publish()
        self.assertEqual(first["snapshot"], latest["snapshot"])
        latest["snapshot"] = "../credentials.xml"
        with self.assertRaises(ValueError):
            backup.validate_index(latest)

    def test_historical_source_files_rejected_even_when_deleted(self):
        self.publish()
        clone = self.root / "history"
        run_git(self.root, "clone", "--branch", backup.BRANCH, str(self.remote), str(clone))
        (clone / "secret.txt").write_text("plaintext must never enter history")
        run_git(clone, "add", "secret.txt")
        run_git(clone, "commit", "-m", "Bad history")
        run_git(clone, "rm", "secret.txt")
        run_git(clone, "commit", "-m", "Delete plaintext")
        run_git(clone, "push", "origin", backup.BRANCH)
        with self.assertRaisesRegex(ValueError, "仅追加"):
            self.publish()

    def test_nonfastforward_retries_without_force_and_preserves_competing_backup(self):
        self.publish(hashlib.sha256(b"initial").hexdigest())
        competing_content = hashlib.sha256(b"competing").hexdigest()
        pushes = []

        def concurrent(repo, env, *args, **kwargs):
            if args[0] == "push":
                pushes.append(args)
                if len(pushes) == 1:
                    with patch.object(backup, "git", self.original_git):
                        self.publish(competing_content)
            return self.original_git(repo, env, *args, **kwargs)

        with patch.object(backup, "git", concurrent):
            result = self.publish()
        self.assertEqual("synced", result["status"])
        self.assertEqual(2, len(pushes))
        self.assertFalse(any("force" in argument or argument.startswith("+")
                             for push in pushes for argument in push))
        self.assertEqual("3", run_git(self.remote, "rev-list", "--count", backup.BRANCH))

    def test_concurrent_identical_content_is_unchanged(self):
        pushes = 0

        def concurrent(repo, env, *args, **kwargs):
            nonlocal pushes
            if args[0] == "push":
                pushes += 1
                with patch.object(backup, "git", self.original_git):
                    self.publish()
            return self.original_git(repo, env, *args, **kwargs)

        with patch.object(backup, "git", concurrent):
            result = self.publish()
        self.assertEqual("unchanged", result["status"])
        self.assertEqual(1, pushes)
        self.assertEqual("1", run_git(self.remote, "rev-list", "--count", backup.BRANCH))

    def test_failure_keeps_pending_report_and_original_ciphertext(self):
        destination = self.root / "report.json"

        def rejected(repo, env, *args, **kwargs):
            if args[0] == "push":
                return subprocess.CompletedProcess(args, 1, b"", b"PRIVATE_KEY secret")
            return self.original_git(repo, env, *args, **kwargs)

        with patch.object(backup, "git", rejected), self.assertRaisesRegex(RuntimeError, "Git push"):
            backup.main(["--snapshot", str(self.snapshot), "--content-sha256", self.content,
                         "--source-commit", self.source, "--build-number", "7", "--report", str(destination)])
        result = json.loads(destination.read_text())
        self.assertEqual("pending", result["status"])
        self.assertNotIn("PRIVATE_KEY", destination.read_text())
        self.assertEqual(self.raw, self.snapshot.read_bytes())

    def test_report_cannot_overwrite_snapshot(self):
        with self.assertRaisesRegex(ValueError, "不能覆盖"):
            backup.main(["--snapshot", str(self.snapshot), "--content-sha256", self.content,
                         "--source-commit", self.source, "--build-number", "7", "--report", str(self.snapshot)])
        self.assertEqual(self.raw, self.snapshot.read_bytes())
        hardlink = self.root / "same-inode.json"
        os.link(self.snapshot, hardlink)
        with self.assertRaisesRegex(ValueError, "不能覆盖"):
            backup.main(["--snapshot", str(self.snapshot), "--content-sha256", self.content,
                         "--source-commit", self.source, "--build-number", "7", "--report", str(hardlink)])
        self.assertEqual(self.raw, self.snapshot.read_bytes())

    def test_historical_index_cannot_hide_private_fields(self):
        self.publish()
        clone = self.root / "index-history"
        run_git(self.root, "clone", "--branch", backup.BRANCH, str(self.remote), str(clone))
        latest = self.latest()
        (clone / "latest.json").write_text(json.dumps({**latest, "password": "not public"}))
        run_git(clone, "add", "latest.json")
        run_git(clone, "commit", "-m", "Invalid historical index")
        (clone / "latest.json").write_text(json.dumps(latest))
        run_git(clone, "add", "latest.json")
        run_git(clone, "commit", "-m", "Remove private field")
        run_git(clone, "push", "origin", backup.BRANCH)
        with self.assertRaisesRegex(ValueError, "索引字段"):
            self.publish()

    def test_continuous_concurrent_push_conflicts_stop_after_two_retries(self):
        self.publish(hashlib.sha256(b"initial").hexdigest())
        pushes = 0

        def concurrent(repo, env, *args, **kwargs):
            nonlocal pushes
            if args[0] == "push":
                pushes += 1
                with patch.object(backup, "git", self.original_git):
                    self.publish(hashlib.sha256(str(pushes).encode()).hexdigest())
            return self.original_git(repo, env, *args, **kwargs)

        with patch.object(backup, "git", concurrent), self.assertRaisesRegex(RuntimeError, "Git push"):
            self.publish()
        self.assertEqual(3, pushes)
        self.assertEqual(self.raw, self.snapshot.read_bytes())

    def test_push_success_still_requires_remote_verification(self):
        def false_success(repo, env, *args, **kwargs):
            if args[0] == "push":
                return subprocess.CompletedProcess(args, 0, b"", b"")
            return self.original_git(repo, env, *args, **kwargs)
        with patch.object(backup, "git", false_success), self.assertRaisesRegex(RuntimeError, "远端快照验证"):
            self.publish()


if __name__ == "__main__":
    unittest.main()
