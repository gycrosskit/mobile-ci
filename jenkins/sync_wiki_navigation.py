#!/usr/bin/env python3
"""从已提交 docs 生成 Wiki 首页；发布只接受远端 master 的当前版本。"""

import argparse
from collections import defaultdict
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, quote, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_config import load_project, validate_project


SOURCE_REMOTE = "git@gitee.com:example/legacy-application.git"
WIKI_REMOTE = "git@gitee.com:example/legacy-application.wiki.git"
WEB_ROOT = "https://gitee.com/example/legacy-application/blob/master/"
WIKI_WEB_ROOT = "https://gitee.com/example/legacy-application/wikis/pages/preview?sort_id="
PAGE = "项目文档首页.md"
TEAM_PAGES = {
    "团队版本提交规范": 18168019,
    "团队Git协作流程": 18168018,
    "团队AI研发协作流程": 18168017,
    "品牌迁移验收记录模板": 18168016,
}
SECTIONS = {
    "规范": "开发规范", "架构": "架构与跨端", "开发指南": "平台与业务指南",
    "品牌": "品牌与迁移", "发布": "构建、发布与协作",
}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True).stdout.decode("utf-8")


def bindings(profile=None):
    if profile is None:
        return SOURCE_REMOTE, WIKI_REMOTE, WEB_ROOT, WIKI_WEB_ROOT.split('?', 1)[0], 'master'
    profile = validate_project(profile)
    application = profile['repositories']['application']
    if profile.get('scm', {}).get('provider', 'gitee') != 'gitee' or application.get('provider', 'gitee') != 'gitee':
        raise ValueError('Wiki 导航只支持受信 Gitee 应用')
    web = application['https'][:-4]
    branch = application['defaultBranch']
    return (application['ssh'], application['ssh'][:-4] + '.wiki.git',
            web + '/blob/' + quote(branch, safe='') + '/', web + '/wikis/pages/preview', branch)


def escaped_label(label):
    label = re.sub(r"\s+", " ", label).strip()
    return re.sub(r"([\\\[\]])", r"\\\1", label)


def link(label, path, web_root=WEB_ROOT):
    return f"[{escaped_label(label)}]({web_root}{quote(path, safe='/')})"


def team_links(content, wiki_root):
    links = {}
    for label, url in re.findall(r'\[((?:\\.|[^\]\\\n])+)\]\((https://[^\s)]+)\)', content):
        try:
            parsed = urlsplit(url)
            query = parse_qs(parsed.query, keep_blank_values=True)
        except ValueError:
            continue
        if (parsed.scheme != 'https' or parsed.netloc != 'gitee.com' or parsed.fragment
                or parsed.scheme + '://' + parsed.netloc + parsed.path != wiki_root
                or set(query) != {'sort_id', 'doc_id'}
                or any(len(values) != 1 or not re.fullmatch(r'[1-9][0-9]*', values[0]) for values in query.values())):
            continue
        canonical = wiki_root + '?' + urlencode({key: query[key][0] for key in ('sort_id', 'doc_id')})
        links.setdefault(canonical, re.sub(r'\\([\\\[\]])', r'\1', label))
    return links


