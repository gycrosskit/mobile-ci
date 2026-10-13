#!/usr/bin/env python3
"""生成 Jenkins 本次发布的更新日志。"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence


if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project import SOURCE_ROOT

DEFAULT_COMMIT_LIMIT = 10


class ChangelogError(RuntimeError):
    """无法从 Git 或用户输入生成更新日志。"""


def _git(repo: Path, arguments: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(repo),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = ""
        if isinstance(error, subprocess.CalledProcessError):
            detail = (error.stderr or "").strip()
        raise ChangelogError(detail or "Git 命令执行失败") from error
    return result.stdout.strip()


def git_value(repo: Path, arguments: Sequence[str], fallback: str) -> str:
    """发布上下文允许 Git 元数据不可用；更新日志生成仍使用严格读取。"""
    try:
        return _git(repo, arguments) or fallback
    except ChangelogError:
        return fallback


def _as_markdown_bullets(lines: Sequence[str]) -> str:
    bullets = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("- ", "* ", "• ")):
            line = line[2:].strip()
        bullets.append("- {}".format(line))
    return "\n".join(bullets)


def _commit_subjects(repo: Path, revision_range: str, limit: Optional[int]) -> List[str]:
    arguments = ["log", revision_range, "--pretty=format:%s"]
    if limit is not None:
        arguments.extend(["-n", str(limit)])
    output = _git(repo, arguments)
    return output.splitlines() if output else []


def _is_commit(repo: Path, revision: str) -> bool:
    try:
        _git(repo, ["rev-parse", "--verify", "{}^{{commit}}".format(revision)])
        return True
    except ChangelogError:
        return False


def generate_changelog(
    repo: Path,
    manual_description: str = "",
    last_successful_commit: str = "",
    fallback_limit: int = DEFAULT_COMMIT_LIMIT,
) -> str:
    """按手工内容、成功构建区间、最近提交的优先级生成 Markdown 列表。"""
    manual = _as_markdown_bullets(manual_description.splitlines())
    if manual:
        return manual

    last_commit = last_successful_commit.strip()
    if last_commit and _is_commit(repo, last_commit):
        try:
            subjects = _commit_subjects(repo, "{}..HEAD".format(last_commit), limit=None)
            if subjects:
                return _as_markdown_bullets(subjects)
            return "- 本次构建未包含新的 Git 提交"
        except ChangelogError:
            print("警告：无法读取成功构建区间，回退到最近提交。", file=sys.stderr)

    if last_commit:
        print("警告：上一次成功构建 Commit 不可用，回退到最近提交。", file=sys.stderr)

    try:
        subjects = _commit_subjects(repo, "HEAD", limit=fallback_limit)
    except ChangelogError:
        print("警告：无法读取 Git 更新日志，发布流程将继续。", file=sys.stderr)
        return "- 未获取到 Git 更新记录"
    if not subjects:
        return "- 未获取到 Git 更新记录"
    return _as_markdown_bullets(subjects)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--manual-description", default="")
    parser.add_argument("--last-successful-commit", default="")
    parser.add_argument("--fallback-limit", type=int, default=DEFAULT_COMMIT_LIMIT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        changelog = generate_changelog(
            repo=args.repo.resolve(),
            manual_description=args.manual_description,
            last_successful_commit=args.last_successful_commit,
            fallback_limit=args.fallback_limit,
        )
    except ChangelogError as error:
        print("生成更新日志失败：{}".format(error), file=sys.stderr)
        return 1

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(changelog + "\n", encoding="utf-8")
    print(changelog)
    return 0


if __name__ == "__main__":
    sys.exit(main())
