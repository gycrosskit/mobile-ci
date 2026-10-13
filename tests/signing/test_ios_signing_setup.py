import base64
import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from signing import ios_signing_setup as signing


TEAM = "Z4BU3HH62Y"


def certificate(key, team=TEAM):
    return signing.openssl("req", "-new", "-x509", "-key", "/dev/stdin", "-days", "90",
                           "-subj", "/CN=Apple Distribution Test/OU=" + team, "-outform", "DER", data=key)


class CertificateInfoTest(unittest.TestCase):
    def test_actual_openssl3_reads_legacy_rc2_without_changing_p12(self):
        if not signing.openssl("version").startswith(b'OpenSSL 3.'):
            self.skipTest('真实 RC2 provider 行为需要 OpenSSL 3；LibreSSL 不使用 -legacy')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = signing.openssl('genrsa', '2048')
            signing.save(root / 'key.pem', key)
            signing.save(root / 'cert.der', certificate(key))
            signing.save(root / 'cert.pem', signing.openssl('x509', '-inform', 'DER', '-in', root / 'cert.der', '-outform', 'PEM'))
            packed = signing.openssl('pkcs12', '-export', '-legacy', '-inkey', root / 'key.pem', '-in', root / 'cert.pem',
                                     '-certpbe', 'PBE-SHA1-RC2-40', '-keypbe', 'PBE-SHA1-3DES', '-passout', 'stdin', data=b'fixture-password')
            signing.save(root / 'legacy.p12', packed)
            _, identity = signing.p12_identity(root / 'legacy.p12', b'fixture-password')
            self.assertEqual(TEAM, identity['teamId'])
            self.assertEqual(signing.public_key(key), identity['publicKey'])
            self.assertEqual(packed, (root / 'legacy.p12').read_bytes())

    def test_only_openssl3_unsupported_rc2_p12_read_retries_with_legacy(self):
        unsupported = b'error:0308010C:digital envelope routines:inner_evp_generic_fetch:unsupported: Algorithm (RC2-40-CBC : 0)'
        for retry_exit in (0, 1):
            with self.subTest(retry_exit=retry_exit):
                results = [subprocess.CompletedProcess([], 1, b'', unsupported),
                           subprocess.CompletedProcess([], 0, b'OpenSSL 3.6.3\n', b''),
                           subprocess.CompletedProcess([], retry_exit, b'certificate', b'private error')]
                with patch.object(signing.subprocess, "run", side_effect=results) as run:
                    if retry_exit:
                        with self.assertRaises(signing.SigningError) as error:
                            signing.openssl("pkcs12", "-in", "existing.p12", "-nokeys", data=b'private-password')
                        self.assertNotIn('private', str(error.exception))
                    else:
                        self.assertEqual(b'certificate', signing.openssl("pkcs12", "-in", "existing.p12", "-nokeys", data=b'private-password'))
                    self.assertEqual(3, run.call_count)
                    self.assertEqual(["openssl", "version"], run.call_args_list[1].args[0])
                    self.assertEqual([*run.call_args_list[0].args[0], "-legacy"], run.call_args.args[0])
                    self.assertEqual(b'private-password', run.call_args.kwargs['input'])
        for command, error, version, calls in (
            (("pkcs12", "-in", "existing.p12"), b'Mac verify error: invalid password?', b'', 1),
            (("pkcs12", "-in", "existing.p12"), unsupported + b'; invalid password', b'', 1),
            (("pkcs12", "-in", "existing.p12"), unsupported, b'LibreSSL 3.3.6\n', 2),
            (("x509", "-in", "existing.p12"), unsupported, b'', 1),
            (("pkcs12", "-export", "-in", "certificate.pem"), unsupported, b'', 1),
            (("pkcs12", "-in", "existing.p12"), b'unsupported Algorithm (AES-256-CBC)', b'', 1),
            (("pkcs12", "-in", "existing.p12"), b'ASN1 decode error RC2', b'', 1),
        ):
            with self.subTest(command=command, error=error):
                results = [subprocess.CompletedProcess([], 1, b'', error),
                           subprocess.CompletedProcess([], 0, version, b'')]
                with patch.object(signing.subprocess, "run", side_effect=results) as run:
                    with self.assertRaises(signing.SigningError):
                        signing.openssl(*command)
                    self.assertEqual(calls, run.call_count)
                    self.assertFalse(any('-legacy' in call.args[0] for call in run.call_args_list))

    def test_team_is_parsed_with_openssl3_and_libressl_subject_prefixes(self):
        original = signing.openssl
        content = certificate(original("genrsa", "2048"))
        for prefix in ("subject=", "subject= "):
            with self.subTest(prefix=prefix):
                def output(*args, data=None):
                    if args[:3] == ("x509", "-noout", "-subject"):
                        return (prefix + "OU=" + TEAM + ",CN=Apple Distribution Test\n").encode()
                    return original(*args, data=data)
                with patch.object(signing, "openssl", side_effect=output):
                    info = signing.certificate_info(content)
                self.assertEqual(TEAM, info["teamId"])
                self.assertEqual(content, info["der"])
                self.assertGreater(info["expires"], datetime.now(timezone.utc))


