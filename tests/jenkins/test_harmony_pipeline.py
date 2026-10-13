import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PIPELINE = (ROOT / 'jenkins/Jenkinsfile.ohos.test').read_text()


class HarmonyPipelineTest(unittest.TestCase):
    def test_all_commands_pass_server_selected_pipeline_and_target(self):
        lines = PIPELINE.splitlines()
        commands = [lines[index + 1] for index, line in enumerate(lines) if 'harmony_release.py" ' in line]
        self.assertEqual(6, len(commands))
        for command in commands:
            for argument in ('--pipeline="$CI_PIPELINE"', '--branch="${CI_BRANCH:-}"', '--tag="${CI_TAG:-}"', '--build-type="$CI_BUILD_TYPE"', '--dry-run="$CI_DRY_RUN"'):
                self.assertIn(argument, command)
        self.assertIn('"refs/tags/${env.CI_TAG}"', PIPELINE)
        self.assertIn('+refs/heads/${env.CI_DEFAULT_BRANCH}:refs/remotes/origin/${env.CI_DEFAULT_BRANCH} +refs/tags/${env.CI_TAG}:refs/tags/${env.CI_TAG}', PIPELINE)
        self.assertNotIn('shallow: true', PIPELINE.split("stage('Checkout Selected Source')", 1)[1])
        self.assertLess(PIPELINE.index('git merge-base --is-ancestor'), PIPELINE.index("stage('Preflight and Unsigned HAP')"))

    def test_actual_commands_accept_absent_optional_refs_but_require_build_identity(self):
        lines = PIPELINE.splitlines()
        commands = []
        for index, line in enumerate(lines):
            if 'python3 "$CI_TOOL_ROOT/delivery/harmony_release.py" ' not in line:
                continue
            command = line.strip().removeprefix("script: '''")
            while command.rstrip().endswith('\\'):
                index += 1
                command += '\n' + lines[index].split("'''", 1)[0]
            commands.append(command)
        self.assertEqual(6, len(commands))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / 'automation/delivery'
            scripts.mkdir(parents=True)
            (scripts / 'harmony_release.py').write_text('import json, sys\nprint(json.dumps(sys.argv[1:]))\n')
            (scripts / 'with_linux_build_memory.sh').write_text('exec "$@"\n')
            for pipeline, build_type, branch, tag, dry_run in [('test', 'QA', 'dev/cihang', '', 'true'),
                                                              ('test', 'Debug', 'dev/cihang', '', 'false'),
                                                              ('release', 'Release', '', 'cihang-ohos-v1.0.0', 'true'),
                                                              ('release', 'Release', '', 'cihang-ohos-v1.0.0', 'false')]:
                environment = {key: value for key, value in os.environ.items() if key not in ('CI_BRANCH', 'CI_TAG')}
                environment.update(CI_BRAND='cihang', CI_PIPELINE=pipeline, CI_BUILD_TYPE=build_type,
                                   CI_DRY_RUN=dry_run, CI_NOTIFY='true', CI_COMMIT='a'*40, CI_BRANDS_FILE='ci/brands.json', HARMONY_UNSIGNED_HAP='ohosApp/entry/build/'+build_type+'/entry-unsigned.hap', WORKSPACE=str(root), CI_TOOL_ROOT=str(root / 'automation'),
                                   APP_SOURCE_ROOT=str(root / 'source'), CI_SIGNING_CONFIG_ROOT=str(root / 'trusted-config'))
                if branch:
                    environment['CI_BRANCH'] = branch
                if tag:
                    environment['CI_TAG'] = tag
                for command in commands:
                    with self.subTest(pipeline=pipeline, command=command):
                        result = subprocess.run(['bash', '-eu', '-c', command], cwd=root, env=environment,
                                                capture_output=True, text=True)
                        self.assertEqual(0, result.returncode, result.stderr)
                        arguments = json.loads(result.stdout)
                        self.assertIn(arguments[0], ('plan', 'preflight', 'build', 'sign', 'upload', 'notify'))
                        for argument in ('--branch=' + branch, '--tag=' + tag, '--brand=cihang',
                                         '--pipeline=' + pipeline, '--build-type=' + build_type, '--dry-run=' + dry_run):
                            self.assertIn(argument, arguments)
                        if arguments[0] == 'upload':
                            self.assertIn('--hap=' + str(root / 'source/build/ci-harmony' / ('cihang-' + build_type + '-signed.hap')), arguments)
                            self.assertIn('--output-dir=' + str(root / 'source/build/ci-harmony'), arguments)
                        for required in ('CI_BRAND', 'CI_PIPELINE', 'CI_BUILD_TYPE', 'CI_DRY_RUN'):
                            missing = {key: value for key, value in environment.items() if key != required}
                            rejected = subprocess.run(['bash', '-eu', '-c', command], cwd=root, env=missing,
                                                      capture_output=True, text=True)
                            self.assertNotEqual(0, rejected.returncode)
                            self.assertIn(required + ': unbound variable', rejected.stderr)
                            self.assertEqual('', rejected.stdout)

    def test_upload_credentials_are_scoped_after_signing_and_reports_are_rearchived(self):
        signing, upload = PIPELINE.split("stage('Distribute HAP to Pgyer')", 1)
        self.assertIn("!(params.UPLOAD_AND_NOTIFY instanceof Boolean)", signing)
        self.assertIn("env.CI_DRY_RUN = params.UPLOAD_AND_NOTIFY ? 'false' : 'true'", signing)
        self.assertNotIn('pgyer-api-key', signing)
        self.assertNotIn('PGYER_API_KEY', signing)
        credential_gate = upload.split("if (env.CI_DRY_RUN == 'false') {", 1)[1].split('}', 1)[0]
        self.assertIn("uploadCredentials.add(string(credentialsId: env.CI_PGYER_CREDENTIAL_ID, variable: 'PGYER_API_KEY'))", credential_gate)
        self.assertEqual(1, PIPELINE.count("credentialsId: env.CI_PGYER_CREDENTIAL_ID"))
        for variable in ('HARMONY_P12_FILE', 'HARMONY_P12_PASSWORD', 'HARMONY_PROFILE_FILE', 'HARMONY_DEVICES_FILE'):
            self.assertIn("variable: '" + variable + "'", signing)
            self.assertIn("variable: '" + variable + "'", upload)
        self.assertIn('withCredentials(uploadCredentials)', upload)
        self.assertIn('set +x', upload)
        self.assertNotIn('source/ci/scripts', PIPELINE)
        self.assertIn('CI_SIGNING_CONFIG_COMMIT', signing)
        self.assertIn('post {\n                always {\n                    archiveArtifacts', upload)
        self.assertIn("artifacts: 'source/build/ci-harmony/*-signed.hap,source/build/ci-harmony/signature-report.json,source/build/ci-harmony/release-info.json'", upload)
        self.assertLess(upload.index('harmony_release.py" upload'), upload.index('archiveArtifacts'))

    def test_formal_tag_outside_master_fails_before_build(self):
        checkout = PIPELINE.split("stage('Checkout Selected Source')", 1)[1].split("stage('Preflight and Unsigned HAP')", 1)[0]
        validation = checkout.split("sh '''", 1)[1].split("'''", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {**os.environ, 'CI_PIPELINE': 'release', 'CI_DEFAULT_BRANCH':'master', 'CI_TAG': 'cihang-ohos-v1.0.0',
                           'GIT_AUTHOR_NAME': 'CI Test', 'GIT_AUTHOR_EMAIL': 'ci-test@example.invalid',
                           'GIT_COMMITTER_NAME': 'CI Test', 'GIT_COMMITTER_EMAIL': 'ci-test@example.invalid'}
            def git(*args):
                return subprocess.run(['git', *args], cwd=root, env=environment, capture_output=True, text=True, check=True)
            git('init', '-b', 'master')
            git('commit', '--allow-empty', '-m', 'baseline')
            git('update-ref', 'refs/remotes/origin/master', 'HEAD')
            git('tag', environment['CI_TAG'])
            accepted = subprocess.run(['bash', '-eu', '-c', validation], cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            git('checkout', '-b', 'feature/cihang/outside-master')
            git('commit', '--allow-empty', '-m', 'unmerged')
            git('tag', '-f', environment['CI_TAG'])
            rejected = subprocess.run(['bash', '-eu', '-c', validation], cwd=root, env=environment, capture_output=True, text=True)
            self.assertNotEqual(0, rejected.returncode)
            self.assertIn('outside origin/master', rejected.stderr)
            environment['CI_PIPELINE'] = 'test'
            skipped = subprocess.run(['bash', '-eu', '-c', validation], cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(0, skipped.returncode, skipped.stderr)


if __name__ == '__main__':
    unittest.main()
