import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]

# Run the production class with in-memory Jenkins IO; never touch a Controller.
GROOVY_CHECKS = r'''
import groovy.xml.XmlUtil
import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.attribute.PosixFilePermissions

def loader = new GroovyClassLoader()
def core = loader.parseClass(new File(args[0]))
String old = '1' * 40
String next = '2' * 40
String tree = '3' * 40
String head = '4' * 40
String job = 'mobile-apps/sxmqlive-android-test'
String inlineJob = 'sxmqlive-ci-pr-validation'
String pipeline = "def CI_AUTOMATION_COMMIT = '__CI_AUTOMATION_COMMIT__'\nnode { echo 'validated' }"
def proof = {
    [commit: next, tree: tree, testedTree: tree, testedMerge: '5' * 40,
     result: 'SUCCESS', building: false, repository: core.REPOSITORY.replaceFirst('^git@gitee.com:', '').replaceFirst(/\.git$/, ''),
     target: 'master', mrState: 'merged', head: head, mergedHead: head, trustedScript: true]
}
def scmConfig = { name = job, commit = old ->
    """<flow-definition plugin="workflow-job@test">
      <description>operator description</description>
      <properties>
        <hudson.security.AuthorizationMatrixProperty><permission>hudson.model.Item.Read:operator</permission></hudson.security.AuthorizationMatrixProperty>
        <hudson.model.ParametersDefinitionProperty><parameterDefinitions><hudson.model.StringParameterDefinition><name>BRAND</name><defaultValue>test-brand</defaultValue></hudson.model.StringParameterDefinition></parameterDefinitions></hudson.model.ParametersDefinitionProperty>
      </properties>
      <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition" plugin="workflow-cps@test">
        <scm class="hudson.plugins.git.GitSCM" plugin="git@test">
          <userRemoteConfigs><hudson.plugins.git.UserRemoteConfig><url>${core.REPOSITORY}</url><credentialsId>operator-ssh-id</credentialsId><refspec>+refs/heads/*:refs/remotes/origin/*</refspec></hudson.plugins.git.UserRemoteConfig></userRemoteConfigs>
          <branches><hudson.plugins.git.BranchSpec><name>${commit}</name></hudson.plugins.git.BranchSpec></branches>
          <extensions><hudson.plugins.git.extensions.impl.CloneOption><shallow>false</shallow></hudson.plugins.git.extensions.impl.CloneOption></extensions>
        </scm>
        <scriptPath>${core.JOBS[name]}</scriptPath><lightweight>false</lightweight>
      </definition>
      <triggers><hudson.triggers.TimerTrigger><spec>H 2 * * *</spec></hudson.triggers.TimerTrigger></triggers>
      <disabled>true</disabled><keepDependencies>true</keepDependencies>
    </flow-definition>"""
}
def inlineConfig = { commit = old ->
    def root = core.parse(scmConfig())
    root.remove(root.definition[0])
    def definition = root.appendNode('definition', [class: 'org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition'])
    definition.appendNode('script', pipeline.replace('__CI_AUTOMATION_COMMIT__', commit))
    definition.appendNode('sandbox', 'true')
    XmlUtil.serialize(root)
}
def rejected = { Class type, Closure action ->
    try { action(); assert false: 'Expected ' + type.simpleName }
    catch (Exception error) { assert type.isInstance(error): "${error.class.name}: ${error.message}" }
}
def outsideDefinition = { String source ->
    def root = core.parse(source)
    root.remove(root.definition[0])
    XmlUtil.serialize(root)
}
def fixture = { String source = scmConfig() ->
    def state = [xml: source, events: [], writes: [], backups: [], completed: [], locked: false, reads: 0]
    def checked = { assert state.locked: 'Every IO operation must be inside the queue lock' }
    def io = [
        lock: { Closure action ->
            assert !state.locked
            state.events << 'lock'
            state.locked = true
            try { action() } finally { state.locked = false; state.events << 'unlock' }
        },
        busy: { checked(); state.events << 'busy'; false },
        read: { checked(); state.events << 'read'; state.reads++; state.xml },
        ancestor: { from, to -> checked(); state.events << 'ancestor'; assert from == old; assert to == next; true },
        backup: { xml -> checked(); state.events << 'backup'; state.backups << xml; 'backup/config.xml' },
        completedBackup: { backup, verified -> checked(); assert verified == state.xml; state.completed << backup },
        write: { xml -> checked(); state.events << 'write'; state.writes << xml; state.xml = xml },
        sameDefinition: { left, right ->
            checked()
            XmlUtil.serialize(core.parse(left).definition[0]) == XmlUtil.serialize(core.parse(right).definition[0])
        }
    ]
    [state, io]
}
def defaultProject = {
    def project = new JsonSlurperClassic().parse(new File(args[4]))
    // 合成 fixture 仅在隔离测试中模拟管理员部署，不作为生产 profile。
    project.remove('testOnly')
    project
}
def installProject = { File directory, Map project ->
    directory.mkdirs()
    Files.setPosixFilePermissions(directory.toPath(),PosixFilePermissions.fromString('rwx------'))
    def file = new File(directory,project.id+'.json')
    file.text=JsonOutput.toJson(project)
    Files.setPosixFilePermissions(file.toPath(),PosixFilePermissions.fromString('rw-------'))
}
def controllerFixture = { Map project = defaultProject() ->
    [
        'com.cloudbees.plugins.credentials.CredentialsProvider': 'class CredentialsProvider { static List credentials=[]; static List lookups=[]; static lookupCredentialsInItem(Class type, Object item, Object auth, List domains) { lookups << item; credentials } }',
        'com.gitee.jenkins.connection.GiteeApiToken': 'class GiteeApiToken {}',
        'hudson.model.RootAction': 'interface RootAction { String getIconFileName(); String getDisplayName(); String getUrlName() }',
        'hudson.model.InvisibleAction': 'class InvisibleAction { String getIconFileName() { null }; String getDisplayName() { null }; String getUrlName() { null } }',
        'hudson.model.TaskListener': 'class TaskListener { static final NULL = new Object() }',
        'hudson.plugins.git.util.BuildData': 'class BuildData {}',
        'hudson.security.ACLContext': """class ACLContext implements AutoCloseable {
            static List events = []
            boolean closed
            int closeCalls
            void close() {
                assert !closed: 'ACL context must close exactly once'
                closed = true; closeCalls++; events << 'close'
            }
        }""",
        'hudson.security.ACL': """class ACL {
            static final SYSTEM2 = new Object()
            static ACLContext lastContext
            static ACLContext as2(authentication) {
                assert authentication.is(SYSTEM2)
                ACLContext.events << 'open'
                lastContext = new ACLContext()
            }
        }""",
        'jenkins.util.Timer': 'class Timer { static def instance; static get() { instance } }',
        'org.jenkinsci.plugins.workflow.flow.FlowDefinition': 'class FlowDefinition { def node }',
        'jenkins.model.Jenkins': 'class Jenkins { static def instance; static def XSTREAM2; static get() { instance } }',
        'hudson.model.Queue': """class Queue {
            static boolean locked
            static List events = []
            static withLock(java.util.concurrent.Callable operation) {
                assert !locked
                locked = true; events << 'lock'
                try { operation.call() } finally { locked = false; events << 'unlock' }
            }
        }"""
    ].each { name, body -> loader.parseClass('package ' + name.substring(0, name.lastIndexOf('.')) + '\n' + body) }
    String source = new File(args[2]).getText('UTF-8')
    int registration = source.indexOf('\ndef jenkins = Jenkins.get()')
    assert registration > 0: 'Load the Controller class without executing its registration'
    def controllerClass = loader.parseClass(source.substring(0, registration))
    def queueClass = loader.loadClass('hudson.model.Queue')
    def jenkinsClass = loader.loadClass('jenkins.model.Jenkins')
    def flowClass = loader.loadClass('org.jenkinsci.plugins.workflow.flow.FlowDefinition')
    def state = [xml: scmConfig(), definitions: [], events: [], building: false]
    def jobObject = new Expando(fullName: job)
    jobObject.ownerTask = jobObject
    jobObject.isBuilding = { state.building }
    jobObject.configFile = new Expando(asString: { state.xml })
    jobObject.setDefinition = { value ->
        assert queueClass.locked: 'Definition must only change under the Jenkins Queue lock'
        state.events << 'setDefinition'; state.definitions << value
        def current = core.parse(state.xml)
        current.definition[0].replaceNode(value.node)
        state.xml = XmlUtil.serialize(current)
    }
    ['updateByXml', 'cancel', 'doStop', 'scheduleBuild', 'scheduleBuild2', 'setDisabled',
     'setTriggers', 'setProperties', 'save'].each { method ->
        jobObject[method] = { Object[] values -> assert false: 'Forbidden Job mutation: ' + method }
    }
    def queueObject = new Expando(items: [])
    jenkinsClass.instance = new Expando(queue: queueObject)
    jenkinsClass.XSTREAM2 = new Expando(fromXML: { String xml ->
        def node = core.parse(xml)
        assert node.name() == 'definition': 'Only definition XML may be deserialized'
        state.events << 'deserializeDefinition'
        flowClass.newInstance(node: node)
    })
    def worker = controllerClass.newInstance(core, new File(args[3], 'controller-home'),project,defaultProject())
    [worker, jobObject, queueObject, state, queueClass, jenkinsClass]
}

switch (args[1]) {
case 'proof':
    core.verifyProof(proof())
    ['commit', 'tree', 'testedTree', 'testedMerge', 'head', 'mergedHead'].each { field ->
        [null, '', 'master', 'a' * 39, 'a' * 41, 'A' * 40, 123].each { invalid ->
            def p = proof(); p[field] = invalid
            rejected(IllegalArgumentException) { core.verifyProof(p) }
        }
    }
    [tree: '6' * 40, result: 'FAILURE', building: true, repository: 'attacker/sxmqlive-ci',
     target: 'release', mrState: 'opened', mergedHead: '6' * 40, trustedScript: false].each { field, value ->
        def p = proof(); p[field] = value
        rejected(IllegalArgumentException) { core.verifyProof(p) }
    }
    [result: null, building: null, trustedScript: null].each { field, value ->
        def p = proof(); p[field] = value
        rejected(IllegalArgumentException) { core.verifyProof(p) }
    }
    break
case 'mixed_project':
    def mixedProject = {
        def project = defaultProject()
        project.scm = [provider:'gitee']
        project.repositories.automation = [provider:'github',identity:'gycrosskit/mobile-ci',
            ssh:'git@github.com:gycrosskit/mobile-ci.git',https:'https://github.com/gycrosskit/mobile-ci.git',
            defaultBranch:'main',trustRef:'refs/heads/main']
        project
    }
    def directory = new File(args[3], 'trusted-projects')
    def project = mixedProject()
    installProject(directory, project)
    def loaded = core.loadProject(directory, project.id)
    assert core.automationProvider(loaded) == 'github'
    assert !loaded.repositories.application.containsKey('provider'): 'Do not rewrite legacy bindings'
    assert loaded.credentials == defaultProject().credentials
    assert loaded.providers == defaultProject().providers && loaded.jobs == defaultProject().jobs
    String source = scmConfig().replace(core.REPOSITORY, project.repositories.automation.ssh)
    String updated = core.updatedConfig(source, job, next, null, loaded)
    assert core.pinnedCommit(updated, job, loaded) == next
    assert outsideDefinition(updated) == outsideDefinition(source)
    rejected(IllegalArgumentException) { core.pinnedCommit(scmConfig(), job, loaded) }
    [
        { it.repositories.automation.provider = 'gitlab' },
        { it.repositories.automation.provider = null },
        { it.repositories.automation.ssh = 'git@gitee.com:gycrosskit/mobile-ci.git' },
        { it.repositories.automation.https = 'https://github.com/attacker/mobile-ci.git' },
        { it.repositories.automation.https = 'https://github.com@attacker.example/gycrosskit/mobile-ci.git' },
        { it.repositories.automation.identity = 'gycrosskit/..' },
        { it.repositories.automation.identity = 'gy.crosskit/mobile-ci' },
        { it.repositories.automation.defaultBranch = '../main' },
        { it.repositories.automation.trustRef = 'refs/heads/master' },
        { it.repositories.automation.credentialsId = 'unapproved' },
        { it.repositories.application.provider = 'github' },
        { it.scm = [provider:'github'] },
        { it.scm = [provider:'gitee',forkPolicy:'allow'] },
        { it.testOnly = true },
        { it.credentials = [:] }
    ].each { mutate ->
        def invalid = mixedProject(); mutate(invalid); installProject(directory, invalid)
        rejected(IllegalArgumentException) { core.loadProject(directory, invalid.id) }
    }
    break
case 'controller_github_blocked':
    def project = defaultProject()
    project.repositories.automation = [provider:'github',identity:'gycrosskit/mobile-ci',
        ssh:'git@github.com:gycrosskit/mobile-ci.git',https:'https://github.com/gycrosskit/mobile-ci.git',
        defaultBranch:'main',trustRef:'refs/heads/main']
    def (worker, jobObject, queueObject, state, queueClass, jenkinsClass) = controllerFixture(project)
    def credentialClass = loader.loadClass('com.cloudbees.plugins.credentials.CredentialsProvider')
    jenkinsClass.instance.getItemByFullName = { String ignored -> assert false: 'No Job lookup for unsupported promotion source' }
    URL.metaClass.openConnection = { assert false: 'No API access for unsupported promotion source' }
    try {
        worker.cache['/branches/main'] = [commit:[sha:next]]
        rejected(IllegalStateException) { worker.api('/branches/main') }
        rejected(IllegalStateException) { worker.ancestor(old, old) }
        rejected(IllegalStateException) { worker.ancestor(old, next) }
        rejected(IllegalStateException) { worker.proof(next) }
        rejected(IllegalStateException) { worker.pipeline(next) }
        def forged = proof(); forged.repository = project.repositories.automation.identity; forged.target = 'main'
        rejected(IllegalStateException) { core.verifyProof(forged, project) }
        def (ioState, io) = fixture()
        rejected(IllegalStateException) { core.promote(job, forged, null, io, project) }
        assert ioState.events.empty && ioState.backups.empty && ioState.writes.empty
        worker.class.privateWrite(new File(worker.home, 'config.json'), JsonOutput.toJson([enabled:false]))
        assert worker.check().status == 'disabled'
        worker.class.privateWrite(new File(worker.home, 'config.json'), JsonOutput.toJson([enabled:true,jobs:[job]]))
        rejected(IllegalStateException) { worker.check() }
        worker.run()
        def persisted = new JsonSlurperClassic().parse(new File(worker.home, 'status.json'))
        assert persisted == [status:'blocked',error:'IllegalStateException',reason:'unsupported-automation-provider']
        assert credentialClass.lookups.empty && state.definitions.empty && queueClass.events.empty
        assert !new File(worker.home, 'backups-v2').exists()
    } finally { GroovySystem.metaClassRegistry.removeMetaClass(URL) }
    break
case 'controller_gitee_binding':
    def project = defaultProject()
    project.remove('testOnly')
    def (worker, jobObject, queueObject, state, queueClass, jenkinsClass) = controllerFixture(project)
    ['com.cloudbees.plugins.credentials.common.StandardCredentials',
     'com.gitee.jenkins.connection.GiteeConnection', 'com.gitee.jenkins.connection.GiteeConnectionConfig',
     'com.gitee.jenkins.connection.GiteeConnectionProperty', 'com.gitee.jenkins.trigger.GiteePushTrigger',
     'com.gitee.jenkins.trigger.filter.BranchFilterType', 'com.gitee.jenkins.trigger.filter.BuildInstructionFilterType',
     'org.jenkinsci.plugins.plaincredentials.StringCredentials'].each { name ->
        loader.parseClass('package ' + name.substring(0,name.lastIndexOf('.')) + '\nclass ' + name.substring(name.lastIndexOf('.')+1) + ' {}')
    }
    def rootDirectory = new File(args[3], 'gitee-controller')
    def home = new File(rootDirectory, 'ci-auto-promotion'); home.mkdirs()
    new File(home, 'CiAutoPromotion.groovy').text = new File(args[0]).getText('UTF-8')
    jenkinsClass.instance.rootDir = rootDirectory
    jenkinsClass.instance.pluginManager = new Expando(uberClassLoader:loader)
    def selected = []
    jenkinsClass.instance.getItemByFullName = { String name -> selected << name; null }
    def script = new File(new File(args[2]).parentFile, '02-configure-gitee.groovy')
    def directory = new File(rootDirectory, 'ci-projects')
    installProject(directory, project)
    new GroovyShell(new GroovyClassLoader(loader)).evaluate(script)
    assert selected == [project.jobs.app_pr.name, project.jobs.ci_pr.name]
    selected.clear()
    project.repositories.automation = [provider:'github',identity:'gycrosskit/mobile-ci',
        ssh:'git@github.com:gycrosskit/mobile-ci.git',https:'https://github.com/gycrosskit/mobile-ci.git',
        defaultBranch:'main',trustRef:'refs/heads/main']
    installProject(directory, project)
    new GroovyShell(new GroovyClassLoader(loader)).evaluate(script)
    assert selected == [project.jobs.app_pr.name]: 'GitHub history gate must retain administrator configuration'
    assert loader.loadClass('com.cloudbees.plugins.credentials.CredentialsProvider').lookups.empty
    assert state.definitions.empty && queueClass.events.empty
    break
case 'config':
    core.JOBS.each { name, path ->
        String before = name == inlineJob ? inlineConfig() : scmConfig(name)
        String after = core.updatedConfig(before, name, next, pipeline)
        assert core.pinnedCommit(after, name) == next
        assert outsideDefinition(before) == outsideDefinition(after)
        assert core.updatedConfig(after, name, next, pipeline) == after
        if (name != inlineJob) {
            def expected = core.parse(before).definition[0]
            expected.scm[0].branches[0].children()[0].name[0].setValue(next)
            assert XmlUtil.serialize(expected) == XmlUtil.serialize(core.parse(after).definition[0])
        } else {
            assert core.parse(after).definition.script.text() == pipeline.replace('__CI_AUTOMATION_COMMIT__', next)
            assert core.parse(after).definition.sandbox.text() == 'true'
        }
    }
    break
case 'scm_trust':
    String source = scmConfig()
    [source.replace(core.REPOSITORY, 'git@gitee.com:attacker/sxmqlive-ci.git'),
     source.replace(core.JOBS[job], 'jenkins/Jenkinsfile.release'),
     source.replace('hudson.plugins.git.GitSCM', 'untrusted.SCM'),
     source.replace('org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition', 'untrusted.Definition'),
     source.replace('<name>' + old + '</name>', '<name>master</name>'),
     source.replace('</userRemoteConfigs>', '<hudson.plugins.git.UserRemoteConfig><url>attacker</url></hudson.plugins.git.UserRemoteConfig></userRemoteConfigs>'),
     source.replace('</branches>', '<hudson.plugins.git.BranchSpec><name>' + next + '</name></hudson.plugins.git.BranchSpec></branches>'),
     source.replace('<lightweight>false</lightweight>', '<lightweight>true</lightweight>'),
     source.replace('<lightweight>false</lightweight>', '')].each { invalid ->
        rejected(IllegalArgumentException) { core.pinnedCommit(invalid, job) }
        rejected(IllegalArgumentException) { core.updatedConfig(invalid, job, next) }
        rejected(IllegalArgumentException) { core.updatedConfig(invalid, job, old) }
    }
    rejected(IllegalArgumentException) { core.pinnedCommit(source, 'operator/private-job') }
    rejected(IllegalArgumentException) { core.updatedConfig(source, job, 'master') }
    break
case 'inline_trust':
    String source = inlineConfig()
    rejected(IllegalArgumentException) { core.updatedConfig(source, inlineJob, next, pipeline + "\necho '{{CI_PROJECT_ID}}'") }
    [source.replace('<sandbox>true</sandbox>', '<sandbox>false</sandbox>'),
     source.replace('<sandbox>true</sandbox>', ''),
     source.replace('org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition', 'untrusted.Definition'),
     source.replace(old, 'master'),
     source.replace("CI_AUTOMATION_COMMIT = '${old}'", "CI_AUTOMATION_COMMIT = '${old}'\nCI_AUTOMATION_COMMIT = '${next}'")].each { invalid ->
        rejected(IllegalArgumentException) { core.updatedConfig(invalid, inlineJob, next, pipeline) }
    }
    [null, '', "node { echo 'missing pin' }"].each { invalid ->
        rejected(IllegalArgumentException) { core.updatedConfig(source, inlineJob, next, invalid) }
    }
    break
case 'project_app_inline':
    def project = defaultProject()
    project.id = 'example-mobile'
    project.jobs.app_pr.name = 'Example/app-pr'
    String source = inlineConfig()
    assert core.inlineRole(project.jobs.app_pr.name, project) == 'app_pr'
    String updated = core.updatedConfig(source, project.jobs.app_pr.name, next, pipeline, project)
    assert core.pinnedCommit(updated, project.jobs.app_pr.name, project) == next
    assert outsideDefinition(source) == outsideDefinition(updated)
    [source.replace('<sandbox>true</sandbox>','<sandbox>false</sandbox>'),
     source.replace('org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition','org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition'),
     source.replace('</script>',"echo '{{CI_PROJECT_ID}}'</script>")].each { invalid ->
        rejected(IllegalArgumentException) { core.pinnedCommit(invalid, project.jobs.app_pr.name, project) }
    }
    rejected(IllegalArgumentException) { core.updatedConfig(source, project.jobs.app_pr.name, next, pipeline + "\necho '{{CI_PROJECT_ID}}'", project) }
    break
case 'busy':
    ['running', 'queued'].each { busyState ->
        def (state, io) = fixture()
        io.busy = { assert state.locked; state.events << busyState; true }
        def result = core.promote(job, proof(), null, io)
        assert result.status == 'busy'
        assert state.xml == scmConfig()
        assert state.reads == 0 && state.backups.empty && state.writes.empty
        assert state.events == ['lock', busyState, 'unlock']
    }
    break
case 'unchanged':
    def (state, io) = fixture(scmConfig(job, next))
    def result = core.promote(job, proof(), null, io)
    assert result.status == 'unchanged' && result.commit == next
    assert state.backups.empty && state.writes.empty
    assert !state.events.contains('ancestor')
    break
case 'ancestor':
    def (state, io) = fixture()
    io.ancestor = { from, to -> assert state.locked; false }
    rejected(IllegalStateException) { core.promote(job, proof(), null, io) }
    assert state.backups.empty && state.writes.empty && state.xml == scmConfig()
    break
case 'cas':
    def (state, io) = fixture()
    String external = scmConfig().replace('operator description', 'admin edit')
    io.read = { assert state.locked; state.reads++; if (state.reads == 2) state.xml = external; state.xml }
    def result = core.promote(job, proof(), null, io)
    assert result.status == 'changed-externally'
    assert state.backups.empty && state.writes.empty && state.xml == external
    break
case 'promotion':
    [job, inlineJob].each { name ->
        String before = name == inlineJob ? inlineConfig() : scmConfig()
        def (state, io) = fixture(before)
        def result = core.promote(name, proof(), pipeline, io)
        assert result == [job: name, status: 'updated', before: old, commit: next, backup: 'backup/config.xml']
        assert state.backups == [before] && state.writes == [state.xml]
        assert state.completed == ['backup/config.xml']
        assert state.events.indexOf('backup') < state.events.indexOf('write')
        assert core.pinnedCommit(state.xml, name) == next
        assert outsideDefinition(state.xml) == outsideDefinition(before)
    }
    def (state, io) = fixture()
    io.backup = { xml -> assert state.locked; throw new IOException('backup unavailable') }
    rejected(IOException) { core.promote(job, proof(), null, io) }
    assert state.writes.empty && state.xml == scmConfig()
    break
case 'rollback':
    // A failed write that persisted our definition must restore the saved configuration.
    def (state, io) = fixture()
    io.write = { xml ->
        assert state.locked; state.writes << xml; state.xml = xml
        if (state.writes.size() == 1) throw new IOException('save failed after persistence')
    }
    rejected(IllegalStateException) { core.promote(job, proof(), null, io) }
    assert state.writes.size() == 2 && core.pinnedCommit(state.xml, job) == old
    assert outsideDefinition(state.xml) == outsideDefinition(scmConfig())
    assert state.backups == [scmConfig()]
    assert state.completed.empty
    // A write that never persisted anything needs no rollback write.
    def (untouched, noWrite) = fixture()
    noWrite.write = { xml -> assert untouched.locked; untouched.writes << xml; throw new IOException('save failed before persistence') }
    rejected(IllegalStateException) { core.promote(job, proof(), null, noWrite) }
    assert untouched.writes.size() == 1 && untouched.xml == scmConfig()
    // Never overwrite a competing definition after a failure.
    def (external, externalIo) = fixture()
    String operatorConfig = scmConfig(job, '6' * 40).replace('operator description', 'admin edit')
    externalIo.write = { xml -> assert external.locked; external.writes << xml; external.xml = operatorConfig; throw new IOException('concurrent save') }
    rejected(IllegalStateException) { core.promote(job, proof(), null, externalIo) }
    assert external.writes.size() == 1 && external.xml == operatorConfig
    break
case 'rollback_external_properties':
    def (state, io) = fixture()
    io.write = { xml ->
        assert state.locked; state.writes << xml
        state.xml = xml
        if (state.writes.size() == 1) {
            state.xml = xml.replace('operator description', 'admin edit')
            throw new IOException('save failed with independent admin change')
        }
    }
    rejected(IllegalStateException) { core.promote(job, proof(), null, io) }
    assert core.pinnedCommit(state.xml, job) == old
    assert core.parse(state.xml).description.text() == 'admin edit': 'Rollback must preserve unrelated administrator edits'
    break
case 'readback_failure':
    def (state, io) = fixture()
    String different = scmConfig(job, '6' * 40)
    io.write = { xml -> assert state.locked; state.writes << xml; state.xml = different }
    rejected(IllegalStateException) { core.promote(job, proof(), null, io) }
    assert state.writes.size() == 1 && state.xml == different
    assert state.backups == [scmConfig()]
    assert state.completed.empty
    break
case 'backup_cache':
    def home = new File(args[3], 'backup-cache')
    String source = scmConfig()
    String first = core.backupSnapshot(home, job, next, source)
    assert first == core.backupSnapshot(home, job, next, source)
    def files = []
    home.eachFileRecurse { file ->
        if (file.isDirectory()) assert Files.getPosixFilePermissions(file.toPath()) == PosixFilePermissions.fromString('rwx------')
        else files << file
    }
    assert files.size() == 1 && files[0].getText('UTF-8') == source
    assert Files.getPosixFilePermissions(files[0].toPath()) == PosixFilePermissions.fromString('rw-------')
    assert first.contains(next + '/' + core.backupDigest(source.getBytes('UTF-8')) + '/config.xml')
    assert first != core.backupSnapshot(home, job, '6' * 40, source)
    rejected(IllegalArgumentException) { core.backupSnapshot(home, 'manual/private-job', next, source) }
    break
case 'backup_retention':
    def home = new File(args[3], 'backup-retention')
    core.backupDirectory(home.toPath())
    def legacy = new File(home, 'backups/legacy-uuid/config.xml'); legacy.parentFile.mkdirs(); legacy.text = 'legacy rollback'
    String failed = core.backupSnapshot(home, job, '9' * 40, scmConfig())
    def snapshots = []
    String previous = old
    File manual
    ['2', '3', '4'].each { digit ->
        String target = digit * 40
        String backup = core.backupSnapshot(home, job, target, scmConfig(job, previous))
        snapshots << backup
        if (snapshots.size() == 1) { manual = new File(new File(backup).parentFile, 'operator-note'); manual.text = 'manual data' }
        core.retainBackup(home, job, target, backup)
        previous = target
    }
    assert !new File(snapshots[0]).exists()
    assert snapshots.drop(1).every { new File(it).exists() }
    assert new File(failed).exists() && legacy.text == 'legacy rollback' && manual.text == 'manual data'
    def index = new File(core.backupJobDirectory(home, job, false).toFile(), 'retained.json')
    def before = index.text
    core.retainBackup(home, job, '4' * 40, snapshots[-1])
    assert index.text == before
    assert Files.getPosixFilePermissions(index.toPath()) == PosixFilePermissions.fromString('rw-------')
    assert new JsonSlurperClassic().parseText(index.text).records.size() == 2
    break
case 'backup_rejections':
    ['symlink-root', 'symlink-job', 'symlink-file', 'hardlink-file', 'tampered-file', 'file-mode', 'directory-mode'].each { kind ->
        def home = new File(args[3], kind)
        String path = core.backupSnapshot(home, job, next, scmConfig())
        def file = new File(path).toPath()
        def outside = new File(args[3], kind + '-outside').toPath()
        switch (kind) {
        case 'symlink-root':
        case 'symlink-job':
            def directory = kind == 'symlink-root' ? home.toPath().resolve('backups-v2') : core.backupJobDirectory(home, job, false)
            Files.move(directory, outside); Files.createSymbolicLink(directory, outside); break
        case 'symlink-file':
            Files.move(file, outside); Files.createSymbolicLink(file, outside); break
        case 'hardlink-file':
            Files.createLink(outside, file); break
        case 'tampered-file': new File(path).text = 'changed'; break
        case 'file-mode': Files.setPosixFilePermissions(file, PosixFilePermissions.fromString('rw-r--r--')); break
        case 'directory-mode': Files.setPosixFilePermissions(file.parent, PosixFilePermissions.fromString('rwxr-xr-x')); break
        }
        rejected(IllegalStateException) { core.backupSnapshot(home, job, next, scmConfig()) }
    }
    ['symlink-index', 'hardlink-index', 'tampered-retained'].each { kind ->
        def home = new File(args[3], kind)
        String first = core.backupSnapshot(home, job, next, scmConfig())
        core.retainBackup(home, job, next, first)
        def index = core.backupJobDirectory(home, job, false).resolve('retained.json')
        def outside = new File(args[3], kind + '-outside').toPath()
        if (kind == 'symlink-index') { Files.move(index, outside); Files.createSymbolicLink(index, outside) }
        if (kind == 'hardlink-index') Files.createLink(outside, index)
        if (kind == 'tampered-retained') new File(first).text = 'changed'
        String backup = core.backupSnapshot(home, job, '6' * 40, scmConfig())
        rejected(IllegalStateException) { core.retainBackup(home, job, '6' * 40, backup) }
        assert new File(first).exists() && new File(backup).exists(): 'Unsafe retention must not delete snapshots'
    }
    break
case 'backup_completion':
    def (state, io) = fixture()
    io.write = { xml -> state.writes << xml; state.xml = xml.replace('operator description', 'admin edit') }
    def result = core.promote(job, proof(), null, io)
    assert result.status == 'updated' && result.backupRetention == 'concurrent-change'
    assert state.completed.empty && state.backups.size() == 1
    def (completed, completedIo) = fixture()
    completedIo.completedBackup = { backup, verified -> throw new IOException('retention unavailable') }
    def blocked = core.promote(job, proof(), null, completedIo)
    assert blocked.status == 'updated' && blocked.backupRetention == 'blocked'
    assert completed.writes.size() == 1 && core.pinnedCommit(completed.xml, job) == next
    def (worker, jobObject, queueObject, controllerState, queueClass) = controllerFixture()
    String backup = core.backupSnapshot(worker.home, job, next, controllerState.xml)
    String verified = controllerState.xml
    controllerState.xml = verified.replace('operator description', 'late admin edit')
    rejected(IllegalStateException) { worker.adapter(jobObject, old, next, true).completedBackup(backup, verified) }
    assert new File(backup).exists()
    assert !new File(core.backupJobDirectory(worker.home, job, false).toFile(), 'retained.json').exists()
    break
case 'reject_before_io':
    def (state, io) = fixture()
    rejected(IllegalArgumentException) { core.promote('operator/private-job', proof(), null, io) }
    def invalid = proof(); invalid.trustedScript = false
    rejected(IllegalArgumentException) { core.promote(job, invalid, null, io) }
    assert state.events.empty && state.writes.empty && state.backups.empty
    break
case 'controller_busy':
    def (worker, jobObject, queueObject, state, queueClass) = controllerFixture()
    assert !worker.busy(jobObject, queueObject)
    state.building = true
    assert worker.busy(jobObject, queueObject)
    state.building = false
    queueObject.items = [new Expando(task: jobObject)]
    assert worker.busy(jobObject, queueObject)
    queueObject.items = [new Expando(task: new Expando(ownerTask: jobObject))]
    assert worker.busy(jobObject, queueObject)
    queueObject.items = [new Expando(task: new Expando(ownerTask: new Object()))]
    assert !worker.busy(jobObject, queueObject)
    assert state.definitions.empty && queueClass.events.empty
    break
case 'controller_definition':
    def (worker, jobObject, queueObject, state, queueClass, jenkinsClass) = controllerFixture()
    String before = state.xml
    String after = core.updatedConfig(before, job, next)
    def io = worker.adapter(jobObject, old, next, true)
    assert io.ancestor(old, next)
    assert !io.ancestor(next, old)
    assert !worker.adapter(jobObject, old, next, false).ancestor(old, next)
    // Model an administrator changing unrelated properties immediately before the setter.
    state.xml = before.replace('operator description', 'admin edit').replace('<disabled>true</disabled>', '<disabled>false</disabled>')
    String administrative = state.xml
    io.lock { io.write(after) }
    assert state.definitions.size() == 1 && core.pinnedCommit(state.xml, job) == next
    assert outsideDefinition(state.xml) == outsideDefinition(administrative)
    assert state.events == ['deserializeDefinition', 'setDefinition']
    assert queueClass.events == ['lock', 'unlock']
    jenkinsClass.XSTREAM2 = new Expando(fromXML: { String xml -> new Object() })
    rejected(IllegalStateException) { io.lock { io.write(after) } }
    assert state.definitions.size() == 1
    break
case 'controller_promotion':
    def (worker, jobObject, queueObject, state, queueClass) = controllerFixture()
    state.xml = state.xml.replace(core.REPOSITORY, worker.project.repositories.automation.ssh)
    def projectProof = proof()
    projectProof.repository = worker.project.repositories.automation.identity
    projectProof.target = worker.project.repositories.automation.defaultBranch
    def io = worker.adapter(jobObject, old, next, true)
    state.building = true
    assert core.promote(job, projectProof, null, io, worker.project).status == 'busy'
    state.building = false
    queueObject.items = [new Expando(task: new Expando(ownerTask: jobObject))]
    assert core.promote(job, projectProof, null, io, worker.project).status == 'busy'
    assert !worker.home.exists() && state.definitions.empty
    queueObject.items = []
    String before = state.xml
    def write = io.write
    io.write = { xml ->
        assert queueClass.locked
        def backups = []
        worker.home.eachFileRecurse { file -> if (file.name == 'config.xml') backups << file }
        assert backups.size() == 1 && backups[0].getText('UTF-8') == before
        assert java.nio.file.Files.getPosixFilePermissions(backups[0].toPath()) ==
            java.nio.file.attribute.PosixFilePermissions.fromString('rw-------')
        write(xml)
    }
    def result = core.promote(job, projectProof, null, io, worker.project)
    assert result.status == 'updated' && new File(result.backup).getText('UTF-8') == before
    assert outsideDefinition(state.xml) == outsideDefinition(before)
    assert state.definitions.size() == 1
    assert core.promote(job, projectProof, null, io, worker.project).status == 'unchanged'
    assert state.definitions.size() == 1
    assert queueClass.events == ['lock', 'unlock'] * 4
    break
case 'controller_bootstrap':
case 'controller_bootstrap_public':
    def bootstrapProject = defaultProject()
    bootstrapProject.remove('testOnly')
    def (previous, jobObject, queueObject, state, queueClass, jenkinsClass) = controllerFixture(bootstrapProject)
    def rootAction = loader.loadClass('hudson.model.RootAction')
    def timerClass = loader.loadClass('jenkins.util.Timer')
    def unrelated = new Object()
    def actions = [unrelated, previous]
    def cancellations = []
    def schedules = []
    previous.future = new Expando(cancel: { boolean interrupt -> cancellations << interrupt; true })
    def rootDirectory = new File(args[3], 'bootstrap-controller')
    rootDirectory.mkdirs()
    jenkinsClass.instance.rootDir = rootDirectory
    jenkinsClass.instance.pluginManager = new Expando(uberClassLoader: loader)
    jenkinsClass.instance.getExtensionList = { Class type -> assert type == rootAction; actions }
    ['getActions', 'addAction', 'removeAction', 'getItemByFullName', 'scheduleBuild2'].each { method ->
        jenkinsClass.instance[method] = { Object[] values -> assert false: 'Forbidden bootstrap action: ' + method }
    }
    timerClass.instance = new Expando(scheduleWithFixedDelay: { worker, delay, period, unit ->
        assert worker instanceof Runnable && rootAction.isInstance(worker)
        assert delay == 15 && period == 120 && unit == java.util.concurrent.TimeUnit.SECONDS
        schedules << worker
        new Expando(cancel: { boolean interrupt -> cancellations << interrupt; true })
    })
    String source = new File(args[2]).getText('UTF-8')
    def reload = { new GroovyShell(new GroovyClassLoader(loader)).evaluate(source, 'ci-auto-promotion-bootstrap.groovy') }
    // Merely deploying the script must not activate it without administrator configuration.
    reload()
    assert actions == [unrelated, previous] && cancellations.empty && schedules.empty
    def home = new File(rootDirectory, 'ci-auto-promotion')
    home.mkdirs()
    new File(home, 'config.json').text = '{"enabled":false}'
    new File(home, 'CiAutoPromotion.groovy').text = new File(args[0]).getText('UTF-8')
    installProject(new File(rootDirectory,'ci-projects'),bootstrapProject)
    def markers = new File(home, 'template-bindings.json')
    def markerSource = new File(new File(args[0]).parentFile.parentFile, 'template-bindings.json')
    if (args[1] == 'controller_bootstrap_public') {
        assert bootstrapProject.id == 'example-mobile'
        assert !new File(rootDirectory, 'ci-projects/sxmqlive.json').exists()
        assert !markers.exists(): 'Neutral rendering needs no company string marker file'
    }
    markers.text = markerSource.getText('UTF-8')
    Files.setPosixFilePermissions(markers.toPath(),PosixFilePermissions.fromString('r--r--r--'))
    reload()
    assert cancellations == [false] && schedules.size() == 1
    assert actions == [unrelated, schedules[0]] && schedules[0].future != null
    reload()
    assert cancellations == [false, false] && schedules.size() == 2
    assert actions == [unrelated, schedules[1]]
    assert state.definitions.empty && queueClass.events.empty && queueObject.items.empty
    break
case 'controller_project_proof':
    def project = defaultProject()
    project.id = 'example-mobile'
    project.providers.each { key, binding -> binding.accountId='example-'+key; binding.legacyNamespace=false }
    project.repositories.automation = [identity:'example-org/example-ci',ssh:'git@gitee.com:example-org/example-ci.git',
        https:'https://gitee.com/example-org/example-ci.git',defaultBranch:'main',trustRef:'refs/heads/main']
    project.credentials.giteeApi='example-api'
    project.jobs.each { role, item -> item.name='example/'+role }
    project.signingBackup.remove('legacyRecipientSha256')
    def (worker, jobObject, queueObject, state, queueClass, jenkinsClass) = controllerFixture(project)
    def credentialClass = loader.loadClass('com.cloudbees.plugins.credentials.CredentialsProvider')
    credentialClass.credentials = [[id:'wrong-default-credential',apiToken:[plainText:'unused']],
                                   [id:'example-api',apiToken:[plainText:'fixture-token']]]
    def revision = [remoteUrls:[project.repositories.automation.ssh],lastBuiltRevision:[sha1:[name:{ '5'*40 }]]]
    def event = [pull_request:[head:[sha:head]]]
    def env = [jsonBody:JsonOutput.toJson(event),giteePullRequestIid:'2',giteeTargetBranch:'main',
               giteeTargetNamespace:'example-org',giteeTargetRepoName:'example-ci']
    def build = new Expando(number:18,result:'SUCCESS',execution:[script:pipeline.replace('__CI_AUTOMATION_COMMIT__',old)])
    build.isBuilding={ false }; build.getActions={ Class type -> [revision] }; build.getEnvironment={ listener -> env }
    def gate = new Expando(builds:[build])
    def selected = []
    jenkinsClass.instance.getItemByFullName = { String name ->
        selected << name
        if (name==project.jobs.ci_pr.name) return gate
        if (name==project.jobs.android_test.name) return jobObject
        throw new IllegalStateException('Unexpected project Job')
    }
    def mr = [state:'merged',base:[ref:'main'],head:[sha:head]]
    def payloads = [('/commits/'+next):[commit:[tree:[sha:tree]]],
                    ('/commits/'+'5'*40):[commit:[tree:[sha:tree]]],
                    '/pulls/2':mr,('/compare/'+old+'...'+next):[merge_base_commit:[sha:old]],
                    ('/contents/jenkins/templates/pipelines/Jenkinsfile.ci-pr-validation?ref='+old):[type:'file',encoding:'base64',content:pipeline.getBytes('UTF-8').encodeBase64().toString()],
                    '/branches/main':[commit:[sha:next]]]
    def urls=[], connections=[]
    URL.metaClass.openConnection = {
        String url=delegate.toString()
        assert url.startsWith('https://gitee.com/api/v5/repos/example-org/example-ci/')
        String path=url.substring('https://gitee.com/api/v5/repos/example-org/example-ci'.length())
        assert payloads.containsKey(path): 'Unexpected trusted API path: '+path
        urls << url
        def connection=new Expando(responseCode:200,inputStream:new ByteArrayInputStream(JsonOutput.toJson(payloads[path]).getBytes('UTF-8')),
            connectTimeout:0,readTimeout:0,instanceFollowRedirects:true,headers:[:],closed:false)
        connection.setRequestProperty={ key,value -> connection.headers[key]=value }
        connection.disconnect={ connection.closed=true }
        connections << connection
        connection
    }
    try {
        def accepted=worker.proof(next)
        assert accepted.repository==project.repositories.automation.identity && accepted.target=='main'
        assert selected.every { it==project.jobs.ci_pr.name }
        assert connections.every { it.closed && !it.instanceFollowRedirects && it.headers.Authorization=='Bearer fixture-token' }
        assert credentialClass.lookups.every { it.is(gate) }
        [
            { env.giteeTargetRepoName='sxmqlive-ci' },
            { env.giteeTargetNamespace='foreign-org' },
            { env.giteeTargetBranch='master' },
            { revision.remoteUrls=[core.REPOSITORY] },
            { payloads['/commits/'+'5'*40].commit.tree.sha='6'*40 },
            { build.execution.script += "\nnode { echo 'unreviewed' }" },
            { mr.head.sha='7'*40 }
        ].each { mutate ->
            env.giteeTargetRepoName='example-ci'; env.giteeTargetNamespace='example-org'; env.giteeTargetBranch='main'
            revision.remoteUrls=[project.repositories.automation.ssh]
            payloads['/commits/'+'5'*40].commit.tree.sha=tree
            build.execution.script=pipeline.replace('__CI_AUTOMATION_COMMIT__',old); mr.head.sha=head
            mutate(); worker.cache=[:]
            rejected(IllegalStateException) { worker.proof(next) }
            assert state.definitions.empty: 'Rejected proof must not mutate any Job'
        }
        env.giteeTargetRepoName='example-ci';env.giteeTargetNamespace='example-org';env.giteeTargetBranch='main'
        revision.remoteUrls=[project.repositories.automation.ssh]
        payloads['/commits/'+'5'*40].commit.tree.sha=tree
        build.execution.script=pipeline.replace('__CI_AUTOMATION_COMMIT__',old);mr.head.sha=head
        project.repositories.automation.defaultBranch='release/main'
        project.repositories.automation.trustRef='refs/heads/release/main'
        env.giteeTargetBranch='release/main';mr.base.ref='release/main'
        payloads.remove('/branches/main')
        payloads['/branches/release%2Fmain']=[commit:[sha:next]]
        project.credentials.giteeApi='missing-approved-credential'
        worker.cache=[:]
        int priorUrls=urls.size()
        rejected(IllegalStateException) { worker.api('/commits/'+next) }
        assert urls.size()==priorUrls: 'Missing scoped credential must fail before any request'
        project.credentials.giteeApi='example-api'
        int priorLookups=credentialClass.lookups.size()
        jenkinsClass.instance.getItemByFullName={ String ignored -> null }
        rejected(IllegalStateException) { worker.api('/commits/'+next) }
        assert credentialClass.lookups.size()==priorLookups && urls.size()==priorUrls
        jenkinsClass.instance.getItemByFullName={ String target -> target==project.jobs.ci_pr.name ? gate : target==project.jobs.android_test.name ? jobObject : null }
        String name=project.jobs.android_test.name
        jobObject.fullName=name
        state.xml=scmConfig().replace(core.REPOSITORY,project.repositories.automation.ssh)
        String before=state.xml
        worker.class.privateWrite(new File(worker.home,'config.json'),JsonOutput.toJson([enabled:true,jobs:[name]]))
        worker.cache=[:]
        def result=worker.check()
        assert result.jobs.size()==1 && result.jobs[0].status=='updated'
        assert core.pinnedCommit(state.xml,name,project)==next
        assert outsideDefinition(state.xml)==outsideDefinition(before)
        assert urls.any { it.endsWith('/branches/release%2Fmain') } && !urls.any { it.contains('/branches/master') }
        assert state.definitions.size()==1 && queueObject.items.empty
        worker.class.privateWrite(new File(worker.home,'config.json'),JsonOutput.toJson([enabled:true,jobs:[job]]))
        int count=urls.size()
        rejected(IllegalStateException) { worker.check() }
        assert urls.size()==count && state.definitions.size()==1: 'Foreign whitelist must reject before remote access or mutation'
    } finally { GroovySystem.metaClassRegistry.removeMetaClass(URL) }
    break
case 'controller_run_success':
case 'controller_run_failure':
    def (worker, jobObject, queueObject, state, queueClass) = controllerFixture()
    def aclClass = loader.loadClass('hudson.security.ACL')
    def contextClass = loader.loadClass('hudson.security.ACLContext')
    assert AutoCloseable.isAssignableFrom(contextClass)
    assert !java.io.Closeable.isAssignableFrom(contextClass): 'Match Jenkins ACLContext, not Groovy 2.4 Closeable extensions'
    int checks = 0
    String sensitive = 'sensitive-exception-message-must-not-be-persisted'
    def statusFile = new File(worker.home, 'status.json')
    worker.class.privateWrite(statusFile, JsonOutput.toJson([status: 'previous']))
    worker.metaClass.check = {
        checks++
        assert aclClass.lastContext != null && !aclClass.lastContext.closed
        contextClass.events << 'check'
        if (args[1] == 'controller_run_failure') throw new IllegalStateException(sensitive)
        def result = [status: 'validated', jobs: []]
        worker.class.privateWrite(statusFile, JsonOutput.toJson(result))
        result
    }
    worker.run()
    assert checks == 1: 'run must execute check inside the acquired SYSTEM context'
    assert aclClass.lastContext.closed && aclClass.lastContext.closeCalls == 1
    assert contextClass.events == ['open', 'check', 'close']
    String persisted = statusFile.getText('UTF-8')
    def status = new JsonSlurperClassic().parseText(persisted)
    if (args[1] == 'controller_run_failure') {
        assert status == [status: 'blocked', error: 'IllegalStateException']
        assert !persisted.contains(sensitive)
    } else {
        assert status == [status: 'validated', jobs: []]
    }
    assert state.definitions.empty && queueClass.events.empty && queueObject.items.empty
    break
default: assert false: 'Unknown test case'
}
println('CiAutoPromotion behavior passed: ' + args[1])
'''


class CiAutoPromotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.classpath = os.environ.get("GROOVY_TEST_CLASSPATH")
        if not cls.classpath and (ROOT / 'build/controller-groovy/groovy-all-2.4.21.jar').is_file():
            cls.classpath = str(ROOT / 'build/controller-groovy/groovy-all-2.4.21.jar')
        if not cls.classpath or not shutil.which("java"):
            raise unittest.SkipTest("需要固定 Controller Groovy 2.4.21/JDK；设置 GROOVY_TEST_CLASSPATH，不自动下载")
        runtime = subprocess.run(
            ["java", "-cp", cls.classpath, "groovy.ui.GroovyMain", "-e", "print GroovySystem.version"],
            capture_output=True, text=True, timeout=30,
        )
        if runtime.returncode or runtime.stdout != "2.4.21":
            raise RuntimeError("Controller 行为测试必须使用 Groovy 2.4.21，不能由 Gradle Groovy 替代")

    def run_behavior(self, case, profile=None):
        profile = profile or ROOT / "tests/fixtures/legacy-project.json"
        project_id = json.loads(Path(profile).read_text())["id"]
        with TemporaryDirectory(prefix="ci-auto-promotion-test-") as directory:
            directory = str(Path(directory).resolve())
            harness = Path(directory) / "behavior.groovy"
            harness.write_text(GROOVY_CHECKS)
            result = subprocess.run(
                ["java", "-cp", self.classpath, "groovy.ui.GroovyMain", str(harness),
                 str(ROOT / "jenkins/controller/CiAutoPromotion.groovy"), case,
                 str(ROOT / "jenkins/controller/init.groovy.d/04-ci-auto-promotion.groovy"), directory, str(profile)],
                capture_output=True, text=True, timeout=30,
                env=dict(os.environ, JENKINS_CI_PROJECT=project_id),
            )
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("CiAutoPromotion behavior passed: " + case, result.stdout)

    def test_controller_project_proof_credentials_gate_repository_and_job_scope(self):
        self.run_behavior('controller_project_proof')

    def test_controller_loads_mixed_sources_and_rejects_spoofed_repository_or_actions_profile(self):
        self.run_behavior('mixed_project')

    def test_github_automation_blocks_gitee_proof_before_network_credentials_or_job_io(self):
        self.run_behavior('controller_github_blocked')

    def test_gitee_bootstrap_only_looks_up_gitee_role_and_leaves_github_history_gate_untouched(self):
        self.run_behavior('controller_gitee_binding')

    def test_exact_shas_and_successful_merged_trusted_mr_tree_are_required(self):
        self.run_behavior("proof")

    def test_all_managed_jobs_preserve_non_definition_config_and_credentials(self):
        self.run_behavior("config")

    def test_fixed_scm_repository_script_path_and_full_checkout_are_required(self):
        self.run_behavior("scm_trust")

    def test_inline_gate_requires_sandbox_unique_pin_and_trusted_pipeline_placeholder(self):
        self.run_behavior("inline_trust")

    def test_second_project_app_gate_uses_fixed_inline_definition_and_rejects_unrendered_tokens(self):
        self.run_behavior('project_app_inline')

    def test_running_and_queued_jobs_do_not_read_backup_or_write_config(self):
        self.run_behavior("busy")

    def test_already_pinned_commit_is_idempotent_without_backup_or_write(self):
        self.run_behavior("unchanged")

    def test_non_ancestor_commit_is_rejected_without_writing(self):
        self.run_behavior("ancestor")

    def test_config_compare_and_swap_preserves_external_update(self):
        self.run_behavior("cas")

    def test_backup_precedes_write_and_backup_failure_prevents_write(self):
        self.run_behavior("promotion")

    def test_backup_reuses_target_and_content_and_enforces_private_permissions(self):
        self.run_behavior("backup_cache")

    def test_successful_retention_keeps_two_and_preserves_failed_legacy_and_manual_data(self):
        self.run_behavior("backup_retention")

    def test_backup_and_retention_reject_links_permissions_and_hash_tampering(self):
        self.run_behavior("backup_rejections")

    def test_backup_cleanup_requires_successful_unchanged_readback(self):
        self.run_behavior("backup_completion")

    def test_write_failure_rolls_back_only_the_definition_owned_by_this_attempt(self):
        self.run_behavior("rollback")

    def test_rollback_preserves_independent_administrator_property_changes(self):
        self.run_behavior("rollback_external_properties")

    def test_wrong_commit_readback_reports_failure_and_preserves_external_definition(self):
        self.run_behavior("readback_failure")

    def test_unmanaged_job_and_untrusted_proof_are_rejected_before_any_io(self):
        self.run_behavior("reject_before_io")

    def test_controller_busy_detects_running_direct_and_owned_queued_tasks(self):
        self.run_behavior("controller_busy")

    def test_controller_deserializes_and_sets_only_definition_under_queue_lock(self):
        self.run_behavior("controller_definition")

    def test_controller_promotion_preserves_job_and_backs_up_before_setting_definition(self):
        self.run_behavior("controller_promotion")

    def test_controller_bootstrap_uses_root_action_extensions_and_only_replaces_its_timer(self):
        self.run_behavior("controller_bootstrap")

    def test_controller_bootstrap_uses_neutral_renderer_without_company_profile_or_markers(self):
        self.run_behavior("controller_bootstrap_public", ROOT / "examples/second-project/project.json")

    def test_controller_run_checks_under_auto_closeable_acl_context_and_closes_on_success(self):
        self.run_behavior("controller_run_success")

    def test_controller_run_closes_acl_context_and_writes_sanitized_blocked_status_on_failure(self):
        self.run_behavior("controller_run_failure")


if __name__ == "__main__":
    unittest.main()
