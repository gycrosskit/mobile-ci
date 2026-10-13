// 运行生产 helper；只替换 Jenkins 权限和文件写入，不接触真实 Controller 或签名材料。
import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic

// 模拟 Pipeline shell 的编译器污染；旧单参重载会把它带入动态 policy。
def pipelineConfiguration = new org.codehaus.groovy.control.CompilerConfiguration()
pipelineConfiguration.addCompilationCustomizers(new org.codehaus.groovy.control.customizers.CompilationCustomizer(
    org.codehaus.groovy.control.CompilePhase.CONVERSION) {
    void call(org.codehaus.groovy.control.SourceUnit source,
              org.codehaus.groovy.classgen.GeneratorContext context, org.codehaus.groovy.ast.ClassNode node) {
        if (node.name == 'CiAutoPromotion') throw new IllegalStateException('policy inherited Pipeline compiler')
    }
})
def loader = new GroovyClassLoader(getClass().classLoader, pipelineConfiguration)
[
'''package com.cloudbees.groovy.cps; @interface NonCPS {}''',
'''package hudson;
class FilePath {
 static Map files=[:]; static List writes=[]; static int reads=0; static boolean changeOnSecondRead=false
 String path
 FilePath(channel,String path) { this.path=path }
 FilePath child(String relative) { new FilePath(null,path+'/'+relative) }
 String getName() { path.tokenize('/').last() }
 String readLink() { files[path].link }
 boolean isDirectory() { files[path].directory ?: false }
 int mode() { files[path].mode }
 long length() { files[path].data.length }
 def list() {
  String prefix=path+'/'
  files.keySet().findAll { it.startsWith(prefix) && !it.substring(prefix.length()).contains('/') }
   .collect { new FilePath(null,it) }
 }
 def read() {
  reads++
  if (changeOnSecondRead && reads==2) { files[path].data='changed profile'.getBytes('UTF-8') }
  new ByteArrayInputStream(files[path].data)
 }
 String readToString() { new String(files[path].data,'UTF-8') }
 void mkdirs() { files[path]=[directory:true,mode:0700] }
 void write(String text,String encoding) { writes << path; files[path]=[data:text.getBytes(encoding),mode:0600] }
}''',
'''package hudson.model;
class AbstractItem {}
class Cause { static class UserIdCause { String userId } }
class User {
 String id
 static getById(String id,boolean create) { id in ['admin','ordinary'] ? new User(id:id) : null }
 def impersonate2() { [id:id] }
}''',
'''package hudson.triggers; class TimerTrigger { static class TimerTriggerCause {} }''',
'''package hudson.util;
class AtomicFileWriter implements Closeable {
 File file; String encoding; String pending
 AtomicFileWriter(File file,String encoding) { this.file=file; this.encoding=encoding }
 void write(String value) { pending=value }
 void commit() { file.setText(pending,encoding) }
 void close() {}
}''',
'''package jenkins.model;
class Jenkins {
 static ADMINISTER='administer'; static instance=new Jenkins(); File rootDir
 List items=[]; List nodes=[]; def computer=[channel:'stub-channel']
 static get() { instance }
 def getAllItems(Class kind) { items }
 def getComputer(String name) { name=='signing-maintenance' ? computer : null }
 def getACL() { [hasPermission2:{ auth,permission -> permission==ADMINISTER && auth.id=='admin' }] }
}'''
].each { loader.parseClass(it) }
def helper = loader.parseClass(new File(args[0]).getText('UTF-8')).getDeclaredConstructor().newInstance()
def directory = new File(args[1])
def Jenkins = loader.loadClass('jenkins.model.Jenkins')
def UserCause = loader.loadClass('hudson.model.Cause$UserIdCause')
def TimerCause = loader.loadClass('hudson.triggers.TimerTrigger$TimerTriggerCause')
Jenkins.instance.rootDir = directory
def run = { String name, String user = null, boolean timer = false ->
    def userCause = user ? UserCause.newInstance(userId:user) : null
    def timerCause = timer ? TimerCause.newInstance() : null
    [parent:[fullName:name, url:'job/test/'], number:12,
     getCause:{ Class kind -> kind==UserCause ? userCause : kind==TimerCause ? timerCause : null }]
}
def fails = { Closure action ->
    boolean rejected = false
    try { action() } catch (IllegalStateException expected) { rejected=true }
    assert rejected:'expected production helper refusal'
}
def maintenance = 'mobile-apps/sxmqlive-signing-maintenance'
def backup = '运维/sxmqlive-signing-backup'
[maintenance,backup].each { helper.authorize(run(it,'admin')) }
helper.authorize(run(backup,null,true))
def projectFile = new File(directory,'ci-projects/sxmqlive.json')
def projectPath = projectFile.toPath()
def privateMode = java.nio.file.attribute.PosixFilePermissions.fromString('rw-------')
try {
    java.nio.file.Files.setPosixFilePermissions(projectPath,
        java.nio.file.attribute.PosixFilePermissions.fromString('rw-r--r--'))
    fails { helper.authorize(run(backup,'admin')) }
} finally { java.nio.file.Files.setPosixFilePermissions(projectPath,privateMode) }
def savedProject = new File(directory,'private-project.json').toPath()
java.nio.file.Files.move(projectPath,savedProject)
try {
    java.nio.file.Files.createSymbolicLink(projectPath,savedProject)
    fails { helper.authorize(run(backup,'admin')) }
} finally {
    java.nio.file.Files.delete(projectPath)
    java.nio.file.Files.move(savedProject,projectPath)
}
helper.authorize(run(backup,'admin'))
println 'isolated policy compiler and private profile checks passed'
[run(maintenance,'ordinary'), run(backup,'ordinary'), run(backup), run(backup,'missing'),
 run(maintenance,null,true), run('other-job','admin'), run('other-job',null,true)].each { denied ->
    fails { helper.authorize(denied) }
}

