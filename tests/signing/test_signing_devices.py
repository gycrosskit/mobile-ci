import json
import os
import stat
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from signing import signing_devices as module


class SigningDevicesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = {"brand": "alpha", "team": "one", "profile": "adhoc"}
        self.phone = {"name": "test-phone", "udid": "a" * 40, "deviceType": "IOS"}
        self.env = patch.dict(os.environ, {"WORKSPACE": str(module.ROOT)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_id_is_platform_and_normalized_udid_bound_and_sources_merge(self):
        first = module.merge_catalog(self.root, "ios", [self.phone], self.source)
        second = module.merge_catalog(self.root, "ios", [dict(self.phone, udid="A" * 40)],
                                      dict(self.source, brand="beta", team="two"))
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(2, len(second[0]["sources"]))
        self.assertNotEqual(module.device_id("harmony", self.phone["udid"]), first[0]["id"])
        self.assertEqual([self.phone], module.select_devices(self.root, "ios", "shared", first[0]["id"]))
        for path, mode in ((self.root / "devices", 0o700), (self.root / "devices/ios.json", 0o600),
                           (self.root / "devices/ios.lock", 0o600)):
            self.assertEqual(mode, stat.S_IMODE(path.stat().st_mode))

    def test_public_catalog_never_contains_full_udid_in_names_or_sources(self):
        secret = self.phone["udid"]
        module.merge_catalog(self.root, "ios", [dict(self.phone, name="phone " + secret)],
                             dict(self.source, profile="profile " + secret.upper()))
        report = json.dumps(module.public_catalog(self.root, "ios"))
        self.assertNotIn(secret, report.lower())
        self.assertIn("maskedUdid", report)
        self.assertIn(secret, (self.root / "devices/ios.json").read_text())

    def test_empty_pool_unknown_ids_and_other_platform_ids_are_rejected(self):
        self.assertEqual([], module.public_catalog(self.root, "ios"))
        with self.assertRaisesRegex(module.DeviceError, "改选 manual"):
            module.select_devices(self.root, "ios", "shared")
        identifier = module.merge_catalog(self.root, "ios", [self.phone], self.source)[0]["id"]
        other = module.merge_catalog(self.root, "harmony", [{"name": "huawei", "udid": "b" * 64, "deviceType": 4}], self.source)[0]["id"]
        for value in ("", "unknown", other, identifier + ",unknown"):
            with self.subTest(ids=value), self.assertRaisesRegex(module.DeviceError, "未知|其他平台"):
                module.select_devices(self.root, "ios", "shared", value)
        with self.assertRaisesRegex(module.DeviceError, "其他平台"):
            module.select_devices(self.root, "harmony", "shared", identifier)
        self.assertEqual(1, len(module.select_devices(self.root, "ios", "shared")))

    def test_manual_fields_are_paired_and_never_silently_ignored(self):
        self.assertEqual([], module.select_devices(self.root, "ios"))
        self.assertEqual([self.phone], module.select_devices(self.root, "ios", name="test-phone", udid="a" * 40))
        for name, udid in (("phone", ""), ("", "a" * 40)):
            with self.assertRaisesRegex(module.DeviceError, "同时填写"):
                module.select_devices(self.root, "ios", "shared", name=name, udid=udid)
        with self.assertRaisesRegex(module.DeviceError, "冲突"):
            module.select_devices(self.root, "ios", "shared", name="phone", udid="a" * 40)
        with self.assertRaises(module.DeviceError):
            module.select_devices(self.root, "ios", "manual", ids="id")

    def test_symlinks_permissions_platform_and_workspace_are_rejected(self):
        module.merge_catalog(self.root, "ios", [self.phone], self.source)
        path = self.root / "devices/ios.json"
        path.chmod(0o644)
        with self.assertRaisesRegex(module.DeviceError, "0600"):
            module.public_catalog(self.root, "ios")
        path.chmod(0o600)
        value = json.loads(path.read_text())
        value["platform"] = "harmony"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(module.DeviceError, "平台"):
            module.select_devices(self.root, "ios", "shared")
        path.unlink()
        path.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(module.DeviceError, "符号链接"):
            module.public_catalog(self.root, "ios")
        path.unlink()
        lock = self.root / "devices/ios.lock"
        lock.unlink()
        lock.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(module.DeviceError, "符号链接"):
            module.merge_catalog(self.root, "ios", [self.phone], self.source)
        with patch.dict(os.environ, {"WORKSPACE": str(self.root)}):
            with self.assertRaisesRegex(module.DeviceError, "workspace"):
                module.public_catalog(self.root, "ios")
        with self.assertRaises(module.DeviceError):
            module.public_catalog(self.root, "android")

    def test_parallel_merges_do_not_lose_approved_devices(self):
        module.merge_catalog(self.root, "ios", [], self.source)
        devices = [dict(self.phone, udid=str(value) * 40) for value in range(4)]
        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(module.merge_catalog, self.root, "ios", [device], self.source) for device in devices]
            for future in futures:
                future.result()
        self.assertEqual(4, len(module.public_catalog(self.root, "ios")))


if __name__ == "__main__":
    unittest.main()
