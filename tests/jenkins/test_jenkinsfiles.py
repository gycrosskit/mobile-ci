import hashlib
import json
import os
import re
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
JENKINS_DIR = REPO_ROOT / "jenkins"
CONTROLLER_JENKINS_DIR = JENKINS_DIR / "controller"
LOCAL_JENKINS_DIR = JENKINS_DIR / "local"
REMOTE_JENKINS_DIR = JENKINS_DIR / "remote"
ANDROID_JOBS = ("test", "release")
IOS_JOBS = ("ios.test", "ios.release")
DELIVERY_JOBS = (*ANDROID_JOBS, *IOS_JOBS)
MANAGED_JOBS = DELIVERY_JOBS
ALL_PIPELINES = (*MANAGED_JOBS, "pr-validation", "alert-test")


def read_text(path):
    return path.read_text(encoding="utf-8")


class JenkinsfilesTest(unittest.TestCase):
    def test_packaging_profile_environment_cannot_be_shadowed_by_declarative_constants(self):
        from project_config import validate_project
        profiles = [validate_project(json.loads(path.read_text())) for path in (
            REPO_ROOT / 'tests/fixtures/legacy-project.json',
            REPO_ROOT / 'examples/second-project/project.json')]
        roles = ('android_test', 'android_release', 'ios_test', 'ios_release', 'harmony_test', 'harmony_release')
        pipelines = [profiles[0]['jobs'][role]['pipeline'] for role in roles]
        self.assertEqual(5, len(set(pipelines)))
        self.assertNotEqual(profiles[0]['repositories']['application']['ssh'], profiles[1]['repositories']['application']['ssh'])

        def assert_no_shadowing(source):
            blocks = re.findall(r'\benvironment\s*\{(.*?)^\s*}', source, re.MULTILINE | re.DOTALL)
            self.assertTrue(blocks)
            declared = set(re.findall(r'^\s*([A-Z][A-Z0-9_]*)\s*=', '\n'.join(blocks), re.MULTILINE))
            assigned = set(re.findall(r'\benv\.([A-Z][A-Z0-9_]*)\s*=(?!=)', source))
            # Declarative withEnv 的优先级高于脚本 env 写入，会把受信配置遮蔽为静态值。
            self.assertFalse(declared & assigned, declared & assigned)

        for role, pipeline in zip(roles, pipelines):
            source = read_text(REPO_ROOT / pipeline)
            with self.subTest(role=role):
                assert_no_shadowing(source)
                assignments = re.findall(r'^\s*env\.APP_REPOSITORY_URL\s*=\s*(.+)$', source, re.MULTILINE)
                self.assertEqual(['ciProject.repositories.application.ssh'], assignments)
                for profile in profiles:
                    value = profile
                    for field in assignments[0].split('.')[1:]:
                        value = value[field]
                    self.assertEqual(profile['repositories']['application']['ssh'], value)
                for key in ('APP_REPOSITORY_URL', 'CI_SCM_CREDENTIAL_ID'):
                    shadowed = source.replace('environment {', f"environment {{\n        {key} = 'fixture-static'", 1)
                    with self.assertRaises(AssertionError):
                        assert_no_shadowing(shadowed)

    def test_delivery_preflight_runs_before_build_for_all_packaging_pipelines(self):
        for name in (*DELIVERY_JOBS, 'ohos.test'):
            with self.subTest(name=name):
                source = read_text(JENKINS_DIR / ('Jenkinsfile.' + name))
                stage = "stage('Delivery Runtime Preflight')"
                build = "stage('Preflight and Unsigned HAP')" if name == 'ohos.test' else "stage('Quality Gate')"
                self.assertEqual(1, source.count(stage))
                self.assertLess(source.index(stage), source.index(build))
                preflight = source[source.index(stage):source.index(build)]
                self.assertIn("env.CI_DRY_RUN == 'true'", preflight)
                self.assertIn(' --dry-run', preflight)
                self.assertIn('delivery/delivery.py" preflight', preflight)
                self.assertNotIn('withCredentials', preflight)

    def test_controller_installation_pins_dependencies_and_keeps_resolution_evidence(self):
        dockerfile = (CONTROLLER_JENKINS_DIR / 'Dockerfile').read_text()
        command = re.search(r'RUN (jenkins-plugin-cli.*?)\n\n', dockerfile, re.DOTALL).group(1)
        self.assertIn('--latest=false', command)
        self.assertIn('--list', command)
        self.assertIn('/usr/share/jenkins/ref/plugins-resolved.txt', command)
        plugins = (CONTROLLER_JENKINS_DIR / 'plugins.txt').read_text()
        for fixed in ('script-security:1429.v0810f1b_530f5', 'git-client:6.6.1',
                      'pipeline-groovy-lib:806.v408277b_33d1d', 'workflow-multibranch:842.v3a_b_59b_57b_e6e',
                      'pipeline-build-step:601.v6d4c6d1a_9dc7', 'jakarta-mail-api:2.1.5-1',
                      'gitee:1304.v2702f1d71cde', 'gradle:2.20.1253.vc116f0763a_eb_'):
            self.assertIn(fixed, plugins.splitlines())
        # 镜像 RUN 的安装失败必须传播，不能被读取清单掩盖。
        for exit_code in (0, 7):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                binary = root / 'jenkins-plugin-cli'
                binary.write_text('#!/bin/sh\necho "resolved-plugins"\nexit ' + str(exit_code) + '\n')
                binary.chmod(0o755)
                manifest = root / 'plugins-resolved.txt'
                script = command.replace('/usr/share/jenkins/ref/plugins-resolved.txt', str(manifest))
                result = subprocess.run(['sh', '-c', script], capture_output=True, text=True,
                                        env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']})
                self.assertEqual(exit_code, result.returncode)
                self.assertEqual('resolved-plugins\n' if exit_code == 0 else '', result.stdout)

    @classmethod
    def setUpClass(cls):
        cls.pipelines = {
            name: read_text(JENKINS_DIR / f"Jenkinsfile.{name}")
            for name in ALL_PIPELINES
        }

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

    def test_android_jobs_keep_test_and_release_pipelines_separate(self):
        for name, mode, other_mode in (
            ("test", "test", "release"),
            ("release", "release", "test"),
        ):
            with self.subTest(pipeline=name):
                self.assertContainsAll(self.pipelines[name], (
                    f"--pipeline={mode}",
                    f'android_ci.sh\" verified-build "$CI_BRAND" {mode}',
                ))
                self.assertNotIn(f"--pipeline={other_mode}", self.pipelines[name])

    def test_delivery_jobs_select_engine_before_checkout_and_validate_before_build(self):
        import xml.etree.ElementTree as ET
        templates = {
            "test": "android-test", "release": "android-release",
            "ios.test": "ios-test", "ios.release": "ios-release",
        }
        for name, template in templates.items():
            with self.subTest(pipeline=name):
                root = ET.fromstring(read_text(LOCAL_JENKINS_DIR / (template + "-job.xml.template")))
                parameter = next(node for node in root.findall(".//hudson.model.ChoiceParameterDefinition")
                                 if node.findtext("name") == "UI_ENGINE")
                self.assertEqual(["cmp", "kuikly"], [node.text for node in parameter.findall(".//string")])
                source = self.pipelines[name]
                self.assertIn("params.UI_ENGINE ?: ciProject.platforms.", source)
                self.assertIn('env.CI_UI_ENGINE in ciProject.platforms.', source)
                self.assertIn("env.CI_UI_ENGINE == 'kuikly' ? '--ui-engine=kuikly' : env.CI_UI_ENGINE == 'native' ? '--ui-engine=native' : ''", source)
                self.assertIn("$CI_UI_ENGINE_ARGS", source)
                self.assertLess(source.index("env.CI_UI_ENGINE ="), source.index("stage('Checkout Selected"))
                if name.startswith("ios."):
                    self.assertIn('ios_ci.sh\" preflight "$IOS_SCHEME"', source)
                    self.assertIn('cd "$IOS_HOST_DIR" && pod install --deployment', source)
                    self.assertIn("ciProject.platforms.ios.hostDirs?.get(env.CI_UI_ENGINE)", source)
                    self.assertIn('ios_release.py" config --config="$CI_SIGNING_CONFIG_ROOT/$CI_BRANDS_FILE" $CI_UI_ENGINE_ARGS', source)
                else:
                    self.assertIn("ciProject.platforms.android.modules[env.CI_UI_ENGINE]", source)
                if name.endswith("test"):
                    self.assertIn("cachedEngine == env.CI_UI_ENGINE", source)
                    self.assertIn("writeFile file: '.ci-test-engine'", source)



    def test_controlled_alert_requires_double_confirmation(self):
        pipeline = self.pipelines["alert-test"]
        self.assertIn("agent { label 'linux-build' }", pipeline)

        self.assertIn("CONFIRM_FAILURE_ALERT", pipeline)
        self.assertIn("SEND_CONTROLLED_FAILURE", pipeline)
        self.assertIn("notify_failure.groovy", pipeline)
        self.assertIn("def alertConfirmed = false", pipeline)
        self.assertLess(pipeline.index("error('未满足"), pipeline.index("alertConfirmed = true"))
        post = pipeline[pipeline.index("    post {"):]
        self.assertRegex(post, r"(?s)if \(!alertConfirmed\) \{[^}]*return\s*\}.*try")

    def test_notification_failure_stage_is_not_overwritten_by_archive_or_cleanup(self):
        for name in DELIVERY_JOBS:
            pipeline = self.pipelines[name]
            with self.subTest(pipeline=name):
                self.assertEqual(1, pipeline.count("ciUnstableStage = env.STAGE_NAME"))
                notification = pipeline[pipeline.index("stage('Notify DingTalk')"):]
                self.assertRegex(
                    notification,
                    r"(?s)post \{\s*unsuccessful \{\s*script \{ ciUnstableStage = env.STAGE_NAME \}",
                )
                self.assertIn("'UNSTABLE', ciUnstableStage ?: ciFailureStage", pipeline)

    def test_all_managed_jobs_send_sanitized_failure_notifications(self):
        for name in MANAGED_JOBS:
            with self.subTest(pipeline=name):
                self.assertContainsAll(self.pipelines[name], (
                    "CI_FAILURE_STAGE", "failure {", "original result is preserved",
                    'load("${env.CI_TOOL_ROOT}/jenkins/notify_failure.groovy")',
                ))
                self.assertNotIn("unstash 'jenkins-failure-notifier'", self.pipelines[name])
        helper = read_text(JENKINS_DIR / "notify_failure.groovy")
        self.assertContainsAll(helper, (
            "credentialsId: provider.webhookCredentialId", "credentialsId: provider.secretCredentialId",
            'project_config.py" --job "$JOB_NAME"',
            'PYTHONPATH="$CI_TOOL_ROOT" python3 "$CI_TOOL_ROOT/jenkins/jenkins_failure_notify.py"',
        ))
        self.assertContainsNone(helper, ("dingtalk-tester-mobile", "echo $DINGTALK", "APP_SOURCE_ROOT/jenkins"))

    def test_failure_stage_survives_agent_reconnect(self):
        for name in ALL_PIPELINES:
            pipeline = self.pipelines[name]
            with self.subTest(pipeline=name):
                self.assertTrue(
                    pipeline.startswith("def ciFailureStage = 'Pipeline Initialization'")
                )
                self.assertIn("ciFailureStage = env.STAGE_NAME", pipeline)
                self.assertNotIn("env.CI_FAILURE_STAGE = env.STAGE_NAME", pipeline)
                if name == "pr-validation":
                    self.assertIn("${ciFailureStage}", pipeline)
                else:
                    self.assertIn(", ciFailureStage)", pipeline)

        helper = read_text(JENKINS_DIR / "notify_failure.groovy")
        self.assertIn(
            "def send(String platform, String result, String failureStage = '')",
            helper,
        )
        self.assertIn(
            '"CI_FAILURE_STAGE=${failureStage ?: env.CI_FAILURE_STAGE ?: \'Pipeline\'}"',
            helper,
        )

    def test_android_builds_use_only_remote_linux_agent(self):
        self.assertPipelineContains(
            ANDROID_JOBS,
            ("agent { label 'android-builder' }",),
            ("android-mac", "agent any", "/var/lib/jenkins"),
        )

    def test_test_packages_preserve_only_same_branch_and_engine_build_cache(self):
        for name, output_dir in (("test", "build/ci"), ("ios.test", "build/ci-ios")):
            with self.subTest(pipeline=name):
                pipeline = self.pipelines[name]
                notifier = pipeline.split("stage('Validate Parameters')", 1)[0]
                checkout = pipeline.split("stage('Checkout Selected Branch')", 1)[1].split(
                    "\n        stage(", 1
                )[0]

                self.assertIn("dir('automation')", notifier)
                self.assertContainsAll(checkout, (
                    "cachedBranch == env.CI_BRANCH && cachedEngine == env.CI_UI_ENGINE && fileExists('.git')",
                    "writeFile file: '.ci-test-engine', text: env.CI_UI_ENGINE",
                    "workspaceDirty = true",
                    "?? fastlane/report.xml",
                    "git status --porcelain",
                    "deleteDir()",
                    "writeFile file: '.ci-test-branch'",
                    f"dir('{output_dir}') {{ deleteDir() }}",
                    "dir('build/reports/profile') { deleteDir() }",
                    "CI_CACHE mode=",
                    "CI_TIMING_END label=checkout",
                    "honorRefspec: true, noTags: true",
                    '+refs/heads/${env.CI_BRANCH}:refs/remotes/origin/${env.CI_BRANCH}',
                ))
                self.assertNotIn('+refs/heads/master:refs/remotes/origin/master', checkout)
                if name == "test":
                    self.assertContainsAll(pipeline, (
                        "--label=android-gradle",
                        "--label=android-upload",
                        "CI_TIMING_END label=android-delivery-archive",
                        "--key=apk_path",
                    ))

    def test_generated_ci_files_do_not_force_a_cold_checkout(self):
        for path in (
            "delivery/__pycache__/release.cpython-311.pyc",
            "signing/__pycache__/ios_devices.cpython-311.pyc",
            "jenkins/__pycache__/sync_jenkins_jobs.cpython-311.pyc",
            "delivery/fastlane/README.md",
            "delivery/fastlane/report.xml",
        ):
            with self.subTest(path=path):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "-q", path],
                    cwd=REPO_ROOT,
                    check=False,
                )
                self.assertEqual(0, result.returncode)

    def test_ios_jobs_require_mac_and_keep_test_release_channels_separate(self):
        test_pipeline = self.pipelines["ios.test"]
        release_pipeline = self.pipelines["ios.release"]
        for pipeline in (test_pipeline, release_pipeline):
            self.assertIn("agent { label 'mac-build' }", pipeline)
            self.assertIn("skipDefaultCheckout(true)", pipeline)
            self.assertIn("def iosConfigJson = sh(", pipeline)
            self.assertIn(
                "new groovy.json.JsonSlurperClassic().parseText(iosConfigJson)",
                pipeline,
            )
            self.assertNotIn("JsonSlurperClassic().parseText(sh(", pipeline)
            self.assertIn("ios_signing.sh\" install", pipeline)
            self.assertIn("ios_signing.sh\" cleanup", pipeline)
            self.assertIn("archiveArtifacts", pipeline)
            for field in ("environment", "distribution", "xcconfig"):
                self.assertIn("iosConfig['{}']".format(field), pipeline)
            self.assertIn('"$IOS_ENVIRONMENT"', pipeline)
            self.assertIn('"$IOS_DISTRIBUTION"', pipeline)
            self.assertIn('"$IOS_XCCONFIG"', pipeline)
        self.assertIn("--pipeline=test", test_pipeline)
        self.assertIn("CI_TEST_CHANNEL", test_pipeline)
        self.assertIn("stage('Distribute Test IPA')", test_pipeline)
        self.assertIn("--pipeline=release", release_pipeline)
        self.assertIn("upload-app-store", release_pipeline)
        self.assertNotIn("Upload To Pgyer", release_pipeline)
        self.assertLess(
            release_pipeline.index("deleteDir()"),
            release_pipeline.index("refs/tags/${env.CI_TAG}"),
        )


    def test_ios_jobs_resolve_namespaced_credentials_from_brand_config(self):
        test_pipeline = self.pipelines["ios.test"]
        release_pipeline = self.pipelines["ios.release"]

        self.assertContainsAll(test_pipeline, (
            "iosConfig['repository-signing-path']", "distribution.p12",
            "distribution-p12.password", "IOS_PROVISIONING_PROFILE",
            "iosConfig['distribution-p12-credential']",
            "credentialsId: env.IOS_P12_CREDENTIAL_ID",
            "env.IOS_SIGNING_SOURCE == 'jenkins'",
        ))
        self.assertContainsNone(test_pipeline, (
            "iosConfig['profile-credential']",
            "credentialsId: env.IOS_PROFILE_CREDENTIAL_ID",
        ))

        self.assertContainsAll(release_pipeline, (
            "iosConfig['distribution-p12-credential']",
            "iosConfig['distribution-p12-password-credential']",
            "credentialsId: env.IOS_P12_CREDENTIAL_ID",
            "credentialsId: env.IOS_P12_PASSWORD_CREDENTIAL_ID",
            "iosConfig['profile-name']", "stage('Download App Store Profile')",
            "ios_provisioning.sh\" sync", "IOS_PROVISIONING_PROFILE",
        ))
        self.assertContainsNone(release_pipeline, (
            "credentialsId: 'ios-distribution-p12'", "credentialsId: 'ios-kangmin-",
            "iosConfig['profile-credential']", "credentialsId: env.IOS_PROFILE_CREDENTIAL_ID",
            "credentialsId: 'app-store-connect-",
        ))

        for field in (
            "asc-api-key-credential",
            "asc-key-id-credential",
            "asc-issuer-id-credential",
        ):
            self.assertIn("iosConfig['{}']".format(field), test_pipeline)
            self.assertIn("iosConfig['{}']".format(field), release_pipeline)
        self.assertContainsAll(release_pipeline, (
            "credentialsId: env.ASC_API_KEY_CREDENTIAL_ID",
            "credentialsId: env.ASC_KEY_ID_CREDENTIAL_ID",
            "credentialsId: env.ASC_ISSUER_ID_CREDENTIAL_ID",
        ))


    def test_android_build_consumes_only_selected_jenkins_signing_files(self):
        for name in ANDROID_JOBS:
            pipeline = self.pipelines[name]
            build = pipeline.split("stage('Build APK')", 1)[1].split("stage('", 1)[0]
            self.assertContainsAll(build, (
                '$CI_TOOL_ROOT/signing/android_signing.py\" credentials',
                "file(credentialsId: signingCredentials[0], variable: 'ANDROID_KEYSTORE_FILE')",
                "file(credentialsId: signingCredentials[1], variable: 'ANDROID_SIGNING_CONFIG_FILE')",
                "ANDROID_SIGNING_SOURCE=jenkins", '$CI_TOOL_ROOT/signing/android_signing.py\" build',
                '--brand="$CI_BRAND"', '--config="$CI_SIGNING_CONFIG_ROOT/$CI_BRANDS_FILE"',
            ))
            self.assertContainsNone(build, ("storePassword", "keyPassword", "unstash", '--config=ci/brands.json'))
            self.assertIn("when { expression { env.CI_NOTIFY == 'true' } }", pipeline)
            self.assertEqual(2, pipeline.count("if (env.CI_NOTIFY == 'true')"))

    def test_ios_test_job_downloads_profile_for_selected_channel(self):
        pipeline = self.pipelines["ios.test"]
        fastfile = read_text(REPO_ROOT / "delivery" / "fastlane" / "Fastfile")

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
            REPO_ROOT / "signing" / "ios_provisioning.sh"
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
        fastfile = read_text(REPO_ROOT / "delivery" / "fastlane" / "Fastfile")
        provisioning_script = read_text(
            REPO_ROOT / "signing" / "ios_provisioning.sh"
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

    def test_fastlane_apple_intermediate_certificates_are_pinned_offline(self):
        certificate_dir = (
            REPO_ROOT / "signing" / "resources" / "apple-intermediate-certificates"
        )
        manifest = certificate_dir / "manifest.txt"
        expected = {
            "G2": "9ed4b3b88c6a339cf1387895bda9ca6ea31a6b5ce9edf7511845923b0c8ac94c",
            "G3": "dcf21878c77f4198e4b4614f03d696d89c66c66008d4244e1b99161aac91601f",
            "G4": "ea4757885538dd8cb59ff4556f676087d83c85e70902c122e42c0808b5bce14c",
            "G5": "53fd008278e5a595fe1e908ae9c5e5675f26243264a5a6438c023e3ce2870760",
            "G6": "bdd4ed6e74691f0c2bfd01be0296197af1379e0418e2d300efa9c3bef642ca30",
            "DEV-ID-G1": "7afc9d01a62f03a2de9637936d4afe68090d2de18d03f29c88cfb0b1ba63587f",
            "DEV-ID-G2": "f16cd3c54c7f83cea4bf1a3e6a0819c8aaa8e4a1528fd144715f350643d2df3a",
        }
        entries = {}
        for line in read_text(manifest).splitlines():
            if not line or line.startswith("#"):
                continue
            alias, fingerprint, filename, source_url = line.split()
            self.assertEqual(
                "https://www.apple.com/certificateauthority/",
                source_url.rsplit("/", 1)[0] + "/",
            )
            certificate = certificate_dir / filename
            der = ssl.PEM_cert_to_DER_cert(certificate.read_text(encoding="ascii"))
            self.assertEqual(fingerprint, hashlib.sha256(der).hexdigest())
            entries[alias] = fingerprint

        self.assertEqual(expected, entries)

        install_script = read_text(
            REPO_ROOT / "signing" / "ios_apple_intermediate_certificates.sh"
        )
        self.assertIn('security import "${certificate_path}"', install_script)
        self.assertIn('shasum -a 256', install_script)
        self.assertNotIn("curl ", install_script)

    def test_ios_provisioning_installs_public_certificates_before_fastlane(self):
        script = read_text(REPO_ROOT / "signing" / "ios_provisioning.sh")

        unlock_index = script.index("security unlock-keychain")
        import_index = script.index(
            'bash "${APPLE_CERTIFICATES_SCRIPT}" install "${keychain_path}"'
        )
        fastlane_index = script.index(
            'fastlane_environment bash "${FASTLANE_SCRIPT}" ios "${lane}"'
        )
        self.assertLess(unlock_index, import_index)
        self.assertLess(import_index, fastlane_index)

    def test_ios_provisioning_passes_resolved_ruby_to_fastlane_runner(self):
        script = read_text(REPO_ROOT / "signing" / "ios_provisioning.sh")
        environment = script[
            script.index("fastlane_environment()") : script.index("\n)\n\nlist_devices()")
        ]

        self.assertIn('ruby_bin="${FASTLANE_RUBY:-$(brew --prefix ruby)/bin/ruby}"', environment)
        self.assertIn('FASTLANE_RUBY="${ruby_bin}" \\', environment)

    def test_ios_device_inventory_masks_console_udids(self):
        fastfile = read_text(REPO_ROOT / "delivery" / "fastlane" / "Fastfile")

        self.assertIn("lane :list_test_devices", fastfile)
        self.assertIn("masked_udid(device.udid.to_s)", fastfile)
        self.assertIn("JSON.pretty_generate(payload)", fastfile)
        self.assertNotIn('UI.message("#{device.udid}', fastfile)

    def test_ios_device_input_requires_name_and_udid_together(self):
        script = REPO_ROOT / "signing" / "ios_provisioning.sh"
        env = os.environ.copy()
        env["NEW_DEVICE_NAME"] = "新测试机"
        env["NEW_DEVICE_UDID"] = ""

        result = subprocess.run(
            ["bash", str(script), "validate-device"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("must be provided together", result.stderr)

    def test_release_jobs_archive_apk_r8_outputs_and_release_metadata(self):
        for name in ANDROID_JOBS:
            pipeline = self.pipelines[name]
            with self.subTest(pipeline=name):
                self.assertContainsAll(pipeline, (
                    '${env.R8_OUTPUT_DIR}/*.txt',
                    'fileExists("${env.R8_OUTPUT_DIR}/mapping.txt")', "build/ci/**",
                    "fingerprint: true", f'android_ci.sh\" mapping-dir "$CI_BRAND" {name}',
                ))
                self.assertIn("${env.CI_APK_ARCHIVE_GLOB}", pipeline)
                self.assertIn("from project import android_contract", pipeline)
                self.assertLess(
                    pipeline.index("stage('Validate R8 Outputs')"),
                    pipeline.index("stage('Upload To Pgyer')"),
                )

    def test_android_jobs_run_fast_contract_checks_then_lint_and_assemble(self):
        android_script = read_text(REPO_ROOT / "delivery" / "android_ci.sh")
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
            "tests.delivery.test_ci_execution_contracts", "tests.tools.test_manifest_contract",
            "tests.tools.test_r8_configuration",
        ))
        self.assertContainsNone(android_script, (
            "unittest discover", "run_gradle clean",
        ))

    def test_delivery_jobs_use_trusted_tool_checkout_and_master_signing_config(self):
        for name in DELIVERY_JOBS:
            with self.subTest(pipeline=name):
                pipeline = self.pipelines[name]
                runtime = pipeline.split("stage('Validate Parameters')", 1)[0]
                self.assertContainsAll(runtime, (
                    "dir('automation')", "CI_AUTOMATION_COMMIT", "env.CI_TOOL_ROOT = pwd()",
                    "env.PYTHONPATH = env.CI_TOOL_ROOT", 'test "$(git rev-parse HEAD)" = "$CI_AUTOMATION_COMMIT"',
                ))
                self.assertRegex(runtime, r"checkout(?: scm|\(changelog: false, poll: false, scm: scm\))")
                self.assertNotIn("APP_SOURCE_ROOT/jenkins", runtime)
                self.assertNotIn("name: 'jenkins-failure-notifier'", runtime)
                if name in ANDROID_JOBS:
                    self.assertContainsAll(runtime, (
                        "dir('automation/trusted-application-config')",
                        "branches: [[name: env.CI_TRUSTED_FACTS_REF]]", "depth: 1, noTags: true, shallow: true",
                        "sparseCheckoutPaths: [[path: env.CI_BRANDS_FILE]]", "url: env.APP_REPOSITORY_URL",
                        "CI_SIGNING_CONFIG_COMMIT", "CI_SIGNING_CONFIG_ROOT",
                    ))
                selected = pipeline.split("stage('Checkout Selected", 1)[1]
                self.assertNotIn("checkout scm", selected)
                self.assertNotIn("scm: scm", selected)
                self.assertIn("url: env.APP_REPOSITORY_URL", selected)

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
        self.assertNotIn("checkout scm", pipeline.split("stage('Checkout Selected", 1)[1])

    def test_test_job_rejects_cross_brand_or_cross_type_branch(self):
        pipeline = self.pipelines["test"]

        self.assertIn("params.BRAND", pipeline)
        self.assertIn('"feature/${env.CI_BRAND}/"', pipeline)
        self.assertIn('"dev/${env.CI_BRAND}"', pipeline)
        self.assertIn('"release/${env.CI_BRAND}-"', pipeline)
        self.assertIn("resolvedBrand != env.CI_BRAND", pipeline)

    def test_mac_build_service_agent_preserves_home_and_pins_toolchains(self):
        installer = LOCAL_JENKINS_DIR / "install-pr-agent-launchdaemon.sh"
        script = read_text(installer)
        template = read_text(
            LOCAL_JENKINS_DIR
            / "com.sxmqlive.jenkins-pr-agent.plist.template"
        )
        self.assertTrue(os.access(installer, os.X_OK))
        syntax = subprocess.run(
            ["bash", "-n", str(installer)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, syntax.returncode, syntax.stderr)
        self.assertContainsAll(script, (
            'SERVICE_USER="${JENKINS_PR_SERVICE_USER:-_sxmqlive_pr}"',
            'REQUIRED_ANDROID_PLATFORM="android-$(python3 "${TOOLCHAIN_LOCK_CLI}" version androidSDK)"',
            'REQUIRED_ANDROID_BUILD_TOOLS="$(python3 "${TOOLCHAIN_LOCK_CLI}" version androidBuildTools)"',
            'REQUIRED_ANDROID_NDK="$(python3 "${TOOLCHAIN_LOCK_CLI}" version androidNDK)"',
            'AuthenticationAuthority ";DisabledUser;"',
        ))
        self.assertContainsAll(script, (
            'AGENT_SECRET_SOURCE="${JENKINS_PR_AGENT_SECRET_FILE:-}"',
            "JENKINS_PR_AGENT_SECRET_FILE must be an existing non-empty absolute path.",
            '"${AGENT_SECRET_SOURCE}"', "launchctl bootstrap system",
            "--accept-android-licenses", 'POD_EXECUTABLE="$(resolve_executable pod)"',
            "xcodebuild -checkFirstLaunchStatus", "xcrun --sdk iphoneos --show-sdk-path",
            "/bin/test", "gitee-known-hosts",
            "SHA256:+ULzij2u99B9eWYFTw1Q4ErYG/aepHLbu96PAUCoV88",
        ))
        self.assertContainsNone(script, (
            "DOCKER_EXECUTABLE", "CONTROLLER_CONTAINER", "/usr/bin/test",
        ))
        self.assertNotIn("docker", script.lower())
        self.assertContainsAll(template, (
            '<key>JENKINS_AGENT_NAME</key>\n        <string>mac-build</string>',
            '<key>ProcessType</key>\n    <string>Standard</string>',
            '<key>SessionCreate</key>\n    <true/>',
            '<key>DELIVERY_STATE_ROOT</key>\n        <string>__AGENT_WORK_DIR__/delivery-state</string>',
            "<key>GRADLE_USER_HOME</key>", "<key>UserName</key>",
            "<key>DEVELOPER_DIR</key>", "<key>FASTLANE_RUBY</key>", "<key>FASTLANE_GEM_HOME</key>",
        ))
        self.assertContainsNone(template, ("DWS_CONFIG_DIR", "Background"))
        self.assertNotIn("signing", template.lower())

        init_script = read_text(
            REMOTE_JENKINS_DIR
            / "init.groovy.d"
            / "03-configure-pr-agent.groovy"
        )
        self.assertContainsAll(init_script, (
            "PR_AGENT_REMOVE_NAMES", "jenkins.removeNode(obsoleteNode)",
        ))

    def test_remote_stack_is_private_by_default_and_adds_linux_agent(self):
        compose = read_text(REMOTE_JENKINS_DIR / "compose.yaml")

        self.assertIn(
            "${JENKINS_HTTP_HOST:-127.0.0.1}:${JENKINS_HTTP_PORT:-18080}:8080",
            compose,
        )
        self.assertNotIn("0.0.0.0:${JENKINS_HTTP_PORT:-18080}:8080", compose)
        env_example = read_text(REMOTE_JENKINS_DIR / ".env.example")
        self.assertIn("JENKINS_HTTP_HOST=127.0.0.1", env_example)
        self.assertContainsAll(compose, (
            "android-agent:", "agent-secrets-init:",
            "install -d -o 1000 -g 1000 -m 700 /android-secret",
            "install -d -o 1000 -g 1000 -m 700 /pr-secret",
            "install -d -o 1000 -g 1000 -m 700 /android-work",
            "install -d -o 1000 -g 1000 -m 700 /android-gradle",
            "install -d -o 1000 -g 1000 -m 700 /pr-work",
            "install -d -o 1000 -g 1000 -m 700 /pr-gradle",
            "jenkins_agent_secret:/run/jenkins-agent-bootstrap:ro",
            "jenkins_agent_work:/home/jenkins/agent",
            "DELIVERY_STATE_ROOT: /home/jenkins/agent/delivery-state",
            "gradle_cache:/home/jenkins/.gradle",
            "android-builder ohos-linux linux-build", "pr-agent:",
            "PR_AGENT_LABELS: pr-builder",
        ))
        self.assertGreaterEqual(compose.count("condition: service_completed_successfully"), 3)
        pr_service = compose.split("  pr-agent:", 1)[1].split("\nvolumes:", 1)[0]
        self.assertContainsAll(pr_service, (
            "jenkins_pr_agent_work:/home/jenkins/agent",
            "pr_gradle_cache:/home/jenkins/.gradle",
        ))
        self.assertNotIn("dws_state", pr_service)
        self.assertNotIn("DELIVERY_STATE_ROOT", pr_service)
        self.assertContainsNone(compose, ("ai-review-agent:", "jenkins_ai_review", ":50000"))

    def test_remote_android_agent_pins_required_toolchain(self):
        dockerfile = read_text(REMOTE_JENKINS_DIR / "android-agent" / "Dockerfile")

        self.assertContainsAll(dockerfile, (
            "jdk21", "https://mirror.nju.edu.cn/debian", '"platforms;android-${ANDROID_PLATFORM_VERSION}"',
            '"build-tools;${ANDROID_BUILD_TOOLS_VERSION}"', '"ndk;${ANDROID_NDK_VERSION}"',
            "ANDROID_COMMAND_LINE_TOOLS_SHA256", "gitee-known-hosts",
            "SHA256:+ULzij2u99B9eWYFTw1Q4ErYG/aepHLbu96PAUCoV88",
        ))

        for known_hosts in (
            LOCAL_JENKINS_DIR / "pr-agent" / "gitee-known-hosts",
            REMOTE_JENKINS_DIR / "android-agent" / "gitee-known-hosts",
        ):
            contents = read_text(known_hosts)
            self.assertIn("gitee.com ssh-ed25519 ", contents)
            self.assertNotIn("StrictHostKeyChecking no", contents)

    def test_remote_controller_provisions_exclusive_android_builder(self):
        init_script = read_text(
            REMOTE_JENKINS_DIR
            / "init.groovy.d"
            / "01-configure-android-agent.groovy"
        )
        self.assertContainsAll(init_script, (
            "android-builder ohos-linux linux-build", "Node.Mode.EXCLUSIVE",
            "setNumExecutors(1)", "JnlpAgentReceiver.DATABASE.getSecretOf",
            "rw-------", "node.getRemoteFS() != remoteFs",
        ))
        self.assertNotIn("node.setRemoteFS", init_script)
        self.assertNotIn("removeNode", init_script)
        self.assertIn("migrate legacy android-linux/ohos-linux", init_script)
        self.assertLess(init_script.index("!computer.isIdle()"), init_script.index("new DumbSlave("))

        pr_init = read_text(
            REMOTE_JENKINS_DIR
            / "init.groovy.d"
            / "03-configure-pr-agent.groovy"
        )
        self.assertContainsAll(pr_init, (
            "PR_AGENT_LABELS", "pr-builder", "Node.Mode.EXCLUSIVE",
            "node.getRemoteFS() != remoteFs",
        ))
        self.assertNotIn("node.setRemoteFS", pr_init)

        legacy_ai_cleanup = read_text(
            REMOTE_JENKINS_DIR
            / "init.groovy.d"
            / "04-remove-legacy-ai-review-agent.groovy"
        )
        self.assertContainsAll(legacy_ai_cleanup, (
            "getNode('ai-review-linux')", "removeNode(legacyNode)",
            "Files.deleteIfExists(legacyScript)",
        ))

        stack = read_text(REMOTE_JENKINS_DIR / "stack.sh")
        self.assertContainsAll(stack, ('"${CONTROLLER_SERVICE}"', '"${ANDROID_SERVICE}"', '"${PR_SERVICE}"'))

        verify = read_text(REMOTE_JENKINS_DIR / "verify.sh")
        self.assertContainsAll(verify, (
            "roles['pr']", "roles['android']", "roles['maintenance']", "load_project",
            "compose-context.sh", 'fields[3] == "01"',
        ))
        self.assertIn('test -w "${JENKINS_AGENT_WORKDIR}"', verify)
        self.assertIn("test -w /home/jenkins/.gradle", verify)
        self.assertNotIn("logs --tail=200", verify)

        remote_compose = read_text(REMOTE_JENKINS_DIR / "compose.yaml")
        android_service = remote_compose.split("  android-agent:", 1)[1].split(
            "  pr-agent:", 1
        )[0]
        self.assertContainsAll(android_service, ("mem_limit: 6g", "memswap_limit: 14g", "mem_swappiness: 60"))
        script_approval = read_text(
            CONTROLLER_JENKINS_DIR
            / "init.groovy.d"
            / "03-configure-script-approvals.groovy"
        )
        self.assertContainsAll(script_approval, (
            "approveSignature('new groovy.json.JsonSlurperClassic')",
            "approveSignature('method groovy.json.JsonSlurperClassic parseText java.lang.String')",
        ))
        self.assertIn("03-configure-script-approvals.groovy", remote_compose)
        check_host = read_text(REMOTE_JENKINS_DIR / "check-host.sh")
        self.assertIn('MIN_MEMORY_GIB="${MIN_MEMORY_GIB:-12}"', check_host)

    def test_public_tunnel_requires_local_oauth_policy(self):
        public_compose = read_text(REMOTE_JENKINS_DIR / "compose.ngrok.yaml")
        policy_template = read_text(REMOTE_JENKINS_DIR / "ngrok-policy.yml.template")
        gitignore = read_text(REPO_ROOT / ".gitignore")

        self.assertContainsAll(public_compose, (
            "--traffic-policy-file", "./runtime/ngrok-policy.yml", "./runtime/ngrok.yml",
        ))
        self.assertContainsAll(policy_template, (
            "type: oauth", "provider: google", "type: deny",
            "actions.ngrok.oauth.identity.email",
        ))
        self.assertNotIn("/git/notifyCommit", policy_template)
        self.assertEqual(
            2,
            policy_template.count("req.url.path != '/project/sxmqlive-pr-validation'"),
        )
        self.assertIn("/jenkins/remote/runtime/", gitignore)

    def test_remote_restore_never_removes_preexisting_jenkins_home(self):
        from tests.jenkins.test_ohos_signing_backup import OhosSigningBackupTest, HOME_VOLUME
        fixture = OhosSigningBackupTest('test_legacy_backup_and_restore_do_not_touch_signing_volume')
        try:
            fixture.setUp()
            fixture.volumes.write_text(json.dumps([HOME_VOLUME]))
            result = fixture.execute('restore.sh', fixture.migration())
            self.assertNotEqual(0, result.returncode)
            self.assertIn('Refusing to overwrite', result.stderr)
            self.assertEqual([HOME_VOLUME], json.loads(fixture.volumes.read_text()))
            self.assertFalse(any(command[:2] in (['volume','rm'], ['volume','create']) for command in fixture.commands()))
        finally:
            fixture.doCleanups()

    def test_unused_dws_is_not_required_by_runtime_or_new_backups(self):
        dockerfile = read_text(REMOTE_JENKINS_DIR / 'android-agent' / 'Dockerfile')
        compose = read_text(REMOTE_JENKINS_DIR / 'compose.yaml')
        verify = read_text(REMOTE_JENKINS_DIR / 'verify.sh')
        backup = read_text(REMOTE_JENKINS_DIR / 'backup.sh')
        self.assertNotIn('dws', dockerfile.lower())
        self.assertNotIn('dws', compose.lower())
        self.assertNotIn('dws version', verify)
        self.assertNotIn('dws auth', backup)
        self.assertNotIn('dws-state.tar.gz', backup)
        self.assertFalse((REMOTE_JENKINS_DIR / 'dws.sh').exists())

    def test_mac_build_agent_uses_service_fastlane_and_keeps_signing_temporary(self):
        installer = read_text(LOCAL_JENKINS_DIR / "install-pr-agent-launchdaemon.sh")
        self.assertContainsAll(installer, (
            'SERVICE_HOME="${JENKINS_PR_SERVICE_HOME:-/Users/Shared/sxmqlive-jenkins-pr}"',
            'HOME="${SERVICE_HOME}" PATH="${TOOL_PATH}" FASTLANE_RUBY="${RUBY_BIN}"',
            'FASTLANE_GEM_HOME="${FASTLANE_GEM_HOME_VALUE}"',
            'FASTLANE_GEM_HOME_VALUE="${SERVICE_HOME}/.local/share/fastlane/',
            'bash "${REPO_ROOT}/delivery/fastlane.sh" "$@"',
            'service_fastlane --install', 'version fastlane', 'version xcode', 'toolchains.py',
            'export DEVELOPER_DIR="${DEVELOPER_DIR_VALUE}"',
            'TOOL_PATH="${RUBY_BIN%/*}:${BREW_PREFIX}/bin:',
        ))
        self.assertFalse((LOCAL_JENKINS_DIR / "com.sxmqlive.jenkins-agent.plist.template").exists())
        for name in IOS_JOBS:
            self.assertContainsAll(self.pipelines[name], ("ios_signing.sh\" install", "ios_signing.sh\" cleanup"))

    def test_mac_agent_bootstrap_pins_required_build_toolchain(self):
        bootstrap = LOCAL_JENKINS_DIR / "bootstrap-mac-agent.sh"
        script = read_text(bootstrap)
        brewfile = read_text(LOCAL_JENKINS_DIR / "Brewfile")
        xcode_brewfile = read_text(LOCAL_JENKINS_DIR / "Brewfile.xcode")
        self.assertTrue(os.access(bootstrap, os.X_OK))
        for package in (
            'tap "xcodesorg/made"',
            'brew "xcodesorg/made/xcodes"',
            'brew "aria2"',
        ):
            self.assertIn(package, xcode_brewfile)
            self.assertNotIn(package, brewfile)
        for package in (
            'brew "openjdk@21"',
            'brew "python"',
            'brew "cocoapods"',
            'brew "ruby"',
            'cask "android-commandlinetools"',
        ):
            self.assertIn(package, brewfile)
        self.assertContainsAll(script, (
            'install-pr-agent-launchdaemon.sh" "${service_options[@]}"',
            'service_options+=(--accept-android-licenses)',
            'xcodes install "${required_version}"', 'sudo xcodebuild -runFirstLaunch',
            "--install-xcode", 'brew bundle --file="${XCODE_BREWFILE}"',
            "--accept-xcode-license", "--accept-android-licenses",
            "--install-controller-tunnel", 'install-controller-tunnel-launchd.sh',
            "--connect-agent",
        ))
        self.assertEqual(
            "26.3",
            read_text(LOCAL_JENKINS_DIR / ".xcode-version").strip(),
        )
        self.assertContainsNone(script, (
            "--with-controller", "--restore-controller", 'bash "${SCRIPT_DIR}/restore.sh"',
            'bash "${SCRIPT_DIR}/controller.sh\" start', "Brewfile.controller",
            "docker-compose", "raw.githubusercontent.com/Homebrew",
            'bash "${SCRIPT_DIR}/install-agent-launchd.sh"',
            'fastlane.sh" --install', "install_android_sdk",
        ))
        self.assertNotIn("colima", script.lower())

    def test_controller_tunnel_is_persistent_and_checks_host_identity(self):
        installer = LOCAL_JENKINS_DIR / "install-controller-tunnel-launchd.sh"
        script = read_text(installer)
        template = read_text(
            LOCAL_JENKINS_DIR
            / "com.sxmqlive.jenkins-controller-tunnel.plist.template"
        )

        self.assertTrue(os.access(installer, os.X_OK))
        syntax = subprocess.run(
            ["bash", "-n", str(installer)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, syntax.returncode, syntax.stderr)
        self.assertContainsAll(template, (
            "<key>RunAtLoad</key>", "<key>KeepAlive</key>",
            "<key>ThrottleInterval</key>",
        ))
        self.assertContainsAll(script, (
            "StrictHostKeyChecking yes", "ExitOnForwardFailure yes",
            "ServerAliveInterval 30", "LocalForward 127.0.0.1:",
            'ssh-keygen -F "${HOST_KEY_QUERY}"',
            'install -m 600 "${SSH_KEY_SOURCE}"',
            'install -m 600 "${KNOWN_HOSTS_SOURCE}"',
        ))
        self.assertContainsNone(script, (
            "StrictHostKeyChecking no", "0.0.0.0:${LOCAL_PORT}",
        ))

        server_installer = read_text(REMOTE_JENKINS_DIR / "init-ios-tunnel-user.sh")
        self.assertContainsAll(server_installer, (
            "restrict,port-forwarding,permitopen=", "127.0.0.1:${CONTROLLER_PORT}",
            'useradd --create-home --shell "${NOLOGIN_SHELL}"',
            "Refusing to overwrite existing authorized_keys",
        ))

    def test_mac_agent_secret_is_not_expanded_in_process_arguments(self):
        runner = read_text(LOCAL_JENKINS_DIR / "run-agent.sh")

        self.assertIn('AGENT_ARGUMENTS="${RUNTIME_DIR}/agent.args"', runner)
        self.assertIn('exec java "@${AGENT_ARGUMENTS}"', runner)
        self.assertNotIn('-secret "$(<"${SECRET_FILE}")"', runner)

    def test_ios_signing_restores_preexisting_profile(self):
        script = read_text(REPO_ROOT / "signing" / "ios_signing.sh")
        provisioning_script = read_text(
            REPO_ROOT / "signing" / "ios_provisioning.sh"
        )

        self.assertIn("PROFILE_BACKUP_FILE", script)
        self.assertIn(
            'INSTALLED_PROFILE_PATH_FILE="${STATE_DIR}/installed-profile-path.txt"',
            script,
        )
        self.assertIn(
            'PROFILE_PATH_FILE="${STATE_DIR}/profile-path.txt"',
            provisioning_script,
        )
        self.assertNotIn(
            'PROFILE_PATH_FILE="${STATE_DIR}/profile-path.txt"',
            script,
        )
        self.assertIn('STATE_DIR="$(pwd)/${STATE_DIR_INPUT}"', script)
        self.assertIn(
            'install -m 600 "${installed_profile}" "${PROFILE_BACKUP_FILE}"',
            script,
        )
        self.assertIn(
            'install -m 600 "${PROFILE_BACKUP_FILE}" "${installed_profile}"',
            script,
        )

    def test_ios_signing_cleanup_does_not_consume_downloaded_profile_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "signing state"
            downloaded_profile = (
                state_dir / "profile-output" / "current-adhoc.mobileprovision"
            )
            downloaded_profile.parent.mkdir(parents=True)
            downloaded_profile.write_bytes(b"test profile")
            (state_dir / "profile-path.txt").write_text(
                f"{downloaded_profile}\n", encoding="utf-8"
            )
            environment = os.environ | {
                "HOME": str(Path(temp_dir) / "home"),
                "IOS_SIGNING_STATE_DIR": str(state_dir),
            }

            def cleanup(script_name):
                return subprocess.run(
                    ["bash", str(REPO_ROOT / "signing" / script_name), "cleanup"],
                    capture_output=True, text=True, env=environment,
                )

            signing_cleanup = cleanup("ios_signing.sh")
            self.assertEqual(0, signing_cleanup.returncode, signing_cleanup.stderr)
            self.assertNotIn(
                "Refusing to remove unexpected provisioning profile path",
                signing_cleanup.stderr,
            )
            self.assertTrue(downloaded_profile.exists())

            provisioning_cleanup = cleanup("ios_provisioning.sh")
            self.assertEqual(0, provisioning_cleanup.returncode, provisioning_cleanup.stderr)
            self.assertFalse(state_dir.exists())

    def test_ios_cleanup_steps_do_not_mask_or_skip_each_other(self):
        test_pipeline = self.pipelines["ios.test"]
        release_pipeline = self.pipelines["ios.release"]

        self.assertGreaterEqual(
            test_pipeline.count("catchError(buildResult: 'UNSTABLE'"),
            3,
        )
        self.assertIn("ios_signing.sh\" cleanup", test_pipeline)
        self.assertIn("ios_provisioning.sh\" cleanup", test_pipeline)
        self.assertIn("catchError(buildResult: 'UNSTABLE'", release_pipeline)
        self.assertIn("ios_signing.sh\" cleanup", release_pipeline)
        self.assertIn("ios_provisioning.sh\" cleanup", release_pipeline)

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
                self.assertNotIn("checkout scm", pipeline.split("stage('Checkout Selected", 1)[1])

    def test_release_jobs_forward_optional_zentao_work_item(self):
        fragments = (
            *(f"params.get('{parameter}')" for parameter in (
                "WORK_ITEM_TYPE", "WORK_ITEM_ID", "WORK_ITEM_URL",
            )),
            '--work-item-type="$CI_WORK_ITEM_TYPE"',
            '--work-item-id="$CI_WORK_ITEM_ID"',
            '--work-item-url="$CI_WORK_ITEM_URL"',
        )
        self.assertPipelineContains(DELIVERY_JOBS, fragments)

    def test_job_template_brand_choices_match_registered_platforms(self):
        result = subprocess.run(
            [
                "python3",
                str(
                    REPO_ROOT / "jenkins" / "generate_jenkins_job_templates.py"
                ),
                "--check",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)


    def test_pr_queues_other_mrs_and_package_stages_have_no_robot_credentials(self):
        self.assertIn("disableConcurrentBuilds()", self.pipelines["pr-validation"])
        self.assertNotIn("abortPrevious", self.pipelines["pr-validation"])
        for name, stage in (("test", "Build APK"), ("ios.test", "Archive QA App")):
            pipeline = self.pipelines[name]
            build = pipeline.split("stage('" + stage + "')", 1)[1].split("stage('", 1)[0]
            self.assertNotIn("DINGTALK_", build)
            self.assertNotIn("dingtalk-webhook", build)


if __name__ == "__main__":
    unittest.main()
