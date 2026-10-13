// 在 Jenkins 自带 Groovy/Core 类路径中执行，不注册页面或调度任务。
def inventory = [brands:[
    [brand:'cihang',platform:'harmony',configurationStatus:'configured',channelEnabled:[designated:true]],
    [brand:'cihang',platform:'ios',configurationStatus:'configured',channelEnabled:[pgyer:true]],
    [brand:'pending',platform:'harmony',configurationStatus:'pending',channelEnabled:[designated:false]],
    [brand:'storeonly',platform:'ios',configurationStatus:'configured',channelEnabled:[appstore:true]]
], devices:[ios:[[id:'a' * 20]], harmony:[[id:'b' * 20]]]]
def base = [operation:'devices',platform:'harmony',mode:'plan',source:'shared',brands:['cihang'],
            deviceIds:['b' * 20], requestId:'12345678-1234-4321-1234-123456789012']
assert SxmqliveSigningPortal.validate(base,inventory).deviceIds == 'b' * 20
assert SxmqliveSigningPortal.validate(base + [source:'manual',deviceIds:[],name:'测试手机',udid:'A' * 32],inventory).name == '测试手机'
assert SxmqliveSigningPortal.validate(base + [platform:'ios',deviceIds:['a' * 20]],inventory).platform == 'ios'
assert SxmqliveSigningPortal.validate([operation:'inventory',mode:'plan',requestId:base.requestId],inventory).platform == 'all'
def invalid = [
    base + [job:'arbitrary'], base + [platform:'android'], base + [brands:['pending']], base + [brands:['unknown']],
    base + [brands:['cihang','cihang']], base + [platform:'ios',brands:['storeonly'],deviceIds:['a' * 20]],
    base + [deviceIds:['a' * 20]], base + [deviceIds:['../secret']], base + [deviceIds:'b' * 20],
    base + [source:'manual',deviceIds:[],name:'手机',udid:'invalid'],
    base + [source:'manual',deviceIds:[],name:'手机\n名称',udid:'A' * 32],
    base + [mode:'unknown'], base + [requestId:'../secret'],
    [operation:'inventory',mode:'apply',requestId:base.requestId]
]
invalid.each { value ->
    boolean rejected = false
    try { SxmqliveSigningPortal.validate(value,inventory) } catch (IllegalArgumentException ignored) { rejected = true }
    assert rejected
}
def log = '''维护品牌 first / Team test / apply
do not expose private output
维护品牌 second / Team test / apply'''
assert SxmqliveSigningPortal.progress(['first','second','third'],'ios',log,true,'FAILURE')*.status == ['success','unknown','not-run']
assert SxmqliveSigningPortal.progress(['first','second'],'ios',log,true,'SUCCESS')*.status == ['success','success']
assert SxmqliveSigningPortal.progress(['first'],'ios','unrelated secret text',false,null)*.status == ['not-run']
assert SxmqliveSigningPortal.progress(['first'],'harmony','维护品牌 first / Team test / apply\n品牌 first 的 Jenkins 签名材料已核对；更新 0 项',true,'FAILURE')*.status == ['success']
def virtualFile = { String value -> [isFile:{true}, open:{new ByteArrayInputStream(value.getBytes('UTF-8'))}] }
def archivedRun = { int number, file ->
    [number:number, url:"job/maintenance/${number}/", isBuilding:{false},
     artifactManager:[root:{[child:{String path -> file}]}]]
}
def project = new groovy.json.JsonSlurperClassic().parse(new File(System.getProperty('ci.project.fixture')))
def portal = new SxmqliveSigningPortal(project)
hudson.model.HdcLauncher.expectedHdc = portal.hdcPath
def valid = groovy.json.JsonOutput.toJson([generatedAt:'2026-10-10T00:00:00Z',brands:[],devices:[ios:[],harmony:[]]])
def archived = [archivedRun(2,virtualFile('{invalid json')),archivedRun(1,virtualFile(valid))]
def snapshot = portal.snapshot([builds:[limit:{int size -> archived}]])
assert snapshot.snapshot.build == 1
assert snapshot.skippedSnapshots == 1
assert portal.snapshot([builds:[limit:{int size -> [archived[0]]}]]).inventory.brands == []
assert portal.snapshot([builds:[limit:{int size -> []}]]).snapshot == null
assert SxmqliveSigningPortal.validate(base + [recoverOnly:true],inventory).brands == 'cihang'

