import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from signing import ios_verify_signature as verifier


class IosSignatureTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = datetime.now(timezone.utc)
        self.ios = SimpleNamespace(key="another", team_id="OTHER12345", bundle_id="com.example.another",
                                   profile_name="another-adhoc", distribution="adhoc", pipeline="test",
                                   environment="dev", ui_engine="cmp")
        self.info = {
            "CFBundleIdentifier": self.ios.bundle_id, "CFBundleShortVersionString": "2.0.1",
            "CFBundleVersion": "20261009123456", "SkillCureBuildType": "release",
            "SkillCureIsDebug": False, "SkillCureDefaultApiEnvironment": "test",
            "SkillCureDebugToolsEnabled": True, "SkillCureSdkProduction": False,
        }
        self.entitlements = {
            "application-identifier": self.ios.team_id + "." + self.ios.bundle_id,
            "com.apple.developer.team-identifier": self.ios.team_id,
            "get-task-allow": False,
            "com.apple.developer.associated-domains": ["applinks:example.com"],
        }
        self.der = b"fake-signer-public-der"
        self.profile = {
            "Name": self.ios.profile_name, "TeamIdentifier": [self.ios.team_id],
            "DeveloperCertificates": [self.der], "ApplicationIdentifierPrefix": [self.ios.team_id],
            "CreationDate": (self.now - timedelta(days=1)).replace(tzinfo=None),
            "ExpirationDate": (self.now + timedelta(days=100)).replace(tzinfo=None),
            "ProvisionedDevices": ["device-identifier-must-not-appear"],
            "Entitlements": self.entitlements | {"com.apple.developer.associated-domains": "*"},
        }
        self.args = SimpleNamespace(
            config=self.root / "brands.json", brand="another", pipeline="test", channel="pgyer",
            ui_engine="cmp", ipa=self.root / "app.ipa", p12=self.root / "distribution.p12",
            password_file=self.root / "password", output=self.root / "report.json",
            expected_version="2.0.1", expected_build="20261009123456",
        )
        self.args.p12.write_bytes(b"inert test p12")
        self.args.password_file.write_text("password-must-not-appear")
        self.args.config.write_text("{}")
        self.write_ipa()
        self.commands, self.temporary_paths = [], []
        self.failure = None
        self.p12_der = self.der
        self.certificate_expires = self.now + timedelta(days=100)

    def write_ipa(self, extras=()):
        with zipfile.ZipFile(self.args.ipa, "w") as archive:
            archive.writestr("Payload/Example.app/Info.plist", plistlib.dumps(self.info))
            for name, content in extras:
                archive.writestr(name, content)

    def fake_native(self, command, **kwargs):
        self.commands.append(command)
        stdout, stderr = b"", b""
        if command[0].endswith("ditto"):
            dest = Path(command[-1])
            self.temporary_paths.append(dest.parent)
            self.assertEqual(0o700, stat.S_IMODE(dest.parent.stat().st_mode))
            app = dest / "Payload/Example.app"
            app.mkdir(parents=True)
            (app / "Info.plist").write_bytes(plistlib.dumps(self.info))
        elif command[0].endswith("codesign"):
            if "--verify" in command:
                if self.failure == "codesign":
                    return subprocess.CompletedProcess(command, 1, b"", b"device-identifier-must-not-appear")
            elif "--verbose=4" in command:
                stderr = ("Identifier=" + self.ios.bundle_id + "\nTeamIdentifier=" + self.ios.team_id + "\n").encode()
            elif any(arg.startswith("--extract-certificates=") for arg in command):
                prefix = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--extract-certificates="))
                Path(prefix + "0").write_bytes(self.der)
            elif "--entitlements" in command:
                stdout = plistlib.dumps(self.entitlements)
        elif command[1] == "pkcs12":
            self.assertIn("-nokeys", command)
            self.assertEqual("file:" + str(self.args.password_file.resolve()), command[command.index("-passin") + 1])
            stdout = b"-----BEGIN CERTIFICATE-----\n" + base64.b64encode(self.p12_der) + b"\n-----END CERTIFICATE-----\n"
        elif command[1] == "x509":
            cert = Path(command[command.index("-in") + 1])
            self.assertEqual(0o600, stat.S_IMODE(cert.stat().st_mode))
            stdout = ("notBefore=" + (self.now - timedelta(days=10)).strftime("%b %d %H:%M:%S %Y GMT")
                      + "\nnotAfter=" + self.certificate_expires.strftime("%b %d %H:%M:%S %Y GMT")
                      + "\nsubject=CN=Apple Distribution: Example,OU=" + self.ios.team_id + ",O=Example\n").encode()
        elif command[0].endswith("security"):
            stdout = plistlib.dumps(self.profile)
        else:
            self.fail("Unexpected command")
        return subprocess.CompletedProcess(command, 0, stdout, stderr)

    def verify(self):
        with patch.object(verifier, "load_ios_brand_config", return_value=self.ios), \
                patch.object(verifier.subprocess, "run", side_effect=self.fake_native):
            return verifier.verify_signature(self.args)

    def validate(self, profile=None, entitlements=None, info=None):
        return verifier.validate_profile(info or self.info, entitlements or self.entitlements,
                                         profile or self.profile, self.ios, self.der, self.now,
                                         self.args.expected_version, self.args.expected_build)

    def test_complete_validation_reuses_brand_settings_and_reports_no_secrets(self):
        report = self.verify()
        self.assertTrue(report["verified"])
        self.assertEqual("another", report["brand"])
        self.assertEqual(hashlib.sha256(self.args.ipa.read_bytes()).hexdigest(), report["sha256"])
        self.assertEqual(1, report["registeredDeviceCount"])
        encoded = json.dumps(report) + str(self.commands)
        self.assertNotIn("device-identifier-must-not-appear", encoded)
        self.assertNotIn("password-must-not-appear", encoded)
        self.assertNotIn("PRIVATE KEY", encoded)
        self.assertTrue(all(not p.exists() for p in self.temporary_paths))

    def test_cms_uses_explicit_temporary_keychain_only_when_provided(self):
        keychain = self.root / "temporary keychain-db"
        keychain.touch()
        for explicit in (False, True):
            with self.subTest(explicit=explicit), patch.dict(os.environ):
                os.environ.pop("IOS_KEYCHAIN_PATH", None)
                if explicit:
                    os.environ["IOS_KEYCHAIN_PATH"] = str(keychain)
                self.commands.clear()
                self.assertTrue(self.verify()["verified"])
                cms = next(command for command in self.commands if command[0].endswith("security"))
                self.assertEqual(explicit, "-k" in cms)
                if explicit:
                    self.assertEqual(str(keychain), cms[cms.index("-k") + 1])

    def test_explicit_missing_keychain_stops_cms_decode(self):
        with patch.dict(os.environ, IOS_KEYCHAIN_PATH=str(self.root / "missing.keychain-db")):
            with self.assertRaisesRegex(verifier.SignatureError, "钥匙串"):
                self.verify()
        self.assertFalse(any(command[0].endswith("security") for command in self.commands))

    def test_codesign_failure_stops_before_reading_p12_and_cleans_temp(self):
        self.failure = "codesign"
        with self.assertRaisesRegex(verifier.SignatureError, "deep strict"):
            self.verify()
        self.assertFalse(any("pkcs12" in command for command in self.commands))
        self.assertTrue(all(not p.exists() for p in self.temporary_paths))

    def test_signer_mismatch_blocks_report_and_cleans_temp(self):
        self.p12_der = b"different-public-cert"
        with self.assertRaisesRegex(verifier.SignatureError, "P12"):
            self.verify()
        self.assertFalse(self.args.output.exists())
        self.assertTrue(all(not p.exists() for p in self.temporary_paths))

    def test_expired_certificate_is_rejected(self):
        self.certificate_expires = self.now - timedelta(seconds=1)
        with self.assertRaisesRegex(verifier.SignatureError, "证书尚未生效或已过期"):
            self.verify()

    def test_expiry_warning_does_not_renew_or_block_valid_signature(self):
        self.certificate_expires = self.now + timedelta(days=5)
        self.profile["ExpirationDate"] = (self.now + timedelta(days=4)).replace(tzinfo=None)
        report = self.verify()
        self.assertEqual(["签名证书将在 30 天内过期", "Profile将在 30 天内过期"], report["expiryWarnings"])

    def test_report_cannot_overwrite_any_input(self):
        for path in (self.args.ipa, self.args.p12, self.args.password_file, self.args.config):
            with self.subTest(path=path):
                self.args.output = path
                with self.assertRaisesRegex(verifier.SignatureError, "不能覆盖"):
                    self.verify()
        self.assertEqual([], self.commands)

    def test_cli_failure_does_not_disclose_native_output_or_create_report(self):
        self.failure = "codesign"
        output = io.StringIO()
        with patch.object(verifier, "load_ios_brand_config", return_value=self.ios), \
                patch.object(verifier.subprocess, "run", side_effect=self.fake_native), \
                contextlib.redirect_stderr(output):
            result = verifier.main(self.cli())
        self.assertEqual(1, result)
        self.assertNotIn("device-identifier", output.getvalue())
        self.assertFalse(self.args.output.exists())

    def cli(self):
        return ["--brand", "another", "--pipeline", "test", "--channel", "pgyer",
                "--ipa", str(self.args.ipa), "--p12", str(self.args.p12),
                "--password-file", str(self.args.password_file), "--output", str(self.args.output),
                "--expected-version", "2.0.1", "--expected-build-number", "20261009123456"]

    def test_cli_writes_private_report_only_after_success(self):
        with patch.object(verifier, "load_ios_brand_config", return_value=self.ios), \
                patch.object(verifier.subprocess, "run", side_effect=self.fake_native), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, verifier.main(self.cli()))
        self.assertTrue(json.loads(self.args.output.read_text())["verified"])
        self.assertEqual(0o600, stat.S_IMODE(self.args.output.stat().st_mode))
        self.assertEqual([], list(self.root.glob(".signature-report-*")))

    def test_root_app_must_be_unique_even_without_second_info_plist(self):
        self.write_ipa((("Payload/Second.app/executable", b"binary"),))
        with self.assertRaisesRegex(verifier.SignatureError, "唯一"):
            self.verify()
        self.assertEqual([], self.commands)

    def test_apple_zip_chinese_filename_without_utf8_flag_matches_ditto(self):
        with zipfile.ZipFile(self.args.ipa, "w") as archive:
            archive.writestr("Payload/慈航中医.app/Info.plist", plistlib.dumps(self.info))
        data = bytearray(self.args.ipa.read_bytes())
        central = data.index(b"PK\x01\x02")
        for offset in (6, central + 8):
            flags = int.from_bytes(data[offset:offset + 2], "little") & ~0x800
            data[offset:offset + 2] = flags.to_bytes(2, "little")
        self.args.ipa.write_bytes(data)
        self.assertEqual("Payload/慈航中医.app", verifier.validate_archive(self.args.ipa))

    def test_default_test_channel_report_is_pgyer(self):
        self.args.channel = ""
        self.assertEqual("pgyer", self.verify()["channel"])

    def test_unsafe_paths_and_duplicates_block_native_extraction(self):
        for name in ("../escape", "/escape", "Payload/../escape", "Payload\\escape",
                     "Payload/./escape", "Payload//escape", "Payload/Example.app/Info.plist"):
            with self.subTest(name=name):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.write_ipa(((name, b"unsafe"),))
                with self.assertRaises(verifier.SignatureError):
                    self.verify()
        self.assertEqual([], self.commands)

    def symlink(self, name):
        entry = zipfile.ZipInfo(name)
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        return entry

    def test_safe_framework_symlinks_are_accepted(self):
        self.write_ipa((("Payload/Example.app/Frameworks/F.framework/Versions/A/F", b"binary"),
                        (self.symlink("Payload/Example.app/Frameworks/F.framework/Versions/Current"), b"A"),
                        (self.symlink("Payload/Example.app/Frameworks/F.framework/F"), b"Versions/Current/F")))
        self.assertEqual("Payload/Example.app", verifier.validate_archive(self.args.ipa))

    def test_escaping_and_cyclic_symlinks_and_link_children_are_rejected(self):
        cases = (
            ((self.symlink("link"), b"/tmp/escape"),),
            ((self.symlink("link"), b"../escape"),),
            ((self.symlink("link"), b"link"),),
            ((self.symlink("link"), b"target"), ("link/child", b"overwrite")),
            ((self.symlink("deep/link"), b"../alias/../../escape"),
             (self.symlink("alias"), b"target")),
        )
        for extras in cases:
            with self.subTest(extras=extras):
                self.write_ipa(extras)
                with self.assertRaises(verifier.SignatureError):
                    self.verify()
        self.assertEqual([], self.commands)

    def test_invalid_identity_profile_certificate_entitlements_and_dates_fail(self):
        changes = (
            ("Name", "wrong-profile"), ("TeamIdentifier", ["WRONG12345"]),
            ("DeveloperCertificates", [b"other-cert"]), ("ProvisionsAllDevices", True),
            ("ProvisionedDevices", []), ("ApplicationIdentifierPrefix", ["WRONG12345"]),
            ("ExpirationDate", (self.now - timedelta(seconds=1)).replace(tzinfo=None)),
            ("CreationDate", (self.now + timedelta(seconds=1)).replace(tzinfo=None)),
        )
        for key, value in changes:
            with self.subTest(key=key), self.assertRaises(verifier.SignatureError):
                self.validate(profile=self.profile | {key: value})
        for key, value in (("get-task-allow", True), ("application-identifier", "wrong.app"),
                           ("com.apple.developer.team-identifier", "WRONG12345"),
                           ("unauthorized-capability", True),
                           ("com.apple.developer.associated-domains", [False])):
            with self.subTest(key=key), self.assertRaises(verifier.SignatureError):
                self.validate(entitlements=self.entitlements | {key: value})

    def test_all_signed_capabilities_must_fit_profile(self):
        profile = copy.deepcopy(self.profile)
        profile["Entitlements"]["com.apple.developer.associated-domains"] = ["applinks:approved.example"]
        with self.assertRaisesRegex(verifier.SignatureError, "超出"):
            self.validate(profile=profile)
        self.assertTrue(verifier.entitlement_allowed(["OTHER12345.com.example.another"], ["OTHER12345.*"]))
        self.assertFalse(verifier.entitlement_allowed("ab", "[ab]"))
        self.assertFalse(verifier.entitlement_allowed(True, 1))

    def test_appstore_profile_rejects_adhoc_enterprise_and_missing_beta_flag(self):
        self.ios.distribution = "appstore"
        profile = copy.deepcopy(self.profile)
        profile.pop("ProvisionedDevices")
        profile["Entitlements"]["beta-reports-active"] = True
        self.assertEqual(0, self.validate(profile=profile)[2])
        for patch_values in ({"ProvisionedDevices": []}, {"ProvisionsAllDevices": True},
                             {"Entitlements": self.profile["Entitlements"]}):
            with self.subTest(patch_values=patch_values), self.assertRaises(verifier.SignatureError):
                self.validate(profile=profile | patch_values)

    def test_version_build_and_bundle_mismatch_fail_but_empty_expected_version_is_allowed(self):
        for key in ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion"):
            with self.subTest(key=key), self.assertRaises(verifier.SignatureError):
                self.validate(info=self.info | {key: "wrong"})
        self.args.expected_version = ""
        self.assertEqual("another-version", self.validate(info=self.info | {"CFBundleShortVersionString": "another-version"})[0])

    def test_build_environment_and_ui_engine_are_checked(self):
        self.info["SkillCureDefaultApiEnvironment"] = "production"
        self.write_ipa()
        with self.assertRaises(verifier.IosReleaseError):
            self.verify()
        self.info["SkillCureDefaultApiEnvironment"] = "test"
        self.write_ipa()
        self.ios.ui_engine = "kuikly"
        with self.assertRaises(verifier.IosReleaseError):
            self.verify()

    def test_disabled_or_unknown_brand_never_reads_signing_inputs(self):
        with patch.object(verifier.subprocess, "run") as native:
            with self.assertRaises(verifier.IosReleaseError):
                verifier.verify_signature(self.args)
        native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
