import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]


class SigningBackupRuntimeTests(unittest.TestCase):
    def test_production_jca_authorization_archive_and_status(self):
        classpath = os.environ.get("GROOVY_TEST_CLASSPATH")
        if not classpath and (ROOT / 'build/controller-groovy/groovy-all-2.4.21.jar').is_file():
            classpath = str(ROOT / 'build/controller-groovy/groovy-all-2.4.21.jar')
        if not classpath:
            cache = Path(os.environ.get("GRADLE_USER_HOME", Path.home() / ".gradle")) / "wrapper/dists"
            for core in sorted(cache.glob("*/*/*/lib/groovy-[0-9]*.jar")):
                jars = [core, core.with_name(core.name.replace("groovy-", "groovy-json-")),
                        core.with_name(core.name.replace("groovy-", "groovy-dateutil-"))]
                if all(jar.is_file() for jar in jars):
                    classpath = os.pathsep.join(map(str, jars))
                    break
        if not classpath or not shutil.which("java") or not shutil.which("node"):
            self.skipTest("需要已有 Groovy/JDK/Node；可设置 GROOVY_TEST_CLASSPATH，不自动下载")

        def checked(command):
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            return result.stdout

        with TemporaryDirectory(prefix="signing-backup-runtime-") as temporary:
            directory = Path(temporary).resolve()
            directory.chmod(0o700)
            helper = ROOT / "signing/signing_backup.cjs"
            key, public, private = [directory / name for name in ["key", "public.pem", "private.enc.json"]]
            checked(["node", str(helper), "keygen", str(key)])
            checked(["node", str(helper), "recipient-key", str(key), str(public), str(private)])
            profile = json.loads((ROOT / 'tests/fixtures/legacy-project.json').read_text())
            profile.pop('testOnly')  # 仅在临时树模拟已审管理员配置。
            recipient = checked(['node', '-e', "const fs=require('fs'),crypto=require('crypto'); console.log(crypto.createHash('sha256').update(crypto.createPublicKey(fs.readFileSync(process.argv[1])).export({type:'spki',format:'der'})).digest('hex'))", str(public)]).strip()
            profile['signingBackup']['recipientSha256'] = recipient
            profile['signingBackup'].pop('legacyRecipientSha256', None)
            for home in (directory, directory / 'controller'):
                (home / 'ci-projects').mkdir(parents=True, mode=0o700)
                config = home / 'ci-projects/sxmqlive.json'
                config.write_text(json.dumps(profile)); config.chmod(0o600)
                (home / 'ci-auto-promotion').mkdir(mode=0o700)
                policy = home / 'ci-auto-promotion/CiAutoPromotion.groovy'
                shutil.copyfile(ROOT / 'jenkins/controller/CiAutoPromotion.groovy', policy)
                policy.chmod(0o600)
            old_helper = directory / 'inherited-compiler.groovy'
            old_helper.write_text((ROOT / 'jenkins/signing-backup.groovy').read_text().replace(
                'new GroovyClassLoader(Jenkins.class.classLoader,\n'
                '                                     new org.codehaus.groovy.control.CompilerConfiguration())',
                'new GroovyClassLoader(getClass().classLoader)'), encoding='utf-8')
            previous = subprocess.run(["java", "-cp", classpath, "groovy.ui.GroovyMain",
                                       str(ROOT / "tests/jenkins/fixtures/signing_backup_runtime.groovy"),
                                       str(old_helper), str(directory)],
                                      capture_output=True, text=True, timeout=30)
            self.assertNotEqual(0, previous.returncode)
            self.assertIn('policy inherited Pipeline compiler', previous.stderr + previous.stdout)
            output = checked(["java", "-cp", classpath, "groovy.ui.GroovyMain",
                              str(ROOT / "tests/jenkins/fixtures/signing_backup_runtime.groovy"),
                              str(ROOT / "jenkins/signing-backup.groovy"), str(directory)])
            self.assertIn("signing backup runtime checks passed", output)
            self.assertIn("publish CPS behavior checks passed", output)
            self.assertIn("isolated policy compiler and private profile checks passed", output)
            self.assertIn("Active Choices cache isolation behavior checks passed", output)
            direct = {"controller/config.xml": b"test controller", "signing-state/ios/profile": b"test profile"}
            collected = direct | {"controller/credentials.xml": b"test credentials",
                                  "controller/secrets/master.key": b"test master key",
                                  "controller/secrets/hudson.util.Secret": b"test hudson secret",
                                  "controller/jobs/mobile-apps/config.xml": b"test folder",
                                  "controller/nodes/signing-maintenance/config.xml": b"test node",
                                  "backup-scope.json": json.dumps({'projectId': 'sxmqlive', 'scope': 'controller_dr', 'recipientSha256': recipient}, separators=(',', ':')).encode()}
            for envelope, expected, materials in [("jca", "expected", direct), ("exported", "exported-expected", collected)]:
                with self.subTest(envelope=envelope):
                    restored = directory / (envelope + "-restored.zip")
                    checked(["node", str(helper), "decrypt-public", str(key), str(private),
                             str(directory / (envelope + ".enc.json")), str(restored)])
                    raw = restored.read_bytes()
                    self.assertEqual((directory / (expected + ".zip")).read_bytes(), raw)
                    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                        self.assertEqual(sorted(archive.namelist()), archive.namelist())
                        manifest = json.loads(archive.read("manifest.json"))
                        self.assertEqual(set(materials), set(manifest))
                        for name, value in materials.items():
                            self.assertEqual(value, archive.read(name))
                            self.assertEqual({"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}, manifest[name])

            # 真实 publisher 只写本地 bare 仓库；业务提交/构建号和选择缓存均不追加快照。
            from tests.signing.test_publish_signing_backup import PublishSigningBackupTests, run_git
            from signing import publish_signing_backup
            fixture = PublishSigningBackupTests("test_content_checksum_skips_reencrypted_identical_archive")
            try:
                fixture.setUp()
                fixture.profile['signingBackup']['recipientSha256'] = recipient
                cases = json.loads((directory / "cache-cases.json").read_text())
                previous = None
                commits = 0
                for index, case in enumerate(cases):
                    result = publish_signing_backup.publish(
                        directory / (case["label"] + ".enc.json"), case["contentSha256"],
                        format(index + 1, "040x"), index + 1,
                    )
                    unchanged = case["label"] in {"source-only", "cache-only", "cache-empty"}
                    self.assertEqual("unchanged" if unchanged else "synced", result["status"], case["label"])
                    if unchanged:
                        self.assertEqual(previous, result["gitCommit"])
                    else:
                        commits += 1
                    self.assertEqual(str(commits), run_git(fixture.remote, "rev-list", "--count", publish_signing_backup.BRANCH))
                    previous = result["gitCommit"]
                self.assertEqual(fixture.master, run_git(fixture.remote, "rev-parse", "master"))
                self.assertEqual(fixture.index_before, (fixture.caller / ".git/index").read_bytes())
            finally:
                fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
