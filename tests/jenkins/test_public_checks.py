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
