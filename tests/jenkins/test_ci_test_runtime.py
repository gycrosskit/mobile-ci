"""实际执行门禁预检；工具缺失不能让安全测试静默 skip。"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / 'jenkins/Jenkinsfile.ci-pr-validation'


class CiTestRuntimeTest(unittest.TestCase):
    def runtime_script(self):
        source = PIPELINE.read_text(encoding='utf-8')
        stage = "stage('Validate CI Test Runtime')"
        self.assertLess(source.index(stage), source.index("stage('CI Behavior and Security Contracts')"))
        self.assertLess(source.index(stage), source.index('python3 -m unittest discover -b -s tests -t .'))
        return source.split(stage, 1)[1].split("sh '''", 1)[1].split("'''", 1)[0]

    def run_runtime(self, *, missing=None, failing=None, classpath="ready"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace = root / 'calls'
            jar = root / 'groovy-all-2.4.21.jar'
            jar.write_bytes(b'test fixture; Java execution is mocked')
            jar.chmod(0o444)
            selected_classpath = str(jar)
            if classpath == 'missing': selected_classpath = ''
            if classpath == 'other': selected_classpath = str(root / 'workdir.jar')
            if classpath == 'multiple': selected_classpath += os.pathsep + str(root / 'extra.jar')
            if classpath == 'absent': jar.unlink()
            if classpath == 'unreadable': jar.chmod(0o000)
            if classpath == 'writable': jar.chmod(0o644)
            if classpath == 'symlink':
                original = root / 'original.jar'; jar.rename(original); jar.symlink_to(original)
            for name in ('node', 'ruby', 'docker', 'java'):
                if name == missing:
                    continue
                script = '#!/bin/sh\nprintf "%s\\n" "' + name + ':$*" >> "$TRACE"\n'
                if name == 'docker':
                    script += '[ "$*" = "compose version --short" ] || exit 99\n'
                elif name == 'java':
                    script += '[ "$1" = "-cp" ] && [ "$2" = "$GROOVY_TEST_CLASSPATH" ] && [ "$3" = "groovy.ui.GroovyMain" ] && [ "$4" = "-e" ] && [ "$5" = "assert GroovySystem.version == \'2.4.21\'" ] || exit 99\n'
                else:
                    script += '[ "$*" = "--version" ] || exit 99\n'
                if name == 'java' and failing == 'java': script += 'printf \'%s\\n\' \'unsupported Groovy version\' >&2\n'
                script += 'exit ' + ('17' if name == failing else '0') + '\n'
                tool = root / name
                tool.write_text(script, encoding='utf-8')
                tool.chmod(0o755)
            command = self.runtime_script().replace('/usr/local/lib/controller-groovy/groovy-all-2.4.21.jar', str(jar))
            result = subprocess.run(['/bin/bash', '-eu', '-c', command],
                                    env=dict(os.environ, PATH=str(root), TRACE=str(trace), GROOVY_TEST_CLASSPATH=selected_classpath),
                                    capture_output=True, text=True, timeout=10)
            calls = trace.read_text().splitlines() if trace.exists() else []
            return result, calls

    def test_missing_node_ruby_or_docker_fails_before_running_any_tool(self):
        for tool in ('node', 'ruby', 'docker', 'java'):
            with self.subTest(tool=tool):
                result, calls = self.run_runtime(missing=tool)
                self.assertEqual(1, result.returncode)
                self.assertIn('CI security test runtime is missing: ' + tool, result.stderr)
                self.assertEqual([], calls)

    def test_nonfunctional_node_ruby_or_compose_stops_preflight(self):
        for tool, expected_calls in (('node', ['node:--version']),
                                     ('ruby', ['node:--version', 'ruby:--version']),
                                     ('docker', ['node:--version', 'ruby:--version', 'docker:compose version --short'])):
            with self.subTest(tool=tool):
                result, calls = self.run_runtime(failing=tool)
                self.assertEqual(17, result.returncode)
                self.assertEqual(expected_calls, calls)

    def test_ready_runtime_only_queries_versions_without_daemon_or_test_execution(self):
        result, calls = self.run_runtime()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(['node:--version', 'ruby:--version', 'docker:compose version --short'], calls[:3])
        self.assertEqual(4, len(calls))
        self.assertIn("groovy.ui.GroovyMain -e assert GroovySystem.version == '2.4.21'", calls[3])

    def test_missing_mutable_or_noncanonical_groovy_classpath_fails_before_execution(self):
        for classpath in ('missing','other','multiple','absent','unreadable','writable','symlink'):
            with self.subTest(classpath=classpath):
                result, calls = self.run_runtime(classpath=classpath)
                self.assertEqual(1, result.returncode)
                self.assertIn('fixed read-only Controller Groovy jar', result.stderr)
                self.assertEqual([], calls)

    def test_unsupported_groovy_version_fails_preflight(self):
        result, calls = self.run_runtime(failing='java')
        self.assertEqual(17, result.returncode)
        self.assertIn('unsupported Groovy version', result.stderr)
        self.assertEqual(4, len(calls))

    def test_shared_agent_installs_official_client_tools_without_daemon_or_socket(self):
        source = (ROOT / 'jenkins/remote/android-agent/Dockerfile').read_text(encoding='utf-8')
        install = source.split('apt-get install --yes --no-install-recommends', 1)[1].split('&&', 1)[0]
        packages = install.replace('\\', '').split()
        self.assertTrue({'nodejs', 'ruby', 'docker-cli', 'docker-compose'} <= set(packages))
        self.assertFalse({'docker.io', 'docker-ce', 'containerd', 'containerd.io', 'runc'} & set(packages))
        self.assertNotIn('dockerd', source)
        self.assertNotIn('docker.sock', source)
        self.assertNotIn('download.docker.com', source)
        self.assertIn('USER jenkins', source)
        self.assertIn('ARG CI_GROOVY_VERSION=2.4.21', source)
        self.assertIn('de65260cf2070442e99882f2f3d72e7531725c1e6a257446cc0cea525c607bd0', source)
        self.assertIn('repo.maven.apache.org/maven2/org/codehaus/groovy/groovy-all/', source)
        self.assertIn('GROOVY_TEST_CLASSPATH=/usr/local/lib/controller-groovy/groovy-all-${CI_GROOVY_VERSION}.jar', source)
        self.assertIn('chown root:root', source)
        self.assertIn('chmod 0644', source)
        self.assertLess(source.index('sha256sum --check -', source.index('# 门禁')), source.index('USER jenkins'))
        compose = (ROOT / 'jenkins/remote/compose.yaml').read_text(encoding='utf-8')
        pr = compose.split('  pr-agent:\n', 1)[1].split('\nvolumes:', 1)[0]
        self.assertIn('context: ./android-agent', pr)
        self.assertNotIn('docker.sock', compose)
        self.assertNotIn('dws_state', pr)
        self.assertNotIn('/home/jenkins/signing', pr)


if __name__ == '__main__':
    unittest.main()