def backupDirectory = File.createTempDir('signing-portal-backup-', '')
def backupFile = new File(backupDirectory, 'signing-backup-status.json')
def writeStatus = { File target, String text ->
    target.text = text
    java.nio.file.Files.setPosixFilePermissions(target.toPath(),java.nio.file.attribute.PosixFilePermissions.fromString('rw-------'))
}
def previousJenkins = Jenkins.instance
try {
    Jenkins.instance = [rootDir:backupDirectory]
    assert portal.backupStatus() == [status:'none', checkedAt:null, lastSuccess:null]
    def success = [at:'2026-10-10T00:00:00Z', gitCommit:'a' * 40,
                   snapshot:'snapshots/20261010T000000123456Z-abcdefabcdef.enc.json', secret:'must not escape', url:'https://evil.example']
    def status = [status:'synced', checkedAt:'2026-10-10T01:00:00Z', lastSuccess:success, privateKey:'must not escape']
    writeStatus(backupFile, groovy.json.JsonOutput.toJson(status))
    def legacy = new groovy.json.JsonSlurperClassic().parseText(groovy.json.JsonOutput.toJson(project))
    legacy.signingBackup.recipientSha256 = '2387e446e9d15b45ebb0d67684550a61598515dc5eceb899d7ec31edcf3219bf'
    legacy.signingBackup.legacyRecipientSha256 = legacy.signingBackup.recipientSha256
    def legacyPortal = new SxmqliveSigningPortal(legacy)
    def publicBackup = legacyPortal.backupStatus()
    assert publicBackup.keySet() == ['status','checkedAt','lastSuccess'] as Set
    assert publicBackup.lastSuccess.keySet() == ['at','gitCommit','snapshot','url'] as Set
    assert publicBackup.lastSuccess.url == portal.backupUrl
    assert !groovy.json.JsonOutput.toJson(publicBackup).contains('must not escape')
    def identity = [projectId:project.id, scope:project.signingBackup.scope, recipientSha256:project.signingBackup.recipientSha256]
    writeStatus(backupFile, groovy.json.JsonOutput.toJson(status + identity))
    assert portal.backupStatus().status == 'synced'
    [[projectId:'other'], [scope:'project_signing'], [recipientSha256:'b' * 64]].each { wrong ->
        writeStatus(backupFile, groovy.json.JsonOutput.toJson(status + identity + wrong))
        assert portal.backupStatus().status == 'pending'
    }
    def rotatedProject = new groovy.json.JsonSlurperClassic().parseText(groovy.json.JsonOutput.toJson(project))
    rotatedProject.signingBackup.recipientSha256 = 'c' * 64
    writeStatus(backupFile, groovy.json.JsonOutput.toJson(status))
    assert new SxmqliveSigningPortal(rotatedProject).backupStatus().status == 'pending'
    ['synced','unchanged','pending','running'].each { value ->
        writeStatus(backupFile, groovy.json.JsonOutput.toJson(status + [status:value]))
        assert legacyPortal.backupStatus().status == value
    }
    [status + [lastSuccess:null], status + [status:'secret status'], status + [checkedAt:'invalid'],
     status + [lastSuccess:success + [gitCommit:'private key']],
     status + [lastSuccess:success + [snapshot:'../credentials.xml']]].each { value ->
        writeStatus(backupFile, groovy.json.JsonOutput.toJson(value))
        assert portal.backupStatus() == [status:'pending', checkedAt:null, lastSuccess:null]
    }
    writeStatus(backupFile, groovy.json.JsonOutput.toJson(status + identity))
    [0644, 0400, 0660].each { mode ->
        java.nio.file.Files.setAttribute(backupFile.toPath(), 'unix:mode', mode)
        assert portal.backupStatus() == [status:'pending', checkedAt:null, lastSuccess:null]
    }
    writeStatus(backupFile, groovy.json.JsonOutput.toJson(status + identity))
    def hardlink = new File(backupDirectory, 'linked-status.json')
    java.nio.file.Files.createLink(hardlink.toPath(), backupFile.toPath())
    assert portal.backupStatus() == [status:'pending', checkedAt:null, lastSuccess:null]
    assert hardlink.delete()
    assert portal.backupStatus().status == 'synced'
    // 无法在普通测试账号下 chown；仅替换 Jenkins Home owner 查询，文件检查仍是真实 FS。
    def wrongOwner = java.nio.file.Files.getAttribute(backupDirectory.toPath(), 'unix:uid') + 1
    java.nio.file.Files.metaClass.static.getAttribute = { java.nio.file.Path path, String attribute, java.nio.file.LinkOption[] options ->
        assert path == backupDirectory.toPath() && attribute == 'unix:uid'
        wrongOwner
    }
    java.nio.file.Files.metaClass.static.newInputStream = { java.nio.file.Path path, java.nio.file.OpenOption[] options ->
        throw new AssertionError('Owner mismatch must fail before reading any status bytes')
    }
    try {
        assert portal.backupStatus() == [status:'pending', checkedAt:null, lastSuccess:null]
    } finally { GroovySystem.metaClassRegistry.removeMetaClass(java.nio.file.Files) }
    assert portal.backupStatus().status == 'synced'
    // 实际打开私有旧文件，关闭后原子替换为另一个合法版本；本次读取必须拒绝跨 inode。
    def replacement = new File(backupDirectory, 'replacement-status.json')
    String replacementText = groovy.json.JsonOutput.toJson(status + identity + [checkedAt:'2026-10-10T02:00:00Z'])
    writeStatus(replacement, replacementText)
    def actualOpen = java.nio.file.Files.getMethod('newInputStream',java.nio.file.Path,java.nio.file.OpenOption[].class)
    int opened = 0
    boolean swapped = false
    def swap = {
        java.nio.file.Files.move(replacement.toPath(),backupFile.toPath(),java.nio.file.StandardCopyOption.REPLACE_EXISTING)
        swapped = true
    }
    java.nio.file.Files.metaClass.static.newInputStream = { java.nio.file.Path path, java.nio.file.OpenOption[] options ->
        assert path == backupFile.toPath() && options.toList() == [java.nio.file.LinkOption.NOFOLLOW_LINKS]
        def stream = actualOpen.invoke(null,[path,options] as Object[])
        opened++
        new FilterInputStream(stream) {
            void close() { super.close(); swap.call() }
        }
    }
    try {
        assert portal.backupStatus() == [status:'pending', checkedAt:null, lastSuccess:null]
        assert opened == 1 && swapped && backupFile.text == replacementText
    } finally { GroovySystem.metaClassRegistry.removeMetaClass(java.nio.file.Files) }
    assert portal.backupStatus().status == 'synced'
    // 部分 namespace 或旧状态缺少固定 legacy 接收人均不能认领历史。
    [[projectId:project.id], [scope:project.signingBackup.scope], [recipientSha256:project.signingBackup.recipientSha256],
     [projectId:null], [projectId:null, scope:null, recipientSha256:null]].each { partial ->
        writeStatus(backupFile, groovy.json.JsonOutput.toJson(status + partial))
        assert portal.backupStatus().status == 'pending'
    }
    def noLegacy = new groovy.json.JsonSlurperClassic().parseText(groovy.json.JsonOutput.toJson(project))
    noLegacy.signingBackup.remove('legacyRecipientSha256')
    writeStatus(backupFile, groovy.json.JsonOutput.toJson(status))
    assert new SxmqliveSigningPortal(noLegacy).backupStatus().status == 'pending'
    writeStatus(backupFile, groovy.json.JsonOutput.toJson(status + identity))
    assert new SxmqliveSigningPortal(noLegacy).backupStatus().status == 'synced'
    writeStatus(backupFile, '{broken json')
    assert portal.backupStatus().status == 'pending'
    writeStatus(backupFile, 'x' * 16385)
    assert portal.backupStatus().status == 'pending'
    assert backupFile.delete()
    def linked = new File(backupDirectory, 'linked-private-status.json')
    writeStatus(linked, groovy.json.JsonOutput.toJson(status))
    java.nio.file.Files.createSymbolicLink(backupFile.toPath(), linked.toPath())
    assert portal.backupStatus() == [status:'pending', checkedAt:null, lastSuccess:null]
    assert linked.text == groovy.json.JsonOutput.toJson(status)
    assert backupFile.delete()
} finally {
    Jenkins.instance = previousJenkins
    backupDirectory.deleteDir()
}


