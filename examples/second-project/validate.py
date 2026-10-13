#!/usr/bin/env python3
"""隔离验证公开第二项目；不部署 Jenkins、不读真实凭据、不请求供应商。"""
import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

EXAMPLE = Path(__file__).resolve().parent
ROOT = EXAMPLE.parents[1]


def run(command, cwd=None):
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError("命令失败：" + " ".join(map(str, command)) + "\n" + result.stdout + result.stderr)
    return result.stdout


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def rejected(action, error):
    try:
        action()
    except error:
        return
    raise RuntimeError("负向验收未拒绝输入")


def git_fixture(path):
    run(["git", "init", "-q", "-b", "main", str(path)])
    run(["git", "-C", str(path), "add", "."])
    run(["git", "-C", str(path), "-c", "user.name=Example Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-q", "-m", "Isolated example fixture"])
    return run(["git", "-C", str(path), "rev-parse", "HEAD"]).strip()


def check_groovy(automation, profile, contract, classpath):
    from project_config import render_project_text
    pipeline = render_project_text((automation / "jenkins/templates/pipelines/Jenkinsfile.project-pr-validation").read_text(), profile, "app_pr")
    webhook = pipeline.split("stage('Validate Webhook Context')", 1)[1].split("ciFailureStage = env.STAGE_NAME", 1)[1].split("ciContextValidated = true", 1)[0]
    evidence = "if (contract.ciSha" + pipeline.split("if (contract.ciSha", 1)[1].split("env.APP_TOOL_DIR", 1)[0]
    repository = profile["repositories"]["application"]
    valid = {"giteePullRequestIid": "12", "giteeTargetBranch": "main", "giteeTargetNamespace": "example",
             "APP_REPOSITORY_URL": repository["ssh"], "giteeTargetRepoSshUrl": repository["ssh"]}
    script = "import groovy.json.JsonSlurperClassic\n"
    for identity, head, accepted in [(repository["identity"], "b" * 40, True), ("foreign/app", "b" * 40, False), (repository["identity"], "fake-SHA", False)]:
        environment = dict(valid, jsonBody=json.dumps({"repo": {"path_with_namespace": identity}, "pull_request": {"head": {"sha": head}}}))
        script += "{ ->\n def env = new JsonSlurperClassic().parseText(" + json.dumps(json.dumps(environment)) + ")\n"
        script += "def error = { message -> throw new IllegalArgumentException(message) }; boolean allowed = false\n try {\n" + webhook + "\nallowed = true\n } catch (IllegalArgumentException ignored) {}\n"
        script += "assert allowed == " + str(accepted).lower() + "\n}();\n"
    for overrides, accepted in [({}, True), ({"appSha": "f" * 40}, False), ({"ciSha": "f" * 40}, False), ({"appDirty": True}, False)]:
        value = dict(contract, **overrides)
        environment = {"CI_AUTOMATION_SHA": contract["ciSha"], "CI_MERGE_SHA": contract["appSha"]}
        script += "{ ->\n def env = new JsonSlurperClassic().parseText(" + json.dumps(json.dumps(environment)) + ")\n"
        script += "def contract = new JsonSlurperClassic().parseText(" + json.dumps(json.dumps(value)) + ")\n"
        script += "def error = { message -> throw new IllegalArgumentException(message) }; boolean allowed = false\n try {\n" + evidence + "\nallowed = true\n } catch (IllegalArgumentException ignored) {}\n"
        script += "assert allowed == " + str(accepted).lower() + "\n}();\n"
    script += "def policy = new GroovyClassLoader().parseClass(new File(" + json.dumps(str(automation / "jenkins/controller/CiAutoPromotion.groovy")) + "))\n"
    script += "try { policy.loadProject(new File(" + json.dumps(str(automation / "projects")) + "), 'example-mobile'); assert false } catch (IllegalArgumentException expected) { assert expected.message.contains('testOnly') }\n"
    script_path = automation.parent / "guards.groovy"
    script_path.write_text(script)
    run(["java", "-cp", classpath, "groovy.ui.GroovyMain", str(script_path)])
    # 复用真实 Jenkinsfile DSL 替身；静默成功/失败均不得绑定通知凭据或调用外部步骤。
    mode_fixture = ROOT / "tests/jenkins/fixtures/delivery_mode_runtime.groovy"
    run(["java", "-cp", classpath, "groovy.ui.GroovyMain", str(mode_fixture),
         str(automation / "jenkins/Jenkinsfile.test"), str(automation / "jenkins/Jenkinsfile.release")])


