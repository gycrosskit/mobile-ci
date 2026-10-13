#!/usr/bin/env python3
"""隔离 GitHub 应用 PR CLI 验收；--build 使用已有 SDK，不下载工具/上传/通知。"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.jenkins.test_github_pr_check import fixture, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true', help='执行真实 Android Debug lint/assemble；需要已有 SDK/Gradle')
    args = parser.parse_args()
    try:
        # 与生产环境隔离；只保留现有 SDK/JDK 路径。
        keep = {'PATH', 'HOME', 'JAVA_HOME', 'LANG', 'LC_ALL', 'ANDROID_HOME', 'ANDROID_SDK_ROOT', 'GRADLE_USER_HOME'}
        from unittest.mock import patch
        with patch.dict(os.environ, {key: value for key, value in os.environ.items() if key in keep}, clear=True):
            with fixture() as (ci, app, base, ci_sha, event_path, env):
                result = run('python3', str(ci / 'jenkins/github_pr_check.py'), 'event', '--project', 'example-mobile', env=env)
                print(result.stdout, end='')
                if args.build:
                    result = run('python3', str(ci / 'jenkins/github_pr_check.py'), 'check', '--project', 'example-mobile',
                                 '--ci-sha', ci_sha, '--app-root', str(app), '--base-root', str(base), env=env)
                    print(result.stdout, end='')
                    print(result.stderr, end='', file=sys.stderr)
                else:
                    run('python3', '-m', 'unittest', '-b', 'tests.jenkins.test_github_pr_check', cwd=ROOT,
                        env=dict(env, PYTHONPATH=str(ROOT)))
                    print('PASS: GitHub PR isolated CLI and negative fixtures; SDK build NOT executed (Gradle boundary stub in test).')
                print('Remote Actions/Checks NOT executed; production credential reads/vendor calls/Jenkins changes: 0.')
    except subprocess.CalledProcessError as error:
        print(error.stdout or '', end='')
        print(error.stderr or '', end='', file=sys.stderr)
        print('FAIL: ' + str(error), file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('FAIL: ' + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
