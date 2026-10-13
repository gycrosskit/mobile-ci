#!/usr/bin/env python3
"""校验 CI 文档和应用/CI 接口，历史应用只保留明确的目录兼容。"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


CI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI_ROOT))
from project_config import available_project_ids, load_project, profile_for_job, validate_project, ProjectConfigError
CAPABILITIES = {"app-layout-v1", "repository-docs-v1"}
PLATFORM_TOOLS = {"android": ("affected_gradle_tests.py",),
                  "ios": ("affected_ios_host_builds.py", "test_ios_bridge_logic.sh"),
                  "harmony": ("affected_ohos_contracts.py",)}
APP_TOOLS = tuple(tool for tools in PLATFORM_TOOLS.values() for tool in tools)


def require_file(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("入口必须是仓库相对文件路径")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("必需入口缺失或越界：" + relative)


def check_markdown_links(root, files=None):
    if files is None:
        result = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others",
                                 "--exclude-standard", "-z", "*.md"], check=True, capture_output=True)
        files = sorted(set(result.stdout.decode().rstrip("\0").split("\0")) - {""})
    errors = []
    checked = 0
    for relative in files:
        path = root / relative
        if not path.is_file():
            continue
        checked += 1
        fence = None
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            marker = re.match(r"\s*(`{3,}|~{3,})(.*)$", line)
            if marker:
                character = marker[1][0]
                if fence is None:
                    fence = (character, len(marker[1]))
                elif fence[0] == character and len(marker[1]) >= fence[1] and not marker[2].strip():
                    fence = None
                continue
            if fence:
                continue
            line = re.sub(r"`+[^`]*`+", "", line)
            destinations = re.findall(r"!?\[[^\]\n]*\]\((<[^>\n]+>|[^)\n]*)\)", line)
            reference = re.match(r"\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)", line)
            if reference:
                destinations.append(reference[1])
            for destination in destinations:
                destination = destination.strip()
                destination = destination.split(">", 1)[0][1:] if destination.startswith("<") else destination.split()[0] if destination else ""
                parsed = urlsplit(destination)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                target = root / unquote(parsed.path).lstrip("/") if parsed.path.startswith("/") else path.parent / unquote(parsed.path)
                if not target.resolve().is_relative_to(root.resolve()) or not target.exists():
                    errors.append(f"{relative}:{number}: 本地链接不存在或越界：{destination}")
    if errors:
        raise ValueError("\n".join(errors))
    return checked


def application_layout(app, base_ref=None, profile=None):
    profile = profile or load_project()
    interface = profile.get("contract", {})
    manifest = app / "ci/contract.json"
    previous = None
    if base_ref:
        subprocess.run(["git", "-C", str(app), "rev-parse", "--verify", base_ref + "^{commit}"],
                       check=True, capture_output=True)
        base = subprocess.run(["git", "-C", str(app), "show", base_ref + ":ci/contract.json"], capture_output=True, text=True)
        if base.returncode == 0:
            previous = json.loads(base.stdout)
            if not isinstance(previous, dict):
                raise ValueError("基线应用 CI 接口不是对象")
            if not manifest.exists():
                raise ValueError("基线已有 ci/contract.json；禁止删除后退回历史兼容路径")
    if manifest.exists():
        require_file(app, "ci/contract.json")
        contract = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(contract, dict) or type(contract.get("version")) is not int or contract["version"] != 1:
            raise ValueError("不支持的应用 CI 接口版本；先升级可信 CI，再合入应用")
        required = contract.get("requiredCiCapabilities")
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ValueError("requiredCiCapabilities 必须是字符串数组")
        if set(required) - CAPABILITIES:
            raise ValueError("当前 CI 缺少应用要求的能力：" + ", ".join(sorted(set(required) - CAPABILITIES)))
        if previous and previous.get("version") == contract["version"]:
            baseline = previous.get("requiredCiCapabilities", [])
            if not isinstance(baseline, list) or not all(isinstance(item, str) for item in baseline):
                raise ValueError("基线 requiredCiCapabilities 必须是字符串数组")
            if set(baseline) - set(required):
                raise ValueError("同一接口版本不能移除已声明 CI 能力；退役能力须显式升级接口")
            baseline_files = previous.get("requiredAppFiles", [])
            candidate_files = contract.get("requiredAppFiles", [])
            if (not isinstance(baseline_files, list) or not all(isinstance(item, str) for item in baseline_files)
                    or not isinstance(candidate_files, list) or not all(isinstance(item, str) for item in candidate_files)
                    or set(baseline_files) - set(candidate_files)):
                raise ValueError("同一接口版本不能移除已声明应用必需文件")
        tools, tests = contract.get("toolDir"), contract.get("testDir")
        files = contract.get("requiredAppFiles", [])
        if not isinstance(files, list):
            raise ValueError("requiredAppFiles 必须是相对文件路径数组")
        for relative in files:
            require_file(app, relative)
        docs = "repository-docs-v1" in required
    else:
        tools = "ci/scripts" if (app / "ci/scripts/affected_gradle_tests.py").is_file() else "tools"
        tests = "ci/tests" if (app / "ci/tests/test_manifest_contract.py").is_file() else "tests/tools"
        docs = False
    permitted_tools = {interface.get("toolDir", "ci/scripts")}
    permitted_tests = {interface.get("testDir", "ci/tests")}
    if profile["id"] == "sxmqlive":
        permitted_tools.add("tools"); permitted_tests.add("tests/tools")
    if not isinstance(tools, str) or not isinstance(tests, str) or tools not in permitted_tools or tests not in permitted_tests:
        raise ValueError("不支持的应用脚本/测试目录映射")
    required_tools = interface.get("requiredTools", {})
    checks = {platform: list(required_tools.get(platform, PLATFORM_TOOLS[platform])) for platform in profile["platforms"]}
    if any(set(tools) - set(PLATFORM_TOOLS[platform]) for platform, tools in checks.items()):
        raise ValueError("项目声明了当前门禁不支持的检查入口；先升级可信 CI 接口")
    if "test_ios_bridge_logic.sh" in checks.get("ios", []) and "affected_ios_host_builds.py" not in checks["ios"]:
        raise ValueError("iOS bridge 检查必须声明对应影响规划入口")
    files = [interface.get("brandsFile", "ci/brands.json"), tests + "/" + interface.get("manifestTest", "test_manifest_contract.py")]
    if interface.get("buildSystem", "gradle") == "gradle":
        files.append("gradlew")
    files.extend(tools + "/" + filename for selected in checks.values() for filename in selected)
    for relative in files:
        require_file(app, relative)
    if docs:
        require_file(app, tools + "/check_repository_docs.py")
        require_file(app, tests + "/test_repository_docs.py")
    return {"toolDir": tools, "testDir": tests, "docsCheck": docs, "projectId": profile["id"], "platforms": sorted(profile["platforms"]), "checks": checks,
            "contractVersion": 1 if manifest.exists() else "legacy"}


def check_project_baseline(base_ref, project_id):
    """候选可新增项目，但不能让既有 Job 认领另一仓库/账号或降低平台检查。"""
    subprocess.run(["git", "-C", str(CI_ROOT), "rev-parse", "--verify", base_ref + "^{commit}"], check=True, capture_output=True)
    result = subprocess.run(["git", "-C", str(CI_ROOT), "show", base_ref + ":projects/" + project_id + ".json"], capture_output=True)
    if result.returncode:
        # 首次从旧版 CI 引入 profiles 的提交，由管理员完整审查与部署固定门禁。
        return
    previous = validate_project(json.loads(result.stdout.decode("utf-8")), project_id)
    current = load_project(project_id)
    for key in ("repositories", "credentials", "providers"):
        if previous.get(key) != current.get(key):
            raise ProjectConfigError("候选不能替换既有项目的仓库、凭据、账号或 Job 身份；需管理员部署")
    if any(current["jobs"].get(role) != binding for role, binding in previous["jobs"].items()):
        raise ProjectConfigError("候选不能删除或替换既有 Job 身份")
    if set(previous["platforms"]) - set(current["platforms"]):
        raise ProjectConfigError("候选不能移除既有平台检查")
    before, after = previous.get("contract", {}), current.get("contract", {})
    for key in ("toolDir", "testDir", "brandsFile", "manifestTest", "buildSystem"):
        if before.get(key) != after.get(key):
            raise ProjectConfigError("候选不能替换既有应用检查接口；需管理员部署")
    for platform in previous["platforms"]:
        old_tools = before.get("requiredTools", {}).get(platform, PLATFORM_TOOLS[platform])
        new_tools = after.get("requiredTools", {}).get(platform, PLATFORM_TOOLS[platform])
        if set(old_tools) - set(new_tools):
            raise ProjectConfigError("候选不能退役既有检查入口")
    for key in ("runtime", "backup", "signingBackup", "portal"):
        if previous.get(key) != current.get(key):
            raise ProjectConfigError("候选不能替换既有管理员运维/备份/门户策略")


def commit(root):
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("无法确认仓库实际提交")
    return sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=Path(os.environ.get("APP_SOURCE_ROOT", Path.cwd())))
    parser.add_argument("--base-ref", help="应用 MR 基线；防止删除 manifest 或同版本移除能力绕过检查")
    parser.add_argument("--project", default=os.environ.get("CI_PROJECT_ID", "sxmqlive"))
    parser.add_argument("--ci-base-ref", help="执行门禁的固定 CI 版本；拒绝候选改写已有安全身份")
    args = parser.parse_args()
    try:
        profile = load_project(args.project)
        if os.environ.get("JOB_NAME") and profile_for_job(os.environ["JOB_NAME"])["id"] != profile["id"]:
            raise ProjectConfigError("实际 Job 与项目配置不一致")
        if args.ci_base_ref:
            baseline_paths = subprocess.check_output(["git", "-C", str(CI_ROOT), "ls-tree", "-r", "--name-only", args.ci_base_ref, "--", "projects"], text=True).splitlines()
            for path in baseline_paths:
                if re.fullmatch(r"projects/[a-z][a-z0-9-]{0,63}\.json", path):
                    check_project_baseline(args.ci_base_ref, Path(path).stem)
        job_names = [job["name"] for project_id in available_project_ids() for job in load_project(project_id)["jobs"].values()]
        if len(job_names) != len(set(job_names)):
            raise ProjectConfigError("受信项目之间不能重复认领 Job 身份")
        layout = application_layout(args.app_root.resolve(), args.base_ref, profile)
        layout.update(ciSha=commit(CI_ROOT), appSha=commit(args.app_root), ciMarkdownFiles=check_markdown_links(CI_ROOT))
        for field, root in (("ciDirty", CI_ROOT), ("appDirty", args.app_root)):
            layout[field] = bool(subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"]).strip())
        print(json.dumps(layout, ensure_ascii=False))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("仓库兼容检查失败：" + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
