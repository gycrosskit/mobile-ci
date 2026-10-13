#!/usr/bin/env python3
"""检查或同步 Jenkins“运维”Folder 中由仓库维护的管理任务。"""

import argparse
import hashlib
import os
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Dict, NamedTuple

try:
    from . import sync_jenkins_jobs as shared
except ImportError:
    import sync_jenkins_jobs as shared


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
TEMPLATE_DIR = REPO_ROOT / "jenkins" / "local" / "admin"
NEUTRAL_TEMPLATE_DIR = REPO_ROOT / "jenkins" / "templates" / "local" / "admin"
NEUTRAL_PIPELINE_DIR = REPO_ROOT / "jenkins" / "templates" / "pipelines"
DEFAULT_BACKUP_DIR = REPO_ROOT / "build" / "jenkins-admin-job-backups"
FOLDER_NAME = "运维"


class AdminJobSpec(NamedTuple):
    name: str
    folder: str
    template_name: str
    script_path: str

    @property
    def role(self):
        from project_config import ROLE_PIPELINES
        return next(role for role, path in ROLE_PIPELINES.items() if path == self.script_path)


JOB_SPECS = (
    AdminJobSpec("sxmqlive-signing-maintenance", "mobile-apps", "signing-maintenance-job.xml.template", "jenkins/Jenkinsfile.signing.maintenance"),
    AdminJobSpec("sxmqlive-signing-backup", FOLDER_NAME, "signing-backup-job.xml.template", "jenkins/Jenkinsfile.signing.backup"),
    AdminJobSpec("sxmqlive-pr-validation", "", "pr-validation-job.xml.template", "jenkins/Jenkinsfile.pr-validation"),
    AdminJobSpec("sxmqlive-ci-pr-validation", "", "ci-pr-validation-job.xml.template", "jenkins/Jenkinsfile.ci-pr-validation"),
    AdminJobSpec("Jenkins 失败告警验收", FOLDER_NAME, "alert-test-job.xml.template", "jenkins/Jenkinsfile.alert-test"),
    AdminJobSpec("sxmqlive-wiki-navigation", FOLDER_NAME, "wiki-navigation-job.xml.template", "jenkins/Jenkinsfile.wiki-navigation"),
)


def render_all(profile=None) -> Dict[AdminJobSpec, str]:
    configs: Dict[AdminJobSpec, str] = {}
    if profile:
        from project_config import project_template_values
        project_template_values(profile)
    for spec in JOB_SPECS:
        if profile:
            if spec.role not in profile["jobs"]:
                continue
            folder, _, name = profile["jobs"][spec.role]["name"].rpartition("/")
            spec = spec._replace(name=name, folder=folder)
        path = (NEUTRAL_TEMPLATE_DIR if profile else TEMPLATE_DIR) / spec.template_name
        try:
            source = path.read_text(encoding="utf-8")
            root = ElementTree.fromstring(source)
        except (OSError, ElementTree.ParseError) as error:
            raise shared.JenkinsJobSyncError("无效运维 Job 模板：{}".format(path)) from error
        if spec.role in ("signing_maintenance", "signing_backup"):
            placeholder = "__SIGNING_MAINTENANCE_PIPELINE__" if spec.role == "signing_maintenance" else "__SIGNING_BACKUP_PIPELINE__"
            definition = root.find("definition")
            if (definition is None or definition.get("class") != "org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition"
                    or definition.findtext("sandbox") != "false"
                    or definition.findtext("script") != placeholder):
                raise shared.JenkinsJobSyncError("签名维护 Job 必须使用完整文本审批模板")
            try:
                pipeline_path = NEUTRAL_PIPELINE_DIR / Path(spec.script_path).name if profile else REPO_ROOT / spec.script_path
                definition.find("script").text = pipeline_path.read_text(encoding="utf-8")
            except OSError as error:
                raise shared.JenkinsJobSyncError("无法读取签名维护完整脚本文本") from error
            source = ElementTree.tostring(root, encoding="unicode")
        elif spec.role == "ci_pr":
            definition = root.find("definition")
            if (definition is None or definition.get("class") != "org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition"
                    or definition.findtext("sandbox") != "true"
                    or definition.findtext("script") != "__CI_PR_VALIDATION_PIPELINE__"):
                raise shared.JenkinsJobSyncError("CI 自身门禁必须使用固定的沙箱 Pipeline 文本")
            pipeline_path = NEUTRAL_PIPELINE_DIR / Path(spec.script_path).name if profile else REPO_ROOT / spec.script_path
            definition.find("script").text = pipeline_path.read_text(encoding="utf-8")
            source = ElementTree.tostring(root, encoding="unicode")
        elif spec.role == 'app_pr' and profile and profile['id'] != 'sxmqlive':
            if root.findtext('definition/scriptPath') != spec.script_path:
                raise shared.JenkinsJobSyncError('应用门禁必须使用固定受信 Pipeline')
            definition = root.find('definition')
            definition.clear()
            definition.set('class', 'org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition')
            ElementTree.SubElement(definition, 'script').text = (NEUTRAL_PIPELINE_DIR / 'Jenkinsfile.project-pr-validation').read_text(encoding='utf-8')
            ElementTree.SubElement(definition, 'sandbox').text = 'true'
            source = ElementTree.tostring(root, encoding='unicode')
        elif root.findtext("definition/scriptPath") != spec.script_path:
            raise shared.JenkinsJobSyncError("运维 Job 模板契约错误：{}".format(path))
        if root.tag != "flow-definition":
            raise shared.JenkinsJobSyncError("运维 Job 模板根节点错误：{}".format(path))
        if not (root.findtext("displayName") or "").strip():
            raise shared.JenkinsJobSyncError("运维 Job 显示名与导入名不一致：{}".format(path))
        if profile:
            from project_config import render_project_text
            source = render_project_text(source, profile, spec.role)
        configs[spec] = shared.pin_ci_config(source)
    return configs


