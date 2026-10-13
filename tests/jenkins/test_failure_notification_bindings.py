import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class FailureNotificationBindingsTest(unittest.TestCase):
    def test_silent_mode_and_fixed_project_credentials(self):
        classpath = os.environ.get("GROOVY_TEST_CLASSPATH")
        if not classpath:
            cache = Path(os.environ.get("GRADLE_USER_HOME", Path.home() / ".gradle")) / "wrapper/dists"
            for core in sorted(cache.glob("*/*/*/lib/groovy-[0-9]*.jar")):
                json_jar = core.with_name(core.name.replace("groovy-", "groovy-json-"))
                if json_jar.is_file():
                    classpath = str(core) + os.pathsep + str(json_jar)
                    break
        if not classpath or not shutil.which("java"):
            self.skipTest("需要已有 Groovy/JDK，不自动下载")
        source = r'''
def execute(Map params, Map environment, Map project, boolean expectBlocked = false) {
    List credentials = []; List queries = []; int sends = 0
    def binding = new Binding([
        params:params, env:environment, echo:{ignored ->},
        error:{String message -> throw new IllegalArgumentException(message)},
        string:{Map input -> input}, withEnv:{List values, Closure body -> body()},
        withCredentials:{List values, Closure body -> credentials.addAll(values); body()},
        sh:{input ->
            if (input instanceof Map) {
                assert input.returnStdout
                assert input.script == 'python3 "$CI_TOOL_ROOT/project_config.py" --job "$JOB_NAME"'
                queries << input.script
                return groovy.json.JsonOutput.toJson(project)
            }
            assert credentials.size() == 2
            sends++; return 0
        }
    ])
    def helper = new GroovyShell(binding).evaluate(new File(args[0]))
    boolean blocked = false
    try { helper.send('HarmonyOS', 'FAILURE', 'Strict Signing') }
    catch (IllegalArgumentException ignored) { blocked = true }
    assert blocked == expectBlocked
    [credentials:credentials, queries:queries, sends:sends]
}
def project = [jobs:[harmony_test:[name:'other-apps/other-harmony-test']],
    providers:[dingtalk:[webhookCredentialId:'other-webhook', secretCredentialId:'other-secret']]]
def environment = [JOB_NAME:'other-apps/other-harmony-test', CI_NOTIFY:'true']
[[UPLOAD_AND_NOTIFY:false], [:], [UPLOAD_AND_NOTIFY:'true'],
 [UPLOAD_AND_NOTIFY:null], [NOTIFY:true, DRY_RUN:false],
 [UPLOAD_AND_NOTIFY:true, NOTIFY:false], [UPLOAD_AND_NOTIFY:true, DRY_RUN:false]].each { params ->
    def result = execute(params, environment, project)
    assert !result.credentials && !result.queries && result.sends == 0
}
def notValidated = execute([UPLOAD_AND_NOTIFY:true], environment + [CI_NOTIFY:'false'], project)
assert !notValidated.credentials && notValidated.sends == 0
def enabled = execute([UPLOAD_AND_NOTIFY:true], environment, project)
assert enabled.credentials*.credentialsId == ['other-webhook','other-secret']
assert enabled.sends == 1 && enabled.queries.size() == 1
def foreign = execute([UPLOAD_AND_NOTIFY:true], environment,
    project + [jobs:[harmony_test:[name:'foreign/job']]], true)
assert !foreign.credentials && foreign.sends == 0
def wrongRole = execute([UPLOAD_AND_NOTIFY:true], environment,
    project + [jobs:[signing_maintenance:[name:environment.JOB_NAME]]], true)
assert !wrongRole.credentials && wrongRole.sends == 0
def missing = execute([UPLOAD_AND_NOTIFY:true], environment,
    project + [providers:[dingtalk:[:]]], true)
assert !missing.credentials && missing.sends == 0
println 'OK'
'''
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "failure_bindings.groovy"
            harness.write_text(source)
            result = subprocess.run(
                ["java", "-cp", classpath, "groovy.ui.GroovyMain", str(harness),
                 str(ROOT / "jenkins/notify_failure.groovy")],
                text=True, capture_output=True, timeout=30,
            )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)
