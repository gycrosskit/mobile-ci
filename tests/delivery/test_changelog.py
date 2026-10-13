import unittest
from pathlib import Path
from unittest.mock import patch

from delivery.changelog import ChangelogError, generate_changelog


class ChangelogTest(unittest.TestCase):
    def test_manual_description_has_highest_priority(self):
        result = generate_changelog(
            repo=Path("."),
            manual_description="修复登录问题\n• 新增客服入口",
            last_successful_commit="invalid",
        )

        self.assertEqual("- 修复登录问题\n- 新增客服入口", result)

    @patch("delivery.changelog._is_commit", return_value=True)
    @patch("delivery.changelog._commit_subjects", return_value=["feat: A", "fix: B"])
    def test_uses_successful_commit_range(self, subjects, _is_commit):
        result = generate_changelog(Path("."), last_successful_commit="abc123")

        self.assertEqual("- feat: A\n- fix: B", result)
        subjects.assert_called_once_with(Path("."), "abc123..HEAD", limit=None)

    @patch("delivery.changelog._commit_subjects", side_effect=ChangelogError("no git"))
    def test_git_failure_does_not_block_release(self, _subjects):
        result = generate_changelog(Path("."))

        self.assertEqual("- 未获取到 Git 更新记录", result)


if __name__ == "__main__":
    unittest.main()
