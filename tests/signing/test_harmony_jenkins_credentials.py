import base64
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from signing import harmony_jenkins_credentials as credentials
from signing import harmony_provisioning as signing
from signing import ios_jenkins_credentials as backend
from tests.signing.test_harmony_provisioning import CONFIG, FakeHuawei, UDID, SECOND
from tests.signing.test_ios_jenkins_credentials import FakeController


class HarmonyCredentialsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api_key = signing.openssl("genrsa", "2048")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "brands.json"
        signing.save(self.config, json.dumps({"_harmonyTeams": {"cihang": {"credentialNamespace": "harmony-team-cihang"}},
            "cihang": {"harmony": {"teamKey": "cihang", "appId": CONFIG["appId"], "bundleName": CONFIG["bundleName"],
                                  "signing": {"certName": CONFIG["certName"], "profileName": CONFIG["profileName"]}}}}).encode())
        self.account = self.root / "account.json"
        signing.save(self.account, json.dumps({"private_key": self.api_key.decode(), "key_id": "fixture-key-id",
                                               "sub_account": "fixture-sub-account", "type": "service_account"}).encode())
        self.password = self.root / "password"
        signing.save(self.password, b"fixture-password\n")
        self.state = signing.private_dir(self.root / "state")
        self.materials = signing.private_dir(self.root / "materials")
        self.api = FakeHuawei(self.root)
        with patch.dict(credentials.os.environ, {"HARMONY_P12_PASSWORD": "fixture-password"}), \
                patch.object(signing, "download", side_effect=lambda url: self.api.contents[url]):
            signing.setup(CONFIG, self.api, self.state, self.materials,
                          [{"deviceName": "fixture", "udid": UDID, "deviceType": 4}], True)
        self.controller = FakeController()
        self.payloads = []
        self.refresh_updates = []
        self.race_sha = False
        self.fail_refresh = False
        self.unknown_refresh = False
        self.client = MagicMock(spec=backend.JenkinsClient)
        self.client.assert_idle = MagicMock()
        def request(path, source):
            self.assertEqual("/scriptText", path)
            match = re.search(r"parseText\(new String\('([A-Za-z0-9+/=]+)'", source)
            payload = json.loads(base64.b64decode(match.group(1)))
            self.payloads.append(payload)
            if payload.get('refreshProfile'):
                return credentials.REFRESH_MARKER + json.dumps(self.refresh(payload)) + "\nResult: null\n"
            return backend.MARKER + json.dumps(self.controller.script(payload)) + "\nResult: null\n"
        self.client.request.side_effect = request

    def items(self, materials=False):
        return credentials.credential_inputs(self.config, "cihang", self.account,
                                            self.materials if materials else None, self.password)

    def refresh(self, payload):
        rows = []
        items = payload['items']
        if payload['apply'] and self.race_sha:
            self.controller.values[items[0]['id']] = ('Secret file', b'external race value')
        for item in items:
            current = self.controller.values.get(item['id'])
            digest = signing.hashlib.sha256(current[1]).hexdigest() if current and current[0] == 'Secret file' else None
            status = 'missing' if current is None else 'wrong-type' if current[0] != 'Secret file' else \
                     'matching' if digest == item['sha256'] else 'update-needed'
            rows.append({'id': item['id'], 'type': item['type'], 'status': status, 'currentSha256': digest})
        if payload['apply']:
            if any(row['status'] == 'wrong-type' or row['currentSha256'] != item['expectedSha256'] for row, item in zip(rows, items)):
                return {'error': 'cas-conflict'}
            old = dict(self.controller.values)
            for index, (row, item) in enumerate(zip(rows, items)):
                if row['status'] != 'matching':
                    self.controller.values[item['id']] = (item['type'], base64.b64decode(item['value']))
                    self.refresh_updates.append(item['id'])
                    row.update(status='updated', currentSha256=item['sha256'])
                if self.fail_refresh and index == 0:
                    self.controller.values = old
                    return {'error': 'refresh-failed'}
            if self.unknown_refresh:
                raise backend.CredentialError('fixture lost response; query recovery required')
        return {'credentials': rows}

    def grow_materials(self):
        with patch.dict(credentials.os.environ, {'HARMONY_P12_PASSWORD': 'fixture-password'}), \
                patch.object(signing, 'download', side_effect=lambda url: self.api.contents[url]):
            signing.setup(CONFIG, self.api, self.state, self.materials,
                          [{'deviceName': 'second', 'udid': SECOND, 'deviceType': 4}], True, True)

    def cli(self, apply=False, materials=False, local=True, refresh=False):
        args = ["--config", str(self.config), "--brand", "cihang", "--jenkins-url", "http://fixture.test",
                "--headers-command", "trusted-helper"]
        if local:
            args += ["--service-account-json", str(self.account), "--password-file", str(self.password)]
        if materials:
            args += ["--materials-dir", str(self.materials)]
        if apply:
            args += ["--apply"]
        if refresh:
            args += ["--refresh-profile"]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(credentials, "JenkinsClient", return_value=self.client), \
                patch.object(credentials, "auth_headers", return_value={"Authorization": "Basic fixture"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = credentials.main(args)
        for value in (UDID, "fixture-password", "fixture-key-id", "fixture-sub-account", "BEGIN RSA PRIVATE KEY"):
            self.assertNotIn(value, out.getvalue() + err.getvalue())
        return result, out.getvalue(), err.getvalue()

    def test_api_only_and_full_ids_use_registered_namespace(self):
        api = self.items()
        self.assertEqual(["harmony-team-cihang-" + suffix for suffix in
                          ("sa-key", "sa-key-id", "sa-sub-account", "release-p12-password")], [item["id"] for item in api])
        all_items = self.items(materials=True)
        self.assertEqual(7, len(all_items))
        self.assertEqual(["harmony-team-cihang-" + suffix for suffix in
                          ("release-p12", "cihang-designated-release-profile", "cihang-devices-json")],
                         [item["id"] for item in all_items[4:]])
        self.assertFalse(any(item["id"].endswith("release-cer") for item in all_items))

    def test_descriptions_keep_team_materials_shared_and_label_application_roster(self):
        items = credentials.credential_inputs(self.config, "cihang")
        self.assertTrue(all(item["description"].startswith("HarmonyOS | Team cihang | ") for item in items))
        self.assertTrue(all("品牌 cihang" not in item["description"] for item in items[:5]))
        self.assertIn("品牌 cihang | 指定设备 Profile", items[5]["description"])
        self.assertIn("品牌 cihang | Profile 设备清单", items[6]["description"])
        self.assertTrue(all("value" not in item for item in items))
        self.assertIn("item.description, item.filename", credentials.REFRESH_GROOVY)

    def test_plan_uses_backend_without_sending_any_local_secret_value(self):
        code, out, _ = self.cli(materials=True)
        self.assertEqual(0, code)
        self.assertEqual(["missing"] * 7, [row["status"] for row in json.loads(out)])
        self.assertEqual({}, self.controller.values)
        self.assertEqual(1, len(self.payloads))
        self.assertFalse(self.payloads[0]["apply"])
        self.assertTrue(all("value" not in item for item in self.payloads[0]["items"]))
        self.client.assert_idle.assert_not_called()

    def test_apply_api_then_materials_creates_missing_only_and_repeat_has_no_write(self):
        self.assertEqual(0, self.cli(apply=True)[0])
        self.assertEqual(4, len(self.controller.creates))
        self.assertEqual(0, self.cli(apply=True, materials=True)[0])
        self.assertEqual(7, len(self.controller.creates))
        calls = len(self.payloads)
        self.assertEqual(0, self.cli(apply=True, materials=True)[0])
        self.assertEqual(7, len(self.controller.creates))
        self.assertEqual(1, len(self.payloads) - calls)
        self.assertEqual(b"fixture-password", self.controller.values["harmony-team-cihang-release-p12-password"][1])
        self.assertEqual(self.api_key, self.controller.values["harmony-team-cihang-sa-key"][1])

    def test_conflict_blocks_all_other_creates_and_never_overwrites(self):
        self.controller.values["harmony-team-cihang-sa-key"] = ("Secret file", b"other")
        code, out, _ = self.cli(apply=True, materials=True)
        self.assertEqual(1, code)
        self.assertEqual("conflict", json.loads(out)[0]["status"])
        self.assertEqual([], self.controller.creates)
        self.assertEqual(1, len(self.payloads))
        self.assertEqual(b"other", self.controller.values["harmony-team-cihang-sa-key"][1])

    def test_apply_without_local_materials_is_blocked(self):
        self.assertEqual(1, self.cli(apply=True, local=False)[0])
        self.assertEqual([], self.payloads)
        self.assertEqual(7, len(credentials.credential_inputs(self.config, "cihang")))

    def test_non_rsa_or_missing_official_service_account_fields_are_rejected(self):
        value = json.loads(self.account.read_bytes())
        value["private_key"] = signing.openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256").decode()
        signing.save(self.account, json.dumps(value).encode())
        self.assertEqual(1, self.cli(apply=True)[0])
        self.assertEqual([], self.payloads)
        value.pop("sub_account")
        signing.save(self.account, json.dumps(value).encode())
        with self.assertRaises(backend.CredentialError):
            self.items()

    def test_material_permissions_symlink_and_password_mismatch_fail_before_jenkins(self):
        self.materials.chmod(0o755)
        self.assertEqual(1, self.cli(apply=True, materials=True)[0])
        self.materials.chmod(0o700)
        self.account.chmod(0o644)
        self.assertEqual(1, self.cli(apply=True)[0])
        self.account.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(self.account)
        with self.assertRaises(backend.CredentialError):
            credentials.credential_inputs(self.config, "cihang", link)
        signing.save(self.password, b"incorrect")
        self.assertEqual(1, self.cli(apply=True, materials=True)[0])
        self.assertEqual([], self.payloads)

    def test_wrong_cer_roster_and_profile_brand_are_rejected(self):
        original = (self.materials / "release.cer").read_bytes()
        signing.save(self.materials / "release.cer", signing.openssl("x509", "-outform", "DER", data=(self.root / "ca.pem").read_bytes()))
        self.assertEqual(1, self.cli(apply=True, materials=True)[0])
        signing.save(self.materials / "release.cer", original)
        roster = signing.read_json((self.materials / "devices.json").read_bytes())
        roster[0]["udid"] = "wrong-device-identifier"
        signing.save(self.materials / "devices.json", json.dumps(roster).encode())
        self.assertEqual(1, self.cli(apply=True, materials=True)[0])
        signing.save(self.materials / "devices.json", json.dumps(self.api.devices).encode())
        signing.save(self.materials / "designated-release.p7b", self.api.signed_profile(self.api.devices, {"bundle-info": {"bundle-name": "wrong.brand"}}))
        self.assertEqual(1, self.cli(apply=True, materials=True)[0])
        self.assertEqual([], self.payloads)

    def test_duplicate_namespace_is_rejected(self):
        value = json.loads(self.config.read_bytes())
        value["_harmonyTeams"]["other"] = {"credentialNamespace": "harmony-team-cihang"}
        signing.save(self.config, json.dumps(value).encode())
        self.assertEqual(1, self.cli(apply=True)[0])
        self.assertEqual([], self.payloads)

    def test_weak_rsa_and_duplicate_roster_fail_before_jenkins(self):
        value = json.loads(self.account.read_bytes())
        value["private_key"] = signing.openssl("genrsa", "1024").decode()
        signing.save(self.account, json.dumps(value).encode())
        self.assertEqual(1, self.cli(apply=True)[0])
        value["private_key"] = self.api_key.decode()
        signing.save(self.account, json.dumps(value).encode())
        roster = signing.read_json((self.materials / "devices.json").read_bytes())
        signing.save(self.materials / "devices.json", json.dumps(roster + roster).encode())
        self.assertEqual(1, self.cli(apply=True, materials=True)[0])
        self.assertEqual([], self.payloads)

    def test_refresh_plan_is_readonly_and_pair_apply_preserves_immutable_credentials(self):
        self.assertEqual(0, self.cli(apply=True, materials=True)[0])
        immutable = {key: value for key, value in self.controller.values.items()
                     if not key.endswith(('designated-release-profile', 'devices-json'))}
        self.grow_materials()
        code, out, _ = self.cli(materials=True, refresh=True)
        self.assertEqual(0, code)
        self.assertEqual(['update-needed', 'update-needed'], [row['status'] for row in json.loads(out)[-2:]])
        self.assertEqual([], self.refresh_updates)
        self.assertTrue(all('value' not in item for item in self.payloads[-1]['items']))
        self.assertEqual(0, self.cli(apply=True, materials=True, refresh=True)[0])
        self.assertEqual(2, len(self.refresh_updates))
        for identity, value in immutable.items():
            self.assertEqual(value, self.controller.values[identity])
        self.assertEqual(0, self.cli(apply=True, materials=True, refresh=True)[0])
        self.assertEqual(2, len(self.refresh_updates))

    def test_cas_race_blocks_both_updates_and_immutable_conflict_blocks_refresh(self):
        self.cli(apply=True, materials=True)
        self.grow_materials()
        self.race_sha = True
        code, _, error = self.cli(apply=True, materials=True, refresh=True)
        self.assertEqual(1, code)
        self.assertIn('重新只读核对', error)
        self.assertEqual([], self.refresh_updates)
        self.race_sha = False
        self.controller.values['harmony-team-cihang-release-p12'] = ('Secret file', b'conflict')
        self.assertEqual(1, self.cli(apply=True, materials=True, refresh=True)[0])
        self.assertEqual([], self.refresh_updates)

    def test_partial_refresh_failure_rolls_pair_back_without_retry(self):
        self.cli(apply=True, materials=True)
        previous = dict(self.controller.values)
        self.grow_materials()
        self.fail_refresh = True
        self.assertEqual(1, self.cli(apply=True, materials=True, refresh=True)[0])
        self.assertEqual(previous, self.controller.values)
        self.assertEqual(1, sum(payload.get('refreshProfile', False) and payload['apply'] for payload in self.payloads))
        self.fail_refresh = False
        code, out, _ = self.cli(materials=True, refresh=True)
        self.assertEqual(0, code)
        self.assertEqual(['update-needed', 'update-needed'], [row['status'] for row in json.loads(out)[-2:]])

    def test_unknown_refresh_response_requeries_next_run_without_rewriting(self):
        self.cli(apply=True, materials=True)
        self.grow_materials()
        self.unknown_refresh = True
        self.assertEqual(1, self.cli(apply=True, materials=True, refresh=True)[0])
        self.assertEqual(2, len(self.refresh_updates))
        self.unknown_refresh = False
        self.assertEqual(0, self.cli(apply=True, materials=True, refresh=True)[0])
        self.assertEqual(2, len(self.refresh_updates))

    def test_refresh_client_enforces_whitelist_and_busy_check_before_write(self):
        all_items = self.items(materials=True)
        with self.assertRaises(backend.CredentialError):
            credentials.refresh_profiles(self.client, all_items[:2], 'harmony-team-cihang', 'cihang')
        selected = credentials.profile_items(all_items, 'harmony-team-cihang', 'cihang')
        self.client.assert_idle.side_effect = backend.CredentialError('busy')
        with self.assertRaises(backend.CredentialError):
            credentials.refresh_profiles(self.client, selected, 'harmony-team-cihang', 'cihang',
                                         {item['id']: None for item in selected})
        self.assertEqual([], self.payloads)

    def test_maintenance_export_contains_only_three_files_expected_sha_and_no_network(self):
        items = self.items(materials=True)
        identities = ['harmony-team-cihang-release-p12', 'harmony-team-cihang-cihang-designated-release-profile',
                      'harmony-team-cihang-cihang-devices-json']
        snapshot = self.root / 'expected.json'
        signing.save(snapshot, json.dumps({'namespace': 'harmony-team-cihang', 'brand': 'cihang',
                     'items': [{'id': identity, 'status': 'missing', 'sha256': None} for identity in identities]}).encode())
        output = signing.private_dir(self.root / 'payloads') / 'payload.json'
        args = ['--config', str(self.config), '--brand', 'cihang', '--service-account-json', str(self.account),
                '--password-file', str(self.password), '--materials-dir', str(self.materials),
                '--export-maintenance-payload', str(output), '--expected-old-json', str(snapshot)]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(credentials, 'JenkinsClient') as client, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(0, credentials.main(args))
            client.assert_not_called()
        payload = signing.read_json(backend.private_bytes(output, 'payload'))
        self.assertEqual({'namespace', 'brand', 'items', 'expected'}, set(payload))
        self.assertEqual(identities, [item['id'] for item in payload['items']])
        self.assertEqual({identity: None for identity in identities}, payload['expected'])
        self.assertEqual(['Secret file'] * 3, [item['type'] for item in payload['items']])
        self.assertEqual([item['description'] for item in items[-3:]],
                         [item['description'] for item in payload['items']])
        self.assertNotIn('fixture-password', json.dumps(payload))
        self.assertNotIn('sa-key', json.dumps(payload))
        self.assertNotIn(UDID, out.getvalue() + err.getvalue())
        self.assertTrue(all(item['id'] in identities for item in payload['items']))

    def test_maintenance_snapshot_rejects_wrong_brand_namespace_ids_or_states(self):
        config = signing.load_brand(self.config, 'cihang')
        items = self.items(materials=True)
        identities = ['harmony-team-cihang-release-p12', 'harmony-team-cihang-cihang-designated-release-profile',
                      'harmony-team-cihang-cihang-devices-json']
        snapshot = self.root / 'expected.json'
        value = {'namespace': 'harmony-team-cihang', 'brand': 'cihang',
                 'items': [{'id': identity, 'status': 'present', 'sha256': 'a' * 64} for identity in identities]}
        for invalid in ({**value, 'brand': 'other'}, {**value, 'namespace': 'other'},
                        {**value, 'items': [{**value['items'][0], 'sha256': None}] + value['items'][1:]},
                        {**value, 'items': [value['items'][0]] * 3}):
            signing.save(snapshot, json.dumps(invalid).encode())
            with self.subTest(invalid=invalid), self.assertRaises(backend.CredentialError):
                credentials.expected_snapshot(snapshot, config)
        signing.save(snapshot, json.dumps(value).encode())
        self.assertEqual({identity: 'a' * 64 for identity in identities}, credentials.expected_snapshot(snapshot, config))
        with self.assertRaises(backend.CredentialError):
            credentials.export_maintenance_payload(items, config, self.materials / 'payload.json', {'wrong': None})

    def test_materials_require_password_file(self):
        with self.assertRaises(backend.CredentialError):
            credentials.credential_inputs(self.config, "cihang", materials_dir=self.materials)


if __name__ == "__main__":
    unittest.main()