def files = ['signing-state/ios/profile':'test profile'.getBytes('UTF-8'),
             'controller/config.xml':'test controller'.getBytes('UTF-8')]
byte[] first = helper.archive(files)
byte[] second = helper.archive(files.entrySet().toList().reverse().collectEntries { [(it.key):it.value] })
assert Arrays.equals(first,second):'insertion order changed archive bytes'
assert helper.digest(first) == helper.digest(second)
new File(directory,'expected.zip').bytes = first
def pem = new File(directory,'public.pem').getText('UTF-8')
def sealed = helper.seal(first,pem)
assert sealed.keySet() == ['format','keyAlgorithm','recipientSha256','wrappedKey','nonce','tag','ciphertext'] as Set
assert sealed.format == 'sxmqlive-signing-backup-v2'
assert sealed.keyAlgorithm == 'RSA-OAEP-SHA256'
assert Base64.decoder.decode(sealed.wrappedKey).length == 384
assert Base64.decoder.decode(sealed.nonce).length == 12
assert Base64.decoder.decode(sealed.tag).length == 16
def again = helper.seal(first,pem)
assert again.nonce != sealed.nonce && again.wrappedKey != sealed.wrappedKey
fails { helper.seal(new byte[0],pem) }
fails { helper.seal(first,'x'*8193) }
new File(directory,'jca.enc.json').setText(JsonOutput.toJson(sealed),'UTF-8')

