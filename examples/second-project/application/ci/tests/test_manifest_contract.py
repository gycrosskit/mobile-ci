import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]

class ManifestContractTest(unittest.TestCase):
    def test_native_application_identity_and_exported_launcher(self):
        brands = json.loads((ROOT / "ci/brands.json").read_text())
        self.assertEqual(["example"], list(brands))
        build = (ROOT / "mobile/build.gradle").read_text()
        self.assertIn("applicationId 'org.example.mobile'", build)
        activity = ET.parse(ROOT / "mobile/src/main/AndroidManifest.xml").find("application/activity")
        android = "{http://schemas.android.com/apk/res/android}"
        self.assertEqual(".MainActivity", activity.get(android + "name"))
        self.assertEqual("true", activity.get(android + "exported"))
