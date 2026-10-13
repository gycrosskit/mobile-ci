"""验证 MR 双仓库输入、隔离测试目录及真实 shell 门禁失败传播。"""

import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest

from project_config import load_project


ROOT = Path(__file__).resolve().parents[2]
PIPELINE = (ROOT / "jenkins/Jenkinsfile.pr-validation").read_text()


def stage(name):
    return PIPELINE.split("stage('" + name + "')", 1)[1].split("\n        stage(", 1)[0]


def shell(name):
    return stage(name).split("sh '''", 1)[1].split("'''", 1)[0]


def groovy_classpath():
    classpath = os.environ.get("GROOVY_TEST_CLASSPATH")
    if not classpath:
        cache = Path(os.environ.get("GRADLE_USER_HOME", Path.home() / ".gradle")) / "wrapper/dists"
        core = next(iter(sorted(cache.glob("*/*/*/lib/groovy-[0-9]*.jar"))), None)
        if core:
            classpath = os.pathsep.join(str(path) for path in sorted(core.parent.glob("groovy*.jar")))
    return classpath


class PrValidationTest(unittest.TestCase):
    def test_webhook_guard_checks_actual_plugin_comment_target(self):
        classpath = groovy_classpath()
        if not classpath or not shutil.which("java"):
            self.skipTest("需要已有 Groovy/JDK，不自动下载")
        ci_pipeline = (ROOT / "jenkins/Jenkinsfile.ci-pr-validation").read_text()
        bindings = load_project()["repositories"]
        for pipeline, repository, variable in [(PIPELINE, "application", "APP_REPOSITORY_URL"),
                                               (ci_pipeline, "automation", "CI_REPOSITORY_URL")]:
            guard = pipeline.split("stage('Validate Webhook Context')", 1)[1].split("ciFailureStage = env.STAGE_NAME", 1)[1].split("ciContextValidated = true", 1)[0]
            app = bindings[repository]["identity"]
            valid = {"giteePullRequestIid": "138", "giteeTargetBranch": bindings[repository]["defaultBranch"],
                     "giteeTargetNamespace": app.split("/")[0],
                     variable: "git@gitee.com:" + app + ".git",
                     "giteeTargetRepoSshUrl": "git@gitee.com:" + app + ".git", "giteePullRequestLastCommit": "a" * 40}
            cases = [
                ({"repo": {"path_with_namespace": app}}, {}, True),
                ({"repository": {"path_with_namespace": app}, "pull_request": {"base": {"repo": {"path_with_namespace": app}}}}, {}, True),
                ({"repo": {"path_with_namespace": app}}, {"giteeTargetRepoSshUrl": "", "giteeTargetRepoHttpUrl": "https://gitee.com/" + app + ".git"}, True),
                ({"repository": {"path_with_namespace": app}}, {}, False),
                ({"repo": {}, "pull_request": {"base": {"repo": {"path_with_namespace": app}}}}, {}, False),
                ({"repo": {"path_with_namespace": "other/repo"}, "repository": {"path_with_namespace": app}}, {}, False),
                ({"repo": {"path_with_namespace": app}, "repository": {"path_with_namespace": "other/repo"}}, {}, False),
                ({"pull_request": {"base": {"repo": {"path_with_namespace": "other/repo"}}}}, {}, False),
                ({"repo": {"path_with_namespace": app}}, {"giteeTargetNamespace": "other"}, False),
                ({"repo": {"path_with_namespace": app}}, {"giteeTargetRepoSshUrl": "git@gitee.com:other/repo.git"}, False),
                ({"repo": {"path_with_namespace": app}}, {"giteePullRequestIid": "../1"}, False),
                ({"repo": {"path_with_namespace": app}}, {"giteeTargetBranch": "dev"}, False),
                (None, {"jsonBody": "broken json"}, False),
            ]
            if repository == "automation":
                cases.extend([
                    ({"repo": {"path_with_namespace": app}}, {"giteePullRequestLastCommit": ""}, False),
                    ({"repo": {"path_with_namespace": app}}, {"giteePullRequestLastCommit": "../master"}, False),
                    ({"repo": {"path_with_namespace": bindings["application"]["identity"]}}, {}, False),
                ])
            for payload, _, _ in cases:
                if payload is not None:
                    payload.setdefault("pull_request", {}).setdefault("head", {"sha": "b" * 40})
            cases.extend([
                ({"repo": {"path_with_namespace": app}, "pull_request": {"head": {}}}, {}, False),
                ({"repo": {"path_with_namespace": app}, "pull_request": {"head": {"sha": "../master"}}}, {}, False),
            ])
            script = "import groovy.json.JsonSlurperClassic\n"
            for payload, overrides, accepted in cases:
                env = {**valid, "jsonBody": json.dumps(payload), **overrides}
                script += "{ ->\n def env = new JsonSlurperClassic().parseText(" + json.dumps(json.dumps(env)) + ")\n"
                script += " def error = { message -> throw new IllegalArgumentException(message) }\n boolean allowed = false\n try {\n" + guard + "\nallowed = true\n } catch (Exception ignored) {}\n"
                script += " assert allowed == " + str(accepted).lower() + "\n}();\n"
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "webhook.groovy"
                path.write_text(script)
                result = subprocess.run(["java", "-cp", classpath, "groovy.ui.GroovyMain", str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertEqual(2, PIPELINE.count("if (ciContextValidated && env.giteePullRequestIid"))

    def test_repository_evidence_guard_rejects_sha_mismatch_and_dirty_checkout(self):
        classpath = groovy_classpath()
        if not classpath or not shutil.which("java"):
            self.skipTest("需要已有 Groovy/JDK，不自动下载")
        ci_pipeline = (ROOT / "jenkins/Jenkinsfile.ci-pr-validation").read_text()
        for pipeline in (PIPELINE, ci_pipeline):
            guard = "if (contract.ciSha" + pipeline.split("if (contract.ciSha", 1)[1].split("env.APP_TOOL_DIR", 1)[0]
            script = "import groovy.json.JsonSlurperClassic\n"
            for overrides, accepted in (({}, True), ({"ciSha": "other"}, False), ({"appSha": "other"}, False),
                                        ({"ciDirty": True}, False), ({"appDirty": True}, False)):
                contract = {"ciSha": "ci", "appSha": "app", "ciDirty": False, "appDirty": False, **overrides}
                script += "{ ->\n def contract = new JsonSlurperClassic().parseText(" + json.dumps(json.dumps(contract)) + ")\n"
                script += " def env = [CI_AUTOMATION_SHA:'ci', CI_MERGE_SHA:" + ("'app'" if pipeline == PIPELINE else "'ci'") + ", APP_FACTS_SHA:'app']\n"
                script += " def error = { message -> throw new IllegalArgumentException(message) }\n boolean allowed = false\n try {\n" + guard + "\nallowed = true\n } catch (Exception ignored) {}\n"
                script += " assert allowed == " + str(accepted).lower() + "\n}();\n"
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "evidence.groovy"
                path.write_text(script)
                result = subprocess.run(["java", "-cp", classpath, "groovy.ui.GroovyMain", str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)

    def fixture(self, directory):
        root = Path(directory).resolve()
        automation, source, binary = (root / name for name in ("automation", "source", "bin"))
        for path in (automation / "delivery", automation / "jenkins", source / "tools", source / "ci/scripts", binary):
            path.mkdir(parents=True, exist_ok=True)
        trace = root / "trace.jsonl"
        python = binary / "python3"
        python.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['TRACE'], 'a') as output:
    output.write(json.dumps({'cwd': os.getcwd(), 'args': sys.argv[1:], 'pythonpath': os.environ['PYTHONPATH'], 'ohos_tools': os.environ.get('OHOS_CONTRACT_TOOLS')}) + '\\n')
if sys.argv[1:2] == ['-m']:
    sys.exit(int(os.environ.get('SUITE_EXIT', '0')))
if sys.argv[1].endswith('/check_repository_docs.py'):
    sys.exit(int(os.environ.get('DOCS_EXIT', '0')))
if '--execute' in sys.argv:
    sys.exit(int(os.environ.get('EXECUTE_EXIT', '0')))
print(os.environ.get('SELECTED_TASKS', ''), end='')
sys.exit(int(os.environ.get('SELECT_EXIT', '0')))
'''.replace('#!/usr/bin/env python3', '#!' + os.path.realpath(os.sys.executable)))
        python.chmod(0o755)
        java = binary / "java"
        java.write_text('#!/bin/sh\nprintf java >> "$MARKERS"\n')
        java.chmod(0o755)
        gate = automation / "delivery/with_linux_build_memory.sh"
        gate.write_text('#!/bin/sh\nprintf lock >> "$MARKERS"\nexec "$@"\n')
        env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
               "TRACE": str(trace), "MARKERS": str(root / "markers"),
               "APP_SOURCE_ROOT": str(source), "CI_TOOL_ROOT": str(automation),
               "PYTHONPATH": str(automation), "APP_TOOL_DIR": "tools", "APP_TEST_DIR": "tests/tools",
               "APP_DOCS_CHECK": "false", "CI_PROJECT_ID":"sxmqlive",
               "CI_BASE_SHA": "master-parent", "CI_MERGE_SHA": "app-merge", "CI_AUTOMATION_SHA": "ci-version",
               "ANDROID_SDK_ROOT": str(root / "sdk"), "ANDROID_HOME": ""}
        return root, env

    def run_shell(self, name, env, root):
        result = subprocess.run(["bash", "-c", shell(name)], cwd=root, env=env, capture_output=True, text=True)
        trace = Path(env["TRACE"])
        calls = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        return result, calls

    def test_ci_repository_gate_propagates_suite_failure_before_template_check(self):
        pipeline = (ROOT / "jenkins/Jenkinsfile.ci-pr-validation").read_text()
        command = pipeline.split("stage('CI Behavior and Security Contracts')", 1)[1].split("sh '''", 1)[1].split("'''", 1)[0]
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as temporary:
                root, env = self.fixture(temporary)
                env["SUITE_EXIT"] = str(exit_code)
                result = subprocess.run(["bash", "-c", command], cwd=env["CI_TOOL_ROOT"], env=env, capture_output=True, text=True)
                calls = [json.loads(line) for line in Path(env["TRACE"]).read_text().splitlines()]
                self.assertEqual(exit_code, result.returncode, result.stderr)
                self.assertEqual(1 if exit_code else 2, len(calls))
                self.assertEqual(["-m", "unittest", "discover", "-b", "-s", "tests", "-t", "."], calls[0]["args"])
                if not exit_code:
                    self.assertEqual(["jenkins/generate_jenkins_job_templates.py", "--project=sxmqlive", "--check"], calls[1]["args"])

    def test_ci_merge_guard_and_application_checkout_use_real_git(self):
        pipeline = (ROOT / "jenkins/Jenkinsfile.ci-pr-validation").read_text()
        checkout = pipeline.split("stage('Checkout CI Merge and Application Facts')", 1)[1]
        guards = [checkout.split("sh '''", 1)[1].split("'''", 1)[0],
                  shell("Checkout Pull Request Merge")]
        app_command = checkout.split("dir('application')", 1)[1].split("sh '''", 1)[1].split("'''", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate, facts, application = (root / name for name in ("candidate", "facts", "application"))
            for repo in (candidate, facts):
                repo.mkdir()
                subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
                subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "--allow-empty", "-m", repo.name], check=True)
            base = subprocess.check_output(["git", "-C", str(candidate), "rev-parse", "HEAD"], text=True).strip()
            subprocess.run(["git", "-C", str(candidate), "checkout", "-q", "-b", "feature"], check=True)
            subprocess.run(["git", "-C", str(candidate), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "--allow-empty", "-m", "feature"], check=True)
            head = subprocess.check_output(["git", "-C", str(candidate), "rev-parse", "HEAD"], text=True).strip()
            subprocess.run(["git", "-C", str(candidate), "checkout", "-q", "master"], check=True)
            subprocess.run(["git", "-C", str(candidate), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "merge", "-q", "--no-ff", "feature", "-m", "merge"], check=True)
            subprocess.run(["git", "-C", str(candidate), "update-ref", "refs/pull/1/MERGE", "HEAD"], check=True)
            subprocess.run(["git", "-C", str(candidate), "checkout", "-q", "feature"], check=True)
            subprocess.run(["git", "-C", str(candidate), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "--allow-empty", "-m", "new source head"], check=True)
            latest = subprocess.check_output(["git", "-C", str(candidate), "rev-parse", "HEAD"], text=True).strip()
            env = {**os.environ, "giteePullRequestIid": "1", "CI_EXPECTED_HEAD": head, "CI_BASE_SHA": base}
            for guard in guards:
                with self.subTest(guard=guard):
                    subprocess.run(["git", "-C", str(candidate), "checkout", "-q", "master"], check=True)
                    env["CI_EXPECTED_HEAD"] = head
                    result = subprocess.run(["bash", "-c", guard], cwd=candidate, env=env, capture_output=True, text=True)
                    self.assertEqual(0, result.returncode, result.stderr)
                    unrelated = subprocess.check_output(["git", "-C", str(facts), "rev-parse", "HEAD"], text=True).strip()
                    for invalid_head in (latest, unrelated):
                        env["CI_EXPECTED_HEAD"] = invalid_head
                        self.assertNotEqual(0, subprocess.run(["bash", "-c", guard], cwd=candidate, env=env, capture_output=True).returncode)
                    env["CI_EXPECTED_HEAD"] = head
                    subprocess.run(["git", "-C", str(candidate), "checkout", "-q", "feature"], check=True)
                    self.assertNotEqual(0, subprocess.run(["bash", "-c", guard], cwd=candidate, env=env, capture_output=True).returncode)
            application.mkdir()
            env.update(APP_REPOSITORY_URL=str(facts), APP_FACTS_SSH_KEY=str(root / "unused-fixture-key"))
            result = subprocess.run(["bash", "-c", app_command], cwd=application, env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(subprocess.check_output(["git", "-C", str(facts), "rev-parse", "master"]),
                             subprocess.check_output(["git", "-C", str(application), "rev-parse", "HEAD"]))

    def test_webhook_ref_and_repository_inputs_remain_fixed(self):
        webhook = stage("Validate Webhook Context")
        self.assertIn("env.giteePullRequestIid ==~ /[1-9][0-9]*/", webhook)
        self.assertIn("env.giteeTargetBranch != 'master'", webhook)
        self.assertIn("APP_REPOSITORY_URL = '" + load_project()["repositories"]["application"]["ssh"] + "'", PIPELINE)
        checkout = stage("Checkout Pull Request Merge")
        self.assertIn('pull/${env.giteePullRequestIid}/MERGE', checkout)
        self.assertIn("url: env.APP_REPOSITORY_URL", checkout)
        self.assertNotIn("giteeSourceRepoURL", PIPELINE)
        self.assertNotIn("giteeTargetRepoURL", PIPELINE)
        self.assertIn("env.CI_MERGE_SHA = sh(script: 'git rev-parse HEAD'", checkout)
        self.assertIn("env.CI_BASE_SHA = sh(script: 'git rev-parse HEAD^1'", checkout)
        self.assertIn('git diff --check "$CI_BASE_SHA"...HEAD', checkout)

    def test_ci_checkout_is_not_mr_changelog_and_mac_pins_same_tool_version(self):
        automation = stage("Checkout Automation")
        self.assertIn("checkout(changelog: false, poll: false, scm: scm)", automation)
        self.assertIn("env.CI_AUTOMATION_SHA =", automation)
        self.assertIn("dir('automation')", automation)
        mac = stage("Affected iOS Host Validation")
        self.assertIn("branches: [[name: env.CI_AUTOMATION_SHA]]", mac)
        self.assertIn("url: env.CI_REPOSITORY_URL", mac)
        self.assertIn('test "$(git rev-parse HEAD)" = "$CI_AUTOMATION_SHA"', mac)
        self.assertIn('test "$(git rev-parse HEAD)" = "$CI_MERGE_SHA"', mac)
        self.assertIn('test "$(git rev-parse HEAD^1)" = "$CI_BASE_SHA"', mac)
        self.assertEqual(2, PIPELINE.count('env.PYTHONPATH = env.CI_TOOL_ROOT'))
        self.assertNotIn("env.PYTHONPATH = env.APP_SOURCE_ROOT", PIPELINE)

    def test_app_layout_comes_from_checked_contract_before_affected_queries(self):
        checkout = stage("Checkout Pull Request Merge")
        self.assertIn('check_repository_contract.py" --app-root "$APP_SOURCE_ROOT"', checkout)
        self.assertIn("env.APP_TOOL_DIR = contract.toolDir", checkout)
        self.assertIn("env.APP_TEST_DIR = contract.testDir", checkout)
        self.assertIn("contract.ciSha != env.CI_AUTOMATION_SHA || contract.appSha != env.CI_MERGE_SHA", checkout)
        for filename in ("affected_ios_host_builds.py", "affected_ohos_contracts.py"):
            self.assertIn('$APP_TOOL_DIR/' + filename, checkout)
        for query in ("--required-only", "--bridge-required-only"):
            self.assertIn(query, checkout)

    def test_deterministic_tests_execute_in_each_repository_without_package_shadowing(self):
        for test_dir in ("tests/tools", "ci/tests"):
            with self.subTest(test_dir=test_dir), tempfile.TemporaryDirectory() as temporary:
                root, env = self.fixture(temporary)
                env["APP_TEST_DIR"] = test_dir
                result, calls = self.run_shell("Deterministic Quality Gate", env, root)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual([env["APP_SOURCE_ROOT"]], [call["cwd"] for call in calls])
                self.assertEqual([test_dir],
                                 [call["args"][call["args"].index("-s") + 1] for call in calls])
                self.assertTrue(all(call["pythonpath"] == env["CI_TOOL_ROOT"] for call in calls))

    def test_modern_document_failure_propagates_in_both_repository_gates(self):
        ci_pipeline = (ROOT / "jenkins/Jenkinsfile.ci-pr-validation").read_text()
        ci_command = ci_pipeline.split("stage('CI Behavior and Security Contracts')", 1)[1].split("sh '''", 1)[1].split("'''", 1)[0]
        for command in (shell("Deterministic Quality Gate"), ci_command):
            for docs_exit in (0, 9):
                with self.subTest(docs_exit=docs_exit), tempfile.TemporaryDirectory() as temporary:
                    root, env = self.fixture(temporary)
                    env.update(APP_DOCS_CHECK="true", DOCS_EXIT=str(docs_exit), APP_TOOL_DIR="ci/scripts")
                    result = subprocess.run(["bash", "-c", command], cwd=env["CI_TOOL_ROOT"], env=env, capture_output=True, text=True)
                    self.assertEqual(docs_exit, result.returncode, result.stderr)
                    calls = [json.loads(line) for line in Path(env["TRACE"]).read_text().splitlines()]
                    self.assertEqual(str(root / "source"), calls[-1]["cwd"])
                    self.assertEqual(["ci/scripts/check_repository_docs.py"], calls[-1]["args"])

    def test_application_test_failure_stops_before_document_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, env = self.fixture(temporary)
            env.update(SUITE_EXIT="7", APP_DOCS_CHECK="true", CI_PROJECT_ID="native-app")
            result, calls = self.run_shell("Deterministic Quality Gate", env, root)
            self.assertEqual(7, result.returncode)
            self.assertEqual(1, len(calls))
            self.assertEqual(env["APP_SOURCE_ROOT"], calls[0]["cwd"])

    def test_android_stage_skips_sdk_for_empty_selection_and_propagates_failures(self):
        for tool_dir in ("tools", "ci/scripts"):
            for tasks, select_exit, sdk_available, execute_exit, expected in (
                ("", 0, False, 0, 0), ("", 7, False, 0, 7),
                (":shared-business:testDebugUnitTest", 0, False, 0, 1),
                (":shared-business:testDebugUnitTest", 0, True, 0, 0),
                (":shared-business:testDebugUnitTest", 0, True, 9, 9),
            ):
                with self.subTest(tools=tool_dir, tasks=tasks, select_exit=select_exit, sdk=sdk_available, execute_exit=execute_exit), \
                        tempfile.TemporaryDirectory() as temporary:
                    root, env = self.fixture(temporary)
                    env.update(APP_TOOL_DIR=tool_dir, SELECTED_TASKS=tasks, SELECT_EXIT=str(select_exit), EXECUTE_EXIT=str(execute_exit))
                    if sdk_available:
                        (root / "sdk/platforms/android-36").mkdir(parents=True)
                    result, calls = self.run_shell("Affected Business Unit Tests", env, root)
                    self.assertEqual(expected, result.returncode, result.stderr)
                    self.assertTrue(all(call["cwd"] == env["APP_SOURCE_ROOT"] for call in calls))
                    self.assertTrue(all(call["args"][0] == tool_dir + "/affected_gradle_tests.py" for call in calls))
                    markers = Path(env["MARKERS"])
                    self.assertEqual(bool(tasks and not select_exit and sdk_available), markers.exists())
                    if markers.exists():
                        self.assertEqual("javalock", markers.read_text())

    def test_mac_source_guard_and_selected_bridge_pods_propagate_failures(self):
        for tool_dir in ("tools", "ci/scripts"):
            for bridge, pods, app_sha, base_sha, execute_exit, expected in (
                (False, False, "app-merge", "master-parent", 0, 0),
                (True, False, "app-merge", "master-parent", 0, 0),
                (False, True, "app-merge", "master-parent", 8, 8),
                (True, True, "app-merge", "master-parent", 0, 0),
                (True, True, "changed-merge", "master-parent", 0, 1),
                (True, True, "app-merge", "changed-base", 0, 1),
            ):
                with self.subTest(tools=tool_dir, bridge=bridge, pods=pods, app=app_sha, base=base_sha), \
                        tempfile.TemporaryDirectory() as temporary:
                    root, env = self.fixture(temporary)
                    git = root / "bin/git"
                    git.write_text('#!/bin/sh\ncase "$2" in HEAD) printf %s "$APP_SHA" ;; HEAD^1) printf %s "$BASE_SHA" ;; *) exit 2 ;; esac\n')
                    git.chmod(0o755)
                    bridge_script = root / "source" / tool_dir / "test_ios_bridge_logic.sh"
                    bridge_script.write_text('printf bridge >> "$MARKERS"\n')
                    env.update(APP_TOOL_DIR=tool_dir, IOS_BRIDGE_VALIDATION_REQUIRED=str(bridge).lower(),
                               IOS_VALIDATION_REQUIRED=str(pods).lower(), APP_SHA=app_sha, BASE_SHA=base_sha,
                               EXECUTE_EXIT=str(execute_exit))
                    result, calls = self.run_shell("Affected iOS Host Validation", env, root)
                    self.assertEqual(expected, result.returncode, result.stderr)
                    matched = app_sha == "app-merge" and base_sha == "master-parent"
                    self.assertEqual(int(pods and matched), len(calls))
                    if calls:
                        self.assertEqual(env["APP_SOURCE_ROOT"], calls[0]["cwd"])
                        self.assertEqual(tool_dir + "/affected_ios_host_builds.py", calls[0]["args"][0])
                        self.assertIn("--execute", calls[0]["args"])
                        self.assertNotIn("--build-host", calls[0]["args"])
                    self.assertEqual(bridge and matched, Path(env["MARKERS"]).exists())

    def test_ohos_and_ios_boundaries_preserve_agents_and_skip_duplicate_builds(self):
        self.assertIn("agent { label 'pr-builder && !ios-pr-builder' }", PIPELINE)
        ohos = stage("Affected OHOS Contracts")
        self.assertIn("GRADLE_USER_HOME = '/home/jenkins/.gradle'", ohos)
        self.assertIn('cd "$APP_SOURCE_ROOT"', ohos)
        self.assertIn('python3 "$APP_TOOL_DIR/affected_ohos_contracts.py"', ohos)
        ios = stage("Affected iOS Host Validation")
        self.assertIn("beforeAgent true", ios)
        self.assertIn("agent { label 'mac-build && ios-pr-builder' }", ios)
        self.assertNotIn("GRADLE_USER_HOME", ios)
        self.assertNotIn("OHOS_VALIDATION_REQUIRED", ios)
        self.assertIn("$APP_TOOL_DIR/test_ios_bridge_logic.sh", ios)
        self.assertNotIn("--build-host", PIPELINE)
        self.assertNotIn("ios_ci.sh preflight", PIPELINE)
        self.assertNotIn("withCredentials", PIPELINE)
        self.assertIn("addGiteeMRComment", PIPELINE)
        self.assertNotIn("abortPrevious", PIPELINE)

    def test_ohos_stage_installs_ci_tools_and_stops_on_installer_failure(self):
        for tool_dir in ("ci/scripts", "tools"):
            for legacy, installer_exit, execute_exit, override in (
                (False, 0, 0, False), (False, 7, 0, False),
                (False, 0, 9, True), (True, 7, 0, False),
            ):
                with self.subTest(tools=tool_dir, legacy=legacy, installer_exit=installer_exit,
                                  execute_exit=execute_exit, override=override), tempfile.TemporaryDirectory() as temporary:
                    root, env = self.fixture(temporary)
                    installer = root / "automation/jenkins/install_ohos_contract_tools.sh"
                    installer.write_text('printf "%s" "$1" > "$MARKERS"\nexit "$INSTALLER_EXIT"\n')
                    if legacy:
                        (root / "source" / tool_dir / "install_ohos_contract_tools.sh").touch()
                    env.update(APP_TOOL_DIR=tool_dir, INSTALLER_EXIT=str(installer_exit), EXECUTE_EXIT=str(execute_exit))
                    env.pop("OHOS_CONTRACT_TOOLS", None)
                    tools = str(root / ("custom-tools" if override else "source/build/ohos-contract-tools"))
                    if override:
                        env["OHOS_CONTRACT_TOOLS"] = tools
                    result, calls = self.run_shell("Affected OHOS Contracts", env, root)
                    self.assertEqual(execute_exit if legacy or not installer_exit else installer_exit, result.returncode, result.stderr)
                    self.assertEqual(int(legacy or not installer_exit), len(calls))
                    markers = Path(env["MARKERS"])
                    self.assertEqual(not legacy, markers.exists())
                    if markers.exists():
                        self.assertEqual(tools, markers.read_text())
                    if calls:
                        self.assertEqual(env["APP_SOURCE_ROOT"], calls[0]["cwd"])
                        self.assertEqual(tool_dir + "/affected_ohos_contracts.py", calls[0]["args"][0])
                        self.assertEqual(None if legacy else tools, calls[0]["ohos_tools"])


if __name__ == "__main__":
    unittest.main()