def allowed = run(backup,'admin')
def statusFile = new File(directory,'signing-backup-status.json')
def readStatus = { new JsonSlurperClassic().parseText(statusFile.getText('UTF-8')) }
helper.status(allowed,[status:'running',secret:'NEVER-PUBLISH-SECRET',unexpected:'ignored'])
def running = readStatus()
assert running.keySet() == ['status','checkedAt','jobUrl','build','lastSuccess','projectId','scope','recipientSha256'] as Set
assert running.status == 'running' && running.lastSuccess == null
assert running.jobUrl == 'job/test/' && running.build == 12
assert !statusFile.text.contains('NEVER-PUBLISH-SECRET')
def identity = [projectId:'sxmqlive',scope:'controller_dr',recipientSha256:sealed.recipientSha256]
def success = identity + [status:'synced',branch:'backup/signing',gitCommit:'a'*40,snapshot:'snapshots/signing_12.enc.json',secret:'NEVER-PUBLISH-SECRET']
helper.status(allowed,success)
def synced = readStatus()
assert synced.lastSuccess.keySet() == ['at','gitCommit','snapshot','url'] as Set
assert synced.lastSuccess.gitCommit == 'a'*40
assert synced.lastSuccess.snapshot == 'snapshots/signing_12.enc.json'
assert synced.lastSuccess.url == new JsonSlurperClassic().parseText(new File(directory,'ci-projects/sxmqlive.json').text).portal.backupUrl
['pending','running'].each { value ->
    helper.status(allowed,[status:value,secret:'NEVER-PUBLISH-SECRET'])
    assert readStatus().lastSuccess == synced.lastSuccess
}
helper.status(allowed,success + [status:'unchanged'])
assert readStatus().status == 'unchanged'
// 实际启用固定旧接收人兼容，否则随机 fixture 公钥会令旧分支始终 false，掩盖 partial 回归。
def trustedStatusProfile = new File(directory,'ci-projects/sxmqlive.json')
String originalStatusProfile = trustedStatusProfile.getText('UTF-8')
try {
    def legacyProfile = new JsonSlurperClassic().parseText(originalStatusProfile)
    String legacyRecipient = '2387e446e9d15b45ebb0d67684550a61598515dc5eceb899d7ec31edcf3219bf'
    legacyProfile.signingBackup.recipientSha256 = legacyRecipient
    legacyProfile.signingBackup.legacyRecipientSha256 = legacyRecipient
    trustedStatusProfile.setText(JsonOutput.toJson(legacyProfile),'UTF-8')
    def legacyStatus = [status:'synced',checkedAt:synced.checkedAt,lastSuccess:synced.lastSuccess]
    statusFile.setText(JsonOutput.toJson(legacyStatus),'UTF-8')
    helper.status(allowed,[status:'pending'])
    assert readStatus().lastSuccess == synced.lastSuccess: 'All absent namespace keys must retain supported legacy success'
    [[scope:'controller_dr'], [recipientSha256:legacyRecipient], [scope:null], [recipientSha256:null],
     [projectId:null], [projectId:null,scope:null,recipientSha256:null],
     [projectId:'sxmqlive',scope:'controller_dr']].each { partial ->
        statusFile.setText(JsonOutput.toJson(legacyStatus + partial),'UTF-8')
        helper.status(allowed,[status:'pending'])
        assert readStatus().lastSuccess == null: 'Partial/null namespace must not become a current successful backup'
    }
} finally { trustedStatusProfile.setText(originalStatusProfile,'UTF-8') }
helper.status(allowed,success + [status:'unchanged'])
def before = statusFile.text
[[status:'failed'], success + [branch:'master'], success + [gitCommit:'not-a-commit'],
 success + [snapshot:'snapshots/../secret.enc.json']].each { invalid ->
    fails { helper.status(allowed,invalid) }; assert statusFile.text == before
}
fails { helper.status(run(backup,'ordinary'),success) }
assert statusFile.text == before && !statusFile.text.contains('NEVER-PUBLISH-SECRET')
statusFile.setText('{broken json','UTF-8')
helper.status(allowed,[status:'pending'])
assert readStatus().status == 'pending' && readStatus().lastSuccess == null
statusFile.delete()
def statusTarget = new File(directory,'status-target.json'); statusTarget.setText('unchanged target','UTF-8')
java.nio.file.Files.createSymbolicLink(statusFile.toPath(),statusTarget.toPath())
fails { helper.status(allowed,[status:'running']) }
assert statusTarget.text == 'unchanged target'
java.nio.file.Files.delete(statusFile.toPath())

