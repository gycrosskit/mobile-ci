"""固定 CI 源码中的项目接线；不接受应用/MR 提供的配置路径、凭据或命令。"""

import argparse
import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


PROJECTS_DIR = Path(__file__).resolve().parent / "projects"
ADMIN_PROJECTS_DIR = Path("/private/etc/mobile-ci/projects" if sys.platform == "darwin" else "/etc/mobile-ci/projects")
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
ROLE_PIPELINES = {
    "android_test": "jenkins/Jenkinsfile.test", "android_release": "jenkins/Jenkinsfile.release",
    "ios_test": "jenkins/Jenkinsfile.ios.test", "ios_release": "jenkins/Jenkinsfile.ios.release",
    "harmony_test": "jenkins/Jenkinsfile.ohos.test", "harmony_release": "jenkins/Jenkinsfile.ohos.test",
    "app_pr": "jenkins/Jenkinsfile.pr-validation", "ci_pr": "jenkins/Jenkinsfile.ci-pr-validation",
    "wiki": "jenkins/Jenkinsfile.wiki-navigation", "signing_maintenance": "jenkins/Jenkinsfile.signing.maintenance",
    "signing_backup": "jenkins/Jenkinsfile.signing.backup", "alert": "jenkins/Jenkinsfile.alert-test",
}
PLATFORM_ROLES = {"android": ("android_test", "android_release"),
                  "ios": ("ios_test", "ios_release"), "harmony": ("harmony_test", "harmony_release")}


class ProjectConfigError(ValueError):
    pass


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value)


def relative_path(value, label="项目路径"):
    if (not isinstance(value, str) or not value or not matches(r"[A-Za-z0-9_ ./{}*-]+", value) or "\\" in value or "\x00" in value
            or PurePosixPath(value).is_absolute() or any(part in ("..", ".", "") for part in value.split("/"))):
        raise ProjectConfigError(label + "必须为非越界相对路径")
    return value