// 验证设备入口实际权限与响应，拒绝时不得调用 Agent Launcher。
boolean jobAllowed = true
def job = [checkPermission:{ permission -> if (permission == Item.BUILD && !jobAllowed) { throw new SecurityException('denied') } },
           hasPermission:{ permission -> permission != Item.BUILD || jobAllowed }]
def checks = []
def allowed = true
boolean online = true
def computer = [isOnline:{online}, channel:new Object(), node:[createLauncher:{ listener ->
    assert listener.is(TaskListener.NULL); new HdcLauncher()
}]]
Jenkins.instance = [
    checkPermission:{ permission -> checks << permission; if (permission == Jenkins.ADMINISTER && !allowed) { throw new SecurityException('denied') } },
    hasPermission:{ permission -> permission != Jenkins.ADMINISTER || allowed },
    getItemByFullName:{ String name -> assert name == portal.jobName; job },
    getComputer:{ String name -> assert name == portal.macNode; computer }
]
Computer.threadPoolForRemoting = [submit:{ java.util.concurrent.Callable task -> new CaptureFuture(task:task) }]
def capture = { Map input, currentPortal = portal ->
    def writer = new StringWriter()
    def response = [status:200, headers:[:], writer:writer]
    def rsp = [setHeader:{ String key, String value -> response.headers[key] = value },
               setStatus:{ int value -> response.status = value }, setContentType:{ String value -> },
               getWriter:{ writer }] as StaplerResponse2
    def req = [getContentType:{'application/json'},
               getInputStream:{new ByteArrayInputStream(groovy.json.JsonOutput.toJson(input).getBytes('UTF-8'))}] as StaplerRequest2
    currentPortal.doCapture(req, rsp)
    response.body = new groovy.json.JsonSlurperClassic().parseText(writer.toString())
    response
}
assert SxmqliveSigningPortal.getDeclaredMethod('doCapture',StaplerRequest2,StaplerResponse2)
    .getAnnotation(org.kohsuke.stapler.interceptor.RequirePOST) != null