def controller = new File(directory,'controller'); controller.mkdirs()
def controllerFiles = [
    'config.xml':'test controller', 'credentials.xml':'test credentials',
    'secrets/master.key':'test master key', 'secrets/hudson.util.Secret':'test hudson secret',
    'jobs/mobile-apps/config.xml':'test folder', 'nodes/signing-maintenance/config.xml':'test node'
]
controllerFiles.each { name,value ->
    def file = new File(controller,name); file.parentFile.mkdirs(); file.setText(value,'UTF-8')
}
Jenkins.instance.rootDir = controller
Jenkins.instance.items = [[rootDir:new File(controller,'jobs/mobile-apps')]]
Jenkins.instance.nodes = [[nodeName:'signing-maintenance']]
def FilePath = loader.loadClass('hudson.FilePath')
def remoteRoot = '/home/jenkins/signing'
def profile = remoteRoot + '/ios/profile'
def ciphertextPath = '/workspace/source/build/signing-backup/snapshot.enc.json'
def resetRemote = {
    FilePath.files = [
        (remoteRoot):[directory:true,mode:0700],
        (remoteRoot+'/ios'):[directory:true,mode:0700],
        (profile):[data:'test profile'.getBytes('UTF-8'),mode:0600],
        '/workspace/source/signing/backups/recipient-public.pem':[data:pem.getBytes('UTF-8'),mode:0644]
    ]
    FilePath.writes=[]; FilePath.reads=0; FilePath.changeOnSecondRead=false
}
resetRemote()
def collected = helper.collectMaterials(Jenkins.instance.computer,helper.projectFor(allowed))
def expectedMaterials = controllerFiles.collectEntries { name,value -> [('controller/'+name):value.getBytes('UTF-8')] }
expectedMaterials['signing-state/ios/profile'] = 'test profile'.getBytes('UTF-8')
expectedMaterials['backup-scope.json'] = JsonOutput.toJson(identity).getBytes('UTF-8')
assert collected.keySet() == expectedMaterials.keySet()
collected.each { name,value -> assert Arrays.equals(value,expectedMaterials[name]) }
resetRemote()
def exported = helper.exportSnapshot(allowed,'signing-maintenance','/workspace/source')
byte[] expectedArchive = helper.archive(expectedMaterials)
assert exported == identity + [contentSha256:helper.digest(expectedArchive),entries:expectedMaterials.size()]
assert FilePath.writes == [ciphertextPath]
new File(directory,'exported.enc.json').bytes = FilePath.files[ciphertextPath].data
new File(directory,'exported-expected.zip').bytes = expectedArchive

// 只清空两个已确认的 Active Choices 缓存；归档和恢复仍保留其他配置。
def businessJob = new File(controller,'jobs/mobile-apps/jobs/business'); businessJob.mkdirs()
def businessConfig = new File(businessJob,'config.xml')
def originalItems = Jenkins.instance.items
Jenkins.instance.items = originalItems + [[rootDir:businessJob]]
String jobXml = '''<flow-definition>
<properties><hudson.security.AuthorizationMatrixProperty><permission>hudson.model.Item.Build:release-team</permission></hudson.security.AuthorizationMatrixProperty>
<hudson.model.ParametersDefinitionProperty><parameterDefinitions>
<hudson.model.ChoiceParameterDefinition><name>BRAND</name><choices><string>kangmin</string><string>cihang</string></choices></hudson.model.ChoiceParameterDefinition>
<org.biouno.unochoice.CascadeChoiceParameter><name>GIT_BRANCH</name><script><secureScript><script>return ["feature/kangmin/a"];&#13;\n// 保留脚本 &lt; &gt; &amp; 引号</script><sandbox>false</sandbox></secureScript></script><parameters class="linked-hash-map"><entry><string>BRAND</string><string>kangmin</string></entry></parameters></org.biouno.unochoice.CascadeChoiceParameter>
<org.biouno.unochoice.DynamicReferenceParameter><name>optional</name><parameters class="linked-hash-map"><entry><string>BRANCH</string><string>dev/kangmin</string></entry></parameters></org.biouno.unochoice.DynamicReferenceParameter>
<example.RealConfiguration><parameters class="linked-hash-map"><entry><string>keep-me</string></entry></parameters></example.RealConfiguration>
</parameterDefinitions></hudson.model.ParametersDefinitionProperty></properties>
<definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition"><scm><credentialsId>fixture-credential</credentialsId><branches><name>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</name></branches></scm><scriptPath>jenkins/Jenkinsfile.test</scriptPath></definition>
<disabled>false</disabled></flow-definition>'''
def parseJob = { String text ->
    def factory = javax.xml.parsers.DocumentBuilderFactory.newInstance()
    factory.setFeature('http://apache.org/xml/features/disallow-doctype-decl',true)
    factory.newDocumentBuilder().parse(new ByteArrayInputStream(text.getBytes('UTF-8')))
}
def xpath = javax.xml.xpath.XPathFactory.newInstance().newXPath()
def snapshotCases = []
def snapshot = { String label, String xml = jobXml, boolean signatureChanged = false ->
    resetRemote()
    businessConfig.setText(xml,'UTF-8')
    if (signatureChanged) FilePath.files[profile].data = 'updated profile'.getBytes('UTF-8')
    def result = helper.exportSnapshot(allowed,'signing-maintenance','/workspace/source')
    new File(directory,label+'.enc.json').bytes = FilePath.files[ciphertextPath].data
    snapshotCases << [label:label,contentSha256:result.contentSha256]
    result.contentSha256
}
String baselineDigest = snapshot('cache-baseline')
byte[] stableConfig = helper.backupJobConfig(jobXml.getBytes('UTF-8'))
def beforeJob = parseJob(jobXml), afterJob = parseJob(new String(stableConfig,'UTF-8'))
['//definition','//hudson.security.AuthorizationMatrixProperty','//disabled',
 '//hudson.model.ChoiceParameterDefinition','//secureScript/script',
 '//example.RealConfiguration/parameters'].each { expression ->
    assert xpath.evaluate(expression,beforeJob) == xpath.evaluate(expression,afterJob): 'Lost real configuration: '+expression
}
assert xpath.evaluate('count(//org.biouno.unochoice.CascadeChoiceParameter/parameters/*)',afterJob) == '0'
assert xpath.evaluate('count(//org.biouno.unochoice.DynamicReferenceParameter/parameters/*)',afterJob) == '0'
def expectedCaches = xpath.evaluate('//org.biouno.unochoice.CascadeChoiceParameter/parameters[@class="linked-hash-map"] | //org.biouno.unochoice.DynamicReferenceParameter/parameters[@class="linked-hash-map"]',
    beforeJob,javax.xml.xpath.XPathConstants.NODESET)
