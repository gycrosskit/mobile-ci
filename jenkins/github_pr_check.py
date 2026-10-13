#!/usr/bin/env python3
"""GitHub 无 secret 应用 PR 检查；受信工具与候选应用分目录。"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

CI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI_ROOT))
from project_config import load_project, ProjectConfigError
from jenkins.check_repository_contract import application_layout


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "必须使用完整小写 40 位提交 SHA")
    return value


def repository(value, expected=None):
    require(isinstance(value, dict), "缺少 GitHub 仓库")
    name = value.get("full_name")
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", name)
            and all(part not in {".", ".."} for part in name.split("/")), "无效 GitHub 仓库身份")
    require(expected is None or name == expected, "GitHub 仓库身份不匹配")
    require(type(value.get("id")) is int and value["id"] > 0 and type(value.get("fork")) is bool, "缺少仓库 ID 或 fork 身份")
    for field, url in {"html_url": "https://github.com/" + name,
                       "clone_url": "https://github.com/" + name + ".git",
                       "url": "https://api.github.com/repos/" + name}.items():
        require(value.get(field) == url, "GitHub 仓库 URL 不匹配")
    return name


def context(profile, event, environment):
    require(profile.get("scm", {}).get("provider") == "github", "只能使用 GitHub PR profile")
    require(environment.get("GITHUB_EVENT_NAME") == "pull_request" and environment.get("GITHUB_SERVER_URL") == "https://github.com", "只接受 github.com pull_request 平台事件")
    require(environment.get("RUNNER_ENVIRONMENT") == "github-hosted", "PR 检查只允许 GitHub-hosted runner")
    expected = profile["repositories"]["application"]["identity"]
    require(environment.get("GITHUB_REPOSITORY") == expected, "caller 仓库不匹配")
    require(isinstance(event, dict) and event.get("action") in {"opened", "reopened", "synchronize", "ready_for_review"}, "不支持的 PR 事件")
    repository(event.get("repository"), expected)
    require(environment.get("GITHUB_REPOSITORY_ID") == str(event["repository"]["id"]), "caller 仓库 ID 不匹配")
    number = event.get("number")
    require(type(number) is int and 0 < number < 1000000000, "无效 PR number")
    pr = event.get("pull_request")
    require(isinstance(pr, dict) and pr.get("number") == number and pr.get("state") == "open"
            and pr.get("html_url") == f"https://github.com/{expected}/pull/{number}", "PR 身份不匹配")
    require(environment.get("GITHUB_REF") == f"refs/pull/{number}/merge", "PR 平台 ref 不匹配")
    base, head = pr.get("base"), pr.get("head")
    require(isinstance(base, dict) and isinstance(head, dict), "缺少 head/base")
    repository(base.get("repo"), expected)
    require(base["repo"]["id"] == event["repository"]["id"] and base["repo"]["fork"] == event["repository"]["fork"], "base 仓库 ID/fork 不匹配")
    require(base.get("ref") == profile["repositories"]["application"]["defaultBranch"], "非受信目标分支")
    head_name = repository(head.get("repo"))
    fork = head_name != expected
    require((fork and head["repo"]["fork"] and head["repo"]["id"] != base["repo"]["id"])
            or (not fork and head["repo"]["id"] == base["repo"]["id"] and head["repo"]["fork"] == base["repo"]["fork"]), "fork 身份不一致")
    require(not fork or profile["scm"]["allowForks"], "项目禁止 fork PR")
    return {"head_sha": sha(head.get("sha")), "base_sha": sha(base.get("sha")),
            "head_ref": f"refs/pull/{number}/head", "repository": expected,
            "head_repository": head_name, "fork": fork, "number": number}


def child_environment(profile, app):
    # 候选代码仅获得 SDK/JDK 运行配置，不能继承 Token、签名或供应商凭据。
    allowed = {"PATH", "HOME", "JAVA_HOME", "LANG", "LC_ALL", "ANDROID_HOME", "ANDROID_SDK_ROOT", "GRADLE_USER_HOME"}
    result = {key: value for key, value in os.environ.items() if key in allowed}
    result.update(APP_SOURCE_ROOT=str(app), CI_TOOL_ROOT=str(CI_ROOT), CI_PROJECT_ID=profile["id"],
                  CI_UI_ENGINE="native", PYTHONPATH=str(CI_ROOT), PYTHONDONTWRITEBYTECODE="1",
                  GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return result


def git(root, *args):
    return subprocess.check_output(["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(root), *args],
                                   text=True, stderr=subprocess.PIPE, timeout=60,
                                   env=child_environment({"id": "git-check"}, root)).strip()


def checkout(root, expected_sha, binding):
    root = root.absolute()
    require(root == root.resolve() and root.is_dir() and not (root / ".git").is_symlink(), "checkout 不能使用符号链接")
    require(git(root, "rev-parse", "--show-toplevel") == str(root), "必须是独立 checkout 根目录")
    require(git(root, "remote", "get-url", "origin") in {binding["https"], binding["https"][:-4], binding["ssh"]}, "checkout origin 不匹配")
    require(git(root, "rev-parse", "HEAD") == sha(expected_sha), "checkout 实际 SHA 不匹配")
    require(not git(root, "status", "--porcelain", "--untracked-files=normal"), "checkout 存在未提交文件")
    return root


def verify(profile, evidence, ci_sha, app, base):
    ci = checkout(CI_ROOT, ci_sha, profile["repositories"]["automation"])
    app = checkout(app, evidence["head_sha"], profile["repositories"]["application"])
    base = checkout(base, evidence["base_sha"], profile["repositories"]["application"])
    roots = (ci, app, base)
    require(all(a != b and a not in b.parents for a in roots for b in roots if a is not b), "CI/head/base 根目录必须彼此隔离")
    git(app, "fetch", "--no-tags", "--no-write-fetch-head", str(base), evidence["base_sha"])
    return application_layout(app, evidence["base_sha"], profile)


def run_checks(profile, layout, app):
    environment = child_environment(profile, app)
    module = layout["testDir"].replace("/", ".") + "." + profile["contract"].get("manifestTest", "test_manifest_contract.py").removesuffix(".py")
    subprocess.run([sys.executable, "-m", "unittest", "-b", module], cwd=app, env=environment, check=True)
    for brand in profile["brands"]:
        subprocess.run(["bash", str(CI_ROOT / "delivery/android_ci.sh"), "verified-build", brand, "test", "native"],
                       cwd=app, env=environment, check=True)


def render(profile, ci_sha, workflow_sha, output):
    require(profile.get("scm", {}).get("provider") == "github", "只能生成 GitHub PR workflow")
    replacements = {"__CI_REPOSITORY__": profile["repositories"]["automation"]["identity"],
                    "__CI_SHA__": sha(ci_sha), "__WORKFLOW_SHA__": sha(workflow_sha), "__PROJECT_ID__": profile["id"],
                    "__BASE_BRANCH__": profile["repositories"]["application"]["defaultBranch"]}
    output.mkdir(parents=True, exist_ok=True)
    for name in ("application-pr.yml", "reusable-application-pr.yml"):
        text = (CI_ROOT / "examples/github-actions" / name).read_text()
        for token, value in replacements.items():
            text = text.replace(token, value)
        (output / name).write_text(text)


def read_event(environment):
    path = Path(environment["GITHUB_EVENT_PATH"]).absolute()
    runner = Path(environment["RUNNER_TEMP"]).resolve()
    require(path == path.resolve() and runner in path.parents, "事件文件必须位于 runner 临时目录且不能使用符号链接")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1 and metadata.st_size <= 1048576, "事件文件必须是有界单链接文件")
        data = source.read(1048577)
    require(len(data) <= 1048576, "事件文件过大")
    return json.loads(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("event", "check", "render"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--ci-sha")
    parser.add_argument("--workflow-sha")
    parser.add_argument("--app-root", type=Path)
    parser.add_argument("--base-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        profile = load_project(args.project)
        if args.command == "render":
            require(args.output is not None, "缺少输出目录")
            render(profile, args.ci_sha, args.workflow_sha, args.output)
        else:
            evidence = context(profile, read_event(os.environ), os.environ)
            if args.command == "event":
                # 只输出已验证的固定字段，不能注入 GitHub step outputs。
                for key in ("head_sha", "base_sha", "head_ref", "repository"):
                    print(key + "=" + evidence[key])
            else:
                require(args.app_root is not None and args.base_root is not None, "缺少 head/base checkout")
                layout = verify(profile, evidence, args.ci_sha, args.app_root, args.base_root)
                run_checks(profile, layout, args.app_root)
                print("PASS: GitHub application PR contract, manifest and native Android Debug checks")
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        print("GitHub PR 检查失败：" + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
