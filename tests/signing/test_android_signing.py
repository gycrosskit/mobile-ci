import json
import os
import shutil
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from signing import android_signing as signing
import project_config


SHA256 = "a" * 64
CONFIG = dict(source="jenkins", keyAlias="release", certificateSha256=SHA256,
              credentials=dict(keystore="android-key-test-keystore", signingJson="android-key-test-signing-json"))


class AndroidSigningTest(unittest.TestCase):
    def test_native_project_wrapper_requires_current_jenkins_receipt_before_apk_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            keystore = repo / "keystore"; keystore.write_bytes(b"fixture key"); keystore.chmod(0o400)
            passwords = repo / "passwords"; passwords.write_text(json.dumps({"storePassword":"fixture", "keyPassword":"fixture"})); passwords.chmod(0o400)
            args = Namespace(output_dir=repo / "output", command=["mock-build"], brand="example", pipeline="test", ui_engine="native")
            fixture_profiles = Path(__file__).resolve().parents[1] / "delivery/fixtures/projects"
            environment = {"CI_PROJECT_ID":"example-mobile", "JOB_NAME":"", "JENKINS_URL":"https://fixture.invalid", "BUILD_NUMBER":"1", "ANDROID_SIGNING_SOURCE":"jenkins", "ANDROID_KEYSTORE_FILE":str(keystore), "ANDROID_SIGNING_CONFIG_FILE":str(passwords)}
            receipt = repo / "mobile/build/android-signing-source.properties"
            def execute(*unused_args, **unused_kwargs):
                apk = repo / "mobile/build/outputs/apk/debug/mobile-debug.apk"; apk.parent.mkdir(parents=True, exist_ok=True); apk.write_bytes(b"fixture apk")
                return subprocess.CompletedProcess(["mock-build"], 0)
            original = Path.cwd()
            try:
                os.chdir(repo)
                with patch.object(project_config, "PROJECTS_DIR", fixture_profiles), patch.dict(os.environ, environment), patch.object(signing, "inspect_certificate", return_value={"matchesExpectedSha256":True}), patch.object(signing.subprocess, "run", side_effect=execute), patch.object(signing, "verify_apk", return_value=SHA256) as verify, patch.object(signing, "checked_output", return_value="a" * 40):
                    with self.assertRaisesRegex(signing.AndroidSigningError, "来源证明"):
                        signing.build(args, CONFIG)
                    verify.assert_not_called(); self.assertFalse((args.output_dir / "android-signature-report.json").exists())
                    def with_receipt(*arguments, **keywords):
                        result = execute(*arguments, **keywords)
                        receipt.write_text("source=jenkins\nbrand=example\nkeystoreSha256=" + signing.hashlib.sha256(keystore.read_bytes()).hexdigest() + "\n")
                        return result
                    with patch.object(signing.subprocess, "run", side_effect=with_receipt):
                        signing.build(args, CONFIG)
                    report = json.loads((args.output_dir / "android-signature-report.json").read_text())
                    self.assertEqual(("native", "jenkins"), (report["uiEngine"], report["source"]))
                    self.assertEqual(repo / "mobile/build/outputs/apk/debug/mobile-debug.apk", Path(report["apk"]))
                    verify.assert_called_once()
            finally:
                os.chdir(original)

    def test_config_only_resolves_selected_brand_and_rejects_non_jenkins(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "brands.json"
            config.write_text(json.dumps(dict(jimin=dict(android=dict(signing=CONFIG)), broken={})))
            self.assertEqual(CONFIG, signing.load_signing(config, "jimin"))
            for brand in ("missing", "broken", "../jimin"):
                with self.subTest(brand=brand), self.assertRaises(signing.AndroidSigningError):
                    signing.load_signing(config, brand)
            config.write_text(json.dumps(dict(jimin=dict(android=dict(signing=CONFIG | dict(source="repository"))))))
            with self.assertRaises(signing.AndroidSigningError):
                signing.load_signing(config, "jimin")

    def test_apk_signature_must_have_exactly_one_matching_verified_signer(self):
        valid = f"Signer #1 certificate SHA-256 digest: {SHA256}\n"
        with patch.object(signing, "find_apksigner", return_value="apksigner"), \
                patch.object(signing, "checked_output", return_value=valid):
            self.assertEqual(SHA256, signing.verify_apk(Path("app.apk"), SHA256))
        for output in ("", valid + valid.replace("#1", "#2"), valid.replace(SHA256, "b" * 64)):
            with self.subTest(output=output), patch.object(signing, "find_apksigner", return_value="apksigner"), \
                    patch.object(signing, "checked_output", return_value=output), \
                    self.assertRaises(signing.AndroidSigningError):
                signing.verify_apk(Path("app.apk"), SHA256)

    def test_tool_error_does_not_leak_stderr_or_arguments(self):
        result = subprocess.CompletedProcess(["keytool"], 1, "", "super secret password")
        with patch.object(signing.subprocess, "run", return_value=result):
            with self.assertRaises(signing.AndroidSigningError) as raised:
                signing.checked_output(["keytool", "private argument"])
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("private argument", str(raised.exception))

    def test_java_properties_preserves_whitespace_unicode_and_metacharacters(self):
        value = " leading:=#!\\\n\r\t\f中😀 trailing "
        self.assertEqual("\\ leading\\:\\=\\#\\!\\\\\\n\\r\\t\\f\\u4e2d\\ud83d\\ude00\\ trailing\\ ",
                         signing.property_value(value))
        if not shutil.which("java"):
            self.skipTest("JDK unavailable")
        with tempfile.TemporaryDirectory() as directory:
            properties = Path(directory) / "config.properties"
            properties.write_text("password=" + signing.property_value(value), encoding="ascii")
            source = Path(directory) / "PropertiesCheck.java"
            source.write_text('''import java.nio.file.*; import java.util.*;
class PropertiesCheck { public static void main(String[] args) throws Exception {
    var properties = new Properties();
    try (var input = Files.newInputStream(Path.of(args[0]))) { properties.load(input); }
    System.out.print(properties.getProperty("password"));
} }''')
            result = subprocess.run(["java", str(source), str(properties)], capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(value, result.stdout.decode())

    def test_build_requires_jenkins_and_removes_stale_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "android-signature-report.json"
            report.write_text("stale")
            args = Namespace(output_dir=directory)
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(signing.AndroidSigningError):
                signing.build(args, CONFIG)
            self.assertFalse(report.exists())

    def test_build_removes_only_selected_apk_outputs_before_running_gradle(self):
        for engine, module in (("cmp", "app"), ("kuikly", "androidKuiklyApp")):
            for pipeline, build_type in (("test", "qa"), ("release", "release")):
                with self.subTest(engine=engine, pipeline=pipeline), tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    keystore = repo / "bound-keystore"
                    keystore.write_bytes(b"fixture private key")
                    keystore.chmod(0o400)
                    passwords = repo / "bound-passwords"
                    passwords.write_text(json.dumps(dict(storePassword="fixture-store", keyPassword="fixture-key")))
                    passwords.chmod(0o400)
                    selected = repo / module / "build/outputs/apk/jimin" / build_type
                    preserved = [repo / module / "build/outputs/apk/chonghe" / build_type,
                                 repo / module / "build/outputs/apk/jimin" / ("release" if build_type == "qa" else "qa"),
                                 repo / module / "build/intermediates/classes/jimin"]
                    for output in [selected, *preserved]:
                        output.mkdir(parents=True)
                        (output / "fixture.bin").write_bytes(b"preserved fixture")
                    args = Namespace(output_dir=repo / "build/ci", command=["build-command"],
                                     brand="jimin", pipeline=pipeline, ui_engine=engine)
                    environment = dict(JENKINS_URL="https://jenkins.invalid", BUILD_NUMBER="42", ANDROID_SIGNING_SOURCE="jenkins",
                                       ANDROID_KEYSTORE_FILE=str(keystore), ANDROID_SIGNING_CONFIG_FILE=str(passwords))

                    def failed_build(command, env, check):
                        self.assertFalse(selected.exists())
                        for output in preserved:
                            self.assertEqual(b"preserved fixture", (output / "fixture.bin").read_bytes())
                        return subprocess.CompletedProcess(command, 1)

                    before = Path.cwd()
                    try:
                        os.chdir(repo)
                        with patch.dict(os.environ, environment), \
                                patch.object(signing, "inspect_certificate", return_value=dict(matchesExpectedSha256=True)), \
                                patch.object(signing.subprocess, "run", side_effect=failed_build), \
                                self.assertRaisesRegex(signing.AndroidSigningError, "构建失败"):
                            signing.build(args, CONFIG)
                    finally:
                        os.chdir(before)

    def test_build_checks_receipt_and_cleans_temporary_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            keystore = repo / "bound-keystore"
            keystore.write_bytes(b"fixture private key")
            keystore.chmod(0o400)
            passwords = repo / "bound-passwords"
            passwords.write_text(json.dumps(dict(storePassword=" fixture:=中 ", keyPassword="fixture-password")))
            passwords.chmod(0o400)
            args = Namespace(output_dir=repo / "build/ci", command=["--", "build-command"],
                             brand="jimin", pipeline="test", ui_engine="kuikly")
            environment = dict(JENKINS_URL="https://jenkins.invalid", BUILD_NUMBER="42", ANDROID_SIGNING_SOURCE="jenkins",
                               ANDROID_KEYSTORE_FILE=str(keystore), ANDROID_SIGNING_CONFIG_FILE=str(passwords))
            temporary_files = []

            def execute(command, env, check):
                properties = Path(env["ANDROID_SIGNING_PROPERTIES"])
                temporary_files.append(properties)
                self.assertEqual(0o600, properties.stat().st_mode & 0o777)
                self.assertEqual("jimin", env["ANDROID_SIGNING_BRAND"])
                self.assertNotIn("ANDROID_STORE_PASSWORD", env)
                self.assertIn("-Dorg.gradle.configuration-cache=false", env["GRADLE_OPTS"])
                self.assertNotIn("fixture-password", " ".join(command))
                content = properties.read_text()
                self.assertIn("signing.jimin.storePassword=\\ fixture\\:\\=\\u4e2d\\ ", content)
                self.assertNotIn("signing.chonghe.", content)
                receipt = repo / "androidKuiklyApp/build/android-signing-source.properties"
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text(f"source=jenkins\nbrand=jimin\nkeystoreSha256={signing.hashlib.sha256(keystore.read_bytes()).hexdigest()}\n")
                apk = repo / "androidKuiklyApp/build/outputs/apk/jimin/qa/app.apk"
                apk.parent.mkdir(parents=True, exist_ok=True)
                apk.write_bytes(b"fixture apk")
                return subprocess.CompletedProcess(command, 0)

            before = Path.cwd()
            try:
                os.chdir(repo)
                with patch.dict(os.environ, environment), \
                        patch.object(signing, "inspect_certificate", return_value=dict(matchesExpectedSha256=True)), \
                        patch.object(signing.subprocess, "run", side_effect=execute), \
                        patch.object(signing, "verify_apk", return_value=SHA256), \
                        patch.object(signing, "checked_output", return_value="fixture-commit"):
                    signing.build(args, CONFIG)
                    report = json.loads((args.output_dir / "android-signature-report.json").read_text())
                    self.assertTrue(report["verified"])
                    self.assertEqual("jenkins", report["source"])
                    self.assertEqual("kuikly", report["uiEngine"])
                    self.assertNotIn("fixture-password", json.dumps(report))
                    receipt = repo / "androidKuiklyApp/build/android-signing-source.properties"
                    with patch.object(signing.subprocess, "run", side_effect=lambda *a, **kw: subprocess.CompletedProcess(a[0], 0)):
                        with self.assertRaisesRegex(signing.AndroidSigningError, "来源证明"):
                            signing.build(args, CONFIG)
                    self.assertFalse(receipt.exists())
                    self.assertFalse((args.output_dir / "android-signature-report.json").exists())
                    def wrong_receipt(command, env, check):
                        result = execute(command, env, check)
                        receipt.write_text("source=jenkins\nbrand=chonghe\nkeystoreSha256=wrong\n")
                        return result
                    with patch.object(signing.subprocess, "run", side_effect=wrong_receipt):
                        with self.assertRaisesRegex(signing.AndroidSigningError, "来源证明"):
                            signing.build(args, CONFIG)
                    self.assertFalse((args.output_dir / "android-signature-report.json").exists())
            finally:
                os.chdir(before)
            self.assertTrue(temporary_files)
            self.assertTrue(all(not path.exists() for path in temporary_files))


@unittest.skipUnless(shutil.which("keytool") and shutil.which("openssl"), "JDK/OpenSSL unavailable")
class AndroidCertificateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name)
        cls.keystore = cls.directory / "fixture.jks"
        cls.passwords = cls.directory / "fixture.json"
        cls.passwords.write_text(json.dumps(dict(storePassword="fixture-store", keyPassword="fixture-key")))
        environment = dict(os.environ, FIXTURE_STORE="fixture-store", FIXTURE_KEY="fixture-key")
        result = subprocess.run([
            "keytool", "-genkeypair", "-alias", "release", "-keystore", str(cls.keystore), "-storetype", "JKS",
            "-storepass:env", "FIXTURE_STORE", "-keypass:env", "FIXTURE_KEY", "-keyalg", "RSA", "-keysize", "2048",
            "-validity", "30", "-dname", "CN=登贵堂签名测试,O=Android signing fixture,C=CN",
        ], env=environment, text=True, capture_output=True)
        if result.returncode:
            raise AssertionError("keytool fixture creation failed")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_certificate_checks_private_key_and_public_metadata(self):
        certificate = signing.inspect_certificate(self.keystore, self.passwords, "release")
        self.assertEqual("valid", certificate["status"])
        self.assertIn("Android signing fixture", certificate["subject"])
        self.assertEqual(64, len(certificate["sha256"]))
        self.assertLess(certificate["startsAt"], certificate["expiresAt"])
        matching = signing.inspect_certificate(self.keystore, self.passwords, "release", certificate["sha256"])
        self.assertTrue(matching["matchesExpectedSha256"])
        mismatch = signing.inspect_certificate(self.keystore, self.passwords, "release", SHA256)
        self.assertEqual("mismatch", mismatch["status"])

    def test_incorrect_key_password_or_alias_are_not_signable(self):
        wrong = self.directory / "wrong.json"
        wrong.write_text(json.dumps(dict(storePassword="fixture-store", keyPassword="wrong-key")))
        for passwords, alias in ((wrong, "release"), (self.passwords, "missing")):
            with self.subTest(alias=alias), self.assertRaises(signing.AndroidSigningError):
                signing.inspect_certificate(self.keystore, passwords, alias)

    def test_chinese_subject_and_issuer_are_displayed_as_utf8(self):
        certificate = signing.inspect_certificate(self.keystore, self.passwords, "release")
        for field in ("subject", "issuer"):
            with self.subTest(field=field):
                self.assertIn("CN=登贵堂签名测试", certificate[field])
                self.assertNotIn("\\E7", certificate[field])

    def test_trusted_certificate_without_private_key_is_not_signable(self):
        certificate = signing.checked_output([
            "keytool", "-exportcert", "-rfc", "-alias", "release", "-keystore", str(self.keystore),
            "-storepass:env", "FIXTURE_STORE",
        ], environment=dict(os.environ, FIXTURE_STORE="fixture-store"))
        pem = self.directory / "fixture.pem"
        pem.write_text(certificate)
        trusted = self.directory / "trusted.jks"
        signing.checked_output([
            "keytool", "-importcert", "-noprompt", "-alias", "release", "-file", str(pem),
            "-keystore", str(trusted), "-storetype", "JKS", "-storepass:env", "FIXTURE_STORE",
        ], environment=dict(os.environ, FIXTURE_STORE="fixture-store"))
        with self.assertRaises(signing.AndroidSigningError):
            signing.inspect_certificate(trusted, self.passwords, "release")


if __name__ == "__main__":
    unittest.main()