for (int index = 0; index < expectedCaches.length; index++) {
    def node = expectedCaches.item(index)
    while (node.hasChildNodes()) node.removeChild(node.firstChild)
}
assert beforeJob.documentElement.isEqualNode(afterJob.documentElement): 'Only known cache contents may change'
assert snapshot('source-only') == baselineDigest
String cacheOnly = jobXml.replace('<entry><string>BRAND</string><string>kangmin</string></entry>',
    '<entry><string>BRAND</string><string>cihang</string></entry>')
    .replace('<entry><string>BRANCH</string><string>dev/kangmin</string></entry>',
    '<entry><string>BRANCH</string><string>release/cihang-1.0.0</string></entry>')
assert snapshot('cache-only',cacheOnly) == baselineDigest
assert snapshot('cache-empty',new String(stableConfig,'UTF-8')) == baselineDigest
[
 choices: jobXml.replace('<choices><string>kangmin</string>', '<choices><string>newbrand</string>'),
 script: jobXml.replace('return ["feature/kangmin/a"]', 'return ["feature/kangmin/new"]'),
 sandbox: jobXml.replace('<sandbox>false</sandbox>','<sandbox>true</sandbox>'),
 pin: jobXml.replace('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'),
 acl: jobXml.replace('hudson.model.Item.Build:release-team','hudson.model.Item.Build:other-team'),
 disabled: jobXml.replace('<disabled>false</disabled>','<disabled>true</disabled>'),
 credential: jobXml.replace('fixture-credential','changed-credential'),
 otherMap: jobXml.replace('keep-me','keep-new-value')
].each { label,xml -> assert snapshot(label,xml) != baselineDigest: 'Ignored real change: '+label }
assert snapshot('signature',jobXml,true) != baselineDigest
[credentials:'credentials.xml',node:'nodes/signing-maintenance/config.xml'].each { label,path ->
    def file = new File(controller,path)
    byte[] saved = file.bytes
    try {
        file.setText('changed '+label,'UTF-8')
        assert snapshot(label) != baselineDigest
    } finally { file.bytes = saved }
}
new File(directory,'cache-cases.json').setText(JsonOutput.toJson(snapshotCases),'UTF-8')
businessConfig.setText(jobXml,'UTF-8')
assert Arrays.equals(helper.backupJobConfig('plain non-job bytes'.getBytes('UTF-8')), 'plain non-job bytes'.getBytes('UTF-8'))
try {
    helper.backupJobConfig(('''<!DOCTYPE flow-definition [<!ENTITY unsafe SYSTEM "file:///unused-fixture">]>'''+jobXml).getBytes('UTF-8'))
    assert false:'DOCTYPE must not enter snapshot parsing'
} catch (org.xml.sax.SAXException expected) { /* 禁止外部实体，不读取目标文件。 */ }
assert businessConfig.getText('UTF-8') == jobXml: 'Backup must not rewrite live Job XML'
Jenkins.instance.items = originalItems
println('Active Choices cache isolation behavior checks passed')

