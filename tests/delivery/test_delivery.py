import unittest
import json
import multiprocessing
import os
import stat
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from delivery.delivery import (
    ArtifactInfo,
    DeliveryContext,
    DeliveryContextError,
    WorkItemRef,
    optional_zentao_work_item,
    read_json_object,
    resolve_test_branch_target,
    write_json_object,
    delivery_operation,
    main as delivery_main,
)


def interrupt_pending(identity, state_root):
    os.environ['DELIVERY_STATE_ROOT'] = state_root
    with delivery_operation(identity) as (_, save):
        save(phase='pending')
        os._exit(23)


class DeliveryPreflightTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'delivery-state'
        environment = patch.dict(os.environ, {'DELIVERY_STATE_ROOT': str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_valid_preflight_preserves_pending_receipts_and_leaves_no_probe(self):
        self.root.mkdir(mode=0o700)
        receipt = self.root / 'existing.json'
        receipt.write_bytes(b'{"phase":"pending"}')
        receipt.chmod(0o600)
        self.assertEqual(0, delivery_main(['preflight']))
        self.assertEqual(b'{"phase":"pending"}', receipt.read_bytes())
        self.assertEqual([receipt], list(self.root.iterdir()))

    def test_missing_or_relative_root_fails_without_creating_state(self):
        for value in ['', 'relative-state']:
            with self.subTest(value=value), patch.dict(os.environ, {'DELIVERY_STATE_ROOT': value}):
                self.assertEqual(1, delivery_main(['preflight']))
        self.assertFalse(self.root.exists())

    def test_symbolic_root_or_parent_fails_without_creating_state(self):
        actual = self.base / 'actual'
        actual.mkdir(mode=0o700)
        link = self.base / 'link'
        link.symlink_to(actual, target_is_directory=True)
        for path in [link, link / 'state']:
            with self.subTest(path=path), patch.dict(os.environ, {'DELIVERY_STATE_ROOT': str(path)}):
                self.assertEqual(1, delivery_main(['preflight']))
        self.assertEqual([], list(actual.iterdir()))

    def test_root_inside_any_workspace_is_rejected_before_creation(self):
        workspace = self.base / 'workspace'
        state = workspace / 'state'
        for key in ['WORKSPACE', 'APP_SOURCE_ROOT', 'CI_TOOL_ROOT']:
            with self.subTest(key=key), patch.dict(os.environ, {key: str(workspace), 'DELIVERY_STATE_ROOT': str(state)}):
                self.assertEqual(1, delivery_main(['preflight']))
        self.assertFalse(state.exists())

    def test_wrong_mode_or_owner_and_unwritable_root_are_rejected(self):
        self.root.mkdir(mode=0o755)
        self.assertEqual(1, delivery_main(['preflight']))
        self.root.chmod(0o700)
        with patch('delivery.delivery.os.getuid', return_value=os.getuid() + 1):
            self.assertEqual(1, delivery_main(['preflight']))
        with patch('delivery.delivery.tempfile.TemporaryFile', side_effect=PermissionError('read-only fixture')):
            self.assertEqual(1, delivery_main(['preflight']))
        self.assertEqual([], list(self.root.iterdir()))

    def test_dry_run_never_reads_or_creates_delivery_state(self):
        with patch('delivery.delivery.delivery_state_root', side_effect=AssertionError('must not inspect state')):
            self.assertEqual(0, delivery_main(['preflight', '--dry-run']))
        self.assertFalse(self.root.exists())


class TestBranchTargetTest(unittest.TestCase):
    def test_resolves_supported_test_branches(self):
        cases = (
            ("feature/miaoshou/login", "miaoshou", None),
            ("refs/heads/dev/kangmin", "kangmin", None),
            ("origin/release/boai-1.2.3", "boai", "1.2.3"),
        )

        for branch, brand, version in cases:
            with self.subTest(branch=branch):
                target = resolve_test_branch_target(branch)
                self.assertEqual(brand, target.brand)
                self.assertEqual(version, target.version)

    def test_rejects_unsupported_test_branch(self):
        with self.assertRaisesRegex(DeliveryContextError, "测试包分支必须使用"):
            resolve_test_branch_target("master")


class JsonObjectTest(unittest.TestCase):
    def test_atomic_write_failure_preserves_old_receipt(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            write_json_object(path, {"phase": "pending"})
            with patch("delivery.delivery.os.replace", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    write_json_object(path, {"phase": "complete"})
            self.assertEqual({"phase": "pending"}, read_json_object(path))
            self.assertEqual([path], list(path.parent.iterdir()))

    def test_round_trip_preserves_json_object(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "context.json"

            write_json_object(path, {"brand": "kangmin", "version": "1.2.3"})

            self.assertEqual(
                {"brand": "kangmin", "version": "1.2.3"},
                read_json_object(path),
            )

    def test_rejects_invalid_json(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(DeliveryContextError, "无法读取发布上下文"):
                read_json_object(path)

    def test_rejects_non_object_json(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(DeliveryContextError, "不是 JSON 对象"):
                read_json_object(path)


class DeliveryOperationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "state"
        self.environment = patch.dict(os.environ, {"DELIVERY_STATE_ROOT": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.identity = {"operation": "fixture", "artifact_sha256": "a" * 64}

    def test_pending_survives_interruption_and_permissions_are_private(self):
        with self.assertRaisesRegex(RuntimeError, "interrupt"):
            with delivery_operation(self.identity) as (state, save):
                save(phase="pending")
                raise RuntimeError("interrupt")
        with delivery_operation(self.identity) as (state, save):
            self.assertEqual("pending", state["phase"])
            save(phase="complete")
        self.assertEqual(0o700, stat.S_IMODE(self.root.stat().st_mode))
        for path in self.root.iterdir():
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_process_exit_releases_lock_and_preserves_pending(self):
        process = multiprocessing.get_context('spawn').Process(
            target=interrupt_pending, args=(self.identity, str(self.root)))
        process.start()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
            self.fail('interrupted process did not exit')
        self.assertEqual(23, process.exitcode)
        process.close()
        with delivery_operation(self.identity) as (state, _):
            self.assertEqual('pending', state['phase'])

    def test_invalid_root_and_receipt_fail_closed(self):
        for value in ("", "relative", str(Path.cwd())):
            with self.subTest(root=value), patch.dict(os.environ, {"DELIVERY_STATE_ROOT": value}):
                with self.assertRaises(DeliveryContextError):
                    with delivery_operation(self.identity):
                        self.fail("must not execute")
        with delivery_operation(self.identity) as (_, save):
            save(phase="pending")
        receipt = next(self.root.glob("*.json"))
        for body in ("{", "[]", json.dumps({"version": 1, "identity": {}}),
                     json.dumps({"version": 1, "identity": self.identity})):
            receipt.write_text(body)
            with self.assertRaises(DeliveryContextError):
                with delivery_operation(self.identity):
                    self.fail("must not execute")

    def test_scope_binds_pending_materials_and_allows_new_delivery_only_after_complete(self):
        scope = {'operation': 'fixture'}
        original = {**scope, 'artifact_sha256': 'a' * 64, 'branch': 'dev/fixture'}
        changed = {**scope, 'artifact_sha256': 'b' * 64, 'branch': 'release/fixture'}
        with delivery_operation(original, scope=scope) as (_, save):
            save(phase='pending')
        with self.assertRaisesRegex(DeliveryContextError, '禁止重新上传或发送'):
            with delivery_operation(changed, scope=scope):
                self.fail('changed materials must not bypass pending')
        with delivery_operation(original, scope=scope) as (_, save):
            save(phase='complete', build_key='key', receipt={})
        with self.assertRaises(DeliveryContextError):
            with delivery_operation(changed, scope=scope):
                self.fail('incomplete receipt must not release scope')
        receipt = ArtifactInfo('fixture', '1', '1', 'com.example', 'key', 'https://www.pgyer.com/abc', '').to_dict()
        with delivery_operation(original, scope=scope) as (_, save):
            save(phase='complete', build_key='key', receipt=receipt)
        with delivery_operation(changed, scope=scope) as (state, save):
            self.assertEqual({'version', 'identity'}, set(state))
            save(phase='pending')

    def test_changed_material_cannot_bypass_tampered_upload_key_in_complete_receipt(self):
        scope = {'operation': 'pgyer-upload', 'platform': 'android', 'brand': 'fixture'}
        original = {**scope, 'artifact_sha256': 'a' * 64}
        changed = {**original, 'artifact_sha256': 'b' * 64}
        receipt = ArtifactInfo('fixture', '1', '1', 'com.example', 'key',
                               'https://www.pgyer.com/abc', '').to_dict()
        for upload_key in ('other.apk', 'key.ipa', 'key.apk.extra', 'key.apk.apk', '', None, 42):
            with self.subTest(upload_key=upload_key):
                with delivery_operation(original, scope=scope) as (_, save):
                    save(phase='complete', build_key='key', upload_key=upload_key, receipt=receipt)
                path = next(self.root.glob('*.json'))
                before = path.read_bytes()
                with self.assertRaises(DeliveryContextError):
                    with delivery_operation(changed, scope=scope):
                        self.fail('changed materials must not reset invalid upload provenance')
                self.assertEqual(before, path.read_bytes())
        for upload_key in ('key', 'key.apk'):
            with delivery_operation(original, scope=scope) as (_, save):
                save(phase='complete', build_key='key', upload_key=upload_key, receipt=receipt)
            with delivery_operation(changed, scope=scope) as (state, _):
                self.assertEqual({'version', 'identity'}, set(state))

    def test_legacy_full_identity_record_cannot_be_bypassed_by_scoped_key(self):
        with delivery_operation(self.identity) as (_, save):
            save(phase='pending')
        with self.assertRaisesRegex(DeliveryContextError, '旧版交付回执'):
            with delivery_operation({**self.identity, 'artifact_sha256': 'b' * 64, 'brand': 'new-field'},
                                    scope={'operation': 'fixture', 'brand': 'new-field'}):
                self.fail('new scoped identity must not bypass legacy pending')

    def test_fsync_failure_before_and_after_replace_preserves_a_blocking_receipt(self):
        for failure in ('file', 'directory'):
            with self.subTest(failure=failure):
                with delivery_operation(self.identity) as (_, save):
                    save(phase='pending')
                fsync = os.fsync
                def fail_sync(descriptor):
                    directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
                    if directory == (failure == 'directory'):
                        raise OSError('fixture fsync failure')
                    fsync(descriptor)
                with patch('delivery.delivery.os.fsync', side_effect=fail_sync):
                    with self.assertRaises(OSError):
                        with delivery_operation(self.identity) as (_, save):
                            save(phase='uploaded')
                with delivery_operation(self.identity) as (state, _):
                    self.assertEqual('uploaded' if failure == 'directory' else 'pending', state['phase'])

    def test_symlink_and_public_state_are_rejected_without_modifying_target(self):
        self.root.mkdir(mode=0o700)
        with delivery_operation(self.identity) as (_, save):
            save(phase="pending")
        receipt = next(self.root.glob("*.json"))
        receipt.chmod(0o644)
        with self.assertRaises(DeliveryContextError):
            with delivery_operation(self.identity):
                self.fail("must not execute")
        target = self.root / "target"
        target.write_text("preserve")
        receipt.unlink()
        receipt.symlink_to(target)
        with self.assertRaises(OSError):
            with delivery_operation(self.identity):
                self.fail("must not execute")
        self.assertEqual("preserve", target.read_text())


class DeliveryContextTest(unittest.TestCase):
    def test_round_trip_preserves_common_and_platform_fields(self):
        context = DeliveryContext(
            platform="android",
            brand="miaoshou",
            display_name="妙手中医",
            environment="test",
            branch="feature/miaoshou/login",
            commit="abc123",
            builder="jenkins",
            version_name="1.2.3",
            version_code="123",
            artifact_path="/tmp/app.apk",
            changelog_path="/tmp/changelog.md",
            distribution="",
            delivery="pgyer",
            work_item=WorkItemRef(
                system="zentao",
                item_type="story",
                item_id="1024",
                url="https://zentao.example.test/story-view-1024.html",
            ),
        )

        value = context.to_dict({"apk_path": "/tmp/app.apk", "build_type": "Qa"})
        restored = DeliveryContext.from_dict(value)

        self.assertEqual(context, restored)
        self.assertEqual("kuikly", DeliveryContext.from_dict(context._replace(ui_engine="kuikly").to_dict()).ui_engine)
        self.assertEqual("Qa", value["build_type"])
        self.assertEqual("1024", value["work_item"]["id"])

    def test_reads_legacy_ios_context_without_generic_artifact_path(self):
        context = DeliveryContext.from_dict(
            {
                "platform": "ios",
                "brand": "kangmin",
                "display_name": "康民中医",
                "environment": "dev",
                "branch": "feature/kangmin/ios-ci",
                "commit": "abc123",
                "builder": "jenkins",
                "version_name": "1.2.3",
                "build_number": "202609010001",
                "ipa_path": "/tmp/Kangmin.ipa",
                "changelog_path": "/tmp/changelog.md",
                "distribution": "adhoc",
                "delivery": "pgyer",
            }
        )

        self.assertEqual("/tmp/Kangmin.ipa", context.artifact_path)
        self.assertEqual("202609010001", context.version_code)

    def test_reads_legacy_android_context_without_platform(self):
        context = DeliveryContext.from_dict(
            {
                "brand": "miaoshou",
                "apk_path": "/tmp/app.apk",
                "changelog_path": "/tmp/changelog.md",
            }
        )

        self.assertEqual("android", context.platform)
        self.assertEqual("pgyer", context.delivery)
        self.assertEqual("cmp", context.ui_engine)

    def test_rejects_unknown_ui_engine(self):
        for engine in ("", "other", None):
            with self.subTest(engine=engine), self.assertRaises(DeliveryContextError):
                DeliveryContext.from_dict({
                    "brand": "jimin", "apk_path": "/tmp/app.apk",
                    "changelog_path": "/tmp/changelog.md", "ui_engine": engine,
                })

    def test_rejects_context_without_artifact(self):
        with self.assertRaises(DeliveryContextError):
            DeliveryContext.from_dict(
                {
                    "platform": "android",
                    "brand": "miaoshou",
                    "changelog_path": "/tmp/changelog.md",
                }
            )


class ArtifactInfoTest(unittest.TestCase):
    def test_serialization_keeps_existing_release_info_schema(self):
        artifact = ArtifactInfo(
            name="妙手中医",
            version="1.2.3",
            version_code="123",
            package_name="com.example.app",
            build_key="pgyer-key",
            download_url="https://example.test/download",
            qr_code_url="https://example.test/qr",
        )

        self.assertEqual(artifact, ArtifactInfo.from_dict(artifact.to_dict()))
        self.assertEqual("pgyer-key", artifact.to_dict()["build_key"])
        self.assertEqual("cmp", ArtifactInfo.from_dict({}).ui_engine)
        self.assertEqual("kuikly", ArtifactInfo.from_dict(artifact._replace(ui_engine="kuikly").to_dict()).ui_engine)
        with self.assertRaises(DeliveryContextError):
            ArtifactInfo.from_dict({"ui_engine": "other"})


class WorkItemRefTest(unittest.TestCase):
    def test_builds_optional_zentao_reference(self):
        work_item = optional_zentao_work_item(
            "story",
            "1024",
            "https://tracker.example.invalid/zentao/story-view-1024.html",
        )

        self.assertIsNotNone(work_item)
        self.assertEqual("需求 #1024", work_item.display_name)
        self.assertEqual("zentao", work_item.system)

    def test_all_empty_fields_mean_no_work_item(self):
        self.assertIsNone(optional_zentao_work_item("", "", ""))

    def test_rejects_partial_work_item(self):
        with self.assertRaisesRegex(DeliveryContextError, "必须同时填写"):
            optional_zentao_work_item("bug", "2048", "")

    def test_rejects_invalid_work_item_fields(self):
        invalid_values = (
            ("issue", "2048", "https://tracker.example.invalid/zentao/bug-view-2048.html"),
            ("bug", "0", "https://tracker.example.invalid/zentao/bug-view-2048.html"),
            ("bug", "2048", "http://tracker.example.invalid/zentao/bug-view-2048.html"),
            ("bug", "2048", "https://user:pass@tracker.example.invalid/zentao/bug-view-2048.html"),
        )

        for values in invalid_values:
            with self.subTest(values=values):
                with self.assertRaises(DeliveryContextError):
                    optional_zentao_work_item(*values)

    def test_rejects_invalid_work_item_read_from_context(self):
        with self.assertRaises(DeliveryContextError):
            WorkItemRef.from_dict(
                {
                    "system": "zentao",
                    "type": "story",
                    "id": "1024",
                    "url": "http://tracker.example.invalid/zentao/story-view-1024.html",
                }
            )


if __name__ == "__main__":
    unittest.main()
