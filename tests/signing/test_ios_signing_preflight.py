import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


from tests.signing.fixtures import write_synthetic_apple_certificates


ROOT = Path(__file__).resolve().parents[2]


class IosSigningPreflightTest(unittest.TestCase):
    def test_profile_codesign_probe_stops_before_archive_and_preserves_authorized_binary(self):
        for failure in ("", "sign", "verify", "timeout"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / "state"
                state.mkdir()
                tools = root / "bin"
                tools.mkdir()
                original = state / "signing-preflight"
                original.write_text("authorized ad-hoc binary")
                keychain = state / "jenkins-ios.keychain-db"
                keychain.write_text("inert keychain")
                password = state / "keychain-password.txt"
                password.write_text("inert password")
                (state / "signing.env").write_text(f"IOS_KEYCHAIN_PATH={keychain}\nIOS_KEYCHAIN_PASSWORD_FILE={password}\n")
                (state / "original-keychains.txt").write_text("")
                profile = root / "profile.mobileprovision"
                profile.write_text("inert profile")
                log = root / "commands.jsonl"
                (tools / "security").write_text(f"#!{sys.executable}\n" + '''
import sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == "find-identity":
    print('1) ' + 'A' * 40 + ' "Apple Distribution: Test (TEAM123)"')
elif args[0] == "cms":
    print("inert decoded profile")
elif args[0] == "delete-keychain":
    Path(args[-1]).unlink()
elif args[0] != "unlock-keychain":
    sys.exit(2)
''')
                (tools / "PlistBuddy").write_text(
                    '#!/bin/sh\ncase "$2" in "Print :UUID") echo fixture-uuid;; "Print :Name") echo fixture-profile;; *) exit 2;; esac\n'
                )
                (tools / "codesign").write_text(f"#!{sys.executable}\n" + '''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
original = Path(os.environ["ORIGINAL_PROBE"])
assert original.read_text() == "authorized ad-hoc binary"
assert Path(args[-1]) != original
assert sys.stdin.read() == ""
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if "--force" in args:
    assert args[args.index("--sign") + 1] == "A" * 40
    assert args[args.index("--keychain") + 1] == os.environ["IOS_KEYCHAIN_PATH"]
    assert "--timestamp=none" in args
    Path(args[-1]).write_text("signed probe copy")
    if os.environ["FAILURE"] == "timeout":
        time.sleep(4)
    if os.environ["FAILURE"] == "sign":
        sys.exit(1)
elif args[:2] == ["--verify", "--strict"]:
    assert Path(args[-1]).read_text() == "signed probe copy"
    if os.environ["FAILURE"] == "verify":
        sys.exit(1)
else:
    sys.exit(2)
''')
                for tool in tools.iterdir():
                    tool.chmod(0o755)
                script = root / "ios_signing.sh"
                source = (ROOT / "signing/ios_signing.sh").read_text()
                script.write_text(source.replace("/usr/bin/codesign", str(tools / "codesign"))
                                  .replace("/usr/libexec/PlistBuddy", str(tools / "PlistBuddy"))
                                  .replace("timeout=30", "timeout=2"))
                archive = root / "archive-started"
                home = root / "home"
                env = os.environ | {
                    "PATH": f"{tools}:{os.environ['PATH']}", "HOME": str(home),
                    "IOS_SIGNING_STATE_DIR": str(state), "IOS_KEYCHAIN_PATH": str(keychain),
                    "IOS_PROVISIONING_PROFILE": str(profile), "IOS_TEAM_ID": "TEAM123",
                    "ORIGINAL_PROBE": str(original), "COMMAND_LOG": str(log), "FAILURE": failure,
                    "SIGNING_SCRIPT": str(script), "ARCHIVE_MARKER": str(archive),
                }
                result = subprocess.run(["/bin/bash", "-eu", "-c",
                                         '/bin/bash "$SIGNING_SCRIPT" install-profile\nprintf started > "$ARCHIVE_MARKER"'],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(bool(failure), result.returncode != 0, result.stderr)
                self.assertEqual(not failure, archive.exists())
                commands = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(1 if failure in ("sign", "timeout") else 2, len(commands))
                self.assertFalse(Path(commands[0][-1]).exists())
                self.assertEqual([], list(state.glob("codesign-preflight-*")))
                if failure:
                    self.assertFalse(keychain.exists())
                    self.assertFalse((home / "Library/MobileDevice/Provisioning Profiles/fixture-uuid.mobileprovision").exists())
                    self.assertFalse((state / "signing.env").exists())
                else:
                    self.assertEqual("authorized ad-hoc binary", original.read_text())
                if failure == "timeout":
                    self.assertIn("timed out after 30 seconds", result.stderr)

    def test_prepare_and_cleanup_preserve_empty_and_existing_keychain_lists(self):
        for original in ([], ["/original/login.keychain-db", "/original/other keychain-db"]):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / "state"
                tools = root / "bin"
                tools.mkdir()
                keychains = root / "keychains.json"
                keychains.write_text(json.dumps(original))
                log = root / "commands.jsonl"
                security = tools / "security"
                security.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
path = Path(os.environ["MOCK_KEYCHAINS"])
keychains = json.loads(path.read_text())
if args == ["list-keychains", "-d", "user"]:
    for keychain in keychains:
        print('    "' + keychain + '"')
elif args[:4] == ["list-keychains", "-d", "user", "-s"]:
    keychains = args[4:]
elif args[0] == "create-keychain":
    Path(args[-1]).touch()
elif args[0] == "delete-keychain":
    Path(args[-1]).unlink()
    keychains = [keychain for keychain in keychains if keychain != args[-1]]
elif args[0] not in ("set-keychain-settings", "unlock-keychain"):
    sys.exit(2)
path.write_text(json.dumps(keychains))
''')
                security.chmod(0o755)
                env = os.environ | {
                    "PATH": f"{tools}:{os.environ['PATH']}",
                    "IOS_SIGNING_STATE_DIR": str(state),
                    "MOCK_KEYCHAINS": str(keychains),
                    "COMMAND_LOG": str(log),
                }
                command = ["/bin/bash", str(ROOT / "signing/ios_signing.sh")]
                prepared = subprocess.run(command + ["prepare"], env=env, capture_output=True, text=True)
                self.assertEqual(0, prepared.returncode, prepared.stderr)
                temporary_keychain = str(state / "jenkins-ios.keychain-db")
                self.assertEqual([temporary_keychain, *original], json.loads(keychains.read_text()))
                self.assertTrue((state / "signing.env").exists())
                cleaned = subprocess.run(command + ["cleanup"], env=env, capture_output=True, text=True)
                self.assertEqual(0, cleaned.returncode, cleaned.stderr)
                self.assertEqual(original, json.loads(keychains.read_text()))
                self.assertFalse(state.exists())
                commands = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertIn(["list-keychains", "-d", "user", "-s", temporary_keychain, *original], commands)
                if original:
                    self.assertIn(["list-keychains", "-d", "user", "-s", *original], commands)

    def test_failed_keychain_deletion_preserves_state_for_cleanup_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            retained = ("jenkins-ios.keychain-db", "keychain-password.txt", "signing.env")
            for name in retained:
                (state / name).write_text("inert mock data")
            (state / "profile.plist").write_text("inert decoded device list")
            tools = root / "bin"
            tools.mkdir()
            security = tools / "security"
            security.write_text('#!/bin/sh\n[ "$1" = delete-keychain ] || exit 2\n'
                                '[ "$FAIL_DELETE" = 0 ] || exit 1\nrm -f "$2"\n')
            security.chmod(0o755)
            env = os.environ | {"PATH": f"{tools}:{os.environ['PATH']}",
                                "IOS_SIGNING_STATE_DIR": str(state), "FAIL_DELETE": "1"}
            command = ["bash", str(ROOT / "signing/ios_signing.sh"), "cleanup"]
            failed = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(0, failed.returncode)
            self.assertNotIn("state cleaned", failed.stdout)
            self.assertTrue(all((state / name).exists() for name in retained))
            self.assertFalse((state / "profile.plist").exists())
            retried = subprocess.run(command, env=env | {"FAIL_DELETE": "0"}, capture_output=True, text=True)
            self.assertEqual(0, retried.returncode, retried.stderr)
            self.assertFalse(state.exists())

    def test_import_preflight_and_failure_cleanup(self):
        # 只替换测试副本的系统工具；不触碰开发机或正在运行的 Agent 钥匙串。
        for failure in ("", "probe", "identity", "team", "partition", "restore"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                scripts = root / "signing"
                scripts.mkdir(parents=True)
                write_synthetic_apple_certificates(root / "signing/resources/apple-intermediate-certificates")
                shutil.copy(ROOT / "signing/ios_apple_intermediate_certificates.sh", scripts)
                signing = (ROOT / "signing/ios_signing.sh").read_text()
                tools = root / "bin"
                tools.mkdir()
                (scripts / "ios_signing.sh").write_text(
                    signing.replace("/usr/bin/codesign", str(tools / "codesign"))
                )
                log = root / "commands"
                security = tools / "security"
                security.write_text(f"#!{sys.executable}\n" + '''
import os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
command = args[0]
if command == "list-keychains" and len(args) == 3:
    print('"/original/login.keychain-db"')
elif args == ["list-keychains", "-d", "user", "-s", "/original/login.keychain-db"] and os.environ["FAILURE"] == "restore":
    sys.exit(1)
elif command == "create-keychain":
    Path(args[-1]).touch()
elif command == "import" and args[1] == os.environ["IOS_DISTRIBUTION_P12"]:
    # Jenkins 的临时绑定文件没有 .p12 后缀，Security 自动识别会失败。
    if "-f" not in args or args[args.index("-f") + 1] != "pkcs12":
        sys.exit(1)
elif command == "delete-keychain":
    Path(args[-1]).unlink(missing_ok=True)
elif command == "find-certificate":
    manifest = Path(os.environ["MANIFEST"]).read_text()
    for line in manifest.splitlines():
        if line and not line.startswith("#"):
            print("SHA-256 hash: " + line.split()[1])
elif command == "find-identity":
    if os.environ["FAILURE"] != "identity":
        team = "OTHERTEAM" if os.environ["FAILURE"] == "team" else "TEAM123"
        print('1) ' + 'A' * 40 + ' "Apple Distribution: Test (' + team + ')"')
elif command == "set-key-partition-list" and os.environ["FAILURE"] == "partition":
    sys.exit(1)
''')
                (tools / "codesign").write_text(
                    '#!/bin/bash\nif [[ "$1" == -d ]]; then echo "CDHash=' + "b" * 40 + '" >&2; fi\n'
                )
                (tools / "xcrun").write_text('''#!/bin/bash
for output; do :; done
cat > "$output" <<'PROBE'
#!/bin/bash
echo probe >> "$COMMAND_LOG"
[[ "$FAILURE" != probe && "$FAILURE" != restore ]]
PROBE
chmod +x "$output"
''')
                for tool in tools.iterdir():
                    tool.chmod(0o755)
                p12 = root / "credential"
                p12.touch()
                state = root / "state"
                env = os.environ | {
                    "PATH": f"{tools}:{os.environ['PATH']}",
                    "COMMAND_LOG": str(log),
                    "MANIFEST": str(root / "signing/resources/apple-intermediate-certificates/manifest.txt"),
                    "FAILURE": failure,
                    "IOS_SIGNING_STATE_DIR": str(state),
                    "IOS_DISTRIBUTION_P12": str(p12),
                    "IOS_DISTRIBUTION_P12_PASSWORD": "test-password",
                    "IOS_TEAM_ID": "TEAM123",
                }
                # 与 Pipeline 相同：prepare 和导入是两个进程，导入必须再次解锁。
                subprocess.run(["bash", str(scripts / "ios_signing.sh"), "prepare"],
                               env=env, check=True, capture_output=True)
                result = subprocess.run(["bash", str(scripts / "ios_signing.sh"), "install-certificate"],
                                        env=env, capture_output=True, text=True)
                commands = log.read_text().splitlines()
                self.assertEqual(2, sum(line.startswith("unlock-keychain") for line in commands))
                self.assertTrue(any("-ut 21600" in line for line in commands))
                self.assertEqual(bool(failure), result.returncode != 0, result.stderr)
                self.assertEqual(not failure or failure == "restore", state.exists())
                if failure == "restore":
                    self.assertEqual(["original-keychains.txt"], [path.name for path in state.iterdir()])
                if failure:
                    self.assertIn("list-keychains -d user -s /original/login.keychain-db", commands)
                    self.assertTrue(any(line.startswith("delete-keychain") for line in commands))
                if failure in ("identity", "team", "partition"):
                    self.assertNotIn("probe", commands)
                else:
                    self.assertIn("probe", commands)
                self.assertTrue(any("apple-tool:,apple:,cdhash:" + "b" * 40 in line for line in commands))

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS Security.framework")
    def test_native_preflight_fails_without_keychain(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "preflight"
            subprocess.run(["xcrun", "clang", "-Werror", "-Wno-deprecated-declarations",
                            str(ROOT / "signing/ios_signing_preflight.c"), "-framework", "Security",
                            "-framework", "CoreFoundation", "-o", str(binary)], check=True)
            result = subprocess.run([str(binary), str(Path(directory) / "missing.keychain-db"), "A" * 40],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("OSStatus=", result.stderr)