def configFile = new File(controller,'config.xml')
configFile.delete()
java.nio.file.Files.createSymbolicLink(configFile.toPath(),statusTarget.toPath())
resetRemote()
fails { helper.exportSnapshot(allowed,'signing-maintenance','/workspace/source') }
assert FilePath.writes.isEmpty() && !FilePath.files.containsKey(ciphertextPath)
java.nio.file.Files.delete(configFile.toPath()); configFile.setText(controllerFiles['config.xml'],'UTF-8')
[[path:profile,field:'link',value:'/outside'], [path:remoteRoot,field:'mode',value:0755],
 [path:profile,field:'mode',value:0644]].each { invalid ->
    resetRemote(); FilePath.files[invalid.path][invalid.field]=invalid.value
    fails { helper.exportSnapshot(allowed,'signing-maintenance','/workspace/source') }
    assert FilePath.writes.isEmpty() && !FilePath.files.containsKey(ciphertextPath)
}
resetRemote(); FilePath.changeOnSecondRead=true
fails { helper.exportSnapshot(allowed,'signing-maintenance','/workspace/source') }
assert FilePath.reads == 2
assert FilePath.writes.isEmpty() && !FilePath.files.containsKey(ciphertextPath)

// 同 Controller 第二项目只能导出其声明签名根，且必须使用管理员已审接收人。
def secondConfig = new JsonSlurperClassic().parseText(new File(controller,'ci-projects/sxmqlive.json').text)
secondConfig.id = 'example-mobile'
secondConfig.providers.each { key, binding -> binding.accountId='example-'+key; binding.legacyNamespace=false }
secondConfig.jobs.each { role, job -> job.name = 'example-app/' + role }
secondConfig.signingBackup.scope = 'project_signing'
secondConfig.signingBackup.prefixes = ['ios/test-team/materials/demo']
secondConfig.signingBackup.remove('legacyRecipientSha256')
def keygen = java.security.KeyPairGenerator.getInstance('RSA'); keygen.initialize(3072)
def secondKeys = keygen.generateKeyPair()
String secondPem = '-----BEGIN PUBLIC KEY-----\n' + Base64.encoder.encodeToString(secondKeys.public.encoded) + '\n-----END PUBLIC KEY-----\n'
secondConfig.signingBackup.recipientSha256 = helper.digest(secondKeys.public.encoded)
def secondFile = new File(controller,'ci-projects/example-mobile.json')
def saveSecond = {
    secondFile.setText(JsonOutput.toJson(secondConfig),'UTF-8')
    java.nio.file.Files.setPosixFilePermissions(secondFile.toPath(), java.nio.file.attribute.PosixFilePermissions.fromString('rw-------'))
}
saveSecond()
def secondRun = run('example-app/signing_backup','admin')
helper.authorize(secondRun)
fails { helper.authorize(run('example-app/signing_maintenance',null,true)) }
helper.authorize(run('example-app/signing_backup',null,true))
resetRemote()
['ios/test-team','ios/test-team/materials','ios/test-team/materials/demo','ios/other-team'].each { path ->
    FilePath.files[remoteRoot+'/'+path] = [directory:true,mode:0700]
}
def approvedPath = remoteRoot + '/ios/test-team/materials/demo/profile'
FilePath.files[approvedPath] = [data:'only own signing profile'.getBytes('UTF-8'),mode:0600]
FilePath.files[remoteRoot+'/ios/other-team/master.key'] = [data:'OTHER PROJECT PRIVATE SECRET'.getBytes('UTF-8'),mode:0600]
fails { helper.exportSnapshot(secondRun,'signing-maintenance','/workspace/source') }
assert FilePath.reads == 0 && FilePath.writes.isEmpty(): 'Wrong recipient must fail before reading private materials'
FilePath.files['/workspace/source/signing/backups/recipient-public.pem'].data = secondPem.getBytes('UTF-8')
def secondExport = helper.exportSnapshot(secondRun,'signing-maintenance','/workspace/source')
assert secondExport.projectId == 'example-mobile' && secondExport.scope == 'project_signing'
def secondEnvelope = new JsonSlurperClassic().parseText(new String(FilePath.files[ciphertextPath].data,'UTF-8'))
def rsa = javax.crypto.Cipher.getInstance('RSA/ECB/OAEPPadding')
rsa.init(javax.crypto.Cipher.DECRYPT_MODE, secondKeys.private,
    new javax.crypto.spec.OAEPParameterSpec('SHA-256','MGF1',java.security.spec.MGF1ParameterSpec.SHA256,javax.crypto.spec.PSource.PSpecified.DEFAULT))
