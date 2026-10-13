import re
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
JENKINS_DIR = REPO_ROOT / "jenkins"
LOCAL_JENKINS_DIR = JENKINS_DIR / "local"
ANDROID_JOBS = ("test", "release")
IOS_JOBS = ("ios.test", "ios.release")
DELIVERY_JOBS = (*ANDROID_JOBS, *IOS_JOBS)
ALL_PIPELINES = DELIVERY_JOBS

def read_text(path):
    return path.read_text(encoding="utf-8")

class PackagingPipelinesTest(unittest.TestCase):
    def test_business_pipelines_do_not_mutate_ci_infrastructure(self):
        for name in (*DELIVERY_JOBS, 'ohos.test', 'pr-validation'):
            source = read_text(JENKINS_DIR / ('Jenkinsfile.' + name))
            with self.subTest(pipeline=name):
                for forbidden in ('sync_jenkins_jobs.py', 'sync_jenkins_admin_jobs.py',
                                  'remote/stack.sh', 'remote/backup.sh', 'docker compose',
                                  'createProject', 'setDefinition(', 'updateByXml('):
                    self.assertNotIn(forbidden, source)

    def test_pipeline_post_conditions_have_executable_steps(self):
        for name in (*DELIVERY_JOBS, "ohos.test"):
            with self.subTest(pipeline=name):
                source = read_text(JENKINS_DIR / ("Jenkinsfile." + name))
                self.assertNotRegex(source, r"\b(?:always|success|failure|unstable|unsuccessful)\s*\{\s*\}")

    def test_automation_and_selected_app_checkouts_keep_their_roots_and_revisions(self):
        for name in (*DELIVERY_JOBS, "ohos.test"):
            with self.subTest(pipeline=name):
                source = read_text(JENKINS_DIR / ("Jenkinsfile." + name))
                runtime = source.split("stage('Checkout Selected", 1)[0]
                self.assertIn("dir('automation')", runtime)
                self.assertEqual(1, source.count("checkout scm"))
                self.assertIn("env.CI_AUTOMATION_COMMIT = automationCheckout.GIT_COMMIT", runtime)
                self.assertIn("env.CI_TOOL_ROOT = pwd()", runtime)
                self.assertIn('env.APP_SOURCE_ROOT = "${pwd()}/../source"', runtime)
                self.assertIn("env.PYTHONPATH = env.CI_TOOL_ROOT", runtime)
                self.assertIn("dir('source')", source.split("stage('Checkout Selected", 1)[1])
                self.assertIn("url: env.APP_REPOSITORY_URL", source)
                self.assertNotIn("params.APP_REPOSITORY_URL", source)
                self.assertNotIn("unstash", source)
                for path in re.findall(r'(?:python3|bash) ([^\n]+)', source):
                    self.assertNotIn("ci/scripts/", path)

    def test_android_and_harmony_signing_authority_stays_on_trusted_application_master(self):
        from project_config import load_project
        self.assertEqual('refs/heads/master', load_project()['repositories']['application']['trustRef'])
        for name in (*ANDROID_JOBS, "ohos.test"):
            with self.subTest(pipeline=name):
                source = read_text(JENKINS_DIR / ("Jenkinsfile." + name))
                runtime = source.split("stage('Checkout Selected", 1)[0]
                self.assertIn("dir('automation/trusted-application-config')", runtime)
                self.assertIn("env.CI_TRUSTED_FACTS_REF = ciProject.repositories.application.trustRef", runtime)
                self.assertIn("branches: [[name: env.CI_TRUSTED_FACTS_REF]]", runtime)
                self.assertNotIn('params.CI_TRUSTED_FACTS_REF', runtime)
                self.assertIn("sparseCheckoutPaths: [[path: env.CI_BRANDS_FILE]]", runtime)
                self.assertIn("env.CI_SIGNING_CONFIG_COMMIT = signingConfigCheckout.GIT_COMMIT", runtime)
                self.assertIn('--config="$CI_SIGNING_CONFIG_ROOT/$CI_BRANDS_FILE"', source)
                self.assertNotIn('--config="$APP_SOURCE_ROOT/ci/brands.json"', source)

    def test_checkout_sha_mismatch_fails_and_embedded_shell_commands_parse(self):
        for name in (*DELIVERY_JOBS, "ohos.test"):
            source = read_text(JENKINS_DIR / ("Jenkinsfile." + name))
            for command in re.findall(r"(?:sh|script:)\s*'''(.*?)'''", source, re.S):
                with self.subTest(pipeline=name, shell=command[:80]):
                    result = subprocess.run(["bash", "-n"], input=command, capture_output=True, text=True)
                    self.assertEqual(0, result.returncode, result.stderr)
            checks = re.findall(r"sh '(test \"\$\(git rev-parse HEAD\).*?)'", source)
            self.assertTrue(checks)
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "ci").mkdir()
                (root / "ci/brands.json").write_text("{}")
                git = root / "git"
                git.write_text("#!/bin/sh\nprintf '%s\\n' 'fixture-commit'\n")
                git.chmod(0o755)
                for command in checks:
                    for matches in (True, False):
                        with self.subTest(pipeline=name, sha_matches=matches):
                            value = "fixture-commit" if matches else "unexpected-commit"
                            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                                       CI_AUTOMATION_COMMIT=value, CI_SIGNING_CONFIG_COMMIT=value, CI_COMMIT=value, CI_BRANDS_FILE='ci/brands.json')
                            result = subprocess.run(["bash", "-eu", "-c", command], cwd=root,
                                                    env=env, capture_output=True, text=True)
                            self.assertEqual(0 if matches else 1, result.returncode, result.stderr)

    def test_harmony_upload_and_notification_use_verified_checkout_commit(self):
        source = read_text(JENKINS_DIR / 'Jenkinsfile.ohos.test')
        selected = source.split("stage('Checkout Selected Source')", 1)[1].split("stage('", 1)[0]
        self.assertIn('env.CI_COMMIT = sourceCheckout.GIT_COMMIT', selected)
        guard = re.search(r"sh '(test .*?\$CI_COMMIT.*?)'", selected).group(1)
        for stage in ('Distribute HAP to Pgyer', 'Notify DingTalk'):
            block = source.split("stage('" + stage + "')", 1)[1].split("stage('", 1)[0]
            self.assertIn('--commit="$CI_COMMIT"', block)
        with tempfile.TemporaryDirectory() as temporary:
            git = Path(temporary) / 'git'
            git.write_text("#!/bin/sh\nprintf '%s\\n' '" + 'a' * 40 + "'\n")
            git.chmod(0o755)
            for commit in (None, 'b' * 40, 'a' * 40):
                with self.subTest(commit=commit):
                    env = dict(os.environ, PATH=temporary + os.pathsep + os.environ['PATH'])
                    env.pop('CI_COMMIT', None)
                    if commit is not None:
                        env['CI_COMMIT'] = commit
                    result = subprocess.run(['bash', '-eu', '-c', guard], env=env,
                                            capture_output=True, text=True)
                    self.assertEqual(commit == 'a' * 40, result.returncode == 0, result.stderr)

    @classmethod
    def setUpClass(cls):
        cls.pipelines = {name: read_text(JENKINS_DIR / f"Jenkinsfile.{name}") for name in ALL_PIPELINES}

    def assertContainsAll(self, source, fragments):
        for fragment in fragments:
            self.assertIn(fragment, source)

    def assertContainsNone(self, source, fragments):
        for fragment in fragments:
            self.assertNotIn(fragment, source)

    def assertPipelineContains(self, names, fragments, excluded=()):
        for name in names:
            with self.subTest(pipeline=name):
                self.assertContainsAll(self.pipelines[name], fragments)
                self.assertContainsNone(self.pipelines[name], excluded)









    def test_android_build_consumes_only_selected_jenkins_signing_files(self):
        for name in ("test", "release"):
            pipeline = self.pipelines[name]
            build = pipeline.split("stage('Build APK')", 1)[1].split("stage('", 1)[0]
            self.assertContainsAll(build, (
                "android_signing.py\" credentials",
                "file(credentialsId: signingCredentials[0], variable: 'ANDROID_KEYSTORE_FILE')",
                "file(credentialsId: signingCredentials[1], variable: 'ANDROID_SIGNING_CONFIG_FILE')",
                "ANDROID_SIGNING_SOURCE=jenkins", "android_signing.py\" build",
                '--brand="$CI_BRAND"', '--config="$CI_SIGNING_CONFIG_ROOT/$CI_BRANDS_FILE"',
            ))
            self.assertNotIn("storePassword", pipeline)
            self.assertNotIn("keyPassword", pipeline)
            self.assertIn("when { expression { env.CI_NOTIFY == 'true' } }", pipeline)
            self.assertEqual(2, pipeline.count("if (env.CI_NOTIFY == 'true')"))

    def test_ios_test_job_downloads_profile_for_selected_channel(self):
        pipeline = self.pipelines["ios.test"]
        fastfile = read_text(REPO_ROOT / "delivery/fastlane/Fastfile")

        self.assertContainsAll(pipeline, (
            "REGISTER_NEW_DEVICE = 'false'", "FORCE_PROFILE_REFRESH = 'false'",
            "stage('Download Test Profile')", "stage('Install Test Profile')",
            '--channel="$CI_TEST_CHANNEL"', "ios_provisioning.sh\" sync",
            "asc-api-key-credential",
        ))
        self.assertContainsNone(pipeline, (
            "params.NEW_DEVICE_NAME", "sshUserPrivateKey", "match-password-credential",
        ))
        provisioning_script = read_text(
            REPO_ROOT / "signing/ios_provisioning.sh"
        )
        self.assertIn("ios_apple_intermediate_certificates.sh", provisioning_script)
        self.assertNotIn("FASTLANE_WWDR_USE_HTTP1_AND_RETRIES", provisioning_script)
        self.assertContainsAll(fastfile, (
            ".fastlane-version", "register_device(", "get_provisioning_profile(",
            "{ force: true } : { readonly: true }",
            "lane :download_appstore_profile", "skip_install: true",
        ))
        self.assertNotIn("skip_certificate_verification", fastfile)

    def test_ios_release_job_downloads_existing_appstore_profile_readonly(self):
        pipeline = self.pipelines["ios.release"]
        fastfile = read_text(REPO_ROOT / "delivery/fastlane/Fastfile")
        provisioning_script = read_text(
            REPO_ROOT / "signing/ios_provisioning.sh"
        )

        self.assertIn("lane :download_appstore_profile", fastfile)
        self.assertIn("readonly: true", fastfile)
        self.assertIn('filename: "current-appstore.mobileprovision"', fastfile)
        self.assertIn("stage('Download App Store Profile')", pipeline)
        self.assertIn("stage('Install App Store Profile')", pipeline)
        self.assertIn("ios_provisioning.sh\" sync", pipeline)
        self.assertIn('lane="download_appstore_profile"', provisioning_script)
        self.assertIn("ProvisionedDevices", provisioning_script)
        self.assertIn("ProvisionsAllDevices", provisioning_script)


    def test_android_jobs_run_fast_contract_checks_then_lint_and_assemble(self):
        android_script = read_text(REPO_ROOT / "delivery/android_ci.sh")
        for name in ANDROID_JOBS:
            pipeline = self.pipelines[name]
            with self.subTest(pipeline=name):
                self.assertContainsAll(pipeline, (
                    "android_ci.sh\" quality-gate", "android_ci.sh\" verified-build",
                    'artifacts: "${env.CI_ANDROID_MODULE}/build/reports/lint-results-*.*,build/reports/profile/*.html"',
                    "timestamps()",
                ))
                for later_stage in ("Build APK", "Upload To Pgyer"):
                    self.assertLess(
                        pipeline.index("stage('Quality Gate')"),
                        pipeline.index(f"stage('{later_stage}')"),
                    )

        self.assertContainsAll(android_script, (
            "tests.delivery.test_ci_execution_contracts", "ci.tests.test_manifest_contract",
        ))
        self.assertContainsNone(android_script, (
            "unittest discover", "run_gradle clean",
        ))

    def test_jobs_resolve_brand_from_git_reference(self):
        self.assertPipelineContains(ANDROID_JOBS, (
            "resolve-brand", '--brand="$CI_BRAND"', "params.BRAND",
            "resolvedBrand != env.CI_BRAND",
        ))

    def test_test_job_checks_out_selected_parameter_branch(self):
        pipeline = self.pipelines["test"]

        self.assertIn("params.BRANCH_TYPE", pipeline)
        self.assertIn("params.GIT_BRANCH", pipeline)
        self.assertIn('branches: [[name: "refs/heads/${env.CI_BRANCH}"]]', pipeline)
        self.assertIn("credentialsId: env.CI_SCM_CREDENTIAL_ID", pipeline)
        self.assertEqual(1, pipeline.count("checkout scm"))



    def test_release_jobs_checkout_selected_tag(self):
        for name, tag_prefix in (
            ("release", '"${env.CI_BRAND}-v"'),
            ("ios.release", '"${env.CI_BRAND}-ios-v"'),
        ):
            pipeline = self.pipelines[name]
            with self.subTest(pipeline=name):
                self.assertContainsAll(pipeline, (
                    "params.BRAND", "params.GIT_TAG", tag_prefix,
                    'branches: [[name: "refs/tags/${env.CI_TAG}"]]',
                    "skipDefaultCheckout(true)", "env.CI_LAST_SUCCESS = ''",
                    "+refs/heads/${env.CI_DEFAULT_BRANCH}:refs/remotes/origin/${env.CI_DEFAULT_BRANCH}",
                    "git rev-list -n 1", "git merge-base --is-ancestor",
                ))
                if name == "ios.release":
                    self.assertIn("resolvedBrand != env.CI_BRAND", pipeline)
                self.assertEqual(1, pipeline.count("checkout scm"))



    def test_test_package_archives_only_delivery_files(self):
        pipeline = (REPO_ROOT / "jenkins/Jenkinsfile.ios.test").read_text()
        archive = pipeline.split("stage('Archive iOS QA Artifacts')", 1)[1]

        self.assertIn("build/ci-ios/export/*.ipa", archive)
        self.assertIn("build/ci-ios/*.zip", archive)
        self.assertIn("build/ci-ios/ExportOptions-*.plist", archive)
        self.assertIn("build/ci-ios/*.jsonl", archive)
        self.assertNotIn("build/ci-ios/**/*.plist", archive)
        self.assertNotIn("build/ci-ios/signing/", archive)

    def test_ios_gates_validate_pods_without_duplicate_builds(self):
        test_pipeline = (REPO_ROOT / "jenkins/Jenkinsfile.ios.test").read_text()
        release_pipeline = (REPO_ROOT / "jenkins/Jenkinsfile.ios.release").read_text()
        mr_pipeline = (REPO_ROOT / "jenkins/Jenkinsfile.pr-validation").read_text()
        test_gate = test_pipeline.split("stage('Quality Gate')", 1)[1].split("\n        stage(", 1)[0]
        release_gate = release_pipeline.split("stage('Quality Gate')", 1)[1].split("\n        stage(", 1)[0]

        self.assertNotIn("affected_gradle_tests.py", test_gate)
        ios_gate = mr_pipeline.split("stage('Affected iOS Host Validation')", 1)[1].split("\n    post", 1)[0]
        self.assertNotIn("affected_gradle_tests.py", ios_gate)
        self.assertNotIn("ios_ci.sh build-unsigned", test_gate)
        self.assertIn('ios_ci.sh" preflight', test_gate)
        self.assertIn("pod install --deployment", test_gate)
        self.assertNotIn("affected_gradle_tests.py", release_gate)
        self.assertNotIn("python3 -m unittest", release_gate)
        self.assertNotIn("ios_ci.sh build-unsigned", release_gate)
        self.assertIn("pod install --deployment", release_gate)
        for pipeline in (test_pipeline, release_pipeline):
            self.assertIn("timestamps()", pipeline)