def navigation(repo, profile=None):
    _, _, web_root, wiki_root, branch = bindings(profile)
    paths = git(repo, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", "docs").split("\0")
    groups = defaultdict(list)
    teams = {}
    for path in sorted(paths):
        if not path or Path(path).suffix.lower() not in (".md", ".markdown"):
            continue
        content = git(repo, "show", f"HEAD:{path}")
        if profile is not None:
            for url, label in team_links(content, wiki_root).items():
                teams.setdefault(url, label)
        if path == 'docs/README.md':
            continue
        title = re.search(r"^#\s+(.+?)\s*#*\s*$", content, re.MULTILINE)
        label = title.group(1) if title else Path(path).stem
        relative = Path(path).relative_to("docs")
        category = relative.parts[0] if len(relative.parts) > 1 else "其他文档"
        if len(relative.parts) > 2:
            label = "/".join(relative.parts[1:-1]) + " / " + label
        groups[category].append(link(label, path, web_root))
    for path in ("README.md", "docs/README.md", "AGENTS.md"):
        git(repo, "cat-file", "-e", f"HEAD:{path}")
    lines = ["# 项目文档首页", "",
             f"本页由 Jenkins 从 {branch} 自动生成。工程文档正文在仓库 docs/ 维护；团队通用规范正文在 Wiki 维护。", "",
             "## 从这里开始", "",
             "- " + link("项目介绍与模块入口", "README.md", web_root),
             "- " + link("完整文档目录与任务路由", "docs/README.md", web_root),
             "- " + link("Codex 与自动化工作约定", "AGENTS.md", web_root),
             "", "## 团队通用规范", ""]
    if profile is None:
        # 旧 Python 调用保留兼容；生产 CLI 始终显式传入受信 profile。
        lines += [f"- [{title}]({WIKI_WEB_ROOT}{page_id}&doc_id=8404213)" for title, page_id in TEAM_PAGES.items()]
    else:
        lines += [f"- [{escaped_label(label)}]({url})" for url, label in teams.items()]
    order = [name for name in SECTIONS if name in groups]
    order += sorted(set(groups) - set(SECTIONS))
    for category in order:
        lines += ["", "## " + SECTIONS.get(category, category), ""]
        lines += ["- " + item for item in groups[category]]
    lines += ["", "## 维护方式", "",
              f"工程文档新增、改名或删除后，合并到 {branch} 即可自动更新本页；团队规范请维护对应 Wiki 页面。",
              "本任务只更新首页，不改团队规范正文。不要手工修改此自动生成页面；阶段进展和临时验收记录放在任务或 MR 中。", ""]
    return "\n".join(lines)


def assert_current_master(repo, sha, profile=None):
    source, _, _, _, branch = bindings(profile)
    ref = 'refs/heads/' + branch
    remote = git(repo, "ls-remote", source, ref).split()
    if len(remote) != 2 or remote[0] != sha or remote[1] != ref:
        raise ValueError(f"仅允许发布远端 {branch} 的最新提交；当前构建已过期或尚未合并")


def publish(repo, profile=None):
    _, wiki, _, _, branch = bindings(profile)
    guard_args = () if profile is None else (profile,)
    sha = git(repo, "rev-parse", "HEAD").strip()
    assert_current_master(repo, sha, *guard_args)
    content = navigation(repo, profile)
    with tempfile.TemporaryDirectory(prefix="wiki-navigation-") as directory:
        checkout = Path(directory) / "wiki"
        git(repo, "clone", "--depth", "1", wiki, str(checkout))
        page = checkout / PAGE
        if page.is_symlink():
            raise ValueError("Wiki 首页不能是符号链接")
        if page.exists() and page.read_text(encoding="utf-8") == content:
            print("Wiki 导航无变化，未创建提交。")
            return False
        page.write_text(content, encoding="utf-8")
        git(checkout, "add", "--", PAGE)
        git(checkout, "-c", "user.name=Jenkins", "-c", "user.email=jenkins@localhost",
            "commit", "-m", f"docs(wiki): 同步 {branch} {sha[:12]} 文档导航")
        # 防止较早的排队构建覆盖更新版本；Wiki 并发改动由普通 push 拒绝，禁止强推。
        assert_current_master(repo, sha, *guard_args)
        git(checkout, "push", "origin", "HEAD")
    print(f"已更新 Wiki 导航，来源 {branch} {sha[:12]}。")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source_root = os.environ.get("APP_SOURCE_ROOT")
    parser.add_argument("--repo", type=Path, default=source_root, required=not source_root,
                        help="应用源码仓库；默认 APP_SOURCE_ROOT")
    parser.add_argument("--publish", action="store_true", help="更新远端 Wiki，仅用于 master CI")
    parser.add_argument("--output", type=Path, help="保存导航预览，不修改 Wiki")
    args = parser.parse_args()
    try:
        profile = load_project(os.environ.get('CI_PROJECT_ID', 'sxmqlive'))
        bindings(profile)
        if args.publish:
            if args.output:
                parser.error("--publish 与 --output 不能同时使用")
            publish(args.repo.resolve(), profile)
        else:
            content = navigation(args.repo.resolve(), profile)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(content, encoding="utf-8")
            else:
                print(content, end="")
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError):
        # Git stderr 可能包含认证细节，不把原始异常写入 CI 日志。
        parser.exit(1, "Wiki 导航同步失败：请检查 master 版本、文档编码及 Wiki SSH 写入权限。\n")


if __name__ == "__main__":
    main()