assert SxmqliveSigningPortal.validateCapture([platform:'harmony']) == ''
assert SxmqliveSigningPortal.validateCapture([platform:'harmony',target:'192.168.0.114:37143']) == '192.168.0.114:37143'
['8.8.8.8:22','127.0.0.1:80','192.168.0.114:0','192.168.0.114:65536',
 '192.168.0.999:55','192.168.0.114:22;id','$(id)','hostname:22',null,123].each { target ->
    try { SxmqliveSigningPortal.validateCapture([platform:'harmony',target:target]); assert false }
    catch (IllegalArgumentException ignored) { }
}
assert capture([platform:'ios']).body.code == 'https-required'
assert HdcLauncher.commands == []
assert portal.captureCapabilities(job).iosAvailable == false
assert portal.captureCapabilities(job).harmonyAvailable
assert HdcLauncher.commands == []
allowed = false
assert !portal.captureCapabilities(job).harmonyAvailable
try { capture([platform:'harmony']); assert false } catch (SecurityException ignored) { }
assert HdcLauncher.commands == []
allowed = true
jobAllowed = false
assert !portal.captureCapabilities(job).harmonyAvailable
try { capture([platform:'harmony']); assert false } catch (SecurityException ignored) { }
assert HdcLauncher.commands == []
jobAllowed = true
online = false
assert capture([platform:'harmony']).body.code == 'agent-offline'
assert HdcLauncher.commands == []
online = true
assert capture([platform:'harmony',target:'127.0.0.1:22']).status == 400
assert capture([platform:'harmony',command:'id']).status == 400
assert HdcLauncher.commands == []
hudson.FilePath.present = false
assert capture([platform:'harmony']).body.code == 'hdc-unavailable'
assert HdcLauncher.commands == []
hudson.FilePath.present = true
HdcLauncher.outputs = [[text:'192.168.0.114:37143'],[text:'udid of current device is :\n' + 'A' * 64],[text:'HUAWEI Mate']]
def captured = capture([platform:'harmony',target:'192.168.0.114:37143'])
assert captured.status == 200 && captured.body.udid == 'A' * 64
assert captured.headers['Cache-Control'] == 'no-store'
assert checks.contains(Jenkins.ADMINISTER)
assert HdcLauncher.commands*.drop(1) == [
    ['list','targets'], ['-t','192.168.0.114:37143','shell','bm','get','-u'],
    ['-t','192.168.0.114:37143','shell','param','get','const.product.model']]