byte[] dataKey = rsa.doFinal(Base64.decoder.decode(secondEnvelope.wrappedKey))
def aes = javax.crypto.Cipher.getInstance('AES/GCM/NoPadding')
aes.init(javax.crypto.Cipher.DECRYPT_MODE,new javax.crypto.spec.SecretKeySpec(dataKey,'AES'),
    new javax.crypto.spec.GCMParameterSpec(128,Base64.decoder.decode(secondEnvelope.nonce)))
aes.updateAAD((secondEnvelope.format+':'+secondEnvelope.recipientSha256).getBytes('UTF-8'))
byte[] ciphertext = Base64.decoder.decode(secondEnvelope.ciphertext), tag = Base64.decoder.decode(secondEnvelope.tag)
def joined = new ByteArrayOutputStream(); joined.write(ciphertext); joined.write(tag)
byte[] plain = aes.doFinal(joined.toByteArray())
def restoredNames = []
new java.util.zip.ZipInputStream(new ByteArrayInputStream(plain)).withCloseable { input ->
    for (def entry = input.nextEntry; entry != null; entry = input.nextEntry) restoredNames << entry.name
}
assert restoredNames == ['backup-scope.json','manifest.json','signing-state/ios/test-team/materials/demo/profile']
assert !new String(plain,'ISO-8859-1').contains('OTHER PROJECT PRIVATE SECRET')
def secondIdentity = [projectId:'example-mobile',scope:'project_signing',recipientSha256:secondEnvelope.recipientSha256]
def secondSuccess = secondIdentity + [status:'synced',branch:'backup/signing',gitCommit:'e'*40,snapshot:'snapshots/example.enc.json']
helper.status(secondRun,secondSuccess)
def secondStatusFile = new File(controller,'signing-backup-status-example-mobile.json')
assert new JsonSlurperClassic().parseText(secondStatusFile.text).lastSuccess.gitCommit == 'e'*40
String savedStatus = secondStatusFile.text
fails { helper.status(secondRun,success) }
assert secondStatusFile.text == savedStatus
secondConfig.signingBackup.recipientSha256 = 'f'*64; saveSecond()
helper.status(secondRun,[status:'pending'])
assert new JsonSlurperClassic().parseText(secondStatusFile.text).lastSuccess == null: 'Recipient rotation must hide old success'
secondConfig.jobs.signing_backup.name = backup; saveSecond()
fails { helper.authorize(allowed) }
secondFile.delete()
resetRemote()
println('Project scope, recipient, multi-Folder and status isolation behavior checks passed')

// JSON parser 不可序列化，不能跨 readFile 的 Pipeline 挂起点存活。
try {
    new ObjectOutputStream(new ByteArrayOutputStream()).writeObject(new JsonSlurperClassic())
    assert false:'JSON parser must not survive a Pipeline suspension'
} catch (NotSerializableException expected) { assert expected.message=='groovy.json.JsonSlurperClassic' }
new ObjectOutputStream(new ByteArrayOutputStream()).writeObject(new JsonSlurperClassic().parseText('{"status":"synced"}'))

