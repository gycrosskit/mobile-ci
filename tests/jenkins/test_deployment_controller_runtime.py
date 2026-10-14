"""真实Groovy执行固定Controller初始化器；只替换fixture路径和当前测试UID。"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DeploymentControllerRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.classpath = os.environ.get('GROOVY_TEST_CLASSPATH')
        if not cls.classpath:
            cache = Path(os.environ.get('GRADLE_USER_HOME', Path.home() / '.gradle')) / 'wrapper/dists'
            for core in sorted(cache.glob('*/*/*/lib/groovy-[0-9]*.jar')):
                jars = [core, core.with_name(core.name.replace('groovy-', 'groovy-json-'))]
                if all(j.is_file() for j in jars):
                    cls.classpath = os.pathsep.join(map(str, jars)); break
        if not cls.classpath or not shutil.which('java'):
            raise unittest.SkipTest('已有Groovy/JDK缺失；不自动下载或部署Controller')

    def run_fixture(self, mutate=None, expected=True, secret_mode=None):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp).resolve()
            home = root / 'home'; home.mkdir(mode=0o700)
            config = home / 'ci-deployment'; config.mkdir(mode=0o700)
            secretroot = root / 'secrets'; secretroot.mkdir(mode=0o700)
            node = {'id': 'one', 'name': 'build-one', 'labels': ['android-builder'], 'capabilities': ['android'],
                    'target': {'os': 'linux', 'arch': 'amd64'}, 'remoteFS': '/home/jenkins/agent', 'executors': 1}
            deployment = b'fixture deployment snapshot\n'
            manifest = {'version': 1, 'deploymentId': 'fixture', 'deploymentSha256': hashlib.sha256(deployment).hexdigest(), 'nodes': [node]}
            if mutate: mutate(manifest)
            (config / 'deployment.json').write_bytes(deployment); (config / 'deployment.json').chmod(0o600)
            (config / 'nodes.json').write_text(json.dumps(manifest)); (config / 'nodes.json').chmod(0o600)
            for n in manifest['nodes']:
                if isinstance(n, dict) and isinstance(n.get('id'), str) and '/' not in n['id']:
                    d = secretroot / n['id']; d.mkdir(mode=0o700, exist_ok=True)
            if secret_mode:
                p = secretroot / 'one/agent.secret'
                p.write_text('unknown-secret'); p.chmod(secret_mode)
            source = (ROOT / 'jenkins/controller/init.groovy.d/06-configure-deployment-agents.groovy').read_text()
            source = source.replace('/var/jenkins_home', str(home)).replace('/run/mobile-ci', str(secretroot)).replace("!= 1000", "!= " + str(os.getuid()))
            scriptfile = root / 'production.groovy'; scriptfile.write_text(source)
            stubs = {
                'hudson.model': 'class Node { enum Mode { EXCLUSIVE } }',
                'hudson.slaves': '''class JNLPLauncher {}\nclass RetentionStrategy { static class Always {} }\nclass DumbSlave {\nString name; String remoteFS; int numExecutors; def mode; String labelString; def launcher; def retentionStrategy\nDumbSlave(name,description,remoteFS,executors,mode,labels,launcher,strategy,properties) { this.name=name; this.remoteFS=remoteFS;this.numExecutors=Integer.parseInt(executors); this.mode=mode;this.labelString=labels;this.launcher=launcher;this.retentionStrategy=strategy }\ndef toComputer(){ [isIdle:{true}] }; def save(){}\n}''',
                'jenkins.slaves': "class JnlpAgentReceiver { static DATABASE = new Database(); static class Database { String getSecretOf(String name) { 'fixture-token-' + name } } }",
                'jenkins.model': '''class Jenkins { static instance = new Jenkins(); static get(){ instance }; def nodes=[:]; def mutations=[]; int numExecutors=2\ndef getNode(name){nodes[name]}; def addNode(n){mutations << 'add';nodes[n.name]=n}; def setNumExecutors(n){mutations << 'executors';numExecutors=n}; def toComputer(){ [isIdle:{true}] }; def save(){mutations << 'save'} }''',
            }
            test = 'def loader = new GroovyClassLoader()\n'
            for package, body in stubs.items():
                test += 'loader.parseClass(' + json.dumps('package ' + package + '\n' + body) + ')\n'
            test += 'def jenkins = loader.loadClass("jenkins.model.Jenkins").get(); boolean accepted=false\n'
            test += 'try { loader.parseClass(new File(' + json.dumps(str(scriptfile)) + ')).newInstance().run(); accepted=true } catch (IllegalStateException expected) {}\n'
            test += 'assert accepted == ' + str(expected).lower() + '\n'
            if expected:
                test += 'assert jenkins.numExecutors == 0; assert jenkins.nodes.size() == 1\n'
                test += 'assert new File(' + json.dumps(str(secretroot / 'one/agent.secret')) + ').text.trim() == "fixture-token-build-one"\n'
                test += 'loader.parseClass(new File(' + json.dumps(str(scriptfile)) + ')).newInstance().run(); assert jenkins.nodes.size() == 1\n'
            else:
                test += 'assert jenkins.mutations.empty; assert jenkins.nodes.isEmpty(); assert jenkins.numExecutors == 2\n'
            runner = root / 'test.groovy'; runner.write_text(test)
            env = dict(os.environ, MOBILE_CI_DEPLOYMENT_FILE=str(config / 'nodes.json'),
                       MOBILE_CI_NODES_SHA256=hashlib.sha256((config / 'nodes.json').read_bytes()).hexdigest(),
                       MOBILE_CI_DEPLOYMENT_SHA256=hashlib.sha256(deployment).hexdigest())
            r = subprocess.run(['java', '-cp', self.classpath, 'groovy.ui.GroovyMain', str(runner)], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(0, r.returncode, r.stderr + r.stdout)

    def test_valid_private_nodes_are_created_and_idempotent(self): self.run_fixture()

    def test_invalid_nodes_are_rejected_before_controller_mutations(self):
        mutations = [lambda m: m['nodes'].append(copy.deepcopy(m['nodes'][0])),
                     lambda m: m['nodes'][0].update(extra='arbitrary'),
                     lambda m: m['nodes'][0].update(name='.'), lambda m: m['nodes'][0].update(name='..'),
                     lambda m: m['nodes'][0].update(id='state-init'),
                     lambda m: m['nodes'][0].update(name='pr-builder'),
                     lambda m: m['nodes'][0].update(name='android-builder', labels=['pr-builder']),
                     lambda m: m['nodes'][0].update(remoteFS='/home/jenkins/../secret'),
                     lambda m: m['nodes'][0]['labels'].append('pr-builder'),
                     lambda m: m['nodes'][0].update(labels=['pr-builder', 'mac-build']),
                     lambda m: m['nodes'][0]['capabilities'].append('ios'),
                     lambda m: m['nodes'][0].update(executors=2),
                     lambda m: m['nodes'][0]['target'].update(os='windows'),
                     lambda m: m['nodes'][0]['target'].update(arch='arm64')]
        for mutation in mutations:
            with self.subTest(mutation=mutation): self.run_fixture(mutation, False)

    def test_existing_insecure_or_unknown_secret_is_preserved(self):
        self.run_fixture(expected=False, secret_mode=0o644)
        self.run_fixture(expected=False, secret_mode=0o600)


if __name__ == '__main__': unittest.main()
