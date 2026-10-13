"""真实双仓 Git fixture 检查和 GitHub PR 信任边界负向回归。"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from project_config import validate_project, ProjectConfigError
from jenkins import github_pr_check as check
from jenkins import sync_jenkins_jobs, sync_jenkins_admin_jobs, generate_jenkins_job_templates
from tests.jenkins import test_github_workflow_examples as workflow_checks

read_workflow = workflow_checks.read_workflow
ACTION_PINS = workflow_checks.ACTION_PINS

ROOT = Path(__file__).resolve().parents[2]


def profile():
    value = json.loads((ROOT / 'examples/second-project/project.json').read_text())
    value['scm'] = {'provider': 'github', 'allowForks': True}
    for binding in value['repositories'].values():
        binding['ssh'] = binding['ssh'].replace('gitee.com', 'github.com')
        binding['https'] = binding['https'].replace('gitee.com', 'github.com')
    for key in ('credentials', 'providers', 'jobs'):
        value[key] = {}
    for key in ('runtime', 'backup'):
        value.pop(key)
    return validate_project(value)


def repo(name, repo_id, fork=False):
    return dict(full_name=name, id=repo_id, fork=fork, html_url='https://github.com/' + name,
                clone_url='https://github.com/' + name + '.git', url='https://api.github.com/repos/' + name)


def event(head='a' * 40, base='b' * 40):
    repository = repo('example/native-mobile', 1)
    return {'action': 'synchronize', 'number': 12, 'repository': repository,
            'pull_request': {'number': 12, 'state': 'open', 'html_url': 'https://github.com/example/native-mobile/pull/12',
                             'base': {'repo': repository, 'sha': base, 'ref': 'main'},
                             'head': {'repo': repo('contributor/native-mobile', 2, True), 'sha': head, 'ref': 'feature'}}}


def environment():
    return dict(GITHUB_EVENT_NAME='pull_request', GITHUB_SERVER_URL='https://github.com',
                GITHUB_REPOSITORY='example/native-mobile', GITHUB_REPOSITORY_ID='1', GITHUB_REF='refs/pull/12/merge',
                RUNNER_ENVIRONMENT='github-hosted')


def run(*args, cwd=None, env=None):
    return subprocess.run(list(args), cwd=cwd, env=env, text=True, capture_output=True, check=True, timeout=300)


def commit(path):
    run('git', '-C', str(path), 'add', '.')
    run('git', '-C', str(path), '-c', 'user.name=Isolated fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-q', '-m', 'Fixture')
    return run('git', '-C', str(path), 'rev-parse', 'HEAD').stdout.strip()


@contextlib.contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix='github-application-pr-') as directory:
        workspace = Path(directory).resolve()
        ci, app, base = (workspace / name for name in ('trusted-ci', 'candidate-app', 'base-app'))
        ci.mkdir()
        files = ('project.py', 'project_config.py', 'delivery/android_ci.sh', 'jenkins/github_pr_check.py',
                 'jenkins/check_repository_contract.py', 'examples/github-actions/application-pr.yml',
                 'examples/github-actions/reusable-application-pr.yml')
        for name in files:
            (ci / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, ci / name)
        (ci / 'projects').mkdir()
        (ci / 'projects/example-mobile.json').write_text(json.dumps(profile()))
        (ci / '.gitignore').write_text('__pycache__/\n*.pyc\n')
        shutil.copytree(ROOT / 'examples/second-project/application', base,
                        ignore=shutil.ignore_patterns('__pycache__', '.gradle', 'build'))
        for path, binding in ((ci, profile()['repositories']['automation']), (base, profile()['repositories']['application'])):
            run('git', 'init', '-q', '-b', 'main', str(path))
            run('git', '-C', str(path), 'remote', 'add', 'origin', binding['https'])
        ci_sha, base_sha = commit(ci), commit(base)
        run('git', 'clone', '-q', str(base), str(app))
        run('git', '-C', str(app), 'remote', 'set-url', 'origin', profile()['repositories']['application']['https'])
        source = app / 'mobile/src/main/java/org/example/mobile/MainActivity.java'
        source.write_text(source.read_text().replace('Example Mobile', 'GitHub Mobile Fixture'))
        head_sha = commit(app)
        event_path = workspace / 'event.json'
        event_path.write_text(json.dumps(event(head_sha, base_sha)))
        env = dict(os.environ, **environment(), GITHUB_EVENT_PATH=str(event_path), RUNNER_TEMP=str(workspace), PYTHONDONTWRITEBYTECODE='1')
        yield ci, app, base, ci_sha, event_path, env


class GitHubPrCheckTest(unittest.TestCase):
    def test_github_cannot_install_jenkins_even_without_test_only_flag(self):
        value = profile(); value['testOnly'] = False
        for module in (sync_jenkins_jobs, sync_jenkins_admin_jobs):
            with patch('project_config.load_project', return_value=value), patch.object(module, 'render_all') as render, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, module.main(['apply', '--project=example-mobile']))
                render.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            with self.assertRaises(ProjectConfigError):
                generate_jenkins_job_templates.synchronize(Path(directory) / 'brands.json', False, value, output)
            self.assertFalse(output.exists())

    def test_provider_is_explicit_and_cannot_mix_gitee_or_release_credentials(self):
        original = profile()
        for mutate in (lambda p: p['scm'].update(provider='gitee'),
                       lambda p: p['repositories']['application'].update(https='https://gitee.com/example/native-mobile.git'),
                       lambda p: p['credentials'].update(giteeApi='production-api'),
                       lambda p: p['providers'].update(pgyer={'credentialId': 'production'}),
                       lambda p: p.update(signingBackup={'scope': 'controller_dr'}),
                       lambda p: p['scm'].update(allowForks='true')):
            bad = copy.deepcopy(original); mutate(bad)
            with self.assertRaises(ProjectConfigError):
                validate_project(bad)

    def test_event_and_fork_are_validated_before_checkout(self):
        original, env = event(), environment()
        result = check.context(profile(), original, env)
        self.assertEqual('refs/pull/12/head', result['head_ref'])
        self.assertTrue(result['fork'])
        bad_profile = profile(); bad_profile['scm']['allowForks'] = False
        with self.assertRaises(ValueError): check.context(bad_profile, original, env)
        for mutate in (lambda e: e.update(number='12\nrepository=evil/app'),
                       lambda e: e['repository'].update(clone_url='https://github.com/evil/app.git'),
                       lambda e: e['pull_request']['head'].update(sha='$(touch /tmp/marker)'),
                       lambda e: e['pull_request']['base'].update(ref='refs/heads/main'),
                       lambda e: e['pull_request']['head']['repo'].update(fork=False),
                       lambda e: e['pull_request']['base']['repo'].update(id=2),
                       lambda e: e['pull_request'].update(html_url='https://gitee.com/example/native-mobile/pulls/12'),
                       lambda e: e['pull_request']['head']['repo'].update(full_name='../evil')):
            bad = copy.deepcopy(original); mutate(bad)
            with self.assertRaises(ValueError): check.context(profile(), bad, env)
        for key, value in [('GITHUB_EVENT_NAME', 'pull_request_target'), ('GITHUB_REF', 'refs/heads/main'),
                           ('GITHUB_REPOSITORY', 'foreign/app'), ('GITHUB_REPOSITORY_ID', '2'), ('GITHUB_SERVER_URL', 'https://gitee.com'),
                           ('RUNNER_ENVIRONMENT', 'self-hosted')]:
            with self.assertRaises(ValueError): check.context(profile(), original, dict(env, **{key: value}))

    def test_event_path_symlink_and_outside_runner_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'event.json'; path.write_text(json.dumps(event()))
            env = dict(GITHUB_EVENT_PATH=str(path), RUNNER_TEMP=str(root))
            self.assertEqual(event(), check.read_event(env))
            link = root / 'link.json'; link.symlink_to(path)
            for bad in (dict(env, GITHUB_EVENT_PATH=str(link)), dict(env, RUNNER_TEMP=str(root / 'other'))):
                with self.assertRaises(ValueError): check.read_event(bad)

    def test_child_environment_drops_publication_and_candidate_override_inputs(self):
        with patch.dict(os.environ, {'PGYER_API_KEY': 'private', 'GITHUB_TOKEN': 'private', 'CI_PROJECT_CONFIG': '/candidate/profile',
                                     'PYTHONPATH': '/candidate', 'JOB_NAME': 'Production', 'CI_TOOL_ROOT': '/candidate'}):
            env = check.child_environment(profile(), Path('/candidate-app'))
        for key in ('PGYER_API_KEY', 'GITHUB_TOKEN', 'CI_PROJECT_CONFIG', 'JOB_NAME'):
            self.assertNotIn(key, env)
        self.assertEqual(str(ROOT), env['CI_TOOL_ROOT'])
        self.assertEqual(str(ROOT), env['PYTHONPATH'])

    def test_cli_exact_commits_contract_manifest_native_entrypoint_and_negative_sha(self):
        with fixture() as (ci, app, base, ci_sha, event_path, env):
            command = ['python3', str(ci / 'jenkins/github_pr_check.py'), 'check', '--project', 'example-mobile',
                       '--ci-sha', ci_sha, '--app-root', str(app), '--base-root', str(base)]
            # 用应用自己的 Gradle 边界替身断言任务；真实 SDK 构建由独立 --build fixture 验收。
            gradle = app / 'gradlew'
            gradle.write_text('#!/bin/bash\nset -eu\n[[ "$*" == *":mobile:lintDebug :mobile:assembleDebug"* ]]\n'
                             '[[ -z "${PGYER_API_KEY:-}" && -z "${GITHUB_TOKEN:-}" && -z "${CI_PROJECT_CONFIG:-}" ]]\n')
            head = commit(app)
            event_path.write_text(json.dumps(event(head, run('git', '-C', str(base), 'rev-parse', 'HEAD').stdout.strip())))
            result = run(*command, env=dict(env, CI_PROJECT_CONFIG=str(app / 'evil-profile'), PGYER_API_KEY='must-not-leak', GITHUB_TOKEN='must-not-leak'))
            self.assertIn('PASS: GitHub application PR', result.stdout)
            for flag, value in (('--ci-sha', 'f' * 40), ('--app-root', str(ci)), ('--base-root', str(app))):
                bad = command.copy(); bad[bad.index(flag) + 1] = value
                with self.assertRaises(subprocess.CalledProcessError): run(*bad, env=env)
            bad_event = json.loads(event_path.read_text()); bad_event['pull_request']['head']['sha'] = 'f' * 40
            event_path.write_text(json.dumps(bad_event))
            with self.assertRaises(subprocess.CalledProcessError): run(*command, env=env)

    def test_mutable_tools_wrong_origin_and_symlink_fail_closed(self):
        with fixture() as (ci, app, base, ci_sha, event_path, env):
            evidence = check.context(profile(), json.loads(event_path.read_text()), env)
            with patch.object(check, 'CI_ROOT', ci):
                self.assertEqual(['android'], check.verify(profile(), evidence, ci_sha, app, base)['platforms'])
                run('git', '-C', str(app), 'remote', 'set-url', 'origin', 'https://gitee.com/example/native-mobile.git')
                with self.assertRaises(ValueError): check.verify(profile(), evidence, ci_sha, app, base)
                run('git', '-C', str(app), 'remote', 'set-url', 'origin', profile()['repositories']['application']['https'])
                link = app.parent / 'app-link'; link.symlink_to(app, target_is_directory=True)
                with self.assertRaises(ValueError): check.verify(profile(), evidence, ci_sha, link, base)
                (ci / 'project_config.py').write_text('candidate overrides trusted tools')
                with self.assertRaises(ValueError): check.verify(profile(), evidence, ci_sha, app, base)

    def test_private_same_repository_keeps_three_pinned_isolated_checkouts(self):
        value = profile()
        value['repositories']['automation'] = copy.deepcopy(value['repositories']['application'])
        value = validate_project(value)
        with fixture() as (ci, app, base, ci_sha, event_path, env):
            run('git', '-C', str(ci), 'remote', 'set-url', 'origin', value['repositories']['automation']['https'])
            evidence = check.context(value, json.loads(event_path.read_text()), env)
            with patch.object(check, 'CI_ROOT', ci):
                self.assertEqual(['android'], check.verify(value, evidence, ci_sha, app, base)['platforms'])
                for actual_sha, candidate, baseline in ((evidence['head_sha'], app, base),
                                                        (ci_sha, ci, base), (ci_sha, app, app)):
                    with self.assertRaises(ValueError): check.verify(value, evidence, actual_sha, candidate, baseline)
            output = ci.parent / 'workflows'
            check.render(value, ci_sha, 'c' * 40, output)
            caller = read_workflow(output / 'application-pr.yml')
            reusable = read_workflow(output / 'reusable-application-pr.yml')
            self.assertEqual('example/native-mobile/.github/workflows/reusable-application-pr.yml@' + 'c' * 40,
                             caller['jobs']['check']['uses'])
            self.assertEqual('example/native-mobile', reusable['jobs']['application-pr']['steps'][0]['with']['repository'])
            self.assertEqual({'contents': 'read'}, reusable['permissions'])

    def test_render_workflows_have_distinct_fixed_pins_and_no_privileged_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            check.render(profile(), 'a' * 40, 'b' * 40, Path(directory))
            caller = read_workflow(Path(directory) / 'application-pr.yml')
            reusable = read_workflow(Path(directory) / 'reusable-application-pr.yml')
        self.assertEqual({'pull_request'}, set(caller['on']))
        self.assertEqual('example/mobile-automation/.github/workflows/reusable-application-pr.yml@' + 'b' * 40,
                         caller['jobs']['check']['uses'])
        self.assertEqual({'project-id'}, set(reusable['on']['workflow_call']['inputs']))
        for workflow in (caller, reusable):
            self.assertEqual({'contents': 'read'}, workflow['permissions'])
            self.assertNotIn('secrets.', json.dumps(workflow))
        job = reusable['jobs']['application-pr']
        self.assertEqual('ubuntu-24.04', job['runs-on'])
        self.assertEqual('a' * 40, job['env']['CI_EXPECTED_SHA'])
        self.assertNotIn('environment', job)
        for step in job['steps']:
            if 'uses' in step:
                self.assertIn(step['uses'], ACTION_PINS)
                if step['uses'].startswith('actions/checkout'):
                    self.assertFalse(step['with']['persist-credentials'])
            if 'run' in step: self.assertNotIn('${{', step['run'])
        trusted = job['steps'][0]['with']
        self.assertEqual('example/mobile-automation', trusted['repository'])
        self.assertEqual('a' * 40, trusted['ref'])
        policy = workflow_checks.GitHubWorkflowExamplesTest()
        policy.assert_safe(reusable, reusable=True, application=True)
        for mutate in (lambda w: w['permissions'].update(contents='write'),
                       lambda w: w['on'].update(pull_request_target={}),
                       lambda w: w['jobs']['application-pr'].update(**{'runs-on': 'self-hosted'}),
                       lambda w: w['jobs']['application-pr'].update(secrets='inherit'),
                       lambda w: w['jobs']['application-pr']['steps'][0].update(uses='actions/checkout@main'),
                       lambda w: w['jobs']['application-pr']['steps'][-1].update(run='${{ inputs.command }}')):
            bad = copy.deepcopy(reusable); mutate(bad)
            with self.assertRaises(AssertionError): policy.assert_safe(bad, reusable=True, application=True)


if __name__ == '__main__':
    unittest.main()
