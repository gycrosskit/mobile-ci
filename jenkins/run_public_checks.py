#!/usr/bin/env python3
"""在无公司配置、SDK和凭据的临时源码树运行公共安全回归；任何 skip 均失败。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jenkins.export_public_source import export

MACOS_NATIVE_CHECK = 'tests.signing.test_ios_signing_preflight.IosSigningPreflightTest.test_native_preflight_fails_without_keychain'

GROOVY_SHA256 = 'de65260cf2070442e99882f2f3d72e7531725c1e6a257446cc0cea525c607bd0'
# 私有部署事实/应用源码校验另跑；按精确用例区分，禁止排除整个安全模块。
PRIVATE_CHECKS = {
    'tests.delivery.test_ios_release.IosReleaseTest.test_current_enabled_test_signing_configs_are_loadable': '真实应用品牌和已审签名绑定',
    'tests.jenkins.test_jenkinsfiles.JenkinsfilesTest.test_fastlane_apple_intermediate_certificates_are_pinned_offline': '实际Apple CA二进制；公共回归另用自签fixture',
    'tests.jenkins.test_jenkinsfiles.JenkinsfilesTest.test_job_template_brand_choices_match_registered_platforms': '真实应用品牌白名单',
    'tests.jenkins.test_project_configuration.ProjectConfigurationTest.test_default_backup_recipient_matches_approved_application_public_key': '已审应用接收人公钥',
    'tests.jenkins.test_project_configuration.ProjectConfigurationTest.test_actual_ios_config_commands_use_trusted_facts_over_candidate_credentials': '实际应用品牌配置解析器',
    'tests.signing.test_publish_signing_backup.SigningBackupDestinationTest.test_destination_is_the_application_backup_branch': '私有旧实例默认备份仓库快照',
    'tests.jenkins.test_ci_repository_contract.CiRepositoryContractTest.test_packaging_selectors_query_application_not_automation': '私有旧实例仓库URL快照；公共多项目selector另测',
}


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def run_suite():
    all_cases = list(cases(unittest.defaultTestLoader.discover('tests', top_level_dir='.')))
    selected = [test for test in all_cases if test.id() not in PRIVATE_CHECKS
                and not (sys.platform == 'linux' and test.id() == MACOS_NATIVE_CHECK)]
    if sys.platform == 'linux':
        print('DELEGATED platform security check: ' + MACOS_NATIVE_CHECK + ': macos-14 native-preflight job', flush=True)
    for identity, reason in PRIVATE_CHECKS.items():
        print('PRIVATE compatibility check: ' + identity + ': ' + reason, flush=True)
    result = unittest.TextTestRunner(verbosity=1, buffer=True).run(unittest.TestSuite(selected))
    if result.skipped:
        print('Public security checks cannot skip: ' + repr(result.skipped), file=sys.stderr)
    return 0 if result.wasSuccessful() and not result.skipped else 1


def run_macos_native_check():
    if sys.platform != 'darwin':
        raise ValueError('原生 Security.framework 安全回归必须在 macOS 执行，不能跳过')
    suite = unittest.defaultTestLoader.loadTestsFromName(MACOS_NATIVE_CHECK)
    result = unittest.TextTestRunner(verbosity=2, buffer=True).run(suite)
    return 0 if result.wasSuccessful() and result.testsRun == 1 and not result.skipped else 1


def main():
    if len(sys.argv) == 2 and sys.argv[1] == '--macos-native-check':
        return run_macos_native_check()
    if len(sys.argv) == 2 and sys.argv[1] == '--isolated-suite':
        return run_suite()
    for name in ('git', 'java', 'node', 'ruby', 'docker', 'openssl'):
        if not shutil.which(name):
            raise ValueError('公共安全回归缺少运行时：' + name)
    compose = ['docker-compose'] if shutil.which('docker-compose') else ['docker', 'compose']
    subprocess.run([*compose, 'version', '--short'], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['ruby', '-ryaml', '-e', 'YAML.safe_load("key: value")'], check=True)
    if not subprocess.check_output(['openssl', 'version']).startswith(b'OpenSSL 3.'):
        raise ValueError('公共安全回归需要 OpenSSL 3 验证 RC2 legacy provider；不允许跳过')
    jar = Path(os.environ.get('GROOVY_TEST_CLASSPATH', ''))
    if not jar.is_file() or jar.is_symlink() or jar.stat().st_nlink != 1 or jar.stat().st_mode & 0o222:
        raise ValueError('GROOVY_TEST_CLASSPATH 必须绑定单个只读普通 Groovy JAR')
    if hashlib.sha256(jar.read_bytes()).hexdigest() != GROOVY_SHA256:
        raise ValueError('Groovy JAR 与审核摘要不同')
    jar = jar.resolve()
    subprocess.run(['java', '-cp', str(jar), 'groovy.ui.GroovyMain', '-e',
                    "assert GroovySystem.version == '2.4.21'; assert System.getProperty('java.specification.version') == '21'"], check=True)
    with tempfile.TemporaryDirectory(prefix='mobile-ci-public-') as directory:
        base = Path(directory).resolve()
        archive_path = base / 'source.tar.gz'
        export(archive_path)
        source = base / 'source'
        source.mkdir()
        with tarfile.open(archive_path) as archive:
            archive.extractall(source)
        assert not (source / 'projects').exists(), '公司配置进入了公开源码'
        fixture = json.loads((source / 'tests/fixtures/legacy-project.json').read_text())
        assert fixture.pop('testOnly') is True
        # 仅临时测试树模拟管理员审核配置，生产 loader 仍拒绝 testOnly fixture。
        (source / 'projects').mkdir()
        (source / 'projects/sxmqlive.json').write_text(json.dumps(fixture))
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(source)], check=True)
        subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Public Fixture', '-c', 'user.email=fixture@example.invalid',
                        '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Isolated public source fixture'], check=True)
        environment = dict(os.environ, GROOVY_TEST_CLASSPATH=str(jar), CI_PROJECT_ID='sxmqlive')
        for key in ('APP_SOURCE_ROOT', 'CI_TOOL_ROOT', 'CI_PROJECT_CONFIG', 'JENKINS_CI_PROJECT', 'JOB_NAME', 'PYTHONPATH'):
            environment.pop(key, None)
        subprocess.run([sys.executable, str(source / 'examples/second-project/validate.py')], cwd=source, env=environment, check=True)
        return subprocess.run([sys.executable, str(source / 'jenkins/run_public_checks.py'), '--isolated-suite'], cwd=source, env=environment).returncode


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