def signing_script_approval_hash(config: str) -> str:
    source = ElementTree.fromstring(config).findtext("definition/script")
    if source is None:
        raise shared.JenkinsJobSyncError("缺少签名维护完整脚本文本")
    return "SHA512:" + hashlib.sha512(("groovy:" + source).encode("utf-8")).hexdigest()


def _folder_path(folder: str = FOLDER_NAME) -> str:
    return "/var/jenkins_home/" + "/".join("jobs/" + name for name in folder.split("/")) + "/config.xml"


def _job_path(spec: AdminJobSpec) -> str:
    if spec.folder:
        return _folder_path(spec.folder + "/" + spec.name)
    return "/var/jenkins_home/jobs/{}/config.xml".format(spec.name)


def _normalize_admin_source(spec: AdminJobSpec, source: str) -> str:
    """规范化 Jenkins 保存 Pipeline 后产生的非声明性运行时字段。"""

    root = ElementTree.fromstring(source)
    disabled = root.find("disabled")
    if disabled is not None:
        disabled.text = "runtime-managed"
    for scm in root.findall(".//scm"):
        browser = scm.find("browser")
        if browser is not None:
            scm.remove(browser)
    for password in root.findall(".//hudson.model.PasswordParameterDefinition/defaultValue"):
        password.text = None

    for parameter in root.findall(".//hudson.model.StringParameterDefinition"):
        trim = parameter.find("trim")
        if trim is not None and trim.text == "false":
            parameter.remove(trim)

    root_triggers = root.find("triggers")
    trigger_nodes = list(root_triggers) if root_triggers is not None else []
    properties = root.find("properties")
    if properties is not None:
        for child in list(properties):
            if child.tag.endswith("PipelineTriggersJobProperty"):
                nested = child.find("triggers")
                if nested is not None:
                    trigger_nodes.extend(list(nested))
                properties.remove(child)
    if root_triggers is not None:
        root.remove(root_triggers)
    root_triggers = ElementTree.Element("triggers")
    for trigger in trigger_nodes:
        root_triggers.append(trigger)
    disabled = root.find("disabled")
    insert_at = list(root).index(disabled) if disabled is not None else len(list(root))
    root.insert(insert_at, root_triggers)

    if spec.template_name in ("pr-validation-job.xml.template", "ci-pr-validation-job.xml.template"):
        properties = root.find("properties")
        if properties is not None:
            for child in list(properties):
                if child.tag == "com.gitee.jenkins.connection.GiteeConnectionProperty":
                    properties.remove(child)
        triggers = root.find("triggers")
        if triggers is not None:
            for child in list(triggers):
                if child.tag == "com.gitee.jenkins.trigger.GiteePushTrigger":
                    triggers.remove(child)
    return ElementTree.tostring(root, encoding="unicode")


def configs_equivalent(spec: AdminJobSpec, desired: str, current: str) -> bool:
    """忽略 Pipeline 运行时缓存和 Gitee 初始化脚本注入的加密配置。"""

    roots = [_normalize_admin_source(spec, source) for source in (desired, current)]
    return shared.configs_equivalent(roots[0], roots[1], "{}/{}".format(spec.folder, spec.name))


def assert_signing_authorization(spec, source):
    if spec.role not in ("signing_maintenance", "signing_backup") or source is None:
        return
    acl = ElementTree.fromstring(source).find("properties/hudson.security.AuthorizationMatrixProperty")
    strategy = "org.jenkinsci.plugins.matrixauth.inheritance.NonInheritingStrategy"
    if (acl is None or len(acl) != 1 or acl[0].tag != "inheritanceStrategy"
            or acl[0].get("class") != strategy):
        raise shared.JenkinsJobSyncError("签名维护 Job 必须为空授权且 NonInheriting；拒绝改写现存 ACL")


