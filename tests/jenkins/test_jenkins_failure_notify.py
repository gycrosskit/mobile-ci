import contextlib
import io
import unittest

from jenkins.jenkins_failure_notify import main


class JenkinsFailureNotifyTest(unittest.TestCase):
    def test_dry_run_identifies_engine_without_losing_invalid_parameter_alert(self):
        for engine in ("cmp", "kuikly", "invalid", ""):
            with self.subTest(engine=engine), contextlib.redirect_stdout(io.StringIO()) as stdout:
                result = main(["--job=sxmqlive-android-test", "--build-number=8", "--result=FAILURE",
                               "--stage=Validate Parameters", "--ui-engine=" + engine, "--dry-run"])
                self.assertEqual(0, result)
                if engine in ("cmp", "kuikly"):
                    self.assertIn("**UI 引擎：** " + ("CMP" if engine == "cmp" else "Kuikly"), stdout.getvalue())
                else:
                    self.assertNotIn("UI 引擎", stdout.getvalue())

    def test_dry_run_omits_invalid_work_item_instead_of_losing_alert(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(
                [
                    "--job=sxmqlive-android-test",
                    "--build-number=8",
                    "--result=FAILURE",
                    "--stage=Validate Parameters",
                    "--platform=Android",
                    "--work-item-type=bug",
                    "--dry-run",
                ]
            )

        self.assertEqual(0, result)
        self.assertIn("Jenkins 构建失败", stdout.getvalue())
        self.assertNotIn("关联禅道", stdout.getvalue())
        self.assertIn("忽略无效的禅道工作项引用", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
