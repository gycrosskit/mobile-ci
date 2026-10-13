import json
import copy
import tempfile
import unittest
from pathlib import Path

from signing.ios_devices import IosDeviceError, resolve_device_brands


from tests.delivery import test_ios_release as ios_release_tests


class IosDevicesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = ios_release_tests.IosReleaseTest()._write_config(Path(self.temporary.name))
        values = json.loads(self.config.read_text())
        values["miaoshou"] = copy.deepcopy(values["kangmin"])
        values["dgtang"] = {}
        self.config.write_text(json.dumps(values))

    def test_all_selects_every_enabled_ios_test_brand(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "brands.json"
            original = json.loads(self.config.read_text())
            enabled = original["miaoshou"]
            disabled = copy.deepcopy(enabled)
            disabled["ios"]["signing"]["test"]["enabled"] = False
            config.write_text(json.dumps({
                "_iosTeams": original["_iosTeams"],
                "first": enabled,
                "disabled": disabled,
                "android_only": {},
                "new_brand": enabled,
            }))
            self.assertEqual(["first", "new_brand"], resolve_device_brands(config, "all"))

    def test_selected_brands_are_normalized_and_deduplicated(self):
        self.assertEqual(
            ["miaoshou", "kangmin"],
            resolve_device_brands(self.config, " MIAOSHOU, kangmin, miaoshou "),
        )

    def test_rejects_brand_without_enabled_ios_test_signing(self):
        with self.assertRaisesRegex(IosDeviceError, "未启用 iOS 测试签名"):
            resolve_device_brands(self.config, "dgtang")

    def test_rejects_empty_or_unsafe_brand_input(self):
        for value in ("", "kangmin;echo bad", "kangmin,,"):
            with self.subTest(value=value):
                with self.assertRaises(IosDeviceError):
                    resolve_device_brands(self.config, value)


if __name__ == "__main__":
    unittest.main()