def inspect(container: str, configs: Dict[AdminJobSpec, str]):
    for folder in sorted({spec.folder for spec in configs if spec.folder}):
        if shared.read_container_file(container, _folder_path(folder)) is None:
            raise shared.JenkinsJobSyncError("Jenkins 缺少 {} Folder，请先按权限规划创建".format(folder))
    current = {}
    changed = []
    for spec, desired in configs.items():
        source = shared.read_container_file(container, _job_path(spec))
        assert_signing_authorization(spec, desired)
        assert_signing_authorization(spec, source)
        current[spec] = source
        if source is None or not configs_equivalent(spec, desired, source):
            changed.append(spec)
    return changed, current


def apply(container: str, configs, backup_root: Path, wait_seconds: int) -> None:
    shared.ensure_controller_running(container)
    changed, current = inspect(container, configs)
    if not changed:
        print("Jenkins 运维 Job 已与仓库模板一致。")
        return
    for spec in changed:
        assert_signing_authorization(spec, configs[spec])
        assert_signing_authorization(spec, current[spec])
    shared.assert_controller_idle(container)
    backup = shared._backup_configs(backup_root, current, changed)
    staged = []
    with tempfile.TemporaryDirectory(prefix="sxmqlive-admin-jobs-") as temporary:
        try:
            for spec in changed:
                local = Path(temporary) / "{}.xml".format(spec.name)
                local.write_text(shared.preserve_job_authorization(
                    configs[spec], current[spec], spec.name), encoding="utf-8")
                os.chmod(str(local), 0o600)
                job_dir = str(Path(_job_path(spec)).parent)
                target = "{}/config.xml.new".format(job_dir)
                staged.append(target)
                shared._docker(["exec", "--user", "0", container, "mkdir", "-p", job_dir])
                shared._docker(["cp", str(local), "{}:{}".format(container, target)])
                # 新 Job 目录由 root 创建，Jenkins 后续需要在目录内写构建记录。
                shared._docker(["exec", "--user", "0", container, "chown", "jenkins:jenkins", job_dir, target])
            # 上传期间可能启动构建，最终替换和重启前再次确认 Controller 空闲。
            shared.assert_controller_idle(container)
            for spec, target in zip(changed, staged):
                shared._docker(["exec", "--user", "0", container, "mv", target, _job_path(spec)])
            staged = []
        finally:
            shared._cleanup_staged(container, staged)
    shared._docker(["restart", container])
    shared._wait_until_ready(container, wait_seconds)
    remaining, unused = inspect(container, configs)
    if remaining:
        raise shared.JenkinsJobSyncError("运维 Job 同步后仍有差异：{}".format("、".join(item.name for item in remaining)))
    print("已同步 Jenkins 运维 Job：{}".format("、".join(item.name for item in changed)))
    if backup:
        print("同步前配置备份：{}".format(backup))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "apply"))
    parser.add_argument("--container", default=shared.DEFAULT_CONTAINER)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--wait-seconds", type=int, default=120)
    parser.add_argument("--project", default="sxmqlive")
    parser.add_argument("--job", help="只检查或同步指定管理任务")
    args = parser.parse_args(argv)
    try:
        from project_config import load_project
        profile = load_project(args.project)
        if profile.get('scm', {}).get('provider', 'gitee') != 'gitee':
            raise shared.JenkinsJobSyncError('GitHub 项目只允许独立 PR 入口，不支持 Jenkins Job 部署')
        if args.command == "apply" and profile.get("testOnly") is True:
            raise shared.JenkinsJobSyncError("testOnly 示例只允许离线检查和渲染，拒绝部署")
        configs = render_all(profile)
        if args.job:
            if args.job not in [spec.name for spec in configs]:
                raise shared.JenkinsJobSyncError("指定管理任务不属于当前受信项目")
            configs = {spec: value for spec, value in configs.items() if spec.name == args.job}
        for spec, value in configs.items():
            if spec.role in ("signing_maintenance", "signing_backup"):
                print("{} 完整脚本审批哈希：{}".format(spec.name, signing_script_approval_hash(value)))
        if args.command == "check":
            shared.ensure_controller_running(args.container)
            changed, unused = inspect(args.container, configs)
            if changed:
                print("Jenkins 运维 Job 未同步：{}".format("、".join(item.name for item in changed)), file=sys.stderr)
                return 1
            print("Jenkins 运维 Job 已与仓库模板一致。")
            return 0
        if args.wait_seconds <= 0:
            raise shared.JenkinsJobSyncError("--wait-seconds 必须大于 0")
        apply(args.container, configs, args.backup_dir.resolve(), args.wait_seconds)
        return 0
    except shared.JenkinsJobSyncError as error:
        print("同步 Jenkins 运维 Job 失败：{}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
