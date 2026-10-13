import base64
import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from signing import harmony_provisioning as signing


UDID = "DO-NOT-LOG-SECRET-UDID-00001"
SECOND = "DO-NOT-LOG-SECRET-UDID-00002"
CONFIG = {"brand": "cihang", "teamKey": "cihang", "appId": "app-123", "bundleName": "com.example.app",
          "certName": "team-release", "profileName": "brand-designated", "enabled": False}


class FakeHuawei:
    def __init__(self, root):
        self.root = root
        self.writes = []
        self.certificates = []
        self.devices = []
        self.profiles = []
        self.contents = {}
        self.unknown = None
        self.accept = True
        self.partial = False
        self.ca_key = root / "ca.key"
        signing.save(self.ca_key, signing.openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256"))
        signing.save(root / "ca.pem", signing.openssl("req", "-new", "-x509", "-key", self.ca_key,
                                                   "-days", "2", "-subj", "/CN=Fixture CA"))

    def request(self, path, method="GET", body=None, headers=None):
        if path == signing.CERT_PATH + "/list":
            return {"ret": {"code": 0}, "certInfo": self.certificates[:]}
        if path.startswith(signing.DEVICE_PATH + "/list?"):
            return {"ret": {"code": 0}, "totalCount": len(self.devices), "deviceList": self.devices[:]}
        if path.startswith(signing.PROFILE_PATH + "/list?"):
            assert headers == {"appId": CONFIG["appId"]}
            return {"ret": {"code": 0}, "totalCount": len(self.profiles), "provisionList": self.profiles[:]}
        self.writes.append((path, method, body))
        result = {"ret": {"code": 0}}
        if self.accept:
            if path == signing.CERT_PATH:
                pem = signing.openssl("x509", "-req", "-CA", self.root / "ca.pem", "-CAkey", self.ca_key,
                                      "-set_serial", "2", "-days", "1", data=body["csr"].encode())
                row = {"id": "certificate-id", "certName": body["certName"], "certType": 2,
                       "certDownloadUrl": "https://example.test/certificate"}
                self.certificates.append(row)
                self.contents[row["certDownloadUrl"]] = pem
                result["certInfo"] = row
            elif path == signing.DEVICE_PATH:
                selected = body["deviceList"][:1] if self.partial else body["deviceList"]
                self.devices.extend({**row, "id": "device-" + str(len(self.devices) + index)}
                                    for index, row in enumerate(selected))
                result.update(successedCount=len(selected), failedCount=len(body["deviceList"]) - len(selected),
                              failedAddDeviceDetail=[{"addDeviceInfo": item, "result": {"code": 123, "msg": "fixture"}}
                                                    for item in body["deviceList"][len(selected):]])
            elif path == signing.PROFILE_PATH:
                if method == "POST":
                    row = {"id": "profile-id", "provisionName": body["provisionName"], "provisionType": 6,
                           "appId": body["appId"], "certName": CONFIG["certName"], "tempFlag": "0",
                           "deviceList": [], "aclPermissionList": ["ohos.permission.FIXTURE"],
                           "provisionDownloadUrl": "https://example.test/profile"}
                    self.profiles.append(row)
                else:
                    row = self.profiles[0]
                row["deviceList"] = [device for device in self.devices if device["id"] in body["deviceIdList"]]
                self.contents[row["provisionDownloadUrl"]] = self.signed_profile(row["deviceList"])
                result["provisionInfo"] = row
        if self.unknown == path:
            raise signing.UnknownWrite("fixture response lost")
        return result

    def signed_profile(self, devices, changes=None):
        pem = self.contents[self.certificates[0]["certDownloadUrl"]]
        profile = {"type": "release", "bundle-info": {"bundle-name": CONFIG["bundleName"],
                    "distribution-certificate": pem.decode(), "app-identifier": "test-app-identifier"},
                   "validity": {"not-before": int(time.time()) - 60, "not-after": int(time.time()) + 3600},
                   "debug-info": {"device-id-type": "udid", "device-ids": [row["udid"] for row in devices]}}
        profile.update(changes or {})
        return signing.openssl("cms", "-sign", "-binary", "-nodetach", "-outform", "DER",
                               "-signer", self.root / "ca.pem", "-inkey", self.ca_key, data=json.dumps(profile).encode())


class JwtTest(unittest.TestCase):
    def test_ps256_is_verified_with_rsa_pss_and_exact_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "api.key"
            signing.save(key, signing.openssl("genrsa", "2048"))
            token = signing.jwt(key, "key-id", "sub-account", 12345)
            parts = token.split(".")
            decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            self.assertEqual({"kid": "key-id", "typ": "JWT", "alg": "PS256"}, json.loads(decode(parts[0])))
            self.assertEqual({"aud": signing.AUDIENCE, "iss": "sub-account", "iat": 12345, "exp": 12945},
                             json.loads(decode(parts[1])))
            signing.save(root / "signature", decode(parts[2]))
            signing.save(root / "public.pem", signing.openssl("pkey", "-in", key, "-pubout"))
            self.assertIn(b"Verified OK", signing.openssl("dgst", "-sha256", "-verify", root / "public.pem",
                "-signature", root / "signature", "-sigopt", "rsa_padding_mode:pss", "-sigopt", "rsa_pss_saltlen:32",
                data=(parts[0] + "." + parts[1]).encode()))


class ResponseTest(unittest.TestCase):
    def test_ret_is_object_with_integer_zero(self):
        for result in ({"ret": 0}, {"ret": {"code": "0"}}, {"ret": {"code": False}}, {}, []):
            with self.subTest(result=result), self.assertRaises(signing.ProvisioningError):
                signing.check_response(result)
        self.assertEqual({"ret": {"code": 0}}, signing.check_response({"ret": {"code": 0}}))
        with self.assertRaisesRegex(signing.ProvisioningError, "code=123"):
            signing.check_response({"ret": {"code": 123, "msg": UDID}})

    def test_official_cert_list_names_and_unknown_shape(self):
        for field in ("certInfo", "certList"):
            self.assertEqual([], signing.certificate_rows({field: []}))
        for value in ({}, {"certInfo": {}}, {"certInfo": [], "certList": []}, {"certList": [{"id": "test"}]}):
            with self.subTest(value=value), self.assertRaises(signing.ProvisioningError):
                signing.certificate_rows(value)

    def test_device_union_preserves_existing_and_uses_agc_ids(self):
        self.assertEqual(["old", "new"], signing.device_union([{"id": "old", "udid": UDID}],
                                              [{"id": "new", "udid": SECOND}, {"id": "old", "udid": UDID}]))
        with self.assertRaises(signing.ProvisioningError):
            signing.device_union([{"udid": UDID}], [])
        with self.assertRaises(signing.ProvisioningError):
            signing.device_union([{"id": str(index)} for index in range(4001)], [])

    def test_pagination_advances_pages_and_fails_on_repeated_record_offset(self):
        class Pages:
            def __init__(self):
                self.calls = []
            def request(self, path, headers=None):
                self.calls.append(path)
                if "fromRecCount=1&" in path:
                    return {"totalCount": 101, "deviceList": [{"id": str(index)} for index in range(100)]}
                return {"totalCount": 101, "deviceList": [{"id": "100"}]}
        api = Pages()
        self.assertEqual(101, len(signing.paged_rows(api, "/api/publish/v2/device/list", "deviceList")))
        self.assertIn("fromRecCount=2", api.calls[1])
        with patch.object(api, "request", return_value={"totalCount": 101, "deviceList": [{"id": "0"}]}):
            with self.assertRaisesRegex(signing.ProvisioningError, "重复"):
                signing.paged_rows(api, "/api/publish/v2/device/list", "deviceList")

    def test_live_empty_device_pool_omits_list_only_on_first_page_int_zero(self):
        api = signing.HuaweiAPI.__new__(signing.HuaweiAPI)
        with patch.object(api, "request", return_value={"ret": {"code": 0}, "totalCount": 0}):
            self.assertEqual([], signing.list_devices(api))
        for value in ({"totalCount": "0"}, {"totalCount": False}, {"totalCount": 1}, {"totalCount": 0, "deviceList": None}):
            with self.subTest(value=value), patch.object(api, "request", return_value=value):
                with self.assertRaises(signing.ProvisioningError):
                    signing.list_devices(api)
        with patch.object(api, "request", return_value={"ret": {"code": 0}, "totalCount": 0}):
            with self.assertRaises(signing.ProvisioningError):
                signing.list_profiles(api, CONFIG)

    def test_profile_total_count_is_optional_and_full_page_continues(self):
        api = signing.HuaweiAPI.__new__(signing.HuaweiAPI)
        with patch.object(api, "request", side_effect=[
                {"provisionList": [{"id": str(index)} for index in range(100)]},
                {"provisionList": [{"id": "100"}]}]) as request:
            self.assertEqual(101, len(signing.list_profiles(api, CONFIG)))
            self.assertEqual(2, request.call_count)
        with patch.object(api, "request", return_value={"ret": {"code": 0}, "provisionList": []}):
            self.assertEqual([], signing.list_profiles(api, CONFIG))

    def test_pagination_rejects_missing_changed_total_or_empty_page(self):
        for pages in ([{"deviceList": []}], [{"totalCount": 1, "deviceList": []}],
                      [{"totalCount": 2, "deviceList": [{"id": "a"}]}, {"totalCount": 3, "deviceList": [{"id": "b"}]}]):
            with self.subTest(pages=pages), patch.object(signing.HuaweiAPI, "request", side_effect=pages):
                with self.assertRaises(signing.ProvisioningError):
                    signing.paged_rows(signing.HuaweiAPI.__new__(signing.HuaweiAPI), "/api/publish/v2/device/list", "deviceList")

    def test_network_timeout_is_unknown_write_but_post_list_is_read(self):
        api = signing.HuaweiAPI.__new__(signing.HuaweiAPI)
        api.key_path, api.key_id, api.sub_account = "fixture", "key", "account"
        class Opener:
            def open(self, request, timeout):
                raise TimeoutError()
        api.opener = Opener()
        with patch.object(signing, "jwt", return_value="fixture-token"):
            with self.assertRaises(signing.UnknownWrite):
                api.request(signing.CERT_PATH, "POST", {"csr": "fixture"})
            with self.assertRaises(signing.ProvisioningError) as caught:
                api.request(signing.CERT_PATH + "/list", "POST", {"certType": 2})
            self.assertNotIsInstance(caught.exception, signing.UnknownWrite)
            with self.assertRaises(signing.ProvisioningError):
                api.request("https://evil.test")

    def test_existing_team_tablet_tv_pc_are_accepted_without_registering_them(self):
        rows = [{"id": str(kind), "udid": "existing-device-" + str(kind), "deviceType": kind}
                for kind in (1, 2, 4, 5, 8, 19)]
        api = signing.HuaweiAPI.__new__(signing.HuaweiAPI)
        with patch.object(signing, "paged_rows", return_value=rows):
            self.assertEqual(rows, signing.list_devices(api))
        with patch.object(signing, "paged_rows", return_value=[{"id": "wrong", "udid": UDID, "deviceType": 3}]):
            with self.assertRaises(signing.ProvisioningError):
                signing.list_devices(api)

    def test_device_input_name_and_udid_limits_and_harmony_type(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            row = {"deviceName": "A" * 100, "udid": "a" * 64, "deviceType": 4}
            signing.save(path, json.dumps([row]).encode())
            self.assertEqual([row], signing.read_devices(path))
            for invalid in ({**row, "deviceName": "A" * 101}, {**row, "udid": "a" * 65},
                            {**row, "deviceType": 5}):
                with self.subTest(invalid=invalid):
                    signing.save(path, json.dumps([invalid]).encode())
                    with self.assertRaises(signing.ProvisioningError):
                        signing.read_devices(path)

    def test_secret_input_permissions_and_devices_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            row = {"deviceName": "fixture", "udid": UDID, "deviceType": 4}
            signing.save(path, json.dumps([row, row]).encode())
            self.assertEqual([row], signing.read_devices(path))
            path.chmod(0o644)
            with self.assertRaises(signing.ProvisioningError):
                signing.read_devices(path)


class ProvisioningTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = signing.private_dir(self.root / "state")
        self.output = signing.private_dir(self.root / "output")
        self.api = FakeHuawei(self.root)
        self.requested = [{"deviceName": "fixture", "udid": UDID, "deviceType": 4}]
        self.download = patch.object(signing, "download", side_effect=lambda url: self.api.contents[url])
        self.download.start()
        self.addCleanup(self.download.stop)
        self.password = patch.dict(os.environ, {"HARMONY_P12_PASSWORD": "fixture-password"})
        self.password.start()
        self.addCleanup(self.password.stop)

    def apply(self, devices=None, devices_only=False):
        return signing.setup(CONFIG, self.api, self.state, self.output,
                             self.requested if devices is None else devices, True, devices_only)

    def test_plan_never_writes_generates_key_or_logs_udid(self):
        report = signing.setup(CONFIG, self.api, self.state, self.output, self.requested)
        self.assertEqual([], self.api.writes)
        self.assertEqual([], list(self.state.iterdir()))
        self.assertEqual([], list(self.output.iterdir()))
        self.assertEqual("create-needed", report["certificate"])
        self.assertNotIn(UDID, json.dumps(report))

    def test_apply_p12_profile_crypto_permissions_and_reuse(self):
        report = self.apply()
        self.assertTrue(report["p12Verified"])
        self.assertTrue(report["profileVerified"])
        self.assertFalse(report["publishingEnabled"])
        self.assertEqual([signing.DEVICE_PATH, signing.CERT_PATH, signing.PROFILE_PATH], [row[0] for row in self.api.writes])
        self.assertNotIn(UDID, json.dumps(report))
        first_p12 = signing.restricted(self.output / "release.p12")
        self.assertEqual("reuse", self.apply()["certificate"])
        self.assertEqual(first_p12, signing.restricted(self.output / "release.p12"))
        self.assertEqual(3, len(self.api.writes))
        for root in (self.state, self.output):
            self.assertEqual(0o700, stat.S_IMODE(root.stat().st_mode))
            for path in root.iterdir():
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual({"release.p12", "release.cer", "designated-release.p7b", "devices.json"},
                         {path.name for path in self.output.iterdir()})
        self.assertEqual({UDID}, {row["udid"] for row in json.loads(signing.restricted(self.output / "devices.json"))})
        exported = signing.openssl("pkcs12", "-in", self.output / "release.p12", "-clcerts", "-nokeys",
                                   "-passin", "stdin", data=b"fixture-password")
        self.assertIn(b"friendlyName: release", exported)

    def test_export_p12_uses_aes256_sha256_without_legacy_cipher(self):
        self.apply()
        path = self.output / "release.p12"
        info = subprocess.run(["openssl", "pkcs12", "-in", str(path), "-info", "-noout", "-passin", "stdin"],
                              input=b"fixture-password", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(0, info.returncode)
        algorithms = info.stderr.decode()
        self.assertNotIn("RC2", algorithms)
        # LibreSSL -info 仅显示 PBES2；实际加密算法由 DER 中的 aes-256-cbc OID 确认。
        packed = signing.restricted(path)
        self.assertGreaterEqual(packed.count(bytes.fromhex("060960864801650304012a")), 2)
        mac = signing.openssl("asn1parse", "-inform", "DER", data=packed).decode()
        self.assertRegex(mac, r"OBJECT\s*:sha256")

    def test_first_key_uses_official_tool_384_curve_and_csr_is_signed_pem(self):
        self.apply()
        csr = next(body["csr"] for path, _, body in self.api.writes if path == signing.CERT_PATH)
        self.assertTrue(csr.startswith("-----BEGIN NEW CERTIFICATE REQUEST-----\n"))
        self.assertTrue(csr.rstrip().endswith("-----END NEW CERTIFICATE REQUEST-----"))
        signing.openssl("req", "-verify", "-noout", data=csr.encode())
        text = signing.openssl("req", "-text", "-noout", data=csr.encode()).decode()
        self.assertIn("prime256v1", text)
        self.assertIn("ecdsa-with-SHA256", text)
        subject = signing.openssl("req", "-subject", "-nameopt", "RFC2253", "-noout", data=csr.encode()).decode()
        for component in ("C=CN", "O=HUAWEI", "OU=HUAWEI IDE", "CN=release"):
            self.assertIn(component, subject)
        pub = signing.openssl("req", "-pubkey", "-noout", data=csr.encode())
        self.assertEqual(signing.public_key(signing.restricted(self.state / "release.key")),
                         signing.openssl("pkey", "-pubin", "-outform", "DER", data=pub))

    def test_existing_private_key_is_not_replaced_to_change_curve(self):
        original = signing.openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:secp384r1", "-pkeyopt", "ec_param_enc:named_curve")
        signing.save(self.state / "release.key", original)
        self.apply()
        self.assertEqual(original, signing.restricted(self.state / "release.key"))
        csr = next(body["csr"] for path, _, body in self.api.writes if path == signing.CERT_PATH)
        pub = signing.openssl("req", "-pubkey", "-noout", data=csr.encode())
        self.assertEqual(signing.public_key(original), signing.openssl("pkey", "-pubin", "-outform", "DER", data=pub))

    def test_certificate_timeout_queries_and_recovers_without_post_retry(self):
        self.api.unknown = signing.CERT_PATH
        self.assertEqual("created", self.apply()["certificate"])
        self.apply()
        self.assertEqual(1, sum(path == signing.CERT_PATH for path, _, _ in self.api.writes))

    def test_explicit_business_reject_removes_pending_but_preserves_certificate_key(self):
        def reject(*args, **kwargs):
            raise signing.RejectedWrite("fixture explicit rejection")
        original = self.api.request
        with patch.object(self.api, "request", side_effect=lambda path, *args, **kwargs:
                          reject() if path == signing.CERT_PATH else original(path, *args, **kwargs)):
            with self.assertRaises(signing.RejectedWrite):
                signing.ensure_certificate(self.api, CONFIG, self.state, True)
        self.assertFalse((self.state / "certificate-pending.json").exists())
        self.assertTrue((self.state / "release.key").exists())
        key = signing.restricted(self.state / "release.key")
        signing.ensure_certificate(self.api, CONFIG, self.state, True)
        self.assertEqual(key, signing.restricted(self.state / "release.key"))

    def test_explicit_device_and_profile_business_reject_clear_only_their_pending(self):
        original = self.api.request
        with patch.object(self.api, "request", side_effect=lambda path, *args, **kwargs:
                          signing.check_response({"ret": {"code": 123, "msg": UDID}}) if path == signing.DEVICE_PATH
                          else original(path, *args, **kwargs)):
            with self.assertRaises(signing.RejectedWrite):
                signing.sync_devices(self.api, self.requested, True, self.state)
        self.assertFalse((self.state / "devices-pending.json").exists())
        devices, _ = signing.sync_devices(self.api, self.requested, True, self.state)
        certificate, _ = signing.ensure_certificate(self.api, CONFIG, self.state, True)
        with patch.object(self.api, "request", side_effect=lambda path, *args, **kwargs:
                          signing.check_response({"ret": {"code": 123, "msg": UDID}}) if path == signing.PROFILE_PATH
                          else original(path, *args, **kwargs)):
            with self.assertRaises(signing.RejectedWrite):
                signing.ensure_profile(self.api, CONFIG, self.state, certificate, devices, True)
        self.assertEqual([], list(self.state.glob("profile-*-pending.json")))

    def test_unknown_certificate_not_found_retains_key_and_blocks_next_post(self):
        self.api.accept = False
        self.api.unknown = signing.CERT_PATH
        with self.assertRaises(signing.ProvisioningError):
            signing.ensure_certificate(self.api, CONFIG, self.state, True)
        self.assertTrue((self.state / "release.key").exists())
        self.assertTrue((self.state / "certificate-pending.json").exists())
        with self.assertRaisesRegex(signing.ProvisioningError, "禁止重复"):
            signing.ensure_certificate(self.api, CONFIG, self.state, True)
        self.assertEqual(1, len(self.api.writes))

    def test_name_alone_cannot_recover_different_public_key(self):
        self.apply()
        signing.save(self.state / "release.key", signing.openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256"))
        found, status = signing.ensure_certificate(self.api, CONFIG, self.state, False)
        self.assertIsNone(found)
        self.assertEqual("create-needed", status)

    def test_device_timeout_requeries_and_recovers(self):
        self.api.unknown = signing.DEVICE_PATH
        self.apply()
        self.assertFalse((self.state / "devices-pending.json").exists())
        self.assertEqual(1, sum(path == signing.DEVICE_PATH for path, _, _ in self.api.writes))

    def test_unknown_device_not_found_blocks_next_post(self):
        self.api.accept = False
        self.api.unknown = signing.DEVICE_PATH
        with self.assertRaises(signing.ProvisioningError):
            signing.sync_devices(self.api, self.requested, True, self.state)
        with self.assertRaisesRegex(signing.ProvisioningError, "禁止重复 POST"):
            signing.sync_devices(self.api, self.requested, True, self.state)
        self.assertEqual(1, len(self.api.writes))

    def test_partial_device_success_requeries_preserves_success_and_does_not_create_profile(self):
        self.api.partial = True
        with self.assertRaisesRegex(signing.ProvisioningError, "未完整注册"):
            self.apply(self.requested + [{"deviceName": "second", "udid": SECOND, "deviceType": 4}])
        self.assertEqual(1, len(self.api.devices))
        self.assertEqual([signing.DEVICE_PATH], [path for path, _, _ in self.api.writes])

    def test_devices_apply_keeps_old_devices_and_acl(self):
        self.apply()
        second = [{"deviceName": "second", "udid": SECOND, "deviceType": 4}]
        report = self.apply(second, devices_only=True)
        self.assertEqual("updated", report["profile"])
        profile = self.api.profiles[0]
        self.assertEqual({UDID, SECOND}, {row["udid"] for row in profile["deviceList"]})
        self.assertEqual(["ohos.permission.FIXTURE"], profile["aclPermissionList"])
        self.assertEqual({"provisionId", "deviceIdList"}, set(self.api.writes[-1][2]))
        self.assertEqual(2, report["profileDeviceCount"])

    def test_masked_profile_roster_is_resolved_by_agc_id_before_exact_cms_validation(self):
        full = "a" * 64
        self.requested = [{"deviceName": "fixture", "udid": full, "deviceType": 4}]
        self.apply()
        profile = self.api.profiles[0]
        profile["deviceList"] = [dict(self.api.devices[0], udid=full[:16] + "*" * 40 + full[-8:])]
        report = self.apply()
        self.assertEqual("reuse", report["profile"])
        self.assertEqual(full, signing.read_json(signing.restricted(self.output / "devices.json"))[0]["udid"])
        self.assertEqual(3, len(self.api.writes))
        self.assertNotIn(full, json.dumps(report))

    def test_masked_profile_conflicting_visible_bits_length_type_missing_or_duplicate_id_fail(self):
        self.apply()
        original = self.api.devices[0]
        for row in ({**original, "udid": "X" + "*" * (len(UDID) - 1)},
                    {**original, "udid": "*" * (len(UDID) - 1)}, {**original, "deviceType": 5},
                    {**original, "id": "absent"}, {**original, "udid": None}):
            self.api.profiles[0]["deviceList"] = [row]
            with self.subTest(row=row), self.assertRaises(signing.ProvisioningError):
                signing.selected_profile(self.api, CONFIG)
        self.api.profiles[0]["deviceList"] = [dict(original), dict(original)]
        with self.assertRaises(signing.ProvisioningError):
            signing.selected_profile(self.api, CONFIG)

    def test_profile_timeout_is_recovered_and_not_recreated(self):
        self.api.unknown = signing.PROFILE_PATH
        self.apply()
        self.apply()
        self.assertEqual(1, sum(path == signing.PROFILE_PATH for path, _, _ in self.api.writes))

    def test_profile_unknown_not_found_blocks_new_post_and_original_devices_are_retained(self):
        signing.sync_devices(self.api, self.requested, True, self.state)
        certificate, _ = signing.ensure_certificate(self.api, CONFIG, self.state, True)
        self.api.accept = False
        self.api.unknown = signing.PROFILE_PATH
        with self.assertRaises(signing.ProvisioningError):
            signing.ensure_profile(self.api, CONFIG, self.state, certificate, self.api.devices, True)
        with self.assertRaisesRegex(signing.ProvisioningError, "禁止重复写入"):
            signing.ensure_profile(self.api, CONFIG, self.state, certificate, self.api.devices, True)
        self.assertEqual(1, sum(path == signing.PROFILE_PATH for path, _, _ in self.api.writes))
        self.assertEqual(1, len(list(self.state.glob("profile-*-pending.json"))))

    def test_devices_pending_cannot_be_cleared_by_different_request_subset(self):
        self.api.accept = False
        self.api.unknown = signing.DEVICE_PATH
        with self.assertRaises(signing.ProvisioningError):
            signing.sync_devices(self.api, self.requested, True, self.state)
        with self.assertRaisesRegex(signing.ProvisioningError, "禁止重复 POST"):
            signing.sync_devices(self.api, [], True, self.state)
        self.assertTrue((self.state / "devices-pending.json").exists())

    def test_recovered_certificate_clears_pending_only_on_apply(self):
        self.apply()
        marker = self.state / "certificate-pending.json"
        signing.pending_write(marker, {"certName": CONFIG["certName"],
            "publicKeySha256": signing.hashlib.sha256(signing.public_key(signing.restricted(self.state / "release.key"))).hexdigest()})
        signing.ensure_certificate(self.api, CONFIG, self.state, False)
        self.assertTrue(marker.exists())
        signing.ensure_certificate(self.api, CONFIG, self.state, True)
        self.assertFalse(marker.exists())

    def test_plan_validates_existing_signed_profile_without_rewriting(self):
        self.apply()
        count = len(self.api.writes)
        self.api.contents[self.api.profiles[0]["provisionDownloadUrl"]] = self.api.signed_profile(self.api.devices, {"type": "debug"})
        with self.assertRaises(signing.ProvisioningError):
            signing.setup(CONFIG, self.api, self.state, self.output, self.requested)
        self.assertEqual(count, len(self.api.writes))

    def test_profile_rejects_wrong_bundle_device_or_certificate_and_expiry(self):
        self.apply()
        cert = self.api.contents[self.api.certificates[0]["certDownloadUrl"]]
        good = self.api.signed_profile(self.api.devices)
        self.assertEqual("release", signing.validate_profile(good, CONFIG, cert, self.api.devices)["type"])
        for changes in ({"type": "debug"}, {"bundle-info": {"bundle-name": "wrong"}},
                        {"debug-info": {"device-id-type": "udid", "device-ids": [SECOND]}},
                        {"validity": {"not-before": 1, "not-after": 2}}, {"debug-info": {}}):
            with self.subTest(changes=changes), self.assertRaises(signing.ProvisioningError):
                signing.validate_profile(self.api.signed_profile(self.api.devices, changes), CONFIG, cert, self.api.devices)
        corrupt = bytearray(good)
        position = good.index(b"bundle-name")
        corrupt[position] ^= 1
        with self.assertRaises(signing.ProvisioningError):
            signing.validate_profile(bytes(corrupt), CONFIG, cert, self.api.devices)

    def test_full_pem_or_pkcs7_download_chain_survives_p12_export(self):
        self.apply()
        leaf = self.api.contents[self.api.certificates[0]["certDownloadUrl"]]
        ca = signing.restricted(self.root / "ca.pem")
        complete = signing.certificate_chain(leaf + ca)
        self.assertEqual(2, complete.count(b"-----BEGIN CERTIFICATE-----"))
        signing.save(self.root / "chain.pem", complete)
        pkcs7 = signing.openssl("crl2pkcs7", "-nocrl", "-certfile", self.root / "chain.pem", "-outform", "DER")
        self.assertEqual(complete, signing.certificate_chain(pkcs7))
        packed = signing.export_p12(self.state / "release.key", complete, "fixture-password")
        signing.save(self.root / "complete.p12", packed)
        exported = signing.openssl("pkcs12", "-in", self.root / "complete.p12", "-nokeys", "-passin", "stdin", data=b"fixture-password")
        self.assertEqual(2, exported.count(b"-----BEGIN CERTIFICATE-----"))
        with self.assertRaises(signing.ProvisioningError):
            signing.stable_p12(self.state, complete, "fixture-password")
        self.api.contents[self.api.certificates[0]["certDownloadUrl"]] = ca + leaf
        found = signing.matching_certificate(self.api, CONFIG, signing.public_key(signing.restricted(self.state / "release.key")))
        self.assertEqual(signing.certificate_public_key(leaf), signing.certificate_public_key(found[1]))
        self.assertEqual(2, found[1].count(b"-----BEGIN CERTIFICATE-----"))

    def test_device_command_checks_existing_signing_before_registration(self):
        with self.assertRaises(signing.ProvisioningError):
            self.apply(devices_only=True)
        self.assertEqual([], self.api.writes)

    def test_main_redacts_full_device_input_on_invalid_material(self):
        path = self.root / "devices.json"
        signing.save(path, json.dumps([{"deviceName": "fixture", "udid": UDID, "deviceType": 1}]).encode())
        root = {"_harmonyTeams": {"cihang": {"credentialNamespace": "harmony-cihang"}},
                "cihang": {"harmony": {"teamKey": "cihang", "appId": CONFIG["appId"], "bundleName": CONFIG["bundleName"],
                           "signing": {"certName": CONFIG["certName"], "profileName": CONFIG["profileName"]}}}}
        signing.save(self.root / "config.json", json.dumps(root).encode())
        stream = io.StringIO()
        with patch("sys.argv", ["harmony_provisioning.py", "plan", "--brand", "cihang", "--config", str(self.root / "config.json"),
                               "--state-dir", str(self.state), "--devices-file", str(path)]), contextlib.redirect_stderr(stream):
            self.assertEqual(1, signing.main())
        self.assertNotIn(UDID, stream.getvalue())


if __name__ == "__main__":
    unittest.main()
