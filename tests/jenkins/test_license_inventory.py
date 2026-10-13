import copy
import json
from pathlib import Path
import tempfile
import unittest
from jenkins.check_license_inventory import check, ROOT


class LicenseInventoryTest(unittest.TestCase):
    def test_actual_pins_have_evidence_and_images_remain_blocked(self):
        self.assertEqual(27, check()['directPlugins'])
        with self.assertRaisesRegex(ValueError, '镜像'):
            check(scope='image')

    def test_changed_pin_unknown_license_or_duplicate_cannot_pass(self):
        original = json.loads((ROOT / 'docs/audits/third-party-inventory.json').read_text())
        mutations = ('pin', 'missing-plugin-license', 'missing-gem-license', 'duplicate', 'bad-tool-license', 'approved-image')
        for kind in mutations:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name in ('docs/audits', 'jenkins/controller', 'delivery'):
                    (root / name).mkdir(parents=True)
                pins = (ROOT / 'jenkins/controller/plugins.txt').read_text()
                value = copy.deepcopy(original)
                if kind == 'pin': pins += '\nunreviewed-plugin:1.0\n'
                elif kind == 'missing-plugin-license': value['plugins'][0]['licenses'] = []
                elif kind == 'missing-gem-license': value['fastlaneRuntime'][0]['licenses'] = []
                elif kind == 'duplicate': value['plugins'].append(value['plugins'][0])
                elif kind == 'bad-tool-license': value['externalTools'][0]['license'] = 'UNKNOWN'
                else: value['binaryReleaseApproved'] = True
                (root / 'docs/audits/third-party-inventory.json').write_text(json.dumps(value))
                (root / 'jenkins/controller/plugins.txt').write_text(pins)
                (root / 'delivery/.fastlane-version').write_text('2.238.0\n')
                with self.assertRaises(ValueError): check(root)