class FakeApple:
    team_id = TEAM

    def __init__(self):
        self.certificates = []
        self.profiles = []
        self.devices = [{"id": "phone", "attributes": {"platform": "IOS", "status": "ENABLED", "udid": "DO-NOT-LOG-FULL-UDID"}},
                        {"id": "disabled", "attributes": {"platform": "IOS", "status": "DISABLED"}},
                        {"id": "mac", "attributes": {"platform": "MAC_OS", "status": "ENABLED"}}]
        self.writes = []
        self.fail_after_accept = None
        self.fail_before_accept = False
        self.interrupt_after_accept = False

    def all(self, path):
        if path.startswith("certificates"):
            return self.certificates[:]
        if path.startswith("bundleIds"):
            return [{"id": "bundle", "attributes": {"identifier": "com.example.app", "platform": "IOS"}}]
        if path.startswith("devices"):
            return self.devices[:]
        if path.startswith("profiles?"):
            return self.profiles[:]
        _, identity, _, relationship = path.split("/")
        profile = next(p for p in self.profiles if p["id"] == identity)
        data = profile["relationships"].get(relationship, {"data": []})["data"]
        return data if isinstance(data, list) else [data]

    def request(self, path, body=None):
        self.writes.append((path, body))
        if self.fail_before_accept:
            raise signing.UnknownWrite("connection interrupted")
        resource = body["data"]
        if path == "certificates":
            csr = resource["attributes"]["csrContent"].encode()
            # 用测试 CA 签真实 CSR，覆盖系统 OpenSSL 的 P12 生成/读取。
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                ca_key = signing.openssl("genrsa", "2048")
                signing.save(root / "ca.key", ca_key)
                signing.save(root / "ca.pem", signing.openssl("req", "-new", "-x509", "-key", "/dev/stdin",
                                                             "-days", "90", "-subj", "/CN=Fixture CA", data=ca_key))
                content = signing.openssl("x509", "-req", "-CA", root / "ca.pem", "-CAkey", root / "ca.key",
                                          "-set_serial", "1", "-days", "90", "-outform", "DER", data=csr)
            row = {"id": "cert", "attributes": {"certificateType": "DISTRIBUTION", "certificateContent": base64.b64encode(content).decode()}}
            self.certificates.append(row)
        else:
            row = {"id": "profile-" + str(len(self.profiles)), "attributes": dict(resource["attributes"]),
                   "relationships": resource["relationships"]}
            row["attributes"].update(profileState="ACTIVE", profileContent=base64.b64encode(b"fixture mobileprovision").decode(),
                                      expirationDate=(datetime.now(timezone.utc) + timedelta(days=60)).isoformat())
            self.profiles.append(row)
        if self.interrupt_after_accept:
            raise KeyboardInterrupt()
        if self.fail_after_accept == path:
            raise signing.UnknownWrite("response lost")
        return {"data": row}


class SigningSetupTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output, self.state = self.root / "output", self.root / "state"
        self.output.mkdir()
        self.state.mkdir()
        self.config = {"brand": "test", "team": "cihang", "teamId": TEAM, "bundleId": "com.example.app",
                       "enabled": False, "profileNames": {"pgyer": "test-adhoc", "testflight": "test-appstore"}}
        self.api = FakeApple()

    def apply(self):
        return signing.setup(self.config, self.api, self.output, self.state, apply=True)

    def test_plan_never_writes_or_generates_key(self):
        report = signing.setup(self.config, self.api, self.output, self.state)
        self.assertEqual("create-needed", report["certificate"])
        self.assertFalse(report["publishingEnabled"])
        self.assertEqual([], self.api.writes)
        self.assertEqual([], list(self.state.iterdir()))
        self.assertEqual([], list(self.output.iterdir()))
        self.assertNotIn("DO-NOT-LOG", json.dumps(report))

    def test_apply_repeats_without_certificate_or_profile_writes(self):
        report = self.apply()
        packed = (self.output / "distribution.p12").read_bytes()
        # 真实导出的证书和私钥均用 AES-256-CBC，MAC 用 SHA-256；不依赖平台默认 PBE。
        self.assertEqual(2, packed.count(bytes.fromhex('060960864801650304012a')))
        self.assertIn(bytes.fromhex('0609608648016503040201'), packed)
        self.assertNotIn(bytes.fromhex('060a2a864886f70d010c0106'), packed)
        password = (self.output / "distribution-p12.password").read_bytes()
        self.assertTrue(report["p12Verified"])
        self.assertEqual(["certificates", "profiles", "profiles"], [path for path, _ in self.api.writes])
        self.assertFalse((self.state / "distribution.key").exists())
        report = self.apply()
        self.assertEqual("reuse", report["certificate"])
        self.assertEqual(3, len(self.api.writes))
        self.assertEqual(packed, (self.output / "distribution.p12").read_bytes())
        self.assertEqual(password, (self.output / "distribution-p12.password").read_bytes())
        for path in self.output.iterdir():
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual([{"type": "devices", "id": "phone"}], self.api.profiles[0]["relationships"]["devices"]["data"])
        self.assertEqual({"distribution.p12", "distribution-p12.password"}, {path.name for path in self.output.iterdir()})
        self.assertTrue((self.state / "test-pgyer.mobileprovision").exists())
        self.assertTrue((self.state / "test-summary.json").exists())

    def test_existing_input_is_reused_and_copy_interruption_recovers(self):
        self.apply()
        source, password = self.output / "distribution.p12", self.output / "distribution-p12.password"
        output, state = self.root / "external", self.root / "external-state"
        output.mkdir()
        state.mkdir()
        writes = len(self.api.writes)
        signing.setup(self.config, self.api, output, state, p12_source=source, password_source=password)
        self.assertFalse(list(output.iterdir()))
        original = signing.save
        def interrupted_save(path, content):
            if Path(path) == output / "distribution.p12":
                raise OSError("simulated copy interruption")
            return original(path, content)
        with patch.object(signing, "save", side_effect=interrupted_save):
            with self.assertRaises(OSError):
                signing.setup(self.config, self.api, output, state, True, p12_source=source, password_source=password)
        self.assertTrue((output / "distribution-p12.password").exists())
        for _ in range(2):
            signing.setup(self.config, self.api, output, state, True, p12_source=source, password_source=password)
        self.assertEqual(source.read_bytes(), (output / "distribution.p12").read_bytes())
        self.assertEqual(writes, len(self.api.writes))
        signing.save(output / "distribution-p12.password", b"conflict\n")
        with self.assertRaisesRegex(signing.SigningError, "冲突"):
            signing.setup(self.config, self.api, output, state, True, p12_source=source, password_source=password)
        self.assertEqual(writes, len(self.api.writes))

    def test_missing_or_wrong_password_stops_before_api_write(self):
        self.apply()
        writes = len(self.api.writes)
        password = self.output / "distribution-p12.password"
        password.unlink()
        with self.assertRaisesRegex(signing.SigningError, "缺少密码"):
            self.apply()
        signing.save(password, b"wrong\n")
        with self.assertRaisesRegex(signing.SigningError, "OpenSSL"):
            self.apply()
        self.assertEqual(writes, len(self.api.writes))

    def test_password_without_p12_stops_before_new_key_or_api_write(self):
        signing.save(self.output / "distribution-p12.password", b"retained-password")
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaisesRegex(signing.SigningError, "缺少 P12"):
                signing.setup(self.config, self.api, self.output, self.state, apply=apply)
        self.assertFalse((self.state / "distribution.key").exists())
        self.assertEqual([], self.api.writes)

    def test_certificate_response_lost_is_recovered_by_public_key(self):
        self.api.fail_after_accept = "certificates"
        self.assertTrue(self.apply()["p12Verified"])
        self.assertEqual(1, sum(path == "certificates" for path, _ in self.api.writes))
        self.apply()
        self.assertEqual(1, sum(path == "certificates" for path, _ in self.api.writes))

    def test_hard_interrupt_after_certificate_accept_recovers_next_run(self):
        self.api.interrupt_after_accept = True
        with self.assertRaises(KeyboardInterrupt):
            self.apply()
        key = (self.state / "distribution.key").read_bytes()
        self.assertTrue((self.state / "certificate-write.json").exists())
        self.api.interrupt_after_accept = False
        self.apply()
        restored, _ = signing.p12_identity(self.output / "distribution.p12", (self.output / "distribution-p12.password").read_bytes())
        self.assertEqual(signing.public_key(key), signing.public_key(restored))
        self.assertEqual(1, sum(path == "certificates" for path, _ in self.api.writes))

    def test_unknown_write_never_blindly_reposts(self):
        self.api.fail_before_accept = True
        with self.assertRaisesRegex(signing.SigningError, "结果未知"):
            self.apply()
        key = (self.state / "distribution.key").read_bytes()
        self.api.fail_before_accept = False
        with self.assertRaisesRegex(signing.SigningError, "上次 Apple 写入结果未知"):
            self.apply()
        self.assertEqual(1, len(self.api.writes))
        self.assertEqual(key, (self.state / "distribution.key").read_bytes())

    def test_unknown_certificate_write_without_original_key_stops_before_regeneration(self):
        self.api.interrupt_after_accept = True
        with self.assertRaises(KeyboardInterrupt):
            self.apply()
        (self.state / "distribution.key").unlink()
        marker = self.state / "certificate-write.json"
        original_marker = marker.read_bytes()
        self.api.interrupt_after_accept = False
        for apply in (False, True):
            with self.subTest(apply=apply), patch.object(self.api, "all") as query, patch.object(signing, "openssl") as openssl:
                with self.assertRaisesRegex(signing.SigningError, "原私钥/P12 已缺失"):
                    signing.setup(self.config, self.api, self.output, self.state, apply=apply)
                query.assert_not_called()
                openssl.assert_not_called()
                self.assertFalse((self.state / "distribution.key").exists())
                self.assertEqual(original_marker, marker.read_bytes())
                self.assertFalse(list(self.output.iterdir()))
                self.assertEqual(1, len(self.api.writes))

    def test_profile_response_lost_does_not_recreate(self):
        self.api.fail_after_accept = "profiles"
        self.apply()
        self.apply()
        self.assertEqual(2, len(self.api.profiles))
        self.assertEqual(3, len(self.api.writes))

    def test_p12_failure_preserves_key_and_restores_without_new_certificate(self):
        original = signing.p12_identity
        with patch.object(signing, "p12_identity", side_effect=signing.SigningError("fixture packing failure")):
            with self.assertRaisesRegex(signing.SigningError, "packing failure"):
                self.apply()
        self.assertTrue((self.state / "distribution.key").exists())
        self.assertTrue((self.state / "certificate-write.json").exists())
        self.assertFalse((self.output / "distribution.p12").exists())
        self.apply()
        original(self.output / "distribution.p12", (self.output / "distribution-p12.password").read_bytes())
        self.assertEqual(1, sum(path == "certificates" for path, _ in self.api.writes))

    def test_profile_conflicts_stop_without_any_write(self):
        for relationship in ("bundleId", "certificates", "devices"):
            with self.subTest(relationship=relationship):
                self.setUp()
                self.apply()
                writes = len(self.api.writes)
                value = {"type": "wrong", "id": "wrong"}
                self.api.profiles[0]["relationships"][relationship]["data"] = value if relationship == "bundleId" else [value]
                with self.assertRaisesRegex(signing.SigningError, "集合冲突"):
                    self.apply()
                self.assertEqual(writes, len(self.api.writes))

    def test_existing_profile_without_original_identity_stops_before_new_key_or_write(self):
        self.apply()
        (self.output / "distribution.p12").unlink()
        (self.output / "distribution-p12.password").unlink()
        original_profiles = json.loads(json.dumps(self.api.profiles))
        original_state = {path.name: path.read_bytes() for path in self.state.iterdir()}
        writes = len(self.api.writes)
        for apply in (False, True):
            with self.subTest(apply=apply), patch.object(signing, "openssl", wraps=signing.openssl) as openssl, patch.object(self.api, "request") as write:
                with self.assertRaisesRegex(signing.SigningError, "先恢复原 P12"):
                    signing.setup(self.config, self.api, self.output, self.state, apply=apply)
                write.assert_not_called()
                self.assertFalse(any(call.args[0] == "genrsa" for call in openssl.call_args_list))
                self.assertFalse((self.state / "distribution.key").exists())
                self.assertFalse(list(self.output.iterdir()))
                self.assertEqual(original_state, {path.name: path.read_bytes() for path in self.state.iterdir()})
                self.assertEqual(original_profiles, self.api.profiles)
                self.assertEqual(writes, len(self.api.writes))

    def test_duplicate_profile_and_wrong_type_or_expiration_are_blocked(self):
        self.apply()
        profile = self.api.profiles[0]
        for attribute, value in (("profileType", "IOS_APP_STORE"), ("profileState", "INVALID"), ("expirationDate", "2000-01-01T00:00:00Z")):
            previous = profile["attributes"][attribute]
            profile["attributes"][attribute] = value
            with self.assertRaises(signing.SigningError):
                self.apply()
            profile["attributes"][attribute] = previous
        self.api.profiles.append(profile)
        with self.assertRaisesRegex(signing.SigningError, "多个同名"):
            self.apply()
        self.assertEqual(3, len(self.api.writes))

    def test_near_expiration_warns_without_renewal(self):
        self.apply()
        self.api.profiles[0]["attributes"]["expirationDate"] = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        report = self.apply()
        self.assertTrue(report["warnings"])
        self.assertEqual(3, len(self.api.writes))

    def test_wrong_team_and_revoked_local_certificate_stop(self):
        key = signing.openssl("genrsa", "2048")
        content = certificate(key, "WRONGTEAM1")
        self.api.certificates = [{"id": "wrong", "attributes": {"certificateType": "DISTRIBUTION", "certificateContent": base64.b64encode(content).decode()}}]
        with self.assertRaisesRegex(signing.SigningError, "Team"):
            self.apply()
        self.assertFalse(self.api.writes)
        self.api.certificates = []
        self.apply()
        self.api.certificates = []
        with self.assertRaisesRegex(signing.SigningError, "吊销"):
            self.apply()
        self.assertEqual(3, len(self.api.writes))

    def test_enabled_ios_devices_required_only_for_adhoc(self):
        self.api.devices = []
        with self.assertRaisesRegex(signing.SigningError, "ENABLE[D]"):
            self.apply()
        self.assertFalse(self.api.writes)
        self.config["profileNames"] = {"testflight": "store"}
        self.apply()
        self.assertEqual(2, len(self.api.writes))

    def test_confirmed_disabled_brand_is_allowed_but_missing_team_is_blocked(self):
        path = self.root / "brands.json"
        value = {"_iosTeams": {"cihang": {"teamId": TEAM, "repositorySigningPath": "signing/ios/cihang"}},
                 "test": {"ios": {"bundleId": "com.example.app", "signing": {"test": {
                     "enabled": False, "team": "cihang", "profileNames": {"pgyer": "adhoc"}}}}}}
        path.write_text(json.dumps(value))
        config = signing.load_brand(path, "test", ["pgyer"])
        self.assertFalse(config["enabled"])
        with self.assertRaisesRegex(signing.SigningError, "渠道"):
            signing.load_brand(path, "test", ["testflight"])
        del value["test"]["ios"]["signing"]["test"]["team"]
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(signing.SigningError, "禁止猜测"):
            signing.load_brand(path, "test", ["pgyer"])

    def test_secret_permissions_and_symlinks_are_rejected(self):
        path = self.root / "key"
        path.write_bytes(b"secret")
        path.chmod(0o644)
        with self.assertRaises(signing.SigningError):
            signing.restricted(path)
        path.chmod(0o600)
        symlink = self.root / "symlink"
        symlink.symlink_to(path)
        with self.assertRaises(signing.SigningError):
            signing.restricted(symlink)
        with self.assertRaises(signing.SigningError):
            signing.save(symlink, b"other")
        self.assertEqual(b"secret", path.read_bytes())

    def test_team_lock_prevents_concurrent_apply(self):
        with signing.locked_directories(self.output, self.state):
            with self.assertRaisesRegex(signing.SigningError, "正在执行"):
                with signing.locked_directories(self.output, self.state):
                    self.fail("lock acquired twice")

    def test_default_command_is_readonly_plan(self):
        api = FakeApple()
        with patch.object(signing, "load_brand", return_value=self.config), patch.object(signing, "AppleAPI", return_value=api):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                result = signing.main(["--brand", "test", "--output-dir", str(self.root / "absent"), "--state-dir", str(self.root / "no-state")])
        self.assertEqual(0, result)
        self.assertEqual("plan", json.loads(stdout.getvalue())["mode"])
        self.assertFalse((self.root / "absent").exists())
        self.assertFalse((self.root / "no-state").exists())
        self.assertFalse(api.writes)