def validate_project(value, expected_id=None):
    """验证静态数据；没有 hook、shell、import 或可执行表达式字段。"""
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise ProjectConfigError("不支持的可信项目配置版本")
    allowed = {"version", "id", "repositories", "credentials", "providers", "platforms", "jobs",
               "contract", "runtime", "backup", "signingBackup", "portal", "testOnly", "brands", "scm"}
    if set(value) - allowed:
        raise ProjectConfigError("可信项目配置存在未知字段")
    if "testOnly" in value and type(value["testOnly"]) is not bool:
        raise ProjectConfigError("testOnly 必须为布尔值")
    project_id = value.get("id")
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id) or (expected_id and project_id != expected_id):
        raise ProjectConfigError("可信项目 ID 不匹配")
    scm = value.get("scm", {"provider": "gitee"})
    if (not isinstance(scm, dict) or set(scm) - {"provider", "allowForks"}
            or scm.get("provider") not in {"gitee", "github"}
            or "allowForks" in scm and type(scm["allowForks"]) is not bool):
        raise ProjectConfigError("无效 SCM provider 或 fork 策略")
    github = scm["provider"] == "github"
    if github and (set(scm) != {"provider", "allowForks"} or any(value.get(key) for key in
            ("credentials", "providers", "jobs", "runtime", "backup", "signingBackup", "portal"))):
        raise ProjectConfigError("GitHub PR profile 必须声明 fork 策略且不能绑定 Jenkins、发布凭据或管理员能力")
    if not github and "allowForks" in scm:
        raise ProjectConfigError("Gitee 不能混用 GitHub fork 策略")
    repositories = value.get("repositories")
    if not isinstance(repositories, dict) or set(repositories) != {"application", "automation"}:
        raise ProjectConfigError("项目必须声明应用与 CI 仓库")
    for role, repository in repositories.items():
        if not isinstance(repository, dict) or set(repository) - {"identity", "ssh", "https", "defaultBranch", "trustRef", "provider"}:
            raise ProjectConfigError("无效仓库接线")
        provider = repository.get("provider", scm["provider"])
        if (not isinstance(provider, str) or provider not in {"gitee", "github"} or role == "application" and provider != scm["provider"]
                or github and provider != "github"):
            raise ProjectConfigError("仓库 provider 与应用 SCM 或 GitHub PR 信任边界不一致")
        host = "github.com" if provider == "github" else "gitee.com"
        identity = repository.get("identity", "")
        branch = repository.get("defaultBranch", "")
        if provider == "github" and (not matches(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", identity)
                       or identity.split("/")[-1] in {".", ".."}):
            raise ProjectConfigError("无效 GitHub 仓库身份")
        if (not matches(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", identity)
                or repository.get("ssh") != "git@" + host + ":" + identity + ".git"
                or repository.get("https") != "https://" + host + "/" + identity + ".git"
                or not matches(r"[A-Za-z0-9][A-Za-z0-9_./-]*", branch) or ".." in branch
                or repository.get("trustRef") != "refs/heads/" + branch):
            raise ProjectConfigError("仓库 URL、身份、默认分支或可信引用不一致")
    credentials = value.get("credentials", {})
    if not isinstance(credentials, dict) or set(credentials) != (set() if github else {"scm", "giteeApi", "giteeWebhook", "giteeConnection"}):
        raise ProjectConfigError("无效凭据绑定")
    credential_values = list(credentials.values())
    providers = value.get("providers", {})
    if not isinstance(providers, dict) or set(providers) - {"pgyer", "dingtalk"}:
        raise ProjectConfigError("无效供应商绑定")
    for provider, binding in providers.items():
        fields = {"accountId", "credentialId", "webhookCredentialId", "secretCredentialId", "testerCredentialId", "legacyNamespace"}
        if not isinstance(binding, dict) or set(binding) - fields:
            raise ProjectConfigError("供应商绑定存在未知字段")
        if not matches(r"[a-z][a-z0-9_.-]{0,127}", binding.get("accountId", "")):
            raise ProjectConfigError("供应商必须声明稳定逻辑账号")
        if type(binding.get("legacyNamespace", False)) is not bool:
            raise ProjectConfigError("无效旧台账兼容声明")
        if binding.get("legacyNamespace") and (project_id != "sxmqlive" or binding["accountId"] != "sxmqlive-" + provider):
            raise ProjectConfigError("新项目不能借用旧项目台账身份")
        required = ("credentialId",) if provider == "pgyer" else ("webhookCredentialId", "secretCredentialId")
        if any(not binding.get(field) for field in required):
            raise ProjectConfigError("供应商缺少固定凭据绑定")
        credential_values.extend(item for key, item in binding.items() if key.endswith("CredentialId") or key == "credentialId")
    if any(not isinstance(item, str) or not matches(r"[A-Za-z0-9_.-]+", item) for item in credential_values):
        raise ProjectConfigError("凭据 ID 必须是固定标识")
    platforms = value.get("platforms")
    if not isinstance(platforms, dict) or not platforms or set(platforms) - set(PLATFORM_ROLES):
        raise ProjectConfigError("项目必须声明支持的平台")
    if github and (set(platforms) != {"android"} or not isinstance(platforms["android"], dict)
                   or platforms["android"].get("adapter") != "gradle"):
        raise ProjectConfigError("GitHub PR 当前仅支持原生 Android Gradle 检查")
    platform_fields = {"adapter", "engines", "modules", "buildTypes", "variants", "applicationId", "artifact", "mapping",
                       "hostDirs", "workspace", "project", "scheme", "xcconfig", "bundleId", "configuration",
                       "distribution", "renderer", "module", "product", "appIdentifier", "metadata"}
    for platform, config in platforms.items():
        if not isinstance(config, dict) or set(config) - platform_fields:
            raise ProjectConfigError("平台配置存在未知字段")
        if config.get("adapter") not in ("sxmqlive", {"android": "gradle", "ios": "xcode", "harmony": "hvigor"}[platform]):
            raise ProjectConfigError("不支持的构建适配器")
        engines = config.get("engines")
        if not isinstance(engines, list) or not engines or any(not isinstance(engine, str) for engine in engines) or len(engines) != len(set(engines)) or set(engines) - {"cmp", "kuikly", "native"}:
            raise ProjectConfigError("无效 UI 引擎声明")
        if config["adapter"] != "sxmqlive" and engines != ["native"]:
            raise ProjectConfigError("原生适配器必须绑定 native 引擎")
        if platform == "android" and (not isinstance(config.get("modules"), dict) or set(config["modules"]) != set(engines)):
            raise ProjectConfigError("Android module 必须逐引擎固定绑定")
        if platform == "ios" and config["adapter"] == "xcode" and (bool(config.get("workspace")) == bool(config.get("project"))):
            raise ProjectConfigError("原生 iOS 必须精确声明 workspace 或 project")
        for key, item in config.items():
            if isinstance(item, dict):
                expected = set(engines) if key in {"modules", "hostDirs", "workspace", "project", "scheme", "configuration"} else {"test", "release"} if key in {"buildTypes", "variants", "xcconfig"} else None
                if expected is None or not item or set(item) - expected:
                    raise ProjectConfigError("平台映射必须使用固定引擎或流水线角色")
            if key in {"modules", "buildTypes", "variants", "hostDirs"} and not isinstance(item, dict):
                raise ProjectConfigError("平台字段必须是固定角色映射")
        for key in ("artifact", "mapping", "metadata", "xcconfig", "workspace", "project", "hostDirs"):
            if key in config:
                items = config[key].values() if isinstance(config[key], dict) else [config[key]]
                for item in items:
                    relative_path(item, key)
                    if not matches(r"[A-Za-z0-9_ ./*{}-]+", item):
                        raise ProjectConfigError("平台路径不能包含命令或表达式")
        for field, item in config.items():
            items = item.values() if isinstance(item, dict) else item if isinstance(item, list) else [item]
            for text in items:
                if isinstance(text, str):
                    names = re.findall(r"\{([^{}]*)\}", text)
                    if set(names) - {"brand", "brandPascal", "buildType", "buildTypeLower", "variant", "module", "product", "engine", "distribution", "environment", "scheme", "pipeline"} or re.sub(r"\{[^{}]*\}", "", text).find('{') >= 0 or re.sub(r"\{[^{}]*\}", "", text).find('}') >= 0:
                        raise ProjectConfigError("构建模板只能使用固定数据字段")
        for key, item in config.items():
            if key in {"adapter", "engines", "artifact", "mapping", "metadata", "xcconfig", "workspace", "project", "hostDirs"}:
                continue
            items = item.values() if isinstance(item, dict) else [item]
            for text in items:
                if not isinstance(text, str) or not matches(r"[A-Za-z0-9_ ./{\}-]+", text) or ".." in text:
                    raise ProjectConfigError("构建字段必须是固定的标识或相对路径")
    brands = value.get("brands", [])
    if not isinstance(brands, list) or any(not isinstance(brand, str) or not matches(r"[a-z][a-z0-9_]*", brand) for brand in brands) or len(brands) != len(set(brands)):
        raise ProjectConfigError("品牌必须是固定且唯一的标识列表")
    if any(config["adapter"] != "sxmqlive" for config in platforms.values()) and not brands:
        raise ProjectConfigError("原生项目必须声明可信品牌列表")
    jobs = value.get("jobs")
    if not isinstance(jobs, dict) or set(jobs) - set(ROLE_PIPELINES):
        raise ProjectConfigError("无效 Job 角色")
    names = []
    for role, job in jobs.items():
        if not isinstance(job, dict) or set(job) != {"name", "pipeline"} or job.get("pipeline") != ROLE_PIPELINES[role]:
            raise ProjectConfigError("Job 必须使用对应受信 Pipeline")
        name = job.get("name", "")
        if not isinstance(name, str) or any(not matches(r"[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_. \u4e00-\u9fff-]*", part) for part in name.split("/")):
            raise ProjectConfigError("无效固定 Job 名称")
        names.append(name)
    if len(names) != len(set(names)) or (not github and any(not set(roles) <= set(jobs) for platform, roles in PLATFORM_ROLES.items() if platform in platforms)):
        raise ProjectConfigError("Job 身份重复或平台任务缺失")
    if any(role in jobs for platform, roles in PLATFORM_ROLES.items() if platform not in platforms for role in roles):
        raise ProjectConfigError("未声明的平台不能创建 Job")
    contract = value.get("contract", {})
    if not isinstance(contract, dict) or set(contract) - {"toolDir", "testDir", "brandsFile", "manifestTest", "buildSystem", "requiredTools"}:
        raise ProjectConfigError("无效应用接口契约")
    if not contract.get("brandsFile"):
        raise ProjectConfigError("打包项目必须声明可信品牌配置路径")
    for key in ("toolDir", "testDir", "brandsFile", "manifestTest"):
        if key in contract:
            relative_path(contract[key], key)
    if contract.get("buildSystem", "gradle") not in ("gradle", "xcode", "hvigor", "none"):
        raise ProjectConfigError("不支持的应用构建系统")
    tools = contract.get("requiredTools", {})
    if not isinstance(tools, dict) or set(tools) - set(platforms):
        raise ProjectConfigError("检查入口必须属于已声明平台")
    for items in tools.values():
        if not isinstance(items, list):
            raise ProjectConfigError("检查入口必须是相对路径列表")
        for item in items:
            relative_path(item, "检查入口")
    runtime = value.get("runtime", {})
    if not isinstance(runtime, dict) or set(runtime) - {"roles", "linuxUid", "macUid"}:
        raise ProjectConfigError("无效运行配置")
    if "roles" in runtime and (not isinstance(runtime["roles"], dict) or set(runtime["roles"]) != {"controller", "android", "pr", "maintenance"}
                               or any(not matches(r"[a-z][a-z0-9-]*", name) for name in runtime["roles"].values())
                               or len(set(runtime["roles"].values())) != 4):
        raise ProjectConfigError("运行角色必须唯一且完整")
    for key in ("linuxUid", "macUid"):
        if key in runtime and (type(runtime[key]) is not int or runtime[key] <= 0):
            raise ProjectConfigError("运行账号必须为非 root UID")
    backup = value.get("backup", {})
    if (not isinstance(backup, dict) or set(backup) - {"scope", "recipientAuthority", "repository", "branch"}
            or backup.get("scope", "controller") not in ("controller", "project")
            or backup.get("recipientAuthority", "controller-admin") not in ("controller-admin", "project-admin")):
        raise ProjectConfigError("无效备份责任边界")
    signing = value.get("signingBackup", {})
    if not isinstance(signing, dict) or set(signing) - {"scope", "node", "root", "recipientPath", "recipientSha256", "legacyRecipientSha256", "repository", "branch", "prefixes"}:
        raise ProjectConfigError("签名备份存在未知字段")
    if signing:
        if signing.get("scope") not in ("controller_dr", "project_signing") or not matches(r"[A-Za-z0-9_.-]+", signing.get("node", "")):
            raise ProjectConfigError("无效签名备份范围或节点")
        root = signing.get("root", "")
        if not isinstance(root, str) or not root.startswith("/") or ".." in root.split("/") or not matches(r"/[A-Za-z0-9_./ -]+", root):
            raise ProjectConfigError("签名根必须是固定绝对路径")
        relative_path(signing.get("recipientPath"), "接收公钥")
        if not matches(r"[0-9a-f]{64}", signing.get("recipientSha256", "")):
            raise ProjectConfigError("接收公钥必须绑定精确 SHA-256")
        legacy = signing.get("legacyRecipientSha256")
        if legacy is not None and (project_id != "sxmqlive" or signing["scope"] != "controller_dr"
                                  or legacy != "2387e446e9d15b45ebb0d67684550a61598515dc5eceb899d7ec31edcf3219bf"
                                  or signing["recipientSha256"] != legacy):
            raise ProjectConfigError("新接收人不能认领旧签名备份状态")
        prefixes = signing.get("prefixes", [])
        if not isinstance(prefixes, list) or signing["scope"] == "project_signing" and not prefixes:
            raise ProjectConfigError("项目签名备份必须声明材料前缀")
        for prefix in prefixes:
            relative_path(prefix, "签名材料前缀")
            if prefix.startswith("controller/") or prefix == "controller":
                raise ProjectConfigError("项目材料前缀不能包含 Controller")
        if len(prefixes) != len(set(prefixes)) or any(a != b and b.startswith(a.rstrip("/") + "/") for a in prefixes for b in prefixes):
            raise ProjectConfigError("签名材料前缀不能重复或重叠")
    for section in (backup, signing):
        if section:
            if section.get("repository") not in repositories:
                raise ProjectConfigError("备份必须使用已审仓库角色")
            branch = section.get("branch", "")
            if not matches(r"[A-Za-z0-9][A-Za-z0-9_./-]*", branch) or ".." in branch:
                raise ProjectConfigError("备份分支必须固定")
    portal = value.get("portal", {})
    if not isinstance(portal, dict) or set(portal) - {"macNode", "hdcPath", "backupUrl"}:
        raise ProjectConfigError("门户存在未知字段")
    if portal:
        if not matches(r"[A-Za-z0-9_.-]+", portal.get("macNode", "")):
            raise ProjectConfigError("门户节点必须固定")
        path = portal.get("hdcPath", "")
        if not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/") or not matches(r"/[A-Za-z0-9_./ -]+", path):
            raise ProjectConfigError("门户 HDC 必须是固定绝对路径")
        expected_url = repositories[signing.get("repository", "application")]["https"][:-4] + "/tree/" + signing.get("branch", "backup/signing")
        if portal.get("backupUrl") != expected_url:
            raise ProjectConfigError("门户备份链接必须绑定同一备份接收仓库")
    return copy.deepcopy(value)


def _read_static_json(path, *, root_owned=False):
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ProjectConfigError("受信静态文件不能使用符号链接")
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 65536
                or metadata.st_mode & 0o022 or root_owned and metadata.st_uid != 0):
            raise ProjectConfigError("可信项目配置必须是有界、不可由其他用户写入的单链接文件")
        data = source.read(65537)
        if len(data) > 65536:
            raise ProjectConfigError("受信静态文件超过大小上限")
        return json.loads(data.decode("utf-8"))


def _admin_projects_directory():
    directory = ADMIN_PROJECTS_DIR
    if directory.absolute() != directory.resolve() or any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ProjectConfigError("管理员项目目录链不能使用符号链接")
    if not directory.exists():
        return None
    for path in (directory, *directory.parents):
        metadata = path.stat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ProjectConfigError("管理员项目目录及祖先必须归 root 所有且不能由其他用户写入")
    return directory


def available_project_ids():
    """公开工具不携带私有 profile；仅补读固定的管理员目录。"""
    if PROJECTS_DIR.absolute() != PROJECTS_DIR.resolve():
        raise ProjectConfigError("可信项目目录链不能使用符号链接")
    identifiers = {path.stem for path in PROJECTS_DIR.glob("*.json")}
    directory = _admin_projects_directory()
    if directory is not None:
        identifiers.update(path.stem for path in directory.glob("*.json"))
    return sorted(identifiers)


def load_project(project_id="sxmqlive"):
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        raise ProjectConfigError("项目必须是受信配置 ID，不能是路径")
    if PROJECTS_DIR.absolute() != PROJECTS_DIR.resolve():
        raise ProjectConfigError("可信项目目录链不能使用符号链接")
    root = PROJECTS_DIR.resolve()
    path = PROJECTS_DIR / (project_id + ".json")
    if PROJECTS_DIR.is_symlink() or path.is_symlink() or path.resolve().parent != root:
        raise ProjectConfigError("可信项目配置不能使用符号链接")
    if path.exists():
        return validate_project(_read_static_json(path), project_id)
    directory = _admin_projects_directory()
    if directory is None:
        raise FileNotFoundError("项目不在固定源码或管理员受信目录中")
    profile = validate_project(_read_static_json(directory / (project_id + ".json"), root_owned=True), project_id)
    if profile.get("testOnly"):
        raise ProjectConfigError("testOnly 示例不能安装为管理员受信项目")
    return profile


def profile_for_job(job_name, role=None):
    profiles = [load_project(project_id) for project_id in available_project_ids()]
    matches = [profile for profile in profiles if any(job["name"] == job_name and (role is None or key == role)
                                                     for key, job in profile["jobs"].items())]
    if len(matches) != 1:
        raise ProjectConfigError("实际 Job 未唯一绑定受信项目角色")
    return matches[0]


PROJECT_TOKEN = re.compile(r"\{\{CI_([A-Z][A-Z0-9_]*)\}\}")


def project_template_values(profile, role=None):
    """固定字段表；模板不能查任意 JSON 路径或执行表达式。"""
    profile = validate_project(profile)
    if profile.get("scm", {}).get("provider", "gitee") != "gitee":
        raise ProjectConfigError("Jenkins 模板只支持 Gitee 项目；GitHub 使用独立 PR 入口")
    values = {"PROJECT_ID": profile["id"]}
    for name, prefix in (("application", "APP"), ("automation", "CI")):
        repository = profile["repositories"][name]
        for field in ("ssh", "https", "identity"):
            values[prefix + "_" + field.upper()] = repository[field]
        values[prefix + "_WEB"] = repository["https"][:-4]
        values[prefix + "_BRANCH"] = repository["defaultBranch"]
    automation = profile["repositories"]["automation"]
    github_tools = automation.get("provider", profile.get("scm", {}).get("provider", "gitee")) == "github"
    if github_tools and role == "ci_pr":
        raise ProjectConfigError("GitHub 工具仓库不能运行旧 Gitee CI PR 门禁")
    values["CI_CHECKOUT_URL"] = automation["https" if github_tools else "ssh"]
    values["CI_CHECKOUT_CREDENTIAL"] = "" if github_tools else profile["credentials"]["scm"]
    values["CI_BROWSER_CLASS"] = "hudson.plugins.git.browser." + ("GithubWeb" if github_tools else "Gitee")
    values["APP_REPO_PATTERN"] = ".*" + profile["repositories"]["application"]["identity"].split("/")[1] + ".git"
    repository = profile["repositories"]["automation" if role == "ci_pr" else "application"]
    values["ROLE_BRANCH"] = repository["defaultBranch"]
    values["ROLE_NAMESPACE"] = repository["identity"].split("/")[0]
    for name, job in profile["jobs"].items():
        values["JOB_" + name.upper()] = job["name"]
    for name, credential in profile["credentials"].items():
        field = re.sub(r"([a-z])([A-Z])", r"\1_\2", name).upper()
        values["CREDENTIAL_" + field] = credential
    if "signing_maintenance" in profile["jobs"]:
        folder = profile["jobs"]["signing_maintenance"]["name"].rpartition("/")[0]
        if not folder:
            raise ProjectConfigError("签名维护必须绑定固定 Folder 凭据域")
        values["SIGNING_FOLDER"] = folder
    if profile.get("signingBackup"):
        values["SIGNING_NODE"] = profile["signingBackup"]["node"]
        values["SIGNING_ROOT"] = profile["signingBackup"]["root"]
    return values


def render_project_text(source, profile, role=None):
    """只渲染受信中性模板的固定占位符；不替换公司旧字符串。"""
    values = project_template_values(profile, role)
    def substitute(match):
        field = match.group(1)
        if field not in values:
            raise ProjectConfigError("模板含未知或未绑定占位符：" + field)
        return values[field]
    rendered = PROJECT_TOKEN.sub(substitute, source)
    if "{{CI_" in rendered:
        raise ProjectConfigError("模板仍含未渲染或畸形占位符")
    return rendered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--project", default=None)
    group.add_argument("--job")
    parser.add_argument("--role", choices=tuple(ROLE_PIPELINES))
    args = parser.parse_args()
    try:
        profile = profile_for_job(args.job, args.role) if args.job else load_project(args.project or "sxmqlive")
        sys.stdout.buffer.write((json.dumps(profile, ensure_ascii=False) + "\n").encode("utf-8"))
    except (OSError, ValueError) as error:
        parser.exit(2, "可信项目配置拒绝：" + str(error) + "\n")


if __name__ == "__main__":
    main()
