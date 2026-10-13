"""公共入口必须保留安全核心，并把缺工具与静默跳过作为失败。"""
import io
import unittest
from unittest.mock import patch

from jenkins import run_public_checks as runner


class PublicChecksTest(unittest.TestCase):
    def test_private_compatibility_list_never_excludes_core_modules(self):
        for identity, reason in runner.PRIVATE_CHECKS.items():
            self.assertEqual(5, len(identity.split('.')), identity)
            self.assertTrue(reason)
            self.assertNotIn('test_ci_auto_promotion', identity)
            self.assertNotIn('test_signing_backup_runtime', identity)
            self.assertNotIn('test_pgyer_upload', identity)
            self.assertNotIn('test_delivery.', identity)

    def test_only_exact_native_case_is_delegated_on_linux_and_mac_runs_it(self):
        self.assertNotIn(runner.MACOS_NATIVE_CHECK, runner.PRIVATE_CHECKS)
        for platform, expected in (("linux", ["portable"]), ("darwin", ["native", "portable"])):
            calls = []
            native = unittest.FunctionTestCase(lambda: calls.append("native"))
            native.id = lambda: runner.MACOS_NATIVE_CHECK
            portable = unittest.FunctionTestCase(lambda: calls.append("portable"))
            portable.id = lambda: "tests.signing.example.other_security_case"
            with self.subTest(platform=platform), patch.object(runner.sys, 'platform', platform), \
                    patch.object(runner.unittest.defaultTestLoader, 'discover', return_value=unittest.TestSuite([native, portable])), \
                    patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
                self.assertEqual(0, runner.run_suite())
                self.assertEqual(expected, calls)

    def test_native_command_rejects_linux_before_loading_a_skipped_test(self):
        with patch.object(runner.sys, 'platform', 'linux'), \
                patch.object(runner.sys, 'argv', ['run_public_checks.py', '--macos-native-check']), \
                patch.object(runner.unittest.defaultTestLoader, 'loadTestsFromName') as load:
            with self.assertRaisesRegex(ValueError, '不能跳过'):
                runner.main()
            load.assert_not_called()

    def test_native_command_requires_one_success_and_no_skips(self):
        for count, skipped, expected in ((0, False, 1), (2, False, 1), (1, True, 1), (1, False, 0)):
            result = unittest.TestResult()
            result.testsRun = count
            if skipped:
                result.addSkip(unittest.FunctionTestCase(lambda: None), 'missing native runtime')
            with self.subTest(count=count, skipped=skipped), patch.object(runner.sys, 'platform', 'darwin'), \
                    patch.object(runner.unittest.defaultTestLoader, 'loadTestsFromName') as load, \
                    patch.object(runner.unittest.TextTestRunner, 'run', return_value=result):
                self.assertEqual(expected, runner.run_macos_native_check())
                load.assert_called_once_with(runner.MACOS_NATIVE_CHECK)

    def test_missing_runtime_is_an_explicit_failure(self):
        with patch.object(runner.sys, 'argv', ['run_public_checks.py']), patch.object(runner.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, '缺少运行时'):
                runner.main()

    def test_passing_but_skipped_suite_is_rejected(self):
        result = unittest.TestResult()
        result.addSkip(unittest.FunctionTestCase(lambda: None), 'missing security runtime')
        with patch.object(runner.unittest.defaultTestLoader, 'discover', return_value=unittest.TestSuite()), \
                patch.object(runner.unittest.TextTestRunner, 'run', return_value=result), \
                patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            self.assertEqual(1, runner.run_suite())


if __name__ == '__main__':
    unittest.main()