class AppleTransportTest(unittest.TestCase):
    def test_http_errors_are_redacted_and_5xx_writes_are_unknown(self):
        api = object.__new__(signing.AppleAPI)
        api.key_path, api.key_id, api.issuer_id = Path("unused"), "key", "issuer"
        signature = b"\x30\x06\x02\x01\x01\x02\x01\x02"
        for status, body, expected in ((403, None, signing.SigningError), (503, {}, signing.UnknownWrite),
                                       (503, None, signing.SigningError)):
            error = urllib.error.HTTPError("https://api.appstoreconnect.apple.com/v1/profiles", status, "DO-NOT-LOG-SECRET", {}, io.BytesIO(b"SECRET-RESPONSE"))
            with patch.object(signing, "openssl", return_value=signature), patch.object(signing.urllib.request, "build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(expected) as caught:
                    api.request("profiles", body)
                self.assertNotIn("SECRET", str(caught.exception))

    def test_delete_204_preserves_default_get_post_transport(self):
        from unittest.mock import MagicMock
        api = object.__new__(signing.AppleAPI)
        api.key_path, api.key_id, api.issuer_id = Path("unused"), "key", "issuer"
        signature = b"\x30\x06\x02\x01\x01\x02\x01\x02"
        with patch.object(signing, "openssl", return_value=signature), patch.object(signing.urllib.request, "build_opener") as build:
            response = MagicMock(status=204)
            response.__enter__.return_value = response
            build.return_value.open.return_value = response
            self.assertEqual({}, api.request("profiles/old-id", method="DELETE"))
            self.assertEqual("DELETE", build.return_value.open.call_args.args[0].get_method())
            response.read.return_value = b'{"data":[]}'
            response.status = 200
            self.assertEqual({"data": []}, api.request("profiles"))
            self.assertEqual("GET", build.return_value.open.call_args.args[0].get_method())
            self.assertEqual({"data": []}, api.request("devices", {"data": {}}))
            self.assertEqual("POST", build.return_value.open.call_args.args[0].get_method())
            build.return_value.open.side_effect = OSError("private-response")
            with self.assertRaises(signing.UnknownWrite):
                api.request("profiles/old-id", method="DELETE")

    def test_redirect_is_refused_without_following_other_host(self):
        with self.assertRaisesRegex(signing.SigningError, "重定向"):
            signing.NoRedirect().redirect_request(None, None, 302, "moved", {}, "https://other.example")

    def test_pagination_visits_every_page_and_rejects_cycles(self):
        api = object.__new__(signing.AppleAPI)
        with patch.object(api, "request", side_effect=[{"data": [{"id": "one"}], "links": {"next": "page2"}},
                                                       {"data": [{"id": "two"}], "links": {}}]) as request:
            self.assertEqual(["one", "two"], [row["id"] for row in api.all("page1")])
            self.assertEqual(2, request.call_count)
        with patch.object(api, "request", return_value={"data": [], "links": {"next": "same"}}):
            with self.assertRaisesRegex(signing.SigningError, "循环"):
                api.all("same")

    def test_non_apple_next_link_rejected_before_token_is_signed(self):
        api = object.__new__(signing.AppleAPI)
        with patch.object(signing, "openssl") as openssl:
            with self.assertRaisesRegex(signing.SigningError, "非 Apple"):
                api.request("https://example.com/v1/certificates")
            openssl.assert_not_called()

    def test_es256_jwt_signature_verifies_using_openssl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = signing.openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout")
            signing.save(root / "key.pem", key)
            signing.save(root / "pub.pem", signing.openssl("pkey", "-pubout", data=key))
            payload = b"jwt header.payload"
            der = signing.openssl("dgst", "-sha256", "-sign", root / "key.pem", data=payload)
            raw = signing.jwt_signature(der)
            self.assertEqual(64, len(raw))
            integers = []
            for chunk in (raw[:32], raw[32:]):
                encoded = chunk.lstrip(b"\0") or b"\0"
                if encoded[0] & 128:
                    encoded = b"\0" + encoded
                integers.append(b"\x02" + bytes([len(encoded)]) + encoded)
            body = b"".join(integers)
            signing.save(root / "signature.der", b"\x30" + bytes([len(body)]) + body)
            self.assertIn(b"Verified OK", signing.openssl("dgst", "-sha256", "-verify", root / "pub.pem", "-signature", root / "signature.der", data=payload))


if __name__ == "__main__":
    unittest.main()
