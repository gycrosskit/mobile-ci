"""验证实际 Git 增删改名、Wiki 幂等发布与过期构建保护。"""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from jenkins import sync_wiki_navigation as module


class WikiNavigationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        module.git(self.repo, "init", "-b", "master")
        for path, content in {"README.md": "# Project", "AGENTS.md": "# Rules",
                              "docs/README.md": "# Index",
                              "docs/规范/测试.md": "# 测试规范\n\nPRIVATE_BODY_MARKER"}.items():
            self.write(path, content)
        self.commit()

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self):
        module.git(self.repo, "add", "--all")
        module.git(self.repo, "-c", "commit.gpgSign=false", "-c", "user.name=Test",
                   "-c", "user.email=test@localhost", "commit", "-m", "fixture")

    def remotes(self):
        source = self.root / "source.git"
        wiki = self.root / "wiki.git"
        module.git(self.repo, "clone", "--bare", str(self.repo), str(source))
        wiki_work = self.root / "wiki-work"
        module.git(self.repo, "clone", str(self.repo), str(wiki_work))
        (wiki_work / module.PAGE).write_text("old navigation", encoding="utf-8")
        for title in module.TEAM_PAGES:
            (wiki_work / (title + ".md")).write_bytes(f"# {title}\r\n\r\nTEAM_BODY_MARKER\r\n".encode("utf-8"))
        module.git(wiki_work, "add", "--", module.PAGE, *(title + ".md" for title in module.TEAM_PAGES))
        module.git(wiki_work, "-c", "commit.gpgSign=false", "-c", "user.name=Test",
                   "-c", "user.email=test@localhost", "commit", "-m", "wiki fixture")
        module.git(self.repo, "clone", "--bare", str(wiki_work), str(wiki))
        return source, wiki

    def test_navigation_uses_titles_links_and_never_copies_bodies(self):
        output = module.navigation(self.repo)
        self.assertIn("## 开发规范", output)
        self.assertIn("[测试规范]", output)
        self.assertIn("docs/%E8%A7%84%E8%8C%83/%E6%B5%8B%E8%AF%95.md", output)
        self.assertNotIn("PRIVATE_BODY_MARKER", output)
        self.assertEqual(output, module.navigation(self.repo))

    def test_add_rename_delete_and_new_category_follow_committed_docs(self):
        (self.repo / "docs/规范/测试.md").unlink()
        self.write("docs/新分类/子目录/新名字.md", "# 新标题")
        self.write("docs/说明.markdown", "正文无标题")
        self.write("docs/image.png", "not a document")
        self.commit()
        self.write("docs/尚未提交.md", "# Uncommitted")
        output = module.navigation(self.repo)
        self.assertNotIn("[测试规范]", output)
        self.assertIn("## 新分类", output)
        self.assertIn("[子目录 / 新标题]", output)
        self.assertIn("[说明]", output)
        self.assertNotIn("image.png", output)
        self.assertNotIn("Uncommitted", output)

    def test_markdown_titles_are_escaped_and_nested_paths_encoded(self):
        self.write("docs/规范/文件 名.md", "# 标题 [link] ###\n\nbody")
        self.commit()
        output = module.navigation(self.repo)
        self.assertIn(r"[标题 \[link\]]", output)
        self.assertIn("%20", output)

    def test_publish_updates_only_homepage_and_is_idempotent(self):
        source, wiki = self.remotes()
        pages = module.git(wiki, "ls-tree", "-r", "--name-only", "-z", "HEAD").split("\0")
        original = {
            page: (module.git(wiki, "show", "HEAD:" + page).encode("utf-8"),
                   module.git(wiki, "log", "-1", "--format=%H", "--", page))
            for page in pages if page and page != module.PAGE
        }
        with patch.object(module, "SOURCE_REMOTE", str(source)), patch.object(module, "WIKI_REMOTE", str(wiki)):
            self.assertTrue(module.publish(self.repo))
            first = module.git(wiki, "rev-parse", "HEAD")
            self.assertFalse(module.publish(self.repo))
        self.assertEqual(first, module.git(wiki, "rev-parse", "HEAD"))
        self.assertEqual(module.PAGE + "\0", module.git(wiki, "diff", "--name-only", "-z", "HEAD^", "HEAD"))
        for page, (content, commit) in original.items():
            with self.subTest(page=page):
                self.assertEqual(content, module.git(wiki, "show", "HEAD:" + page).encode("utf-8"))
                self.assertEqual(commit, module.git(wiki, "log", "-1", "--format=%H", "--", page))
        output = module.git(wiki, "show", "HEAD:" + module.PAGE)
        self.assertEqual(module.navigation(self.repo), output)
        for title, page_id in (("团队版本提交规范", 18168019), ("团队Git协作流程", 18168018),
                               ("团队AI研发协作流程", 18168017), ("品牌迁移验收记录模板", 18168016)):
            self.assertIn(f"[{title}]({module.WIKI_WEB_ROOT}{page_id}&doc_id=8404213)", output)
        self.assertNotIn("TEAM_BODY_MARKER", output)

    def test_unmerged_source_is_rejected_before_wiki_clone(self):
        source, wiki = self.remotes()
        self.write("docs/new.md", "# Not merged")
        self.commit()
        previous = module.git(wiki, "rev-parse", "HEAD")
        with patch.object(module, "SOURCE_REMOTE", str(source)), patch.object(module, "WIKI_REMOTE", str(wiki)):
            with self.assertRaisesRegex(ValueError, "尚未合并"):
                module.publish(self.repo)
        self.assertEqual(previous, module.git(wiki, "rev-parse", "HEAD"))

    def test_master_advancing_before_publish_does_not_overwrite_wiki(self):
        source, wiki = self.remotes()
        previous = module.git(wiki, "rev-parse", "HEAD")
        guard = module.assert_current_master
        checks = []

        def advance(repo, sha):
            checks.append(sha)
            if len(checks) == 2:
                self.write("docs/new.md", "# New master")
                self.commit()
                module.git(self.repo, "push", str(source), "master")
            guard(repo, sha)

        with patch.object(module, "SOURCE_REMOTE", str(source)), patch.object(module, "WIKI_REMOTE", str(wiki)), \
                patch.object(module, "assert_current_master", side_effect=advance):
            with self.assertRaises(ValueError):
                module.publish(self.repo)
        self.assertEqual(previous, module.git(wiki, "rev-parse", "HEAD"))

    def test_concurrent_wiki_edit_is_rejected_without_force_push(self):
        source, wiki = self.remotes()
        real_git = module.git

        def concurrent(repo, *args):
            if args == ("push", "origin", "HEAD"):
                work = self.root / "other-editor"
                real_git(self.repo, "clone", str(wiki), str(work))
                (work / "README.md").write_text("manual wiki edit", encoding="utf-8")
                real_git(work, "add", "README.md")
                real_git(work, "-c", "commit.gpgSign=false", "-c", "user.name=Test",
                         "-c", "user.email=test@localhost", "commit", "-m", "concurrent")
                real_git(work, "push", "origin", "master")
            return real_git(repo, *args)

        with patch.object(module, "SOURCE_REMOTE", str(source)), patch.object(module, "WIKI_REMOTE", str(wiki)), \
                patch.object(module, "git", side_effect=concurrent):
            with self.assertRaises(subprocess.CalledProcessError):
                module.publish(self.repo)
        self.assertEqual("manual wiki edit", module.git(wiki, "show", "HEAD:README.md"))
        self.assertEqual("old navigation", module.git(wiki, "show", "HEAD:" + module.PAGE))

    def test_explicit_repo_reads_application_docs_from_a_separate_ci_cwd(self):
        output = self.root / "navigation.md"
        other = self.root / "ci-repository"
        other.mkdir()
        (other / "docs").mkdir()
        (other / "docs/README.md").write_text("# WRONG CI docs")
        script = Path(module.__file__)
        result = subprocess.run(["python3", str(script), "--repo", str(self.repo), "--output", str(output)],
                                cwd=other, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(module.navigation(self.repo), output.read_text())
        self.assertNotIn("WRONG CI", output.read_text())

    def test_publish_cli_passes_explicit_application_root_to_both_master_guards(self):
        source, wiki = self.remotes()
        guard = module.assert_current_master
        with patch("sys.argv", ["sync", "--publish", "--repo", str(self.repo)]), \
                patch.object(module, "SOURCE_REMOTE", str(source)), patch.object(module, "WIKI_REMOTE", str(wiki)), \
                patch.object(module, "assert_current_master", wraps=guard) as checks:
            module.main()
        sha = module.git(self.repo, "rev-parse", "HEAD").strip()
        self.assertEqual([(self.repo.resolve(), sha), (self.repo.resolve(), sha)], [call.args for call in checks.call_args_list])

    def test_environment_reads_application_docs_from_ci_cwd(self):
        output = self.root / "navigation.md"
        result = subprocess.run(["python3", str(Path(module.__file__)), "--output", str(output)],
                                cwd=Path(module.__file__).parents[1],
                                env=dict(os.environ, APP_SOURCE_ROOT=str(self.repo)), capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(module.navigation(self.repo), output.read_text())

    def test_missing_application_root_is_rejected_instead_of_reading_ci_docs(self):
        environment = dict(os.environ)
        environment.pop("APP_SOURCE_ROOT", None)
        result = subprocess.run(["python3", str(Path(module.__file__))],
                                cwd=Path(module.__file__).parents[1], env=environment,
                                capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn("--repo", result.stderr)

    def test_wiki_pipeline_always_runs_the_ci_implementation(self):
        pipeline = (Path(module.__file__).parent / "Jenkinsfile.wiki-navigation").read_text()
        self.assertIn("dir('automation')", pipeline)
        self.assertIn("python3 -m unittest tests.jenkins.test_sync_wiki_navigation", pipeline)
        self.assertIn('python3 "$CI_TOOL_ROOT/jenkins/sync_wiki_navigation.py" --repo "$APP_SOURCE_ROOT" --publish', pipeline)
        self.assertNotIn("tools/sync_wiki_navigation.py", pipeline)
        self.assertNotIn("ci/scripts/sync_wiki_navigation.py", pipeline)


if __name__ == "__main__":
    unittest.main()