def validate(workspace, build, classpath):
    automation, application = workspace / "automation", workspace / "application"
    automation.mkdir()
    for name in ("project.py", "project_config.py"):
        shutil.copy2(ROOT / name, automation / name)
    for name in ("delivery", "signing", "jenkins"):
        shutil.copytree(ROOT / name, automation / name, ignore=shutil.ignore_patterns("__pycache__", "*.md"))
    profiles = automation / "projects"
    profiles.mkdir(mode=0o700)
    shutil.copy2(EXAMPLE / "project.json", profiles / "example-mobile.json")
    (profiles / "example-mobile.json").chmod(0o600)
    (automation / ".gitignore").write_text("__pycache__/\n*.pyc\nbuild/\n")
    shutil.copytree(EXAMPLE / "application", application, ignore=shutil.ignore_patterns("__pycache__", ".gradle", "build"))
    ci_sha, app_sha = git_fixture(automation), git_fixture(application)
    os.environ.update(APP_SOURCE_ROOT=str(application), CI_TOOL_ROOT=str(automation), PYTHONPATH=str(automation),
                      CI_PROJECT_ID="example-mobile", JOB_NAME="Example Mobile/android-debug", CI_UI_ENGINE="native",
                      CI_AUTOMATION_COMMIT=ci_sha, DELIVERY_STATE_ROOT=str(workspace / "receipts"))
    sys.path.insert(0, str(automation))
    from project_config import load_project, profile_for_job, validate_project, ProjectConfigError
    from project import provider_namespace
    from jenkins import generate_jenkins_job_templates as generator
    from jenkins import sync_jenkins_jobs as sync
    from jenkins import sync_jenkins_admin_jobs as admin_sync
    from delivery import release
    from delivery.delivery import delivery_receipt, delivery_operation, DeliveryContextError
    profile = load_project("example-mobile")
    require(profile["testOnly"] is True, "示例必须标为 testOnly")
    candidate = application / "ci/project.json"
    candidate.write_text(json.dumps({"id": "example-mobile", "providers": {"pgyer": {"accountId": "foreign-pgyer"}}}))
    with patch.dict(os.environ, CI_PROJECT_CONFIG=str(candidate)):
        require(load_project("example-mobile") == profile, "候选应用配置覆盖了受信 profile")
    candidate.unlink()
    contract = json.loads(run([sys.executable, str(automation / "jenkins/check_repository_contract.py"), "--project=example-mobile", "--app-root", str(application)]))
    require(contract["ciSha"] == ci_sha and contract["appSha"] == app_sha and not contract["appDirty"] and not contract["ciDirty"], "双仓 SHA 或洁净状态不一致")
    require(contract["platforms"] == ["android"], "示例必须是 Android 单平台")
    run([sys.executable, "-m", "unittest", "ci.tests.test_manifest_contract"], cwd=application)
    require(run([sys.executable, str(application / "ci/scripts/affected_gradle_tests.py"), "--base-ref", app_sha]) == "", "无差异不应触发 SDK")
    target = application / "mobile/src/main/java/org/example/mobile/MainActivity.java"
    target.write_text(target.read_text().replace("Example Mobile", "Example Mobile fixture"))
    git_fixture_change(application)
    planned = run([sys.executable, str(application / "ci/scripts/affected_gradle_tests.py"), "--base-ref", app_sha])
    require(planned.splitlines() == [":mobile:lintDebug", ":mobile:testDebugUnitTest"], "受影响任务规划错误")
    run(["git", "-C", str(application), "checkout", "--detach", app_sha])
    output = workspace / "templates"
    require(generator.synchronize(application / "ci/brands.json", False, profile, output) == 0, "模板生成失败")
    require(generator.synchronize(application / "ci/brands.json", True, profile, output) == 0, "模板检查失败")
    with contextlib.redirect_stderr(io.StringIO()), patch.object(sync, "apply_configs") as deploy:
        require(sync.main(["apply", "--project=example-mobile"]) != 0, "testOnly 示例未拒绝部署")
        deploy.assert_not_called()
    with contextlib.redirect_stderr(io.StringIO()), patch.object(admin_sync, "apply") as deploy:
        require(admin_sync.main(["apply", "--project=example-mobile"]) != 0, "testOnly 示例未拒绝管理 Job 部署")
        deploy.assert_not_called()
    configs = sync.render_all(profile=profile)
    require(len(configs) == 2, "单平台应只渲染两个打包 Job")
    for spec, xml in configs.items():
        tree = ET.fromstring(xml)
        require(tree.findtext(".//projectFullName") == spec.full_name, "Folder/Job 身份未绑定")
        require(profile["repositories"]["application"]["ssh"] in xml and "example-mobile-scm" in xml, "仓库/凭据绑定错误")
        parameters = {node.findtext("name"): node for node in tree.findall(".//parameterDefinitions/*")}
        require([node.text for node in parameters["BRAND"].findall("choices/a/string")] == ["example"], "品牌参数不是受信示例品牌")
        require([node.text for node in parameters["UI_ENGINE"].findall("choices/a/string")] == ["native"], "引擎参数不是受信 native")
        if spec.script_path.endswith(".test"):
            selector = tree.findtext(".//org.biouno.unochoice.CascadeChoiceParameter/script/secureScript/script")
            require("it == 'main'" in selector, "选择器未绑定示例默认 main 分支")
        require("sxmqlive-android" not in xml, "模板遗留项目事实")
    tasks = run(["bash", str(automation / "delivery/android_ci.sh"), "describe", "example", "test", "native"])
    require(":mobile:lintDebug" in tasks and ":mobile:assembleDebug" in tasks, "原生 Android 适配任务错误")
    credentials = run([sys.executable, str(automation / "signing/android_signing.py"), "credentials", "--config", str(application / "ci/brands.json"), "--brand", "example"])
    require(credentials.splitlines() == ["example-mobile-test-keystore", "example-mobile-test-signing-json"], "签名凭据没有绑定示例项目")
    check_groovy(automation, profile, contract, classpath)
    rejected(lambda: load_project("../example-mobile"), ProjectConfigError)
    rejected(lambda: profile_for_job("Foreign Folder/android-debug", "android_test"), ProjectConfigError)
    bad = copy.deepcopy(profile); bad["platforms"]["android"]["artifact"] = "../foreign.apk"
    rejected(lambda: validate_project(bad), ProjectConfigError)
    bad = copy.deepcopy(profile); bad["providers"]["pgyer"]["legacyNamespace"] = True
    rejected(lambda: validate_project(bad), ProjectConfigError)
    namespace, legacy = provider_namespace("pgyer")
    require(namespace == {"project_id": "example-mobile", "provider_account_id": "example-mobile-pgyer"} and not legacy, "项目账户未隔离")
    identity = {"operation": "upload", "platform": "android", "brand": "example"}
    foreign = dict(namespace, provider_account_id="foreign-pgyer")
    with delivery_operation(identity, namespace=foreign) as (_, save):
        save(phase="pending")
    rejected(lambda: delivery_receipt(identity), DeliveryContextError)
    def reopen():
        with delivery_operation(identity, namespace=namespace):
            pass
    rejected(reopen, DeliveryContextError)
    if build:
        sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
        require(sdk and (Path(sdk) / "platforms/android-36").is_dir(), "--build 缺少 Android SDK platform 36")
        require(shutil.which("gradle"), "--build 缺少预装 Gradle 8.x（至少 8.11.1）")
        run(["bash", str(automation / "delivery/android_ci.sh"), "verified-build", "example", "test", "native"], cwd=application)
    else:
        apk_dir = application / "mobile/build/outputs/apk/debug"
        apk_dir.mkdir(parents=True)
        with zipfile.ZipFile(apk_dir / "mobile-debug.apk", "w") as apk:
            apk.writestr("AndroidManifest.xml", "SYNTHETIC TEST FIXTURE; NOT AN INSTALLABLE APK")
        (apk_dir / "output-metadata.json").write_text(json.dumps({"variantName": "debug", "applicationId": "org.example.mobile", "elements": [{"outputFile": "mobile-debug.apk", "filters": [], "versionName": "1.0.0"}]}))
    parser = release.build_parser()
    context_dir = workspace / "dry-run"
    with contextlib.redirect_stdout(io.StringIO()), patch.object(release, "upload_artifact") as upload, patch.object(release, "send_release_notification") as notify:
        prepare = parser.parse_args(["prepare", "--repo", str(application), "--config", str(application / "ci/brands.json"), "--brand", "example", "--pipeline", "test", "--ui-engine", "native", "--branch", "main", "--commit", app_sha, "--manual-changelog", "Synthetic fixture wiring validation", "--output-dir", str(context_dir)])
        require(prepare.action(prepare) == 0, "发布上下文生成失败")
        common = ["--config", str(application / "ci/brands.json"), "--context", str(context_dir / "context.json"), "--dry-run"]
        upload_args = parser.parse_args(["upload", *common, "--output", str(context_dir / "release-info.json")])
        require(upload_args.action(upload_args) == 0, "上传 dry-run 失败")
        notify_args = parser.parse_args(["notify", *common, "--release-info", str(context_dir / "release-info.json")])
        require(notify_args.action(notify_args) == 0, "通知 dry-run 失败")
        upload.assert_not_called(); notify.assert_not_called()
    print("PASS: second-project trusted profile, application contract, real SHA, templates, native adapter, dry-run and negative guards.")
    print("SDK build: " + ("executed (local Debug only; no Jenkins signing verification)" if build else "NOT executed; APK is explicitly synthetic"))
    print("Vendor calls: 0; production credential reads: 0; Jenkins deployment: 0.")


