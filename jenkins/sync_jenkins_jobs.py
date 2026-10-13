#!/usr/bin/env python3
"""渲染、检查并同步 sxmqlive 的 Jenkins 打包 Job。"""

import argparse
import copy
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
TEMPLATE_DIR = REPO_ROOT / "jenkins" / "local"
NEUTRAL_TEMPLATE_DIR = REPO_ROOT / "jenkins" / "templates" / "local"
DEFAULT_CONTAINER = "sxmqlive-jenkins-controller"
DEFAULT_RENDER_DIR = REPO_ROOT / "build" / "jenkins-jobs"
DEFAULT_BACKUP_DIR = REPO_ROOT / "build" / "jenkins-job-backups"
PACKAGING_FOLDER = "mobile-apps"
JOB_NAME_PLACEHOLDER = "__JOB_NAME__"
CONTAINER_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
JOB_NAME_PATTERN = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_. \u4e00-\u9fff-]*")


def pin_ci_config(source: str, commit: Optional[str] = None) -> str:
    commit = commit or os.environ.get("CI_AUTOMATION_COMMIT", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise JenkinsJobSyncError("必须指定已审的 40 位 CI_AUTOMATION_COMMIT，不能使用浮动分支")
    if "{{CI_" in source:
        raise JenkinsJobSyncError("拒绝上线未渲染的项目占位符")
    return source.replace("__CI_AUTOMATION_COMMIT__", commit)


class JobSpec(NamedTuple):
    """描述一个由仓库维护的 mobile-apps Folder 发布任务。"""

    name: str
    display_name: str
    template_name: str
    script_path: str
    platform: str
    folder: str = PACKAGING_FOLDER

    @property
    def full_name(self) -> str:
        return self.folder + "/" + self.name if self.folder else self.name


JOB_SPECS: Tuple[JobSpec, ...] = (
    JobSpec(
        "sxmqlive-android-test",
        "Android QA 测试包",
        "android-test-job.xml.template",
        "jenkins/Jenkinsfile.test",
        "android",
    ),
    JobSpec(
        "sxmqlive-android-formal-release",
        "Android Release 正式包",
        "android-release-job.xml.template",
        "jenkins/Jenkinsfile.release",
        "android",
    ),
    JobSpec(
        "sxmqlive-ios-test",
        "iOS QA 测试包",
        "ios-test-job.xml.template",
        "jenkins/Jenkinsfile.ios.test",
        "ios",
    ),
    JobSpec(
        "sxmqlive-ios-formal-release",
        "iOS Release 正式包",
        "ios-release-job.xml.template",
        "jenkins/Jenkinsfile.ios.release",
        "ios",
    ),
    JobSpec(
        "sxmqlive-ohos-test",
        "鸿蒙 QA 测试包",
        "ohos-test-job.xml.template",
        "jenkins/Jenkinsfile.ohos.test",
        "harmony",
    ),
    JobSpec(
        "sxmqlive-ohos-formal-release",
        "鸿蒙 Release 正式包",
        "ohos-release-job.xml.template",
        "jenkins/Jenkinsfile.ohos.test",
        "harmony",
    ),
)


def project_job_specs(profile):
    from project_config import PLATFORM_ROLES
    specs = []
    for original in JOB_SPECS:
        index = 0 if original.name.endswith("-test") else 1
        role = PLATFORM_ROLES[original.platform][index]
        if role not in profile["jobs"]:
            continue
        name = profile["jobs"][role]["name"]
        folder, _, leaf = name.rpartition("/")
        specs.append(original._replace(name=leaf, folder=folder))
    return tuple(specs)


class JenkinsJobSyncError(RuntimeError):
    """Job 模板、Docker Controller 或同步结果不满足安全约束。"""


def _parse_xml(source: str, label: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(source)
    except ElementTree.ParseError as error:
        raise JenkinsJobSyncError("{} 不是有效的 Jenkins XML：{}".format(label, error))


def render_job_config(template: str, spec: JobSpec) -> str:
    """将模板中的固定占位符替换为任务完整名称，并验证关键契约。"""

    if not JOB_NAME_PATTERN.fullmatch(spec.name):
        raise JenkinsJobSyncError("无效 Jenkins Job 名称：{}".format(spec.name))
    count = template.count(JOB_NAME_PLACEHOLDER)
    if count != 3:
        raise JenkinsJobSyncError(
            "{} 必须且只能包含 3 个 {}，当前为 {} 个".format(
                spec.template_name,
                JOB_NAME_PLACEHOLDER,
                count,
            )
        )
    rendered = pin_ci_config(template).replace(JOB_NAME_PLACEHOLDER, spec.full_name)
    rendered = rendered.replace("<projectName>" + spec.full_name + "</projectName>",
                                "<projectName>" + spec.name + "</projectName>")
    root = _parse_xml(rendered, spec.template_name)
    if root.tag != "flow-definition":
        raise JenkinsJobSyncError("{} 根节点必须是 flow-definition".format(spec.template_name))
    if root.findtext("displayName") != spec.display_name:
        raise JenkinsJobSyncError("{} 显示名与任务清单不一致".format(spec.template_name))
    if root.findtext("definition/scriptPath") != spec.script_path:
        raise JenkinsJobSyncError("{} Script Path 与任务清单不一致".format(spec.template_name))

    project_names = [node.text for node in root.findall(".//projectName")]
    project_full_names = [node.text for node in root.findall(".//projectFullName")]
    if project_names != [spec.name] or project_full_names != [spec.full_name]:
        raise JenkinsJobSyncError("{} 未正确绑定当前 Job 完整名称".format(spec.template_name))
    scripts = [
        node.text or ""
        for node in root.findall(".//org.biouno.unochoice.CascadeChoiceParameter/script/secureScript/script")
    ]
    expected_assignment = "def jobName = '{}'".format(spec.full_name)
    if len(scripts) != 1 or expected_assignment not in scripts[0]:
        raise JenkinsJobSyncError("{} 缺少固定 jobName 绑定".format(spec.template_name))
    if JOB_NAME_PLACEHOLDER in rendered or "-next" in rendered:
        raise JenkinsJobSyncError("{} 仍包含临时任务名或未渲染占位符".format(spec.template_name))
    return rendered


def render_all(template_dir: Path = TEMPLATE_DIR, profile=None) -> Mapping[JobSpec, str]:
    """读取并渲染受管模板。"""

    rendered: Dict[JobSpec, str] = {}
    if profile:
        from project_config import project_template_values
        project_template_values(profile)
    for spec in project_job_specs(profile) if profile else JOB_SPECS:
        path = (NEUTRAL_TEMPLATE_DIR if profile and template_dir == TEMPLATE_DIR else template_dir) / spec.template_name
        try:
            template = path.read_text(encoding="utf-8")
        except OSError as error:
            raise JenkinsJobSyncError("无法读取 Jenkins 模板：{}".format(path)) from error
        if profile:
            from project_config import render_project_text
            if profile["id"] != "sxmqlive":
                from jenkins.generate_jenkins_job_templates import render_template, load_platform_brands
                brands = profile.get("brands") or load_platform_brands(Path(os.environ.get("APP_SOURCE_ROOT", Path.cwd())) / profile["contract"]["brandsFile"], profile["platforms"])[spec.platform]
                template = render_template(template, brands, spec, profile)
            template = render_project_text(template, profile)
        rendered[spec] = render_job_config(template, spec)
    return rendered


def render_to_directory(output_dir: Path, configs: Mapping[JobSpec, str]) -> None:
    """将可直接导入 Jenkins 的 XML 写入被 Git 忽略的输出目录。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    for spec, source in configs.items():
        path = output_dir / "{}.xml".format(spec.name)
        path.write_text(source, encoding="utf-8")
        os.chmod(str(path), 0o600)
        print("已渲染：{} -> {}".format(spec.name, path))


def _normalize_config(source: str, label: str) -> bytes:
    """移除 Jenkins 运行时元数据，只比较仓库模板负责的配置。"""

    root = copy.deepcopy(_parse_xml(source, label))
    actions = root.find("actions")
    if actions is not None:
        actions.clear()

    properties = root.find("properties")
    if properties is not None:
        for child in list(properties):
            if child.tag.endswith("DisableConcurrentBuildsJobProperty"):
                properties.remove(child)
        # Jenkins 可调整互不依赖的 JobProperty 顺序，权限属性的位置不应触发重载。
        properties[:] = sorted(properties, key=lambda child: child.tag)

    # Active Choices 会把页面最近一次选择缓存进 config.xml；它不是模板配置。
    for node in root.findall(".//parameters"):
        if node.attrib.get("class") == "linked-hash-map":
            node.clear()
            node.set("class", "linked-hash-map")

    for node in root.iter():
        node.attrib.pop("plugin", None)
        if node.text is not None and not node.text.strip():
            node.text = None
        if node.tail is not None and not node.tail.strip():
            node.tail = None

    # Jenkins 保存 GitSCM 时可能调整互不依赖字段的顺序。
    for node in root.findall(".//scm"):
        if node.attrib.get("class") == "hudson.plugins.git.GitSCM":
            node[:] = sorted(list(node), key=lambda child: child.tag)
    return ElementTree.tostring(root, encoding="utf-8")


def _restore_omitted_scm_browsers(
    desired: str,
    current: str,
    label: str,
) -> str:
    """补回 Jenkins 保存 GitSCM 时省略的可选仓库浏览器元数据。"""

    desired_root = _parse_xml(desired, label)
    current_root = _parse_xml(current, label)
    desired_scms = desired_root.findall(".//scm")
    current_scms = current_root.findall(".//scm")
    if len(desired_scms) != len(current_scms):
        return current

    for desired_scm, current_scm in zip(desired_scms, current_scms):
        if desired_scm.attrib.get("class") != "hudson.plugins.git.GitSCM":
            continue
        if current_scm.attrib.get("class") != "hudson.plugins.git.GitSCM":
            continue
        desired_browser = desired_scm.find("browser")
        if desired_browser is None or current_scm.find("browser") is not None:
            continue
        if desired_browser.attrib.get("class") != "hudson.plugins.git.browser.Gitee":
            continue
        if not (desired_browser.findtext("url") or "").strip():
            continue
        current_scm.append(copy.deepcopy(desired_browser))

    return ElementTree.tostring(current_root, encoding="unicode")


def configs_equivalent(desired: str, current: str, label: str) -> bool:
    """判断线上 Job 是否与仓库模板的受管配置一致。"""

    desired = preserve_job_authorization(desired, current, label)
    restored_current = _restore_omitted_scm_browsers(desired, current, label)
    return _normalize_config(desired, label) == _normalize_config(
        restored_current,
        label,
    )


def preserve_job_authorization(desired: str, current: Optional[str], label: str) -> str:
    """线上权限和启停状态由管理员维护，不能随模板同步改变。"""
    if current is None:
        return desired
    current_root = _parse_xml(current, label)
    root = _parse_xml(desired, label)
    disabled = root.find("disabled")
    current_disabled = current_root.find("disabled")
    if current_disabled is not None:
        if disabled is None:
            disabled = ElementTree.SubElement(root, "disabled")
        disabled.text = current_disabled.text
    elif disabled is not None:
        # 缺省 disabled 的现存 Job 仍是启用状态，不能套用新建模板的默认值。
        root.remove(disabled)
    authorization = current_root.find("properties/hudson.security.AuthorizationMatrixProperty")
    if authorization is None:
        return ElementTree.tostring(root, encoding="unicode")
    properties = root.find("properties")
    if properties is None:
        properties = ElementTree.SubElement(root, "properties")
    for node in properties.findall(authorization.tag):
        properties.remove(node)
    properties.append(copy.deepcopy(authorization))
    return ElementTree.tostring(root, encoding="unicode")


def _run(command: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
        raise JenkinsJobSyncError("命令执行失败：{}".format(detail))
    return result


def _docker(arguments: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    return _run(["docker"] + list(arguments), check=check)


def _validate_container_name(container: str) -> None:
    if not CONTAINER_NAME_PATTERN.fullmatch(container):
        raise JenkinsJobSyncError("无效 Docker 容器名称：{}".format(container))


def ensure_controller_running(container: str) -> None:
    """确认目标确实是正在运行的本地 Docker Controller。"""

    _validate_container_name(container)
    result = _docker(["inspect", "--format={{.State.Running}}", container])
    if result.stdout.strip() != "true":
        raise JenkinsJobSyncError("Jenkins Controller 未运行：{}".format(container))


def _container_path_exists(container: str, path: str) -> bool:
    return _docker(["exec", container, "test", "-f", path], check=False).returncode == 0


def read_container_file(container: str, path: str) -> Optional[str]:
    if not _container_path_exists(container, path):
        return None
    return _docker(["exec", container, "cat", path]).stdout


def _job_config_path(spec: JobSpec) -> str:
    return "/var/jenkins_home/" + "/".join("jobs/" + name for name in spec.full_name.split("/")) + "/config.xml"


def inspect_jobs(
    container: str,
    configs: Mapping[JobSpec, str],
) -> Tuple[List[JobSpec], Mapping[JobSpec, Optional[str]]]:
    """读取线上配置并返回缺失或与模板不一致的任务。"""

    for name in sorted({spec.folder for spec in configs if spec.folder}):
        path = "/var/jenkins_home/" + "/".join("jobs/" + part for part in name.split("/")) + "/config.xml"
        folder = read_container_file(container, path)
        if folder is None or _parse_xml(folder, name).tag != "com.cloudbees.hudson.plugins.folder.Folder":
            raise JenkinsJobSyncError("Jenkins 缺少 {} Folder，请先完成受控迁移".format(name))
    current: Dict[JobSpec, Optional[str]] = {}
    changed: List[JobSpec] = []
    for spec, desired in configs.items():
        source = read_container_file(container, _job_config_path(spec))
        current[spec] = source
        if source is None or not configs_equivalent(desired, source, spec.name):
            changed.append(spec)
    return changed, current


def _queue_is_empty(container: str) -> bool:
    source = read_container_file(container, "/var/jenkins_home/queue.xml")
    if source is None:
        return True
    root = _parse_xml(source, "Jenkins queue.xml")
    items = root.find("items")
    return items is None or len(list(items)) == 0


def _running_builds(container: str) -> List[str]:
    result = _docker(
        [
            "exec",
            container,
            "find",
            "/var/jenkins_home/jobs",
            "-path",
            "*/builds/[0-9]*/build.xml",
            "-type",
            "f",
        ]
    )
    running: List[str] = []
    for path in (line.strip() for line in result.stdout.splitlines()):
        if not path:
            continue
        source = read_container_file(container, path)
        if source is None:
            continue
        root = _parse_xml(source, path)
        result_node = root.find("result")
        if result_node is None or not (result_node.text or "").strip():
            running.append(path)
    return running


def assert_controller_idle(container: str) -> None:
    """重启前同时检查排队项和已经进入执行阶段的 Build。"""

    if not _queue_is_empty(container):
        raise JenkinsJobSyncError("Jenkins 构建队列不为空，拒绝同步并重启 Controller")
    running = _running_builds(container)
    if running:
        raise JenkinsJobSyncError(
            "仍有 {} 个构建正在执行，拒绝同步并重启 Controller".format(len(running))
        )


def _backup_configs(
    backup_dir: Path,
    current: Mapping[JobSpec, Optional[str]],
    changed: Sequence[JobSpec],
) -> Optional[Path]:
    existing = [spec for spec in changed if current.get(spec) is not None]
    if not existing:
        return None
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    destination = backup_dir / stamp
    destination.mkdir(parents=True, exist_ok=False)
    os.chmod(str(destination), 0o700)
    for spec in existing:
        path = destination / "{}.xml".format(spec.name)
        path.write_text(current[spec] or "", encoding="utf-8")
        os.chmod(str(path), 0o600)
    return destination


def _stage_config(container: str, spec: JobSpec, source_path: Path) -> str:
    job_dir = str(Path(_job_config_path(spec)).parent)
    target = "{}/config.xml.codex-job-sync-new".format(job_dir)
    _docker(["exec", "--user", "0", container, "mkdir", "-p", job_dir])
    _docker(["exec", "--user", "0", container, "chown", "jenkins:jenkins", job_dir])
    _docker(["cp", str(source_path), "{}:{}".format(container, target)])
    _docker(["exec", "--user", "0", container, "chown", "jenkins:jenkins", target])
    _docker(["exec", "--user", "0", container, "chmod", "644", target])
    return target


def _cleanup_staged(container: str, paths: Sequence[str]) -> None:
    for path in paths:
        _docker(["exec", "--user", "0", container, "rm", "-f", path], check=False)


def _wait_until_ready(container: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = _docker(
            [
                "exec",
                container,
                "curl",
                "--fail",
                "--silent",
                "http://127.0.0.1:8080/login",
            ],
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise JenkinsJobSyncError(
        "Jenkins Controller 在 {} 秒内未恢复，请使用备份检查配置".format(timeout_seconds)
    )


def apply_configs(
    container: str,
    backup_dir: Path,
    configs: Mapping[JobSpec, str],
    timeout_seconds: int,
) -> None:
    """备份、原子替换受管 Job，并在 Controller 重载后复核。"""

    ensure_controller_running(container)
    changed, current = inspect_jobs(container, configs)
    if not changed:
        print("受管 Jenkins Job 已与仓库模板一致，无需重启。")
        return
    assert_controller_idle(container)
    backup = _backup_configs(backup_dir, current, changed)

    staged: List[str] = []
    with tempfile.TemporaryDirectory(prefix="sxmqlive-jenkins-jobs-") as temp_dir:
        try:
            for spec in changed:
                local_path = Path(temp_dir) / "{}.xml".format(spec.name)
                local_path.write_text(
                    preserve_job_authorization(configs[spec], current[spec], spec.name),
                    encoding="utf-8",
                )
                os.chmod(str(local_path), 0o600)
                staged.append(_stage_config(container, spec, local_path))

            # 阶段化期间可能有人触发构建，替换前必须再次检查。
            assert_controller_idle(container)
            for spec, staged_path in zip(changed, staged):
                _docker(
                    [
                        "exec",
                        "--user",
                        "0",
                        container,
                        "mv",
                        staged_path,
                        _job_config_path(spec),
                    ]
                )
            staged = []
        finally:
            _cleanup_staged(container, staged)

    _docker(["restart", container])
    _wait_until_ready(container, timeout_seconds)
    remaining, unused = inspect_jobs(container, configs)
    if remaining:
        raise JenkinsJobSyncError(
            "Controller 已启动，但以下 Job 仍与模板不一致：{}".format(
                ", ".join(spec.name for spec in remaining)
            )
        )
    print("已同步 Jenkins Job：{}".format(", ".join(spec.name for spec in changed)))
    if backup is not None:
        print("同步前配置备份：{}".format(backup))


def check_configs(container: str, configs: Mapping[JobSpec, str]) -> int:
    ensure_controller_running(container)
    changed, unused = inspect_jobs(container, configs)
    if changed:
        print(
            "Jenkins Job 未同步：{}".format(", ".join(spec.name for spec in changed)),
            file=sys.stderr,
        )
        return 1
    print("受管 Jenkins Job 已与仓库模板一致。")
    return 0


def _add_container_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--container",
        default=DEFAULT_CONTAINER,
        help="Jenkins Controller 容器名，默认 %(default)s",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")

    render_parser = commands.add_parser("render", help="只渲染 Job XML，不连接 Jenkins")
    render_parser.add_argument("--output-dir", type=Path, default=DEFAULT_RENDER_DIR)
    render_parser.add_argument("--project", default="sxmqlive")

    check_parser = commands.add_parser("check", help="只读检查本地 Docker Controller")
    _add_container_argument(check_parser)
    check_parser.add_argument("--project", default="sxmqlive")

    apply_parser = commands.add_parser("apply", help="备份并同步本地 Docker Controller")
    _add_container_argument(apply_parser)
    apply_parser.add_argument("--project", default="sxmqlive")
    apply_parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    apply_parser.add_argument("--wait-seconds", type=int, default=120)
    return parser


def main(arguments: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        from project_config import load_project
        profile = load_project(args.project)
        if profile.get('scm', {}).get('provider', 'gitee') != 'gitee':
            raise JenkinsJobSyncError('GitHub 项目只允许独立 PR 入口，不支持 Jenkins Job 部署')
        if args.command == "apply" and profile.get("testOnly") is True:
            raise JenkinsJobSyncError("testOnly 示例只允许离线检查和渲染，拒绝部署")
        configs = render_all(profile=profile)
        if args.command == "render":
            render_to_directory(args.output_dir.resolve(), configs)
            return 0
        if args.command == "check":
            return check_configs(args.container, configs)
        if args.wait_seconds <= 0:
            raise JenkinsJobSyncError("--wait-seconds 必须大于 0")
        apply_configs(
            args.container,
            args.backup_dir.resolve(),
            configs,
            args.wait_seconds,
        )
        return 0
    except JenkinsJobSyncError as error:
        print("同步 Jenkins Job 失败：{}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
