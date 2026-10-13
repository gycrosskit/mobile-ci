import base64
import io
import json
import os
import plistlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from signing import signing_inventory as inventory


TEAM = "Z4BU3HH62Y"
UDID = "0123456789abcdef0123456789abcdef01234567"


class InventoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        cls.key_path, cls.cert_path = root / "key.pem", root / "certificate.pem"
        cls.key_path.write_bytes(inventory.ios.openssl("genrsa", "2048"))
        cls.cert_path.write_bytes(inventory.ios.openssl("req", "-new", "-x509", "-key", cls.key_path,
            "-days", "90", "-subj", "/CN=Apple Distribution: Test/OU=" + TEAM))
        cls.info = inventory.certificate(cls.cert_path.read_bytes())

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def signed(self, data):
        return inventory.ios.openssl("cms", "-sign", "-binary", "-nodetach", "-outform", "DER",
            "-signer", self.cert_path, "-inkey", self.key_path, data=data)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "brands.json"
        self.values = {
            "_iosTeams": {"team": {"teamId": TEAM, "credentialNamespace": "ios-team", "testSigningSource": "jenkins", "repositorySigningPath": "signing/ios/team"}},
            "_harmonyTeams": {"team": {"credentialNamespace": "harmony-team", "companyName": "公司"}},
            "one": {"displayName": "一号品牌", "android": {"applicationId": "com.example.one", "signing": {
                "source": "jenkins", "keyAlias": "one", "certificateSha256": self.info["sha256"],
                "keyOwner": "历史共享密钥组：one", "credentials": {
                    "keystore": "android-key-one-keystore", "signingJson": "android-key-one-signing-json"}}},
                "ios": {"bundleId": "com.example.one", "signing": {
                "test": {"team": "team", "enabled": True, "profileNames": {"pgyer": "approved-profile"}}}},
                "harmony": {"teamKey": "team", "bundleName": "com.example.one", "appId": "app-1", "signing": {
                    "certName": "distribution", "profileName": "approved-profile", "enabled": True}}},
            "alias": {"displayName": "同材料品牌", "ios": {"bundleId": "com.example.one", "signing": {
                "test": {"team": "team", "enabled": True, "profileNames": {"pgyer": "approved-profile"}}}}},
            "pending": {"displayName": "未接入", "ios": {"bundleId": "com.example.pending", "signing": {"test": {"enabled": False}}}}
        }
        self.write_config()
        self.expires = datetime.now(timezone.utc) + timedelta(days=60)
        self.profile = {"Name": "approved-profile", "TeamIdentifier": [TEAM], "CreationDate": datetime.now(timezone.utc) - timedelta(days=1),
                        "ExpirationDate": self.expires, "DeveloperCertificates": [self.info["_der"]],
                        "ApplicationIdentifierPrefix": [TEAM], "ProvisionedDevices": [UDID],
                        "Entitlements": {"com.apple.developer.team-identifier": TEAM, "application-identifier": TEAM + ".com.example.one", "get-task-allow": False}}
        self.merged = []

    def write_config(self):
        self.config.write_text(json.dumps(self.values))

    def merge(self, root, platform, devices, source):
        self.merged.append((platform, devices, source))
        return [{"id": inventory.signing_devices.device_id(platform, UDID), "name": devices[0]["name"], "maskedUdid": inventory.mask(UDID), "deviceType": devices[0]["deviceType"], "sources": [source]},
                {"id": "unrelated-device", "name": "other-brand-phone", "maskedUdid": "1234…5678", "deviceType": devices[0]["deviceType"], "sources": []}]

    def apple(self):
        profile = {"id": "profile-id", "attributes": {"name": "approved-profile", "profileType": "IOS_APP_ADHOC",
            "profileState": "ACTIVE", "expirationDate": self.expires.replace(microsecond=0).isoformat(),
            "profileContent": base64.b64encode(self.signed(plistlib.dumps(self.profile))).decode()}}
        responses = {
            "certificates?limit=200": [{"id": "cert-id", "attributes": {"certificateType": "DISTRIBUTION", "certificateContent": base64.b64encode(self.info["_der"]).decode()}}],
            "devices?limit=200": [{"id": "device-id", "attributes": {"name": "phone", "udid": UDID, "platform": "IOS", "status": "ENABLED"}}],
            "profiles?limit=200": [profile],
            "bundleIds?filter%5Bidentifier%5D=com.example.one&limit=200": [{"id": "bundle-id", "attributes": {"identifier": "com.example.one", "platform": "IOS"}}],
            "profiles/profile-id/relationships/bundleId": [{"id": "bundle-id"}],
            "profiles/profile-id/relationships/certificates": [{"id": "cert-id"}],
            "profiles/profile-id/relationships/devices": [{"id": "device-id"}],
        }
        class API:
            def all(self, path):
                return responses[path]
        return API()

    def collect_ios(self, local=True):
        with patch.object(inventory, "ReadOnlyApple", return_value=self.apple()), patch.object(inventory, "material_certificate", return_value=self.info if local else None), patch.object(inventory.signing_devices, "merge_catalog", side_effect=self.merge):
            return inventory.collect(self.config, "one", "ios", self.root, {})

    def test_targets_include_all_brands_platforms_and_shared_profile_aliases(self):
        rows = inventory.targets(self.config)
        self.assertEqual(9, len(rows))
        self.assertEqual(3, len(inventory.targets(self.config, "ios")))
        self.assertEqual("pending", next(row for row in rows if row["brand"] == "pending" and row["platform"] == "harmony")["configurationStatus"])
        one = inventory.targets(self.config, "ios", "one")[0]
        self.assertEqual("ios-team-distribution-p12", one["credentialIds"]["p12"])

    def test_android_config_retains_explicit_owner_and_shared_credential_group(self):
        row = inventory.targets(self.config, "android", "one")[0]
        self.assertEqual("configured", row["configurationStatus"])
        self.assertEqual("历史共享密钥组：one", row["keyOwner"])
        self.assertEqual("android-key-one-signing-json", row["credentialIds"]["signingConfig"])
        self.assertEqual({}, row["profileNames"])
        self.assertEqual({}, row["channelEnabled"])
        self.assertEqual("", row["team"])
        self.values["one"]["android"]["signing"]["credentials"]["signingJson"] = "android-key-other-signing-json"
        self.write_config()
        self.assertEqual("pending", inventory.targets(self.config, "android", "one")[0]["configurationStatus"])

    def test_android_collect_validates_private_material_without_provider_or_device_writes(self):
        info = {**self.info, "issuer": "CN=Self signed", "matchesExpectedSha256": True, "storePassword": "NEVER_PUBLIC"}
        env = {"ANDROID_KEYSTORE_FILE": "/binding/keystore", "ANDROID_SIGNING_CONFIG_FILE": "/binding/config"}
        with patch.dict(os.environ, env, clear=True), patch.object(inventory.android_signing, "inspect_certificate", return_value=info) as inspect, patch.object(inventory, "ReadOnlyApple") as apple, patch.object(inventory, "ReadOnlyHuawei") as huawei, patch.object(inventory.signing_devices, "merge_catalog") as merge:
            result = inventory.collect(self.config, "one", "android", self.root, {})
        inspect.assert_called_once_with(Path(env["ANDROID_KEYSTORE_FILE"]), Path(env["ANDROID_SIGNING_CONFIG_FILE"]), "one", self.info["sha256"])
        apple.assert_not_called(); huawei.assert_not_called(); merge.assert_not_called()
        self.assertEqual("complete", result["queryStatus"])
        self.assertEqual("configured", result["materialStatus"])
        self.assertEqual("Jenkins", result["materialSource"])
        self.assertEqual([], result["devices"])
        self.assertEqual([], result["profiles"])
        self.assertTrue(result["certificates"][0]["matchesExpectedSha256"])
        self.assertNotIn("NEVER_PUBLIC", json.dumps(result))
        self.assertNotIn("_der", json.dumps(result))

    def test_android_missing_pair_failure_and_fingerprint_mismatch_never_pass(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(inventory.android_signing, "inspect_certificate") as inspect:
            result = inventory.collect(self.config, "one", "android", self.root, {})
        inspect.assert_not_called()
        self.assertEqual("missing", result["materialStatus"])
        self.assertEqual("partial", result["queryStatus"])
        env = {"ANDROID_KEYSTORE_FILE": "/binding/keystore", "ANDROID_SIGNING_CONFIG_FILE": "/binding/config"}
        with patch.dict(os.environ, env, clear=True), patch.object(inventory.android_signing, "inspect_certificate", side_effect=ValueError("NEVER_PUBLIC password")):
            result = inventory.collect(self.config, "one", "android", self.root, {})
        self.assertEqual("invalid", result["materialStatus"])
        self.assertEqual("unknown", result["queryStatus"])
        self.assertNotIn("NEVER_PUBLIC", json.dumps(result))
        for passwords, statuses in (({"ANDROID_KEYSTORE_FILE": "/binding/keystore"}, {}), (env, {"android-key-one-keystore": "wrong-type"})):
            with patch.dict(os.environ, passwords, clear=True), patch.object(inventory.android_signing, "inspect_certificate") as inspect:
                result = inventory.collect(self.config, "one", "android", self.root, statuses)
            inspect.assert_not_called()
            self.assertEqual("invalid", result["materialStatus"])
        info = {**self.info, "matchesExpectedSha256": False}
        with patch.dict(os.environ, env, clear=True), patch.object(inventory.android_signing, "inspect_certificate", return_value=info):
            result = inventory.collect(self.config, "one", "android", self.root, {})
        self.assertEqual("mismatch", result["certificates"][0]["status"])
        self.assertEqual("invalid", result["materialStatus"])

    def test_android_render_has_no_device_pool_or_profile(self):
        row = inventory.targets(self.config, "android", "one")[0]
        row.update(certificates=[{**self.info, "matchesExpectedSha256": True}], credentials=[], profiles=[], devices=[])
        reports = self.root / "reports"
        reports.mkdir()
        (reports / "android.json").write_text(json.dumps(inventory.public_report(row)))
        with patch.object(inventory.signing_devices, "public_catalog", return_value=[]) as catalog:
            document = inventory.render(reports, self.root, self.root / "public")
        self.assertEqual({"ios", "harmony"}, set(document["devices"]))
        self.assertEqual(["ios", "harmony"], [call.args[1] for call in catalog.call_args_list])
        self.assertIn("不限制设备", (self.root / "public/index.html").read_text())

    def test_different_release_team_is_not_silently_associated(self):
        self.values["one"]["ios"]["signing"]["release"] = {"team": "other", "profileName": "other-profile", "enabled": True}
        self.write_config()
        row = inventory.targets(self.config, "ios", "one")[0]
        self.assertNotIn("appstore", row["profileNames"])
        self.assertIn("Team 不同", row["configurationNotes"][0])

    def test_unconfigured_brand_retains_report_without_credentials_or_queries(self):
        with patch.object(inventory, "ReadOnlyApple") as api:
            result = inventory.collect(self.config, "pending", "ios", self.root, {})
        api.assert_not_called()
        self.assertEqual("not-configured", result["queryStatus"])
        self.assertEqual("pending", result["brand"])

    def test_only_verified_ios_profile_enters_private_catalog(self):
        result = self.collect_ios()
        self.assertEqual("complete", result["queryStatus"])
        self.assertTrue(result["profiles"][0]["identityVerified"])
        self.assertEqual(1, len(self.merged))
        self.assertEqual(1, len(result["devices"]))
        self.assertEqual(UDID, self.merged[0][1][0]["udid"])
        public = json.dumps(result)
        self.assertNotIn(UDID, public)
        self.assertNotIn("PRIVATE KEY", public)
        self.assertNotIn("_der", public)

    def test_missing_p12_still_displays_remote_states_without_approving_devices(self):
        result = self.collect_ios(False)
        self.assertEqual("complete", result["queryStatus"])
        self.assertEqual("valid", result["profiles"][0]["status"])
        self.assertFalse(result["profiles"][0]["materialReady"])
        self.assertFalse(result["profiles"][0]["approvedForReuse"])
        self.assertEqual("phone", result["devices"][0]["name"])
        self.assertEqual(inventory.mask(UDID), result["devices"][0]["maskedUdid"])
        self.assertNotIn(UDID, json.dumps(result))
        self.assertEqual([], self.merged)

    def test_testflight_profile_has_no_device_requirement_and_never_populates_pool(self):
        self.values["one"]["ios"]["signing"]["test"]["profileNames"] = {"testflight": "approved-profile"}
        self.write_config()
        self.profile.pop("ProvisionedDevices")
        self.profile["Entitlements"]["beta-reports-active"] = True
        api = self.apple()
        previous = api.all
        def all_rows(path):
            if path.endswith("relationships/devices"):
                return []
            rows = previous(path)
            if path == "profiles?limit=200":
                rows[0]["attributes"]["profileType"] = "IOS_APP_STORE"
            return rows
        api.all = all_rows
        with patch.object(inventory, "ReadOnlyApple", return_value=api), patch.object(inventory, "material_certificate", return_value=self.info), patch.object(inventory.signing_devices, "merge_catalog", side_effect=self.merge):
            result = inventory.collect(self.config, "one", "ios", self.root, {})
        self.assertTrue(result["profiles"][0]["identityVerified"])
        self.assertEqual(0, result["profiles"][0]["deviceCount"])
        self.assertIn("不限制 UDID", result["profiles"][0]["devicePolicy"])
        self.assertEqual([], self.merged)

    def test_expired_profile_remains_visible_but_is_not_approved(self):
        self.expires = datetime.now(timezone.utc) - timedelta(days=1)
        self.profile["ExpirationDate"] = self.expires
        result = self.collect_ios()
        self.assertEqual("expired", result["profiles"][0]["status"])
        self.assertFalse(result["profiles"][0]["approvedForReuse"])
        self.assertEqual(1, len(result["devices"]))
        self.assertNotIn(UDID, json.dumps(result))
        self.assertEqual([], self.merged)

    def test_profile_device_bundle_certificate_and_cms_conflicts_do_not_approve(self):
        for mutate in (lambda p: p.update(ProvisionedDevices=["another-private-device"]),
                       lambda p: p.update(TeamIdentifier=["WRONGTEAM1"]),
                       lambda p: p.update(DeveloperCertificates=[b"wrong"]),
                       lambda p: p["Entitlements"].update({"application-identifier": TEAM + ".wrong.bundle"})):
            original = plistlib.loads(plistlib.dumps(self.profile))
            mutate(self.profile)
            result = self.collect_ios()
            self.assertEqual("mismatch", result["profiles"][0]["status"])
            self.assertEqual([], self.merged)
            self.profile = original
        with patch.object(inventory, "cms", side_effect=ValueError("SECRET " + UDID)):
            result = self.collect_ios()
        self.assertEqual("unknown", result["profiles"][0]["status"])
        self.assertEqual("partial", result["queryStatus"])
        self.assertNotIn(UDID, json.dumps(result))

    def test_profile_relationship_transport_failure_is_unknown_not_conflict(self):
        api = self.apple()
        previous = api.all
        api.all = lambda path: (_ for _ in ()).throw(OSError("SECRET " + UDID)) if "/relationships/" in path else previous(path)
        with patch.object(inventory, "ReadOnlyApple", return_value=api), patch.object(inventory, "material_certificate", return_value=self.info), patch.object(inventory.signing_devices, "merge_catalog", side_effect=self.merge):
            result = inventory.collect(self.config, "one", "ios", self.root, {})
        self.assertEqual("unknown", result["profiles"][0]["status"])
        self.assertEqual("partial", result["queryStatus"])
        self.assertEqual(1, len(result["certificates"]))
        self.assertTrue(result["errors"])
        self.assertNotIn(UDID, json.dumps(result))
        self.assertEqual([], self.merged)

    def test_query_failure_is_unknown_and_cannot_use_old_success(self):
        with patch.object(inventory, "ReadOnlyApple", side_effect=ValueError("SECRET " + UDID)), patch.object(inventory, "material_certificate", return_value=None):
            result = inventory.collect(self.config, "one", "ios", self.root, {})
        self.assertEqual("unknown", result["queryStatus"])
        self.assertNotIn(UDID, json.dumps(result))

    def test_clients_block_write_methods_before_transport(self):
        apple = inventory.ReadOnlyApple.__new__(inventory.ReadOnlyApple)
        huawei = inventory.ReadOnlyHuawei.__new__(inventory.ReadOnlyHuawei)
        with patch.object(inventory.ios.AppleAPI, "request", return_value={}) as request:
            for method in ("POST", "PUT", "DELETE"):
                with self.assertRaises(ValueError):
                    apple.request("devices", method=method)
            request.assert_not_called()
            apple.request("devices")
            request.assert_called_once_with("devices", method="GET")
        with patch.object(inventory.harmony.HuaweiAPI, "request", return_value={}) as request:
            for method in ("POST", "PUT", "DELETE"):
                with self.assertRaises(ValueError):
                    huawei.request(inventory.harmony.DEVICE_PATH, method)
            request.assert_not_called()
            huawei.request(inventory.harmony.CERT_PATH + "/list", "POST", {"certType": 2})
            self.assertEqual(1, request.call_count)

    def test_repository_source_uses_restricted_copies_and_cleans_up(self):
        self.values["_iosTeams"]["team"].pop("testSigningSource")
        self.write_config()
        source = self.root / "signing/ios/team"
        source.mkdir(parents=True)
        p12, password = source / "distribution.p12", source / "distribution-p12.password"
        p12.write_bytes(b"fixture-p12")
        password.write_bytes(b"fixture-password")
        p12.chmod(0o644)
        password.chmod(0o644)
        seen = []
        def identity(path, secret):
            seen.append(path.parent)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)
            self.assertEqual(b"fixture-password", secret)
            return b"private", {"der": self.info["_der"]}
        row = inventory.targets(self.config, "ios", "one")[0]
        with patch.dict(os.environ, {}, clear=True), patch.object(inventory, "ROOT", self.root), patch.object(inventory.ios, "p12_identity", side_effect=identity):
            result = inventory.material_certificate("ios", self.config, row, {})
        self.assertEqual("repository", result["_source"])
        self.assertFalse(seen[0].exists())
        self.assertEqual(0o644, p12.stat().st_mode & 0o777)
        self.assertEqual(b"fixture-p12", p12.read_bytes())
        password.unlink()
        with patch.dict(os.environ, {}, clear=True), patch.object(inventory, "ROOT", self.root):
            with self.assertRaises(ValueError):
                inventory.material_certificate("ios", self.config, row, {})

    def test_bound_partial_or_wrong_type_pair_cannot_fall_back_to_repository(self):
        row = inventory.targets(self.config, "ios", "one")[0]
        with patch.dict(os.environ, {"IOS_DISTRIBUTION_P12": "/missing"}, clear=True):
            with self.assertRaises(ValueError):
                inventory.material_certificate("ios", self.config, row, {})
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                inventory.material_certificate("ios", self.config, row, {row["credentialIds"]["p12"]: "wrong-type"})

    def harmony_data(self):
        devices = [{"id": "device-id", "deviceName": "phone", "udid": UDID, "deviceType": 4}]
        content = self.signed(json.dumps({"type": "release", "validity": {
            "not-before": int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp()), "not-after": int(self.expires.timestamp())},
            "bundle-info": {"bundle-name": "com.example.one", "distribution-certificate": self.cert_path.read_text()},
            "debug-info": {"device-id-type": "udid", "device-ids": [UDID]}}).encode())
        profile = {"id": "profile-id", "provisionName": "approved-profile", "provisionType": 6, "tempFlag": "0",
                   "appId": "app-1", "certName": "distribution", "deviceList": devices, "provisionDownloadUrl": "https://example.test/profile"}
        cert = {"id": "cert-id", "certName": "distribution", "certType": 2, "certDownloadUrl": "https://example.test/certificate"}
        return devices, content, profile, cert

    def collect_harmony(self, bound=True, local=True):
        devices, content, profile, cert = self.harmony_data()
        env = {}
        if bound:
            for key, name, data in (("HARMONY_PROFILE_FILE", "profile.p7b", content), ("HARMONY_DEVICES_FILE", "devices.json", json.dumps(devices).encode())):
                path = self.root / name
                path.write_bytes(data)
                path.chmod(0o600)
                env[key] = str(path)
        with patch.dict(os.environ, env, clear=True), patch.object(inventory, "ReadOnlyHuawei"), patch.object(inventory.harmony, "list_certificates", return_value=[cert]), patch.object(inventory.harmony, "list_profiles", return_value=[profile]), patch.object(inventory.harmony, "list_devices", return_value=devices), patch.object(inventory.harmony, "download", side_effect=lambda url: content if url.endswith("profile") else self.cert_path.read_bytes()), patch.object(inventory, "material_certificate", return_value=self.info if local else None), patch.object(inventory.signing_devices, "merge_catalog", side_effect=self.merge):
            return inventory.collect(self.config, "one", "harmony", self.root, {})

    def test_harmony_requires_approved_bound_profile_and_roster_for_pool(self):
        result = self.collect_harmony()
        self.assertEqual("complete", result["queryStatus"])
        self.assertTrue(result["profiles"][0]["approvedMaterialsMatch"])
        self.assertFalse(result["profiles"][0]["caTrustVerified"])
        self.assertEqual(1, len(self.merged))
        self.assertEqual(4, self.merged[0][1][0]["deviceType"])
        self.assertNotIn(UDID, json.dumps(result))
        self.merged = []
        result = self.collect_harmony(False)
        self.assertFalse(result["profiles"][0]["approvedMaterialsMatch"])
        self.assertEqual(1, len(result["devices"]))
        self.assertFalse(result["profiles"][0]["approvedForReuse"])
        self.assertEqual([], self.merged)

    def test_harmony_expired_profile_is_not_approved(self):
        self.expires = datetime.now(timezone.utc) - timedelta(days=1)
        result = self.collect_harmony()
        self.assertEqual("expired", result["profiles"][0]["status"])
        self.assertEqual(1, len(result["devices"]))
        self.assertFalse(result["profiles"][0]["approvedForReuse"])
        self.assertNotIn(UDID, json.dumps(result))
        self.assertEqual([], self.merged)

    def test_harmony_missing_p12_displays_profile_devices_without_pool_write(self):
        result = self.collect_harmony(local=False)
        self.assertTrue(result["profiles"][0]["identityVerified"])
        self.assertFalse(result["profiles"][0]["approvedForReuse"])
        self.assertEqual("phone", result["devices"][0]["name"])
        self.assertNotIn(UDID, json.dumps(result))
        self.assertEqual([], self.merged)

    def test_missing_p12_profile_devices_are_visible_in_html_without_public_pool_entry(self):
        row = self.collect_ios(False)
        reports = self.root / "reports"
        reports.mkdir()
        (reports / "one.json").write_text(json.dumps(row))
        with patch.object(inventory.signing_devices, "public_catalog", return_value=[]):
            document = inventory.render(reports, self.root, self.root / "public")
        text = (self.root / "public/index.html").read_text()
        self.assertIn("当前 Profile 中的测试设备", text)
        self.assertIn("phone", text)
        self.assertIn("设备仅供查询，未加入共享池", text)
        self.assertEqual([], document["devices"]["ios"])
        self.assertEqual([], self.merged)

    def test_profile_device_display_reuses_catalog_scrubbing_without_approval(self):
        report = {"devices": [], "_stateRoot": self.root}
        with patch.object(inventory.signing_devices, "merge_catalog") as merge:
            inventory.record_devices(report, "ios", [{"name": "phone " + UDID, "udid": UDID, "deviceType": "IOS"}],
                                     {"brand": "one", "team": "team", "profile": "profile " + UDID}, False)
        merge.assert_not_called()
        self.assertNotIn(UDID, json.dumps(report["devices"]))
        self.assertIn("设备标识已隐藏", report["devices"][0]["name"])

    def test_static_html_escapes_content_and_has_no_script_css_or_full_udid(self):
        reports = self.root / "reports"
        reports.mkdir()
        row = inventory.collect(self.config, "pending", "ios", self.root, {})
        row["displayName"] = '<script>alert("x")</script>'
        row["rawSecret"] = UDID
        row["credentials"] = [{"role": "p12", "id": "safe", "status": "missing", "value": UDID}]
        (reports / "pending.json").write_text(json.dumps(row))
        output = self.root / "public"
        devices = [{"id": "opaque", "name": "<img src=x>", "maskedUdid": inventory.mask(UDID), "deviceType": "IOS", "sources": [{"brand": "one", "team": "team", "profile": "profile"}]}]
        with patch.object(inventory.signing_devices, "public_catalog", side_effect=lambda root, platform: devices if platform == "ios" else []):
            inventory.render(reports, self.root, output)
        text = (output / "index.html").read_text()
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script", text)
        self.assertNotIn("<style", text)
        self.assertNotIn(UDID, text)
        self.assertNotIn(UDID, (output / "inventory.json").read_text())
        self.assertIn("品牌汇总", text)
        self.assertIn("+08:00", text)
        self.assertIn("report.css", text)
        self.assertTrue((output / "report.css").is_file())
        self.assertIn('<h2 id="harmony">鸿蒙</h2>', text)
        self.assertIn("团队：待确认", text)
        self.assertIn("min-width:760px", (output / "report.css").read_text())

    def test_public_destination_refuses_private_root_inputs_and_symlinks(self):
        root = self.root.resolve()
        with self.assertRaises(ValueError):
            inventory.public_destination(root / "private" / "report.json", root / "private", ())
        with self.assertRaises(ValueError):
            inventory.public_destination(self.config, root / "private", (self.config,))
        link = root / "public-link"
        link.symlink_to(self.config)
        with self.assertRaises(ValueError):
            inventory.public_destination(link, root / "private", ())

    def test_repository_source_explains_missing_jenkins_pair_without_hiding_api_missing(self):
        row = self.collect_ios(False)
        row.update(materialSource="repository", materialStatus="configured")
        reports = self.root / "reports"
        reports.mkdir()
        (reports / "one.json").write_text(json.dumps(row))
        with patch.object(inventory.signing_devices, "public_catalog", return_value=[]):
            document = inventory.render(reports, self.root, self.root / "public")
        text = (self.root / "public/index.html").read_text()
        self.assertIn("证书 P12：Jenkins 未保存；当前从受控仓库读取", text)
        self.assertIn("P12 密码：Jenkins 未保存；当前从受控仓库读取", text)
        self.assertIn("API 私钥：缺失", text)
        self.assertTrue(all(item["status"] == "missing" for item in document["brands"][0]["credentials"]))

    def test_cli_plan_and_failure_never_print_secret_material(self):
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(0, inventory.main(["plan", "--config", str(self.config)]))
        self.assertEqual(9, len(json.loads(stdout.getvalue())))
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(1, inventory.main(["collect", "--config", str(self.config), "--brand", UDID]))
        self.assertNotIn(UDID, stderr.getvalue())

    def test_cli_render_prints_only_safe_summary_and_counts_query_failures(self):
        root = self.root.resolve()
        reports = root / "reports"
        reports.mkdir()
        row = inventory.collect(self.config, "pending", "ios", root, {})
        (reports / "pending.json").write_text(json.dumps(row))
        other = {**row, "brand": "one", "queryStatus": "partial"}
        (reports / "failed.json").write_text(json.dumps(other))
        with patch.object(inventory.signing_devices, "public_catalog", return_value=[]), redirect_stdout(io.StringIO()) as stdout:
            code = inventory.main(["render", "--reports-dir", str(reports), "--state-root", str(root / "private"), "--output-dir", str(root / "public")])
        self.assertEqual(0, code)
        self.assertEqual({"brands": 2, "queryFailures": 1, "devices": {"ios": 0, "harmony": 0}}, json.loads(stdout.getvalue()))


if __name__ == "__main__":
    unittest.main()