def git_fixture_change(application):
    run(["git", "-C", str(application), "add", "."])
    run(["git", "-C", str(application), "-c", "user.name=Example Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "Fixture changed source"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="另执行真实 Gradle/SDK Debug 构建；不签名验收、不上传通知")
    args = parser.parse_args()
    try:
        require(sys.version_info >= (3, 9), "需要 Python 3.9 或更新版本")
        require(shutil.which("git") and shutil.which("java"), "缺少 git 或 Java/JDK；不把缺工具当成功")
        classpath = os.environ.get("GROOVY_TEST_CLASSPATH", "")
        require(classpath and all(Path(item).is_file() for item in classpath.split(os.pathsep)), "缺少 GROOVY_TEST_CLASSPATH；请指向本机已有 Groovy jar，不自动下载")
        # 子工具只获得必要运行配置，不继承 Jenkins 或供应商凭据环境变量。
        environment = {key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "JAVA_HOME", "LANG", "LC_ALL", "ANDROID_HOME", "ANDROID_SDK_ROOT", "GRADLE_USER_HOME")}
        environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        with patch.dict(os.environ, environment, clear=True), tempfile.TemporaryDirectory(prefix="mobile-ci-example-") as directory:
            validate(Path(directory).resolve(), args.build, classpath)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print("FAIL: " + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