// checkout 不提供 GIT_COMMIT 时，生产 publish 仍必须以工作区实际 HEAD 为快照来源。
def sourceCommit = 'b' * 40
def publishReport = identity + [status:'synced',branch:'backup/signing',gitCommit:'c'*40,snapshot:'snapshots/signing_12.enc.json']
['success','success-ci-head','invalid-revision','revision-failed','publish-failed'].each { scenario ->
    def calls = [exports:0, credentials:0, shells:[], environments:[], artifacts:[], errors:[],directories:[]]
    helper.metaClass.exportSnapshot = { givenRun, String nodeName, String workspace ->
        assert givenRun.is(allowed) && nodeName=='signing-maintenance' && workspace=='/workspace/source'
        calls.exports++
        [contentSha256:'d'*64,entries:7]
    }
    def environment = [NODE_NAME:'signing-maintenance',WORKSPACE:'/workspace',APP_SOURCE_ROOT:'/workspace/source',CI_TOOL_ROOT:'/workspace/automation',BUILD_NUMBER:'12']
    assert !environment.containsKey('GIT_COMMIT')
    if (scenario=='success-ci-head') { environment.GIT_COMMIT='a'*40 }
    helper.binding = new Binding([
        currentBuild:[rawBuild:allowed], env:environment,
        dir:{ String path, Closure action ->
            assert path=='/workspace/source'
            calls.directories << path; action()
        },
        sh:{ value ->
            calls.shells << value
            if (value instanceof Map) {
                assert value.returnStdout && value.script.contains('git rev-parse HEAD')
                assert calls.directories==['/workspace/source']
                if (scenario=='revision-failed') { throw new IOException('synthetic revision shell failure') }
                return scenario=='invalid-revision' ? 'invalid head\n' : sourceCommit+'\n'
            }
            assert value instanceof String && value.contains('python3 "$CI_TOOL_ROOT/signing/publish_signing_backup.py"')
            assert value.contains('--source-commit "$BACKUP_SOURCE_COMMIT"')
            assert !value.contains('$GIT_COMMIT')
            assert !value.contains('signing_maintenance.py') && !value.contains('ios_release.py')
            if (scenario=='publish-failed') { throw new IOException('synthetic publish shell failure') }
            null
        },
        sshUserPrivateKey:{ Map value ->
            assert value==[credentialsId:new JsonSlurperClassic().parseText(new File(directory,'ci-projects/sxmqlive.json').text).credentials.scm,keyFileVariable:'BACKUP_SSH_KEY']; value
        },
        withCredentials:{ List values, Closure action ->
            assert values==[[credentialsId:new JsonSlurperClassic().parseText(new File(directory,'ci-projects/sxmqlive.json').text).credentials.scm,keyFileVariable:'BACKUP_SSH_KEY']]
            calls.credentials++; action()
        },
        withEnv:{ List values, Closure action ->
            assert (values as Set) == ([('BACKUP_CONTENT_SHA256='+'d'*64),('BACKUP_SOURCE_COMMIT='+sourceCommit),'BACKUP_PROJECT_ID=sxmqlive'] as Set)
            calls.environments << values; action()
        },
        readFile:{ String name ->
            assert name=='build/signing-backup/report.json'; JsonOutput.toJson(publishReport)
        },
        archiveArtifacts:{ Map value -> calls.artifacts << value },
        echo:{ String value -> assert value.contains(publishReport.gitCommit) || value.startsWith('签名备份步骤失败：') },
        error:{ String value -> calls.errors << value; throw new IllegalStateException('synthetic Pipeline error') }
    ])
    if (scenario.startsWith('success')) {
        helper.publish()
        assert calls.errors.isEmpty()
        assert calls.exports==1 && calls.credentials==1 && calls.environments.size()==1
        assert calls.artifacts==[[artifacts:'build/signing-backup/report.json',fingerprint:true]]
        def state = new JsonSlurperClassic().parseText(new File(controller,'signing-backup-status.json').text)
        assert state.status=='synced' && state.lastSuccess.gitCommit==publishReport.gitCommit
    } else {
        fails { helper.publish() }
        def state = new JsonSlurperClassic().parseText(new File(controller,'signing-backup-status.json').text)
        assert state.status=='pending'
        assert calls.artifacts.isEmpty() && !calls.errors.isEmpty()
        assert calls.exports==(scenario=='publish-failed' ? 1 : 0)
        assert calls.credentials==(scenario=='publish-failed' ? 1 : 0)
        assert calls.shells.size()==(scenario=='publish-failed' ? 2 : 1)
    }
}
println('publish CPS behavior checks passed')
println('signing backup runtime checks passed')
