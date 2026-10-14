"""实际Groovy执行Gitee启动接线，验证重启不能覆盖管理员禁用声明。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class GiteeControllerBootstrapTests(unittest.TestCase):
    def test_ready_credentials_preserve_disabled_jobs_and_legacy_default(self):
        classpath = os.environ.get('GROOVY_TEST_CLASSPATH')
        if not classpath:
            for core in sorted((Path.home() / '.gradle/wrapper/dists').glob('*/*/*/lib/groovy-[0-9]*.jar')):
                jars = [core, core.with_name(core.name.replace('groovy-', 'groovy-json-')),
                        core.with_name(core.name.replace('groovy-', 'groovy-xml-'))]
                if all(jar.is_file() for jar in jars): classpath = os.pathsep.join(map(str, jars)); break
        self.assertTrue(classpath and shutil.which('java'), '安全行为测试需要已有Groovy/JDK，不能skip')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); root.chmod(0o700)
            source = ROOT / 'jenkins/controller'
            (root / 'ci-auto-promotion').mkdir(); (root / 'ci-projects').mkdir(mode=0o700)
            shutil.copyfile(source / 'CiAutoPromotion.groovy', root / 'ci-auto-promotion/CiAutoPromotion.groovy')
            profile = json.loads((ROOT / 'examples/second-project/project.json').read_text()); profile.pop('testOnly')
            for role in ('app_pr', 'ci_pr'): profile['jobs'][role]['enabled'] = False
            profile_file = root / 'ci-projects/example-mobile.json'
            profile_file.write_text(json.dumps(profile)); profile_file.chmod(0o600)
            definitions = {
                'com.cloudbees.plugins.credentials.common': 'class StandardCredentials {}',
                'com.cloudbees.plugins.credentials': 'class CredentialsProvider { static List credentials=[]; static lookupCredentialsInItem(Class type,Object item,Object auth,List domains){credentials} }',
                'com.gitee.jenkins.connection': '''class GiteeApiToken extends com.cloudbees.plugins.credentials.common.StandardCredentials { String id }
class GiteeConnection { String name; GiteeConnection(Object... values){name=values[0]} }
class GiteeConnectionConfig { List connections=[]; void save(){} }
class GiteeConnectionProperty { String giteeConnection; GiteeConnectionProperty(String name){giteeConnection=name} }''',
                'org.jenkinsci.plugins.plaincredentials': 'class StringCredentials extends com.cloudbees.plugins.credentials.common.StandardCredentials { String id; def secret=[plainText:"fixture-webhook"] }',
                'com.gitee.jenkins.trigger': 'class GiteePushTrigger { Map settings=[:]; def methodMissing(String name,def args){assert name.startsWith("set"); settings[name]=args[0]} }',
                'com.gitee.jenkins.trigger.filter': 'enum BranchFilterType { NameBasedFilter }; enum BuildInstructionFilterType { CI_SKIP }',
                'hudson.security': 'class ACL { static SYSTEM2=new Object() }',
                'jenkins.model': 'class Jenkins { static instance; static get(){instance} }',
            }
            test = 'import groovy.json.JsonOutput\nimport groovy.json.JsonSlurperClassic\ndef loader=new GroovyClassLoader()\n'
            for package, body in definitions.items(): test += 'loader.parseClass(' + json.dumps('package '+package+'\n'+body) + ')\n'
            test += 'def project=new JsonSlurperClassic().parse(new File('+json.dumps(str(profile_file))+'))\n'
            test += '''def state=[:];def jobs=[:]
['app_pr','ci_pr'].each { role ->
 def name=project.jobs[role].name;state[name]=[disabled:false,saves:0]
 def job=new Expando(fullName:name)
 job.getProperty={Class ignored->null};job.removeProperty={Class ignored->};job.addProperty={Object ignored->}
 job.getTriggers={[: ]};job.addTrigger={Object ignored->}
 job.setDisabled={boolean disabled->state[name].disabled=disabled};job.save={state[name].saves++}
 jobs[name]=job
}
def descriptor=loader.loadClass('com.gitee.jenkins.connection.GiteeConnectionConfig').newInstance()
def jenkins=new Expando(rootDir:new File(ROOTPATH),pluginManager:new Expando(uberClassLoader:loader))
jenkins.getItemByFullName={String name->jobs[name]};jenkins.getDescriptor={Class ignored->descriptor}
loader.loadClass('jenkins.model.Jenkins').instance=jenkins
loader.loadClass('com.cloudbees.plugins.credentials.CredentialsProvider').credentials=[
 loader.loadClass('com.gitee.jenkins.connection.GiteeApiToken').newInstance(id:project.credentials.giteeApi),
 loader.loadClass('org.jenkinsci.plugins.plaincredentials.StringCredentials').newInstance(id:project.credentials.giteeWebhook)]
def boot={new GroovyShell(new GroovyClassLoader(loader)).evaluate(new File(SCRIPTPATH))}
def persist={new File(PROFILEPATH).text=JsonOutput.toJson(project)}
boot();assert state.values().every {it.disabled && it.saves==1}: 'Explicit disabled declaration must survive ready credentials'
boot();assert state.values().every {it.disabled && it.saves==2}: 'Repeated bootstrap must preserve disabled state'
project.jobs.app_pr.enabled=true;project.jobs.ci_pr.remove('enabled');persist()
boot();assert state.values().every {!it.disabled && it.saves==3}: 'True and missing enabled retain legacy activation'
project.jobs.app_pr.enabled=false;persist();loader.loadClass('com.cloudbees.plugins.credentials.CredentialsProvider').credentials=[]
boot();assert state.values().every {it.disabled && it.saves==4}: 'Missing credentials remain fail closed'
project.jobs.app_pr.enabled='false';persist();boolean rejected=false
try {boot()} catch(IllegalArgumentException expected){rejected=true}
assert rejected;assert state.values().every {it.saves==4}: 'Invalid flag rejected by actual trusted loader before Job IO'
'''.replace('ROOTPATH',json.dumps(str(root))).replace('SCRIPTPATH',json.dumps(str(source / 'init.groovy.d/02-configure-gitee.groovy'))).replace('PROFILEPATH',json.dumps(str(profile_file)))
            runner = root / 'test.groovy'; runner.write_text(test)
            env = dict(os.environ, JENKINS_CI_PROJECT='example-mobile')
            r = subprocess.run(['java','-cp',classpath,'groovy.ui.GroovyMain',str(runner)], env=env, capture_output=True,text=True,timeout=30)
            self.assertEqual(0,r.returncode,r.stderr+r.stdout)


if __name__ == '__main__': unittest.main()
