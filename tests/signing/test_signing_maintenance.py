import argparse
import base64
import json
import os
import stat
import subprocess
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from signing import signing_maintenance as module


class FakeIosDevices:
    def __init__(self):
        self.info = {"teamId": "ABCDEFGHIJ", "der": b"certificate", "expires": datetime.now(timezone.utc) + timedelta(days=90)}
        self.devices = [{"id": "device-one", "attributes": {"udid": "1" * 40, "platform": "IOS", "status": "ENABLED"}}]
        self.profiles = [self.profile("old", {"device-one"})]
        self.bundles = [{"id": "bundle", "attributes": {"identifier": "com.example.one", "platform": "IOS"}}]
        self.writes, self.fail = [], None
        self.pending = None

    def profile(self, identity, devices):
        return {"id": identity, "attributes": {"name": "adhoc", "profileState": "ACTIVE", "profileType": "IOS_APP_ADHOC",
                "expirationDate": (datetime.now(timezone.utc) + timedelta(days=60)).isoformat(),
                "profileContent": base64.b64encode(b"profile-content").decode()},
                "relations": {"bundleId": {"bundle"}, "certificates": {"cert"}, "devices": devices}}

    def all(self, path):
        if path.startswith("certificates?"):
            return [{"id": "cert", "attributes": {"certificateType": "DISTRIBUTION"}}]
        if path.startswith("bundleIds?"):
            return self.bundles
        if path.startswith("devices?"):
            return self.devices
        if path.startswith("profiles?"):
            return self.profiles
        prefix, identity, _, relation = path.split("/")
        assert prefix == "profiles"
        profile = next(row for row in self.profiles if row["id"] == identity)
        return [{"id": value} for value in profile["relations"][relation]]

    def request(self, path, body=None, method=None):
        assert self.pending.is_file()
        assert stat.S_IMODE(self.pending.stat().st_mode) == 0o600
        phase = json.loads(self.pending.read_text())["phase"]
        assert phase == ("delete" if method == "DELETE" else "register" if path == "devices" else "create")
        self.writes.append((method or "POST", path))
        if method == "DELETE":
            if self.fail == "delete-before":
                raise module.ios.UnknownWrite("connection lost")
            self.profiles = [row for row in self.profiles if row["id"] != path.split("/")[1]]
            if self.fail == "delete-after":
                raise module.ios.UnknownWrite("response lost")
            return {}
        if path == "devices":
            number = sum(1 for _, target in self.writes if target == "devices")
            row = {"id": "device-new" if number == 1 else "device-new-" + str(number),
                   "attributes": dict(body["data"]["attributes"], status="ENABLED")}
            self.devices.append(row)
            if self.fail == "device-interrupt-after":
                raise KeyboardInterrupt()
        else:
            if self.fail == "create-before":
                raise module.ios.UnknownWrite("connection lost")
            row = self.profile("new" if not self.profiles else "new-" + str(len(self.profiles) + 1),
                               {item["id"] for item in body["data"]["relationships"]["devices"]["data"]})
            row["attributes"]["name"] = body["data"]["attributes"]["name"]
            row["relations"]["bundleId"] = {body["data"]["relationships"]["bundleId"]["data"]["id"]}
            self.profiles.append(row)
            if self.fail == "create-interrupt":
                raise KeyboardInterrupt()
        return {"data": row}


class SigningMaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.config = self.directory / "brands.json"
        self.root = {
            "_iosTeams": {"one": {"teamId": "ABCDEFGHIJ", "credentialNamespace": "ios-team-one",
                                  "repositorySigningPath": "signing/ios/one"}},
            "_harmonyTeams": {"one": {"credentialNamespace": "harmony-team-one"}},
            "alpha": {"ios": {"bundleId": "com.example.one", "signing": {"test": {
                "team": "one", "enabled": True, "profileNames": {"pgyer": "adhoc", "testflight": "store"}}}},
                "harmony": {"teamKey": "one", "appId": "123", "bundleName": "com.example.one",
                            "signing": {"certName": "release", "profileName": "test-profile"}}},
        }
        self.save()

    def save(self):
        self.config.write_text(json.dumps(self.root))

    def snapshot_file(self, p12=None):
        import hashlib
        namespace = "harmony-team-one"
        ids = [namespace + "-release-p12", namespace + "-alpha-designated-release-profile", namespace + "-alpha-devices-json"]
        value = {"namespace": namespace, "brand": "alpha", "items": [
            {"id": identity, "status": "present" if index == 0 and p12 is not None else "missing",
             "sha256": hashlib.sha256(p12).hexdigest() if index == 0 and p12 is not None else None}
            for index, identity in enumerate(ids)]}
        team = module.team_directory(self.directory, "harmony", "one")
        path = module.sync_directory(team, "alpha") / "expected.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def args(self, platform="ios", operation="onboard", mode="plan"):
        return argparse.Namespace(config=self.config, platform=platform, operation=operation,
                                  mode=mode, brand="alpha", state_root=self.directory,
                                  register_device=True, payload_file=None, expected_file=None)

    def test_duplicate_profiles_are_processed_once_and_teams_register_once(self):
        self.root["beta"] = json.loads(json.dumps(self.root["alpha"]))
        self.root["gamma"] = json.loads(json.dumps(self.root["alpha"]))
        self.root["gamma"]["ios"]["bundleId"] = "com.example.two"
        self.save()
        rows = module.resolve(self.config, "ios", "devices", "alpha,beta,gamma,alpha")
        self.assertEqual(["alpha", "gamma"], [row["brand"] for row in rows])
        self.assertEqual([True, False], [row["registerDevice"] for row in rows])

    def test_onboard_accepts_confirmed_disabled_brand_but_devices_refuses_it(self):
        self.root["alpha"]["ios"]["signing"]["test"]["enabled"] = False
        self.save()
        self.assertEqual("alpha", module.resolve(self.config, "ios", "onboard", "alpha")[0]["brand"])
        with self.assertRaisesRegex(ValueError, "已启用"):
            module.resolve(self.config, "ios", "devices", "alpha")

    def test_invalid_namespace_and_unknown_brand_are_rejected_before_credentials(self):
        self.root["_iosTeams"]["one"]["credentialNamespace"] = "../../secret"
        self.save()
        with self.assertRaises(ValueError):
            module.resolve(self.config, "ios", "onboard", "alpha")
        with self.assertRaises(ValueError):
            module.resolve(self.config, "harmony", "onboard", "alpha;whoami")

    def test_persistent_state_requires_external_0700_owned_directory(self):
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "0700"):
            module.team_directory(self.directory, "ios", "one")
        self.directory.chmod(0o700)
        with patch.dict(os.environ, {"WORKSPACE": str(self.directory)}):
            with self.assertRaisesRegex(ValueError, "workspace"):
                module.team_directory(self.directory, "ios", "one")
        link = self.directory / "linked"
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            module.team_directory(link, "ios", "one")

    def test_onboard_calls_existing_ios_cli_with_persistent_state_and_materials(self):
        key = self.directory / "api.p8"
        key.write_text("test-api")
        commands = []
        with patch.dict(os.environ, {"ASC_API_KEY_PATH": str(key), "WORKSPACE": str(module.ROOT)}), \
                patch.object(module, "invoke", side_effect=lambda command, env=None: commands.append(command) or b'{}'):
            module.run(self.args(mode="apply"))
        command = commands[0]
        self.assertTrue(command[1].endswith("ios_signing_setup.py"))
        self.assertEqual("apply", command[2])
        self.assertEqual(str(self.directory / "ios/one/state"), command[command.index("--state-dir") + 1])
        self.assertEqual(str(self.directory / "ios/one/materials"), command[command.index("--output-dir") + 1])
        self.assertFalse(any((self.directory / "ios/one").glob(".maintenance-*")))

    def test_ios_first_pair_exports_validated_payload_without_binding_missing_p12(self):
        import base64
        import hashlib
        self.root["_iosTeams"]["one"]["testSigningSource"] = "jenkins"
        self.save()
        args = self.args(mode="apply")
        team = module.team_directory(self.directory, "ios", "one")
        directory = module.sync_directory(team, "alpha")
        args.expected_file, args.payload_file = directory / "expected.json", directory / "payload.json"
        ids = ["ios-team-one-distribution-p12", "ios-team-one-distribution-p12-password"]
        args.expected_file.write_text(json.dumps({"namespace": "ios-team-one", "brand": "alpha", "items": [
            {"id": identity, "status": "missing", "sha256": None} for identity in ids]}))
        args.expected_file.chmod(0o600)
        key = self.directory / "api.p8"
        key.write_text("test-api")
        calls = []
        def invoke(command, env=None):
            calls.append(command)
            self.assertNotIn("--p12-path", command)
            output = Path(command[command.index("--output-dir") + 1])
            (output / "distribution.p12").write_bytes(b"verified-p12")
            (output / "distribution-p12.password").write_bytes(b"verified-password\n")
            for file in output.glob("distribution*"):
                file.chmod(0o600)
            return b'{}'
        items = [{"id": identity, "type": "Secret file" if index == 0 else "Secret text",
                  "filename": "distribution.p12" if index == 0 else None,
                  "value": base64.b64encode(value).decode(), "sha256": hashlib.sha256(value).hexdigest()}
                 for index, (identity, value) in enumerate(zip(ids, [b"verified-p12", b"verified-password"]))]
        with patch.dict(os.environ, {"ASC_API_KEY_PATH": str(key), "WORKSPACE": str(module.ROOT)}), \
                patch.object(module, "invoke", side_effect=invoke), \
                patch.object(module.ios_credentials, "credential_inputs", return_value=items) as validate:
            module.run(args)
        validate.assert_called_once_with(self.config, "alpha", p12=team / "materials/distribution.p12",
                                          password_file=team / "materials/distribution-p12.password")
        payload = json.loads(args.payload_file.read_text())
        self.assertEqual(items, payload["items"])
        self.assertEqual(dict.fromkeys(ids), payload["expected"])
        self.assertEqual(0o600, stat.S_IMODE(args.payload_file.stat().st_mode))
        snapshot = json.loads(args.expected_file.read_text())
        snapshot["items"][0].update(status="present", sha256="1" * 64)
        args.expected_file.write_text(json.dumps(snapshot))
        with patch.object(module, "invoke") as supplier:
            with self.assertRaisesRegex(ValueError, "同为缺失或完整"):
                module.run(args)
            supplier.assert_not_called()

    def ios_device_case(self):
        args = self.args(operation="devices", mode="apply")
        item = module.ios.load_brand(self.config, "alpha", ["pgyer"])
        state = module.team_directory(self.directory, "ios", "one") / "state"
        state.mkdir(mode=0o700)
        password = self.directory / "password"
        password.write_bytes(b"password")
        password.chmod(0o600)
        api = FakeIosDevices()
        import hashlib
        api.pending = state / ("devices-" + hashlib.sha256(b"com.example.one:adhoc").hexdigest() + "-pending.json")
        return api, args, item, state, password

    def device_sync(self, api, args, item, state, password):
        with patch.object(module.ios, "p12_identity", return_value=(b"key", dict(api.info, teamId="ABCDEFGHIJ"))), \
                patch.object(module.ios, "remote_certificate", return_value=api.info):
            module.ios_device_sync(api, args, item, state, self.directory / "p12", password, "phone", "2" * 40)

    def test_ios_device_modes_validate_real_team_bundle_and_disabled_device_before_writes(self):
        api, args, item, state, password = self.ios_device_case()
        for mode in ("plan", "apply"):
            args.mode = mode
            for issue in ("team", "bundle", "disabled"):
                with self.subTest(mode=mode, issue=issue):
                    api.info["teamId"] = "ZZZZZZZZZZ" if issue == "team" else "ABCDEFGHIJ"
                    api.bundles = [] if issue == "bundle" else [{"id": "bundle", "attributes": {"identifier": "com.example.one", "platform": "IOS"}}]
                    api.devices = [{"id": "device", "attributes": {"udid": "2" * 40, "platform": "IOS", "status": "DISABLED"}}] if issue == "disabled" else []
                    with self.assertRaises(ValueError):
                        self.device_sync(api, args, item, state, password)
                    self.assertEqual([], api.writes)
                    self.assertFalse(api.pending.exists())

    def test_ios_device_refresh_is_phased_and_only_deletes_immutable_old_id(self):
        api, args, item, state, password = self.ios_device_case()
        self.device_sync(api, args, item, state, password)
        self.assertEqual([("POST", "devices"), ("DELETE", "profiles/old"), ("POST", "profiles")], api.writes)
        self.assertFalse(api.pending.exists())
        self.assertEqual({"device-one", "device-new"}, api.profiles[0]["relations"]["devices"])
        self.assertEqual(b"profile-content", (state / "alpha-pgyer.mobileprovision").read_bytes())

    def test_ios_illegal_old_profile_or_device_union_is_rejected_before_register(self):
        api, args, item, state, password = self.ios_device_case()
        for relation, unexpected in (("bundleId", {"other-bundle"}), ("certificates", {"other-cert"}), ("devices", {"unknown-device"})):
            with self.subTest(relation=relation):
                api.profiles = [api.profile("old", {"device-one"})]
                api.profiles[0]["relations"][relation] = unexpected
                with self.assertRaises((module.ios.SigningError, ValueError)):
                    self.device_sync(api, args, item, state, password)
                self.assertEqual([], api.writes)
                self.assertFalse(api.pending.exists())

    def test_ios_unknown_delete_or_create_never_repeats_writes(self):
        for failure in ("delete-before", "delete-after", "create-before"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                original = self.directory
                self.directory = Path(directory).resolve()
                try:
                    api, args, item, state, password = self.ios_device_case()
                    api.fail = failure
                    with self.assertRaises(module.ios.SigningError):
                        self.device_sync(api, args, item, state, password)
                    self.assertTrue(api.pending.exists())
                    writes = list(api.writes)
                    api.fail = None
                    with self.assertRaisesRegex(ValueError, "未知写入未查回"):
                        self.device_sync(api, args, item, state, password)
                    self.assertEqual(writes, api.writes)
                finally:
                    self.directory = original

    def test_ios_interrupted_create_recovers_readonly_after_exact_profile_check(self):
        api, args, item, state, password = self.ios_device_case()
        api.fail = "create-interrupt"
        with self.assertRaises(KeyboardInterrupt):
            self.device_sync(api, args, item, state, password)
        writes = list(api.writes)
        self.assertTrue(api.pending.exists())
        # 完整关系不一致时不得清 pending 或再写；恢复匹配后只读完成。
        api.profiles[0]["relations"]["certificates"] = {"foreign-cert"}
        with self.assertRaises(module.ios.SigningError):
            self.device_sync(api, args, item, state, password)
        self.assertTrue(api.pending.exists())
        self.assertEqual(writes, api.writes)
        api.profiles[0]["relations"]["certificates"] = {"cert"}
        self.device_sync(api, args, item, state, password)
        self.assertFalse(api.pending.exists())
        self.assertEqual(writes, api.writes)

    def test_ios_password_only_persistent_output_blocks_both_modes_before_setup(self):
        team = module.team_directory(self.directory, "ios", "one")
        (team / "materials").mkdir(mode=0o700)
        (team / "materials/distribution-p12.password").write_text("existing-password")
        key = self.directory / "api.p8"
        key.write_text("key")
        for mode in ("plan", "apply"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"ASC_API_KEY_PATH": str(key)}), \
                    patch.object(module, "invoke") as supplier:
                with self.assertRaisesRegex(ValueError, "缺一项"):
                    module.run(self.args(mode=mode))
                supplier.assert_not_called()

    def test_ios_binding_partial_pair_stops_without_platform_write(self):
        self.root["_iosTeams"]["one"]["testSigningSource"] = "jenkins"
        self.save()
        key = self.directory / "api.p8"
        key.write_text("key")
        for p12, password in (("", "password-only"), ("missing-p12", "")):
            with self.subTest(p12=bool(p12)), patch.dict(os.environ, {"ASC_API_KEY_PATH": str(key),
                    "IOS_DISTRIBUTION_P12": p12, "IOS_DISTRIBUTION_P12_PASSWORD": password}), \
                    patch.object(module, "invoke") as supplier:
                with self.assertRaisesRegex(ValueError, "绑定 iOS P12/password 缺一项"):
                    module.run(self.args(mode="apply"))
                supplier.assert_not_called()

    def test_harmony_device_plan_uses_private_json_without_apply(self):
        captured = []
        def invoke(command, env=None):
            devices = Path(command[command.index("--devices-file") + 1])
            captured.append((command, json.loads(devices.read_text()), stat.S_IMODE(devices.stat().st_mode)))
            return b'{}'
        with patch.dict(os.environ, {"DEVICE_NAME": "test-phone", "DEVICE_UDID": "private-device-udid",
                                     "WORKSPACE": str(module.ROOT)}), patch.object(module, "invoke", side_effect=invoke):
            module.run(self.args(platform="harmony", operation="devices"))
        command, devices, permissions = captured[0]
        self.assertEqual("devices", command[2])
        self.assertNotIn("--apply", command)
        self.assertNotIn("private-device-udid", command)
        self.assertEqual([{"deviceName": "test-phone", "udid": "private-device-udid", "deviceType": 4}], devices)
        self.assertEqual(0o600, permissions)
        self.assertFalse(Path(command[command.index("--devices-file") + 1]).exists())

    def test_harmony_apply_is_explicit_and_team_state_is_shared(self):
        commands = []
        with patch.dict(os.environ, {"DEVICE_NAME": "phone", "DEVICE_UDID": "private-device-udid",
                                     "WORKSPACE": str(module.ROOT)}), \
                patch.object(module, "invoke", side_effect=lambda command, env=None: commands.append(command) or b'{}'):
            module.run(self.args(platform="harmony", operation="devices", mode="apply"))
        self.assertIn("--apply", commands[0])
        self.assertEqual(str(self.directory / "harmony/one/state"), commands[0][commands[0].index("--state-dir") + 1])

    def test_harmony_onboard_passes_private_device_input_and_preserves_materials(self):
        captured = []
        def invoke(command, env=None):
            devices = Path(command[command.index("--devices-file") + 1])
            output = Path(command[command.index("--output-dir") + 1])
            captured.append((command, json.loads(devices.read_text()), stat.S_IMODE(devices.stat().st_mode)))
            self.assertEqual(0o700, stat.S_IMODE(output.stat().st_mode))
            (output / "release.p12").write_bytes(b"test-material")
            (output / "release.p12").chmod(0o600)
            return b'{}'
        with patch.dict(os.environ, {"DEVICE_NAME": "onboard-phone", "DEVICE_UDID": "private-onboard-udid",
                                     "WORKSPACE": str(module.ROOT)}), patch.object(module, "invoke", side_effect=invoke):
            module.run(self.args(platform="harmony", operation="onboard", mode="apply"))
        command, devices, permissions = captured[0]
        self.assertEqual("apply", command[2])
        self.assertNotIn("--apply", command)
        self.assertNotIn("private-onboard-udid", command)
        self.assertEqual([{"deviceName": "onboard-phone", "udid": "private-onboard-udid", "deviceType": 4}], devices)
        self.assertEqual(0o600, permissions)
        self.assertFalse(Path(command[command.index("--devices-file") + 1]).exists())
        material = self.directory / "harmony/one/materials/alpha/release.p12"
        self.assertEqual(b"test-material", material.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(material.stat().st_mode))

    def test_harmony_onboard_rejects_partial_device_input_before_platform_call(self):
        for name, udid in (("phone", ""), ("", "private-device-udid")):
            with self.subTest(name=bool(name), udid=bool(udid)), \
                    patch.dict(os.environ, {"DEVICE_NAME": name, "DEVICE_UDID": udid,
                                             "WORKSPACE": str(module.ROOT)}), patch.object(module, "invoke") as invoke:
                with self.assertRaisesRegex(ValueError, "同时填写"):
                    module.run(self.args(platform="harmony", operation="onboard", mode="apply"))
                invoke.assert_not_called()

    def test_harmony_onboard_plan_allows_no_device_input(self):
        commands = []
        with patch.dict(os.environ, {"DEVICE_NAME": "", "DEVICE_UDID": "", "WORKSPACE": str(module.ROOT)}), \
                patch.object(module, "invoke", side_effect=lambda command, env=None: commands.append(command) or b'{}'):
            module.run(self.args(platform="harmony", operation="onboard"))
        self.assertEqual("plan", commands[0][2])
        self.assertNotIn("--devices-file", commands[0])
        self.assertEqual(str(self.directory / "harmony/one/materials/alpha"),
                         commands[0][commands[0].index("--output-dir") + 1])

    def test_harmony_materials_directory_rejects_unsafe_existing_permissions(self):
        with patch.dict(os.environ, {"WORKSPACE": str(module.ROOT)}):
            team = module.team_directory(self.directory, "harmony", "one")
        (team / "materials").mkdir(mode=0o755)
        with patch.dict(os.environ, {"WORKSPACE": str(module.ROOT)}), patch.object(module, "invoke") as invoke:
            with self.assertRaisesRegex(ValueError, "0700"):
                module.run(self.args(platform="harmony"))
            invoke.assert_not_called()

    def test_harmony_apply_exports_validated_payload_with_private_password_and_snapshot(self):
        args = self.args(platform="harmony", mode="apply")
        args.expected_file = self.snapshot_file()
        args.payload_file = args.expected_file.with_name("payload.json")
        commands, passwords = [], []
        def invoke(command, env=None):
            commands.append(command)
            if "--export-maintenance-payload" in command:
                password = Path(command[command.index("--password-file") + 1])
                self.assertEqual(0o600, stat.S_IMODE(password.stat().st_mode))
                passwords.append((password, password.read_text()))
            return b'{}'
        with patch.dict(os.environ, {"DEVICE_NAME": "", "DEVICE_UDID": "", "HARMONY_P12_PASSWORD": "private-password",
                                     "WORKSPACE": str(module.ROOT)}), patch.object(module, "invoke", side_effect=invoke):
            module.run(args)
        self.assertEqual(2, len(commands))
        self.assertIn("--export-maintenance-payload", commands[1])
        self.assertEqual(str(args.expected_file), commands[1][commands[1].index("--expected-old-json") + 1])
        self.assertNotIn("private-password", commands[1])
        self.assertEqual("private-password", passwords[0][1])
        self.assertFalse(passwords[0][0].exists())

    def test_payload_outside_selected_materials_is_rejected_before_vendor(self):
        args = self.args(platform="harmony", mode="apply")
        args.expected_file = self.snapshot_file()
        args.payload_file = self.directory / "payload.json"
        with patch.dict(os.environ, {"WORKSPACE": str(module.ROOT)}), patch.object(module, "invoke") as invoke:
            with self.assertRaisesRegex(ValueError, "所选品牌受限材料目录"):
                module.run(args)
            invoke.assert_not_called()

    def test_prepare_sync_creates_private_snapshot_and_refuses_link(self):
        command = ["prepare", "--config", str(self.config), "--platform", "harmony", "--operation", "onboard",
                   "--brand", "alpha", "--state-root", str(self.directory)]
        with patch.dict(os.environ, {"WORKSPACE": str(module.ROOT)}):
            self.assertEqual(0, module.main(command))
            expected = self.directory / "harmony/one/materials/alpha/.jenkins-maintenance/expected.json"
            self.assertEqual(0o600, stat.S_IMODE(expected.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(expected.parent.stat().st_mode))
            expected.unlink()
            expected.symlink_to(self.config)
            with patch("sys.stderr"):
                self.assertEqual(1, module.main(command))

    def test_existing_p12_state_identity_is_verified_before_supplier_apply(self):
        args = self.args(platform="harmony", mode="apply")
        packed = b"exact-cached-p12"
        args.expected_file = self.snapshot_file(packed)
        args.payload_file = args.expected_file.with_name("payload.json")
        with patch.dict(os.environ, {"WORKSPACE": str(module.ROOT)}):
            team = module.team_directory(self.directory, "harmony", "one")
        state = team / "state"
        state.mkdir(mode=0o700)
        (state / "release.key").write_bytes(b"original-key")
        (state / "release.key").chmod(0o600)
        (state / "release.p12").write_bytes(packed)
        (state / "release.p12").chmod(0o600)
        existing = self.directory / "jenkins.p12"
        existing.write_bytes(packed)
        existing.chmod(0o600)
        events = []
        with patch.dict(os.environ, {"DEVICE_NAME": "", "DEVICE_UDID": "", "HARMONY_P12_PASSWORD": "private",
                                     "HARMONY_EXISTING_P12_PATH": str(existing), "WORKSPACE": str(module.ROOT)}), \
                patch.object(module.harmony, "openssl", return_value=b"certificate-chain"), \
                patch.object(module.harmony, "stable_p12", side_effect=lambda *args: events.append("identity-verified") or packed), \
                patch.object(module, "invoke", side_effect=lambda command, env=None: events.append("vendor" if command[1].endswith("harmony_provisioning.py") else "export") or b'{}'):
            module.run(args)
        self.assertEqual(["identity-verified", "vendor", "export"], events)
        self.assertEqual(packed, (state / "release.p12").read_bytes())

    def test_existing_jenkins_p12_without_state_stops_before_supplier_apply(self):
        args = self.args(platform="harmony", mode="apply")
        args.expected_file = self.snapshot_file(b"existing-p12")
        args.payload_file = args.expected_file.with_name("payload.json")
        with patch.dict(os.environ, {"DEVICE_NAME": "", "DEVICE_UDID": "", "WORKSPACE": str(module.ROOT)}), \
                patch.object(module, "invoke") as invoke:
            with self.assertRaisesRegex(ValueError, "先恢复原私钥"):
                module.run(args)
            invoke.assert_not_called()

    def test_payload_without_snapshot_is_rejected_before_vendor_writes(self):
        args = self.args(platform="harmony", mode="apply")
        args.payload_file = self.directory / "payload.json"
        with patch.object(module, "invoke") as invoke:
            with self.assertRaisesRegex(ValueError, "同时提供"):
                module.run(args)
            invoke.assert_not_called()

    def test_vendor_failure_does_not_expose_raw_device_or_secret(self):
        with patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["vendor"], 2, stdout=b"private-device-udid", stderr=b"private-token")):
            with self.assertRaises(ValueError) as caught:
                module.invoke(["vendor"])
        self.assertNotIn("private", str(caught.exception))
        self.assertIn("退出码 2", str(caught.exception))

    def bulk_sync(self, api, args, item, state, password, devices):
        with patch.object(module.ios, "p12_identity", return_value=(b"key", dict(api.info))), \
                patch.object(module.ios, "remote_certificate", return_value=api.info):
            module.ios_device_sync(api, args, item, state, self.directory / "p12", password, requested=devices)

    def test_bulk_preserves_original_profile_without_adding_unselected_team_devices(self):
        api, args, item, state, password = self.ios_device_case()
        api.devices.append({"id": "unselected", "attributes": {"udid": "f" * 40, "platform": "IOS", "status": "ENABLED"}})
        selected = [{"name": "first", "udid": "2" * 40}, {"name": "second", "udid": "3" * 40}]
        self.bulk_sync(api, args, item, state, password, selected)
        self.assertEqual({"device-one", "device-new", "device-new-2"}, api.profiles[0]["relations"]["devices"])
        self.assertNotIn("unselected", api.profiles[0]["relations"]["devices"])
        writes = list(api.writes)
        self.bulk_sync(api, args, item, state, password, selected)
        self.assertEqual(writes, api.writes)

    def test_two_brands_same_team_register_once_and_profiles_get_target_ids(self):
        api, args, item, state, password = self.ios_device_case()
        selected = [{"name": "first", "udid": "2" * 40}, {"name": "second", "udid": "3" * 40}]
        self.bulk_sync(api, args, item, state, password, selected)
        second = dict(item, brand="beta", bundleId="com.example.two", profileNames={"pgyer": "second-adhoc"})
        api.bundles = [{"id": "bundle-two", "attributes": {"identifier": "com.example.two", "platform": "IOS"}}]
        args.brand, args.register_device = "beta", False
        import hashlib
        api.pending = state / ("devices-" + hashlib.sha256(b"com.example.two:second-adhoc").hexdigest() + "-pending.json")
        self.bulk_sync(api, args, second, state, password, selected)
        self.assertEqual(2, sum(1 for _, path in api.writes if path == "devices"))
        self.assertEqual({"device-new", "device-new-2"}, api.profiles[1]["relations"]["devices"])
        writes = list(api.writes)
        self.bulk_sync(api, args, second, state, password, selected)
        self.assertEqual(writes, api.writes)

    def test_cross_team_uses_each_team_device_ids_and_registers_missing_locally(self):
        first, args, item, state, password = self.ios_device_case()
        selected = [{"name": "first", "udid": "2" * 40}, {"name": "second", "udid": "3" * 40}]
        self.bulk_sync(first, args, item, state, password, selected)
        other_state = state.parent / "other-state"
        other_state.mkdir(mode=0o700)
        second = FakeIosDevices()
        second.info["teamId"] = "ZZZZZZZZZZ"
        second.devices = [{"id": "team-two-existing", "attributes": {"udid": "2" * 40, "platform": "IOS", "status": "ENABLED"}}]
        second.profiles = []
        second.pending = other_state / first.pending.name
        other_item = dict(item, teamId="ZZZZZZZZZZ")
        self.bulk_sync(second, args, other_item, other_state, password, selected)
        self.assertEqual(1, sum(1 for _, path in second.writes if path == "devices"))
        self.assertEqual({"team-two-existing", "device-new"}, second.profiles[0]["relations"]["devices"])
        self.assertNotIn("device-new-2", second.profiles[0]["relations"]["devices"])

    def test_partial_bulk_registration_retains_pending_and_retry_is_readonly(self):
        api, args, item, state, password = self.ios_device_case()
        api.fail = "device-interrupt-after"
        selected = [{"name": "first", "udid": "2" * 40}, {"name": "second", "udid": "3" * 40}]
        with self.assertRaises(KeyboardInterrupt):
            self.bulk_sync(api, args, item, state, password, selected)
        self.assertTrue(api.pending.is_file())
        self.assertEqual("old", api.profiles[0]["id"])
        writes = list(api.writes)
        api.fail = None
        with self.assertRaisesRegex(ValueError, "未知写入未查回"):
            self.bulk_sync(api, args, item, state, password, selected)
        self.assertEqual(writes, api.writes)
        self.assertTrue(api.pending.is_file())

    def test_ios_shared_onboard_certificate_plan_supports_pgyer_only_without_writes(self):
        self.root["alpha"]["ios"]["signing"]["test"]["profileNames"].pop("testflight")
        self.save()
        self.assertEqual("alpha", module.resolve(self.config, "ios", "onboard", "alpha")[0]["brand"])
        module.device_catalog.merge_catalog(self.directory, "ios", [{"name": "phone", "udid": "2" * 40}],
                                           {"brand": "source", "team": "source-team", "profile": "verified"})
        args = self.args()
        args.device_source, args.device_ids = "shared", "all"
        key = self.directory / "api.p8"
        key.write_text("key")
        with patch.dict(os.environ, {"ASC_API_KEY_PATH": str(key), "DEVICE_NAME": "", "DEVICE_UDID": ""}), \
                patch.object(module.ios, "AppleAPI") as api, patch.object(module.ios, "setup", return_value={"certificate": "create-needed"}) as setup, \
                patch.object(module, "ios_device_sync") as sync, patch.object(module, "invoke") as invoke:
            module.run(args)
        api.assert_called_once()
        self.assertEqual({}, setup.call_args.args[0]["profileNames"])
        self.assertFalse(setup.call_args.args[4])
        sync.assert_not_called()
        invoke.assert_not_called()

    def test_harmony_shared_bulk_is_private_and_does_not_expand_source_device_ids(self):
        approved = [{"name": "first", "udid": "a" * 64, "deviceType": 4}, {"name": "second", "udid": "b" * 64, "deviceType": 4}]
        module.device_catalog.merge_catalog(self.directory, "harmony", approved,
                                           {"brand": "source", "team": "source-team", "profile": "verified"})
        args = self.args(platform="harmony", operation="devices")
        args.device_source = "shared"
        def invoke(command, env=None):
            path = Path(command[command.index("--devices-file") + 1])
            devices = json.loads(path.read_text())
            self.assertEqual(approved, [{"name": row["deviceName"], "udid": row["udid"], "deviceType": row["deviceType"]}
                                        for row in sorted(devices, key=lambda row: row["deviceName"])])
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertNotIn("--apply", command)
            return b'{}'
        with patch.dict(os.environ, {"DEVICE_NAME": "", "DEVICE_UDID": ""}), patch.object(module, "invoke", side_effect=invoke):
            module.run(args)

    def test_empty_shared_pool_and_manual_conflict_stop_before_vendor(self):
        args = self.args(platform="harmony")
        args.device_source = "shared"
        with patch.dict(os.environ, {"DEVICE_NAME": "", "DEVICE_UDID": ""}), patch.object(module, "invoke") as invoke:
            with self.assertRaisesRegex(ValueError, "改选 manual"):
                module.run(args)
        invoke.assert_not_called()

        with patch.dict(os.environ, {"DEVICE_NAME": "phone", "DEVICE_UDID": "a" * 64}), patch.object(module, "invoke") as invoke:
            with self.assertRaisesRegex(ValueError, "冲突"):
                module.run(args)
        invoke.assert_not_called()

    def test_ios_shared_onboard_apply_reuses_setup_and_then_builds_selected_adhoc(self):
        self.root["alpha"]["ios"]["signing"]["test"]["profileNames"].pop("testflight")
        self.save()
        selected = [{"name": "phone", "udid": "2" * 40, "deviceType": "IOS"}]
        module.device_catalog.merge_catalog(self.directory, "ios", selected,
                                           {"brand": "source", "team": "source-team", "profile": "verified"})
        args = self.args(mode="apply")
        args.device_source = "shared"
        key = self.directory / "api.p8"
        key.write_text("key")
        calls = []
        def setup(item, api, output, state, apply, **kwargs):
            self.assertTrue(apply)
            self.assertEqual({}, item["profileNames"])
            self.assertIsNone(kwargs["p12_source"])
            module.ios.save(output / "distribution.p12", b"verified-p12")
            module.ios.save(output / "distribution-p12.password", b"verified-password")
            calls.append("certificate")
            return {"certificate": "created"}
        def sync(api, args, item, state, p12, password, *, requested):
            self.assertEqual(selected, requested)
            self.assertEqual(b"verified-p12", module.ios.restricted(p12))
            self.assertEqual(b"verified-password", module.ios.restricted(password))
            self.assertEqual({"pgyer": "adhoc"}, item["profileNames"])
            calls.append("adhoc")
        with patch.dict(os.environ, {"ASC_API_KEY_PATH": str(key), "DEVICE_NAME": "", "DEVICE_UDID": ""}), \
                patch.object(module.ios, "AppleAPI"), patch.object(module.ios, "setup", side_effect=setup), \
                patch.object(module, "ios_device_sync", side_effect=sync), patch.object(module, "invoke") as invoke:
            module.run(args)
        self.assertEqual(["certificate", "adhoc"], calls)
        invoke.assert_not_called()

    def test_testflight_only_onboard_has_no_device_requirement_in_plan_or_apply(self):
        self.root["alpha"]["ios"]["signing"]["test"]["profileNames"].pop("pgyer")
        self.save()
        key = self.directory / "api.p8"
        key.write_text("key")
        for mode in ("plan", "apply"):
            with self.subTest(mode=mode):
                args = self.args(mode=mode)
                args.device_source, args.device_ids = "shared", "all"
                with patch.dict(os.environ, {"ASC_API_KEY_PATH": str(key), "DEVICE_NAME": "", "DEVICE_UDID": ""}), \
                        patch.object(module, "invoke", return_value=b'{}') as invoke, patch.object(module.ios, "AppleAPI") as api:
                    module.run(args)
                command = invoke.call_args.args[0]
                self.assertEqual(mode, command[2])
                self.assertEqual("testflight", command[command.index("--channels") + 1])
                api.assert_not_called()
                self.assertFalse((self.directory / "devices").exists())
        command = ["plan", "--config", str(self.config), "--platform", "ios", "--operation", "onboard", "--brands", "alpha",
                   "--state-root", str(self.directory), "--device-source", "shared"]
        with patch.dict(os.environ, {"DEVICE_NAME": "", "DEVICE_UDID": ""}):
            self.assertEqual(0, module.main(command))

    def test_testflight_explicit_device_fields_and_specific_ids_are_not_ignored(self):
        self.root["alpha"]["ios"]["signing"]["test"]["profileNames"].pop("pgyer")
        self.save()
        args = self.args()
        args.device_source = "shared"
        for ids, name, udid in (("selected-id", "", ""), ("all", "phone", "2" * 40), ("", "", "")):
            with self.subTest(ids=ids):
                args.device_ids = ids
                with patch.dict(os.environ, {"DEVICE_NAME": name, "DEVICE_UDID": udid}), patch.object(module, "invoke") as invoke:
                    with self.assertRaisesRegex(ValueError, "TestFlight"):
                        module.run(args)
                invoke.assert_not_called()

    def test_failed_manual_apply_never_approves_device_catalog(self):
        args = self.args(platform="harmony", operation="devices", mode="apply")
        with patch.dict(os.environ, {"DEVICE_NAME": "phone", "DEVICE_UDID": "a" * 64}), \
                patch.object(module, "invoke", side_effect=ValueError("platform maintenance failed")), \
                patch.object(module.device_catalog, "merge_catalog") as merge:
            with self.assertRaises(ValueError):
                module.run(args)
        merge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