CaptureFuture.timeout = true
assert capture([platform:'harmony',target:'192.168.0.114:37143']).body.code == 'capture-timeout'
assert CaptureFuture.cancelled
CaptureFuture.timeout = false
HdcLauncher.outputs = [[exception:true]]
def failed = capture([platform:'harmony',target:'192.168.0.114:37143'])
assert failed.body.code == 'hdc-failed'
assert !groovy.json.JsonOutput.toJson(failed.body).contains('private')
assert SxmqliveSigningPortal.captureTargets('[Empty]') == []
assert SxmqliveSigningPortal.captureTargets('usb-id\n192.168.0.114:37143\n') == ['usb-id','192.168.0.114:37143']
assert SxmqliveSigningPortal.captureUdid('udid: ' + 'B' * 64 + '\n') == 'B' * 64
assert SxmqliveSigningPortal.captureUdid('udid of current device is :\n' + 'B' * 64) == 'B' * 64
assert SxmqliveSigningPortal.captureUdid('error: ' + 'B' * 64) == null
HdcLauncher.outputs = [[text:'[Empty]']]
assert capture([platform:'harmony']).body.code == 'device-not-connected'
HdcLauncher.outputs = [[text:'one\ntwo']]
assert capture([platform:'harmony']).body.code == 'multiple-devices'
HdcLauncher.outputs = [[text:'other']]
assert capture([platform:'harmony',target:'192.168.0.114:37143']).body.code == 'target-not-connected'
HdcLauncher.outputs = [[timeout:true]]
assert capture([platform:'harmony']).body.code == 'capture-timeout'
assert HdcProc.killed
HdcProc.killed = false
HdcLauncher.outputs = [[interrupted:true]]
try { SxmqliveSigningPortal.runHdc(new HdcLauncher(), ['list','targets'], portal.hdcPath); assert false }
catch (InterruptedException ignored) { }
assert HdcProc.killed
HdcLauncher.outputs = [[text:'x' * 20000]]
assert capture([platform:'harmony']).body.code == 'hdc-failed'
HdcLauncher.outputs = [[text:'phone'],[text:'permission denied']]
assert capture([platform:'harmony']).body.code == 'invalid-device-identity'
def output = new SigningCaptureOutput()
output.write(new byte[16384]); output.write(32)
assert output.size() == 16384 && output.oversized
// 第二项目切换固定绑定；网页仍不能改变 Job、节点或命令。
def second = new groovy.json.JsonSlurperClassic().parseText(groovy.json.JsonOutput.toJson(project))
second.id = 'other'
second.signingBackup.scope = 'project_signing'
second.signingBackup.recipientSha256 = 'b' * 64
second.signingBackup.remove('legacyRecipientSha256')
second.jobs.signing_maintenance.name = 'other-apps/other-signing-maintenance'
second.portal.macNode = 'other.mac'
second.portal.hdcPath = '/Users/Shared/other-ci/tools/hdc'
second.portal.backupUrl = 'https://gitee.com/example/other-app/tree/backup/signing'
def other = new SxmqliveSigningPortal(second)
def otherHome = File.createTempDir('other-portal-backup-', '')
def otherJenkins = Jenkins.instance
try {
    Jenkins.instance = [rootDir:otherHome]
    def otherStatus = new File(otherHome, 'signing-backup-status-other.json')
    def success = [at:'2026-10-10T00:00:00Z', gitCommit:'a' * 40, snapshot:'snapshots/20261010T000000123456Z-abcdefabcdef.enc.json']
    def record = [status:'synced', checkedAt:'2026-10-10T01:00:00Z', lastSuccess:success]
    writeStatus(otherStatus, groovy.json.JsonOutput.toJson(record))
    assert other.backupStatus().status == 'pending'
    writeStatus(otherStatus, groovy.json.JsonOutput.toJson(record + [projectId:'sxmqlive', scope:'controller_dr', recipientSha256:project.signingBackup.recipientSha256]))
    assert other.backupStatus().status == 'pending'
    writeStatus(otherStatus, groovy.json.JsonOutput.toJson(record + [projectId:second.id, scope:second.signingBackup.scope, recipientSha256:second.signingBackup.recipientSha256]))
    assert other.backupStatus().status == 'synced'
    assert other.backupStatus().lastSuccess.url == other.backupUrl
} finally { Jenkins.instance = otherJenkins; otherHome.deleteDir() }
Jenkins.instance.getItemByFullName = { String name -> assert name == other.jobName; job }
Jenkins.instance.getComputer = { String name -> assert name == other.macNode; computer }
HdcLauncher.expectedHdc = other.hdcPath
HdcLauncher.outputs = [[text:'other-phone'],[text:'udid: ' + 'C' * 64],[text:'Other phone']]
assert other.captureCapabilities(job).harmonyAvailable
assert capture([platform:'harmony'], other).body.udid == 'C' * 64
assert HdcLauncher.commands.takeRight(3)*.first() == [other.hdcPath] * 3
int commandsBefore = HdcLauncher.commands.size()
['job','node','hdc','command'].each { key ->
    assert capture([platform:'harmony', (key):'arbitrary'], other).status == 400
}
jobAllowed = false
try { capture([platform:'harmony'], other); assert false } catch (SecurityException ignored) { }
assert HdcLauncher.commands.size() == commandsBefore
jobAllowed = true
[second + [jobs:[signing_maintenance:[name:'../other-job']]],
 second + [portal:second.portal + [hdcPath:'relative/hdc']],
 second + [portal:second.portal + [hdcPath:'/tmp/../bin/hdc']],
 second + [portal:second.portal + [backupUrl:'http://example.test/backup']],
 second + [portal:second.portal + [backupUrl:'https://user:password@example.test/backup']]].each { bad ->
    try { new SxmqliveSigningPortal(bad); assert false } catch (IllegalArgumentException ignored) { }
}
println groovy.json.JsonOutput.toJson([ok:true])
