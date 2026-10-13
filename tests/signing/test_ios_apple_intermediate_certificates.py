import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


from tests.signing.fixtures import write_synthetic_apple_certificates


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "signing/ios_apple_intermediate_certificates.sh"
MANIFEST = ROOT / "signing/resources/apple-intermediate-certificates/manifest.txt"


class IosAppleIntermediateCertificatesTest(unittest.TestCase):
    def setUp(self):
        self.material = tempfile.TemporaryDirectory(prefix="apple-fixture-")
        self.addCleanup(self.material.cleanup)
        self.manifest = write_synthetic_apple_certificates(Path(self.material.name))

    def test_install_is_idempotent_and_preserves_security_failures(self):
        hashes = [line.split()[1].upper() for line in self.manifest.read_text().splitlines()
                  if line and not line.startswith("#")]
        for initial, failure in ((0, ""), (3, ""), (7, ""), (0, "find"),
                                 (0, "import"), (0, "missing"), (7, "tampered")):
            with self.subTest(initial=initial, failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                scripts = root / "signing"
                scripts.mkdir(parents=True)
                shutil.copy(SCRIPT, scripts)
                shutil.copytree(self.manifest.parent, root / "signing/resources/apple-intermediate-certificates")
                if failure == "tampered":
                    certificate = next((root / "signing/resources/apple-intermediate-certificates").glob("*.pem"))
                    certificate.write_text("invalid certificate")
                keychain = root / "temporary.keychain-db"
                keychain.write_text("\n".join(hashes[:initial]))
                commands = root / "imports"
                security = root / "security"
                security.write_text(f"#!{sys.executable}\n" + '''
import hashlib, os, ssl, sys
from pathlib import Path
args = sys.argv[1:]
failure = os.environ["FAILURE"]
keychain = Path(args[-1])
if args[0] == "find-certificate":
    if failure == "find":
        print("keychain unavailable", file=sys.stderr)
        sys.exit(23)
    for fingerprint in keychain.read_text().splitlines():
        print("SHA-256 hash: " + fingerprint)
elif args[0] == "import":
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(Path(args[1]).read_text())).hexdigest().upper()
    with open(os.environ["IMPORTS"], "a") as log:
        log.write(fingerprint + "\\n")
    if failure == "import":
        print("keychain is locked", file=sys.stderr)
        sys.exit(24)
    if fingerprint in keychain.read_text().splitlines():
        print("The specified item already exists in the keychain.", file=sys.stderr)
        sys.exit(25)
    if failure != "missing":
        with keychain.open("a") as output:
            output.write("\\n" + fingerprint)
else:
    sys.exit(26)
''')
                security.chmod(0o755)
                env = os.environ | {"PATH": f"{root}:{os.environ['PATH']}",
                                    "FAILURE": failure, "IMPORTS": str(commands)}
                command = ["bash", str(scripts / SCRIPT.name), "install", str(keychain)]
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                if failure:
                    self.assertNotEqual(0, result.returncode, result.stdout)
                    if failure in ("find", "import"):
                        self.assertEqual({"find": 23, "import": 24}[failure], result.returncode)
                    if failure in ("find", "tampered"):
                        self.assertFalse(commands.exists())
                    if failure == "missing":
                        self.assertIn("was not imported", result.stderr)
                else:
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual(set(hashes), set(keychain.read_text().split()))
                    imported = commands.read_text().splitlines() if commands.exists() else []
                    self.assertEqual(hashes[initial:], imported)
                    repeated = subprocess.run(command, env=env, capture_output=True, text=True)
                    self.assertEqual(0, repeated.returncode, repeated.stderr)
                    self.assertEqual(imported, commands.read_text().splitlines() if commands.exists() else [])

    def test_system_bootstrap_imports_only_g3_and_requires_admin(self):
        g3 = next(line.split()[1].upper() for line in self.manifest.read_text().splitlines()
                  if line.startswith("G3 "))
        for action, uid, initial, failure in (("check-system", "501", False, ""),
                ("check-system", "501", True, ""), ("install-system", "501", False, ""),
                ("install-system", "0", False, ""), ("install-system", "0", True, ""),
                ("install-system", "0", False, "import"),
                ("install-system", "0", False, "missing")):
            with self.subTest(action=action, uid=uid, initial=initial, failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                scripts = root / "signing"
                scripts.mkdir(parents=True)
                keychain = root / "System.keychain"
                keychain.write_text(g3 if initial else "EXISTING-CERT")
                (scripts / SCRIPT.name).write_text(SCRIPT.read_text().replace(
                    "/Library/Keychains/System.keychain", str(keychain)))
                shutil.copytree(self.manifest.parent, root / "signing/resources/apple-intermediate-certificates")
                (root / "id").write_text("#!/bin/sh\necho " + uid + "\n")
                (root / "id").chmod(0o755)
                imports = root / "imports"
                (root / "security").write_text(f"#!{sys.executable}\n" + r'''
import hashlib, os, ssl, sys
from pathlib import Path
args = sys.argv[1:]
kc = Path(args[-1])
if args[0] == "find-certificate":
    for fingerprint in kc.read_text().splitlines():
        print("SHA-256 hash: " + fingerprint)
elif args[0] == "import":
    assert Path(args[1]).name == "AppleWWDRCAG3.pem"
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(Path(args[1]).read_text())).hexdigest().upper()
    with open(os.environ["IMPORTS"], "a") as log:
        log.write(fingerprint + "\n")
    if os.environ["FAILURE"] == "import":
        sys.exit(24)
    if os.environ["FAILURE"] != "missing":
        with kc.open("a") as output:
            output.write("\n" + fingerprint)
else:
    sys.exit(25)
''')
                (root / "security").chmod(0o755)
                env = os.environ | {"PATH": f"{root}:{os.environ['PATH']}",
                                    "FAILURE": failure, "IMPORTS": str(imports)}
                command = ["bash", str(scripts / SCRIPT.name), action]
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                success = not failure and (initial or (action == "install-system" and uid == "0"))
                if action == "install-system" and uid != "0":
                    success = False
                self.assertEqual(success, result.returncode == 0, result.stderr)
                attempted = action == "install-system" and uid == "0" and not initial
                self.assertEqual(attempted, imports.exists())
                self.assertIn("EXISTING-CERT" if not initial else g3, keychain.read_text())
                if success:
                    repeated = subprocess.run(command, env=env, capture_output=True, text=True)
                    self.assertEqual(0, repeated.returncode, repeated.stderr)
                    self.assertEqual([g3] if attempted else [], imports.read_text().splitlines() if imports.exists() else [])
