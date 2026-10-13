import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class IosWorkflowTest(unittest.TestCase):
    def test_profile_metadata_failure_cleans_decoded_profile_and_keychain(self):
        for field in ("UUID", "Name"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                tools, state = root / "bin", root / "state"
                tools.mkdir()
                state.mkdir(mode=0o700)
                script = root / "ios_signing.sh"
                script.write_text((ROOT / "signing/ios_signing.sh").read_text().replace(
                    "/usr/libexec/PlistBuddy", str(tools / "PlistBuddy")))
                (tools / "PlistBuddy").write_text(
                    '#!/bin/sh\n[ "$2" != "Print :$TEST_FAIL_FIELD" ] || exit 1\n'
                    'printf "fixture-uuid\\n"\n')
                (tools / "security").write_text(
                    '#!/bin/sh\ncase "$1" in\n'
                    'unlock-keychain) exit 0;;\n'
                    'cms) printf "%s\\n" "$TEST_PROFILE_CONTENT";;\n'
                    'delete-keychain) rm -f "$2";;\n'
                    '*) exit 2;;\nesac\n')
                for tool in tools.iterdir():
                    tool.chmod(0o755)
                for name in ("jenkins-ios.keychain-db", "keychain-password.txt", "signing.env"):
                    (state / name).write_text("inert mock data")
                profile = root / "input.mobileprovision"
                profile.write_text("inert encoded profile")
                content = "<plist>fixture-complete-device-list</plist>"
                env = os.environ | {"PATH": f"{tools}:{os.environ['PATH']}",
                    "IOS_SIGNING_STATE_DIR": str(state), "IOS_PROVISIONING_PROFILE": str(profile),
                    "TEST_FAIL_FIELD": field, "TEST_PROFILE_CONTENT": content}
                result = subprocess.run(["bash", str(script), "install-profile"],
                                        env=env, capture_output=True, text=True)
                self.assertNotEqual(0, result.returncode)
                self.assertFalse((state / "profile.plist").exists())
                self.assertFalse((state / "jenkins-ios.keychain-db").exists())
                self.assertFalse(state.exists())
                self.assertNotIn(content, result.stdout + result.stderr)

    def test_profile_download_and_install_decode_with_temporary_keychain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "signing"
            tools = root / "bin"
            scripts.mkdir(parents=True)
            tools.mkdir()
            for name in ("ios_provisioning.sh", "ios_signing.sh"):
                source = (ROOT / "signing" / name).read_text()
                (scripts / name).write_text(source.replace("/usr/libexec/PlistBuddy", str(tools / "PlistBuddy"))
                                           .replace("/usr/bin/codesign", str(tools / "codesign")))
            (root / "delivery").mkdir()
            version = (ROOT / "delivery/.fastlane-version").read_text().strip()
            (root / "delivery/.fastlane-version").write_text(version)
            (root / "delivery/fastlane.sh").write_text(
                '#!/bin/bash\nif [[ "$1" == --version ]]; then echo "fastlane ' + version + '"; else\n'
                '[[ "$PWD" == "$FASTLANE_TEST_CWD" ]] || exit 97\n'
                'printf "inert profile" > "$IOS_PROFILE_OUTPUT_PATH/current-adhoc.mobileprovision"\nfi\n'
            )
            (scripts / "ios_apple_intermediate_certificates.sh").write_text("exit 0\n")
            (tools / "ruby").write_text("#!/bin/sh\nprintf '3.4.0\\n'\n")
            (tools / "codesign").write_text("#!/bin/sh\nexit 0\n")
            (tools / "PlistBuddy").write_text(f"#!{sys.executable}\n" + '''
import sys
fields = {":Entitlements:application-identifier": "TESTTEAM01.com.example.app",
          ":TeamIdentifier:0": "TESTTEAM01", ":ProvisionedDevices:0": "fixture-device",
          ":UUID": "fixture-uuid", ":Name": "Test Profile"}
print(fields[sys.argv[2].removeprefix("Print ")])
''')
            (tools / "security").write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[0] == "cms":
    if "-k" not in args or args[args.index("-k") + 1] != os.environ["IOS_KEYCHAIN_PATH"]:
        sys.exit("A default keychain could not be found")
    print("inert decoded profile")
elif args[0] == "find-identity":
    print('1) ' + 'A' * 40 + ' "Apple Distribution: Test (TESTTEAM01)"')
elif args[0] not in ("list-keychains", "default-keychain", "unlock-keychain"):
    sys.exit(2)
''')
            for tool in tools.iterdir():
                tool.chmod(0o755)
            state = root / "signing-state"
            state.mkdir()
            keychain = state / "jenkins-ios.keychain-db"
            keychain.write_text("inert mock keychain")
            password = state / "keychain-password.txt"
            password.write_text("inert password")
            (state / "signing-preflight").write_text("inert ad-hoc binary")
            (state / "signing.env").write_text(f"IOS_KEYCHAIN_PATH={keychain}\nIOS_KEYCHAIN_PASSWORD_FILE={password}\n")
            api_key = root / "api-key"
            api_key.write_text("inert mock API key")
            log = root / "commands.jsonl"
            env = dict(os.environ, PATH=f"{tools}:{os.environ['PATH']}", HOME=str(root / "home"),
                       IOS_SIGNING_STATE_DIR=str(state), IOS_KEYCHAIN_PATH=str(keychain),
                       FASTLANE_RUBY=str(tools / "ruby"), FASTLANE_TEST_CWD=str(root / "delivery"), ASC_API_KEY_PATH=str(api_key),
                       ASC_API_KEY_ID="test", ASC_ISSUER_ID="test", IOS_BUNDLE_ID="com.example.app",
                       IOS_TEAM_ID="TESTTEAM01", IOS_PROFILE_NAME="Test Profile", IOS_DISTRIBUTION="adhoc", REGISTER_NEW_DEVICE="false",
                       COMMAND_LOG=str(log), NEW_DEVICE_NAME="", NEW_DEVICE_UDID="")
            downloaded = subprocess.run(["/bin/bash", str(scripts / "ios_provisioning.sh"), "sync"],
                                        env=env, capture_output=True, text=True)
            self.assertEqual(0, downloaded.returncode, downloaded.stderr)
            profile = state / "profile-output/current-adhoc.mobileprovision"
            env["IOS_PROVISIONING_PROFILE"] = str(profile)
            installed = subprocess.run(["/bin/bash", str(scripts / "ios_signing.sh"), "install-profile"],
                                       env=env, capture_output=True, text=True)
            self.assertEqual(0, installed.returncode, installed.stderr)
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            cms = [command for command in commands if command[0] == "cms"]
            self.assertEqual(2, len(cms))
            for command in cms:
                self.assertEqual(str(keychain), command[command.index("-k") + 1])
            self.assertEqual(str(profile), (state / "profile-path.txt").read_text().strip())
            self.assertEqual(profile.read_bytes(), (root / "home/Library/MobileDevice/Provisioning Profiles/fixture-uuid.mobileprovision").read_bytes())

    def test_signature_password_temp_file_is_restricted_and_removed_on_success_or_failure(self):
        for pipeline, status, trusted in (("test", 0, False), ("test", 1, True), ("release", 0, True), ("release", 1, False)):
            with self.subTest(pipeline=pipeline, status=status, trusted=trusted), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary).resolve()
                scripts = directory / "signing"
                scripts.mkdir(parents=True)
                shutil.copyfile(ROOT / "signing/ios_signing.sh", scripts / "ios_signing.sh")
                (scripts / "ios_verify_signature.py").write_text(
                    'import os,sys,stat\nfrom pathlib import Path\n'
                    'p=Path(next(a.split("=",1)[1] for a in sys.argv if a.startswith("--password-file=")))\n'
                    'assert stat.S_IMODE(p.stat().st_mode)==0o600\n'
                    'assert p.read_text()==os.environ["IOS_DISTRIBUTION_P12_PASSWORD"]\n'
                    'assert ("--channel=pgyer" in sys.argv)==(os.environ["CI_IOS_PIPELINE"]=="test")\n'
                    'assert "--channel=" not in sys.argv\n'
                    'assert next(a.split("=",1)[1] for a in sys.argv if a.startswith("--config=")) == os.environ["TEST_EXPECTED_CONFIG"]\n'
                    'Path(os.environ["TEST_SEEN"]).write_text(str(p))\n'
                    'sys.exit(int(os.environ["TEST_STATUS"]))\n'
                )
                output = directory / "output"
                output.mkdir()
                (output / "context.json").write_text(json.dumps({"version_target": "1.0.0"}))
                seen = directory / "seen"
                secret = "fixture-secret-not-for-console"
                result = subprocess.run(["bash", str(scripts / "ios_signing.sh"), "verify-ipa"],
                    cwd=directory, env=dict(os.environ, CI_OUTPUT_DIR=str(output),
                    IOS_SIGNING_STATE_DIR=str(directory / "signing"), IOS_DISTRIBUTION_P12_PASSWORD=secret,
                    IOS_DISTRIBUTION_P12="fixture.p12", CI_BRAND="cihang", CI_IOS_PIPELINE=pipeline, CI_TEST_CHANNEL="pgyer",
                    IOS_IPA_PATH="fixture.ipa", IOS_BUILD_NUMBER="123", TEST_SEEN=str(seen), TEST_STATUS=str(status),
                    IOS_BRAND_CONFIG=str(directory / "trusted/brands.json") if trusted else "",
                    TEST_EXPECTED_CONFIG=str(directory / "trusted/brands.json") if trusted else str(directory / "ci/brands.json")),
                    capture_output=True, text=True)
                self.assertEqual(status, result.returncode, result.stderr)
                self.assertFalse(Path(seen.read_text()).exists())
                self.assertNotIn(secret, result.stdout + result.stderr)
