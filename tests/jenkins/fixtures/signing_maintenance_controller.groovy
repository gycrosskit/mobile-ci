// 执行生产 NonCPS helper 的控制流；Jenkins IO/权限/Store 用受控内存替身，不连接真实 Controller。
import groovy.json.JsonOutput
import java.security.MessageDigest

def loader = new GroovyClassLoader(getClass().classLoader)
[
'''package com.cloudbees.groovy.cps; @interface NonCPS {}''',
'''package com.cloudbees.plugins.credentials; enum CredentialsScope { GLOBAL, SYSTEM }''',
'''package com.cloudbees.plugins.credentials; class SecretBytes { byte[] bytes; static fromBytes(byte[] b) { new SecretBytes(bytes:b) } }''',
'''package com.cloudbees.plugins.credentials.domains; class Domain { static global() { 'global' } }''',
'''package org.jenkinsci.plugins.plaincredentials.impl;
class FileCredentialsImpl {
 def scope; String id; String description; String fileName; byte[] data
 FileCredentialsImpl(scope, id, description, fileName, secret) { this.scope=scope; this.id=id; this.description=description; this.fileName=fileName; this.data=secret.bytes }
 def getContent() { new ByteArrayInputStream(data) }
}''',
'''package com.cloudbees.plugins.credentials;
class FakeStore {
 def context; Map values=[:]; Map other=[:]; String failUpdate; int writes=0
 List getDomains(){ ['global'] + other.keySet() }
 List getCredentials(domain) { domain=='global' ? values.values().toList() : (other[domain] ?: []) }
 boolean addCredentials(domain, c) { writes++; if (values[c.id]) return false; values[c.id]=c; true }
 boolean updateCredentials(domain, old, c) { writes++; if(c.id==failUpdate) {failUpdate=null; return false}; if(!values[c.id].is(old)) return false; values[c.id]=c; true }
 boolean removeCredentials(domain,c) { writes++; values.remove(c.id); true }
}
class CredentialsProvider { static instance; static List rootStores=[]; static List folderStores=[]; static lookupStores(context){context==instance ? folderStores : rootStores} }''',
'''package com.cloudbees.hudson.plugins.folder; class Folder { def store; boolean failCommit=false }''',
'''package hudson;
class BulkChange { def provider; BulkChange(p){provider=p}; void commit(){if(provider.failCommit){provider.failCommit=false;throw new IOException('disk-save-failed')}}; void abort(){} }
class FilePath {
 static Map files=[:]; String path; FilePath(channel,String path){this.path=path}
 FilePath child(String relative){new FilePath(null,path+'/'+relative)}
 String readToString(){files[path].text}; String readLink(){files[path].link}; boolean isDirectory(){files[path].directory ?: false}; int mode(){files[path].mode ?: 0600}; long length(){readToString().bytes.length}
}''',
'''package hudson.util; class Secret { String plainText; static fromString(String text){new Secret(plainText:text)} }''',
'''package org.jenkinsci.plugins.plaincredentials.impl;
class StringCredentialsImpl { def scope; String id; String description; def secret
 StringCredentialsImpl(scope,id,description,secret){this.scope=scope;this.id=id;this.description=description;this.secret=secret} }''',
'''package hudson.model;
class Node { enum Mode { EXCLUSIVE } }
class Cause { static class UserIdCause { String userId; String getUserId(){userId} } }
class User { String id; static getById(String id,boolean create){new User(id:id)}; def impersonate2(){[id:id]} }''',
'''package hudson.slaves; class DumbSlave {} ; class JNLPLauncher {}; class RetentionStrategy { static class Always {} }''',
'''package jenkins.slaves; class JnlpAgentReceiver {}''',
'''package jenkins.model;
class Jenkins {
 static ADMINISTER='administer'; static instance=new Jenkins(); def computers=[]; def queue=[items:[] as Object[]]; def computer=[channel:'channel']
 def folder; Map nodes=[:]; def getItemByFullName(String name,Class kind){ name=='mobile-apps' ? folder : null }; static get(){instance}; def getNode(String n){nodes[n]}; def getComputer(String n){computer}; void checkPermission(p){}; def getACL(){[hasPermission2:{ auth,p -> auth.id=='admin' }]}
}'''
].each { loader.parseClass(it) }

def source = new File(args[0]).getText('UTF-8')
def helper = loader.parseClass(source).getDeclaredConstructor().newInstance()
def Provider = loader.loadClass('com.cloudbees.plugins.credentials.CredentialsProvider')
def Folder = loader.loadClass('com.cloudbees.hudson.plugins.folder.Folder')
def Store = loader.loadClass('com.cloudbees.plugins.credentials.FakeStore')
Provider.instance=Folder.newInstance(store:Store.newInstance()); Provider.instance.store.context=Provider.instance
def rootStore=Store.newInstance()
def Scope = loader.loadClass('com.cloudbees.plugins.credentials.CredentialsScope')
def Bytes = loader.loadClass('com.cloudbees.plugins.credentials.SecretBytes')
def Credential = loader.loadClass('org.jenkinsci.plugins.plaincredentials.impl.FileCredentialsImpl')
def FilePath = loader.loadClass('hudson.FilePath')
def Jenkins = loader.loadClass('jenkins.model.Jenkins')
def Cause = loader.loadClass('hudson.model.Cause$UserIdCause')
def ns='harmony-team-one'; def ids=[ns+'-release-p12',ns+'-alpha-designated-release-profile',ns+'-alpha-devices-json']
def filenames=['release.p12','designated-release.p7b','devices.json']
def hash={ byte[] b -> MessageDigest.getInstance('SHA-256').digest(b).encodeHex().toString() }
def credential={ id,value -> Credential.newInstance(Scope.GLOBAL,id,'kept description','name',Bytes.fromBytes(value.bytes)) }
def job=[getFullName:{'mobile-apps/sxmqlive-signing-maintenance'}]
def cause=Cause.newInstance(userId:'admin')
def run=[getParent:{job},getCause:{ ignored -> cause}]
def config=[alpha:[harmony:[teamKey:'one']],_harmonyTeams:[one:[credentialNamespace:ns]]]
rootStore.context=Jenkins.instance
def reset={
 Provider.folderStores=[Provider.instance.store]; Provider.rootStores=[rootStore]; rootStore.values=[:]; rootStore.other=[:]; Jenkins.instance.folder=Provider.instance
 Provider.instance.store.other=[:]; Provider.instance.store.values=[:]; Provider.instance.store.failUpdate=null; Provider.instance.store.writes=0; Provider.instance.failCommit=false
 ids.eachWithIndex { id,i -> Provider.instance.store.values[id]=credential(id,'old-'+i) }
 Jenkins.instance.computers=[]; Jenkins.instance.queue=[items:[] as Object[]]; cause.userId='admin'
 FilePath.files=['/workspace/source/ci/brands.json':[text:JsonOutput.toJson(config)]]
 ['/home/jenkins/signing','/home/jenkins/signing/harmony','/home/jenkins/signing/harmony/one',
  '/home/jenkins/signing/harmony/one/materials','/home/jenkins/signing/harmony/one/materials/alpha',
  '/home/jenkins/signing/harmony/one/materials/alpha/.jenkins-maintenance'].each {FilePath.files[it]=[directory:true,mode:0700]}
}
def snapshot={helper.maintenanceCredentials(run,'signing-maintenance','/workspace/source','alpha')}
def inventory={platform='harmony' -> helper.inventoryCredentialStatus(run,'signing-maintenance','/workspace/source','alpha',platform)}
reset()
def inventoryResult=inventory()
assert inventoryResult[ns+'-release-p12']=='configured'
assert inventoryResult[ns+'-sa-key']=='missing'
assert inventoryResult.size()==7
assert Provider.instance.store.writes==0
Provider.instance.store.values[ns+'-sa-key']=credential(ns+'-sa-key','NEVER-READ-SECRET')
Provider.instance.store.values[ns+'-sa-key'].metaClass.getContent={throw new AssertionError('inventory-read-secret')}
assert inventory()[ns+'-sa-key']=='configured'
Provider.instance.store.values[ns+'-sa-key'].scope=Scope.SYSTEM
assert inventory()[ns+'-sa-key']=='wrong-type'
Provider.instance.store.values['duplicate']=credential(ns+'-release-p12','another')
assert inventory()[ns+'-release-p12']=='wrong-type'
cause.userId='ordinary'
try {inventory();assert false} catch(IllegalStateException expected) {assert expected.message.contains('管理员')}
reset()
def payload={ previous, values ->
 def rows=ids.withIndex().collect { id,i -> [id:id,type:'Secret file',description:'HarmonyOS | Team one | '+id,filename:filenames[i],value:values[i].bytes.encodeBase64().toString(),sha256:hash(values[i].bytes)] }
 def value=[namespace:ns,brand:'alpha',items:rows,expected:previous.items.collectEntries {[(it.id):it.sha256]}]
 FilePath.files['/home/jenkins/signing/harmony/one/materials/alpha/.jenkins-maintenance/payload.json']=[text:JsonOutput.toJson(value),mode:0600]
 value
}
def apply={ previous -> helper.maintenanceCredentials(run,'signing-maintenance','/workspace/source','alpha',previous) }
def fails={ String part, Closure action ->
 try { action(); assert false:'expected refusal' } catch(IllegalStateException error) {assert error.message.contains(part):error.message}
}
def values={ ids.collect { new String(Provider.instance.store.values[it].data) } }

reset(); def old=snapshot(); assert old.items.size()==3; assert !JsonOutput.toJson(old).contains('old-')
payload(old,['old-0','new-profile','new-roster']); def report=apply(old)
assert report.updated==ids.drop(1); assert values()==['old-0','new-profile','new-roster']
assert Provider.instance.store.values[ids[1]].description=='HarmonyOS | Team one | '+ids[1]
assert Provider.instance.store.values[ids[0]].description=='kept description'
reset(); old=snapshot(); payload(old,['replacement-p12','new-profile','new-roster'])
fails('禁止替换'){apply(old)}; assert values()==['old-0','old-1','old-2']; assert Provider.instance.store.writes==0
reset(); old=snapshot(); payload(old,['old-0','new-profile','new-roster']); Provider.instance.store.values[ids[1]]=credential(ids[1],'external-change')
fails('CAS'){apply(old)}; assert values()==['old-0','external-change','old-2']; assert Provider.instance.store.writes==0
reset(); old=snapshot(); payload(old,['old-0','new-profile','new-roster']); Provider.instance.store.failUpdate=ids[2]
fails('已恢复旧值'){apply(old)}; assert values()==['old-0','old-1','old-2']
reset(); old=snapshot(); payload(old,['old-0','new-profile','new-roster']); Provider.instance.failCommit=true
fails('已恢复旧值'){apply(old)}; assert values()==['old-0','old-1','old-2']
reset(); Provider.instance.store.values.remove(ids[0]); old=snapshot(); payload(old,['created-p12','new-profile','new-roster']); apply(old)
assert values()==['created-p12','new-profile','new-roster']
reset(); old=snapshot(); payload(old,['old-0','old-1','old-2']); assert apply(old).updated==[]; assert Provider.instance.store.writes==0
reset(); cause.userId='ordinary'; fails('实际管理员'){snapshot()}
reset(); Jenkins.instance.queue.items=[[task:[fullName:'other-job']]] as Object[]; fails('其他构建或队列'){snapshot()}
reset(); Jenkins.instance.queue.items=[[task:[fullName:'运维/sxmqlive-signing-backup']]] as Object[]; assert snapshot().items.size()==3
reset(); Jenkins.instance.queue.items=[[task:[fullName:'运维/sxmqlive-signing-backup']], [task:[fullName:'other-job']]] as Object[]; fails('其他构建或队列'){snapshot()}
reset(); Jenkins.instance.computers=[[executors:[[currentExecutable:[getParent:{[getOwnerExecutable:{'other-run'}]}]]],oneOffExecutors:[]]]
fails('其他构建或队列'){snapshot()}
reset(); Provider.instance.store.values[ids[0]].scope=Scope.SYSTEM; fails('GLOBAL'){snapshot()}
reset(); old=snapshot(); def unexpected=payload(old,['old-0','new-profile','new-roster']); unexpected.items[1].id=ns+'-sa-key'
FilePath.files['/home/jenkins/signing/harmony/one/materials/alpha/.jenkins-maintenance/payload.json'].text=JsonOutput.toJson(unexpected)
fails('固定范围'){apply(old)}; assert Provider.instance.store.writes==0
reset(); old=snapshot(); payload(old,['old-0','new-profile','new-roster']); FilePath.files['/home/jenkins/signing/harmony/one/materials/alpha/.jenkins-maintenance/payload.json'].mode=0644
fails('0600'){apply(old)}; assert Provider.instance.store.writes==0
reset(); fails('维护节点'){helper.maintenanceCredentials(run,'ohos-linux','/workspace/source','alpha')}
['ohos-maintenance','android-mac'].each { legacyNode ->
 reset(); fails('维护节点'){helper.maintenanceCredentials(run,legacyNode,'/workspace/source','alpha')}
}
reset(); old=snapshot(); payload(old,['old-0','new-profile','new-roster']); FilePath.files['/home/jenkins/signing/harmony/one/materials'].link='/outside'
fails('每级目录'){apply(old)}; assert Provider.instance.store.writes==0
reset(); old=snapshot(); payload(old,['old-0','new-profile','new-roster']); FilePath.files['/home/jenkins/signing/harmony/one/materials/alpha'].mode=0755
fails('每级目录'){apply(old)}; assert Provider.instance.store.writes==0
reset(); Jenkins.instance.computers=[[executors:[[currentExecutable:[getParent:{[getOwnerExecutable:{run}]}]]],oneOffExecutors:[]]]
assert snapshot().items.size()==3
reset(); def sameJobOtherRun=[getParent:{job}]
Jenkins.instance.computers=[[executors:[[currentExecutable:[getParent:{[getOwnerTask:{job},getOwnerExecutable:{sameJobOtherRun}]}]]],oneOffExecutors:[]]]
fails('其他构建或队列'){snapshot()}
reset(); Jenkins.instance.computers=[[executors:[[currentExecutable:[getParent:{[getOwnerTask:{job},getOwnerExecutable:{null}]}]]],oneOffExecutors:[]]]
fails('其他构建或队列'){snapshot()}
def iosNS='ios-team-one'; def iosIds=[iosNS+'-distribution-p12',iosNS+'-distribution-p12-password']
def iosRoot='/home/jenkins/signing'
def iosSnapshot={helper.maintenanceCredentials(run,'signing-maintenance','/workspace/source','alpha',null,'ios')}
def iosApply={previous -> helper.maintenanceCredentials(run,'signing-maintenance','/workspace/source','alpha',previous,'ios')}
def iosPayload={previous,packed,password ->
 def items=iosIds.withIndex().collect {id,i -> def text=i==0?packed:password
  [id:id,type:i==0?'Secret file':'Secret text',description:'iOS | Team one | '+id,filename:i==0?'distribution.p12':null,value:text.bytes.encodeBase64().toString(),sha256:hash(text.bytes)] }
 [iosRoot,iosRoot+'/ios',iosRoot+'/ios/one',iosRoot+'/ios/one/materials',iosRoot+'/ios/one/materials/alpha',iosRoot+'/ios/one/materials/alpha/.jenkins-maintenance'].each {FilePath.files[it]=[directory:true,mode:0700]}
 FilePath.files[iosRoot+'/ios/one/materials/alpha/.jenkins-maintenance/payload.json']=[mode:0600,text:JsonOutput.toJson([namespace:iosNS,brand:'alpha',items:items,expected:previous.items.collectEntries {[(it.id):it.sha256]}])]
}
def iosReset={reset(); Provider.instance.store.values=[:]; FilePath.files['/workspace/source/ci/brands.json'].text=JsonOutput.toJson([alpha:[ios:[signing:[test:[team:'one']]]],_iosTeams:[one:[credentialNamespace:iosNS]]])}
iosReset(); old=iosSnapshot(); iosPayload(old,'ios-packed','ios-password'); assert iosApply(old).updated==iosIds
assert Provider.instance.store.values[iosIds[1]].secret.plainText=='ios-password'
assert Provider.instance.store.values[iosIds[0]].description=='iOS | Team one | '+iosIds[0]
assert Provider.instance.store.values[iosIds[1]].description=='iOS | Team one | '+iosIds[1]
old=iosSnapshot(); iosPayload(old,'ios-packed','ios-password'); def writes=Provider.instance.store.writes; assert iosApply(old).updated==[]; assert Provider.instance.store.writes==writes
old=iosSnapshot(); iosPayload(old,'replacement','ios-password'); fails('禁止替换'){iosApply(old)}; assert Provider.instance.store.writes==writes
iosReset(); Provider.instance.store.values[iosIds[0]]=credential(iosIds[0],'ios-packed'); fails('同为缺失或完整'){iosSnapshot()}; assert Provider.instance.store.writes==0
iosReset(); def StringCredential=loader.loadClass('org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl')
Provider.instance.store.values[iosIds[1]]=StringCredential.newInstance(Scope.GLOBAL,iosIds[1],'kept',loader.loadClass('hudson.util.Secret').fromString('password'))
fails('同为缺失或完整'){iosSnapshot()}; assert Provider.instance.store.writes==0
['ohos-maintenance','android-mac'].each { legacyNode ->
 iosReset(); fails('维护节点'){helper.maintenanceCredentials(run,legacyNode,'/workspace/source','alpha',null,'ios')}
}
iosReset(); old=iosSnapshot(); iosPayload(old,'ios-packed','ios-password')
FilePath.files[iosRoot+'/harmony/one/materials/alpha'].mode=0755
assert iosApply(old).updated==iosIds
reset(); old=snapshot(); payload(old,['old-0','new-profile','new-roster'])
FilePath.files['/home/jenkins/signing/ios']=[directory:true,mode:0755]
assert apply(old).updated==ids.drop(1)
def bootstrap = loader.parseClass(new File(args[1]).getText('UTF-8').replace('def env = System.getenv()', 'def env = testEnvironment'))
Jenkins.instance.nodes=['ohos-maintenance':new Object()]
fails('migrate the legacy'){bootstrap.getDeclaredConstructor().newInstance().with {it.binding=new Binding(testEnvironment:[:]); it.run()}}
assert Jenkins.instance.nodes.keySet()==['ohos-maintenance'] as Set
Jenkins.instance.nodes=[:]
fails('requires signing-maintenance'){bootstrap.getDeclaredConstructor().newInstance().with {
 it.binding=new Binding(testEnvironment:[OHOS_MAINTENANCE_AGENT_NAME:'other-maintenance']); it.run()
}}
assert Jenkins.instance.nodes.isEmpty()
def buildBootstrap = loader.parseClass(new File(args[2]).getText('UTF-8').replace('def env = System.getenv()', 'def env = testEnvironment'))
['android-linux','ohos-linux'].each { legacyName ->
 Jenkins.instance.nodes=[(legacyName):new Object()]
 fails('migrate legacy'){buildBootstrap.getDeclaredConstructor().newInstance().with {it.binding=new Binding(testEnvironment:[:]); it.run()}}
 assert Jenkins.instance.nodes.keySet()==[legacyName] as Set
}
Jenkins.instance.nodes=[:]
fails('requires linux-build'){buildBootstrap.getDeclaredConstructor().newInstance().with {
 it.binding=new Binding(testEnvironment:[ANDROID_AGENT_NAME:'other-build']); it.run()
}}
assert Jenkins.instance.nodes.isEmpty()
reset(); Jenkins.instance.folder=null; fails('folder-missing'){snapshot()}; assert Provider.instance.store.writes==0
reset(); Provider.folderStores=[]; fails('store-missing'){snapshot()}; assert Provider.instance.store.writes==0
reset(); Provider.folderStores=[rootStore]; fails('store-missing'){snapshot()}
reset(); rootStore.values[ids[0]]=credential(ids[0],'root-secret'); fails('同 ID'){snapshot()}; assert rootStore.writes==0
reset(); rootStore.other['legacy']=[credential(ids[1],'domain-secret')]; fails('同 ID'){inventory()}
reset(); Provider.instance.store.other['legacy']=[credential(ids[1],'domain-secret')]; fails('同 ID'){snapshot()}
reset(); rootStore.values['gitee-ssh']=credential('gitee-ssh','shared-root'); assert snapshot().items.size()==3
reset(); def androidIds=['android-key-team-one-keystore','android-key-team-one-signing-json']
FilePath.files['/workspace/source/ci/brands.json'].text=JsonOutput.toJson([alpha:[android:[signing:[credentials:[keystore:androidIds[0],signingJson:androidIds[1]]]]]])
Provider.instance.store.values=[:]; androidIds.each{Provider.instance.store.values[it]=credential(it,'private-android')}
Provider.instance.store.values[androidIds[0]].metaClass.getContent={throw new AssertionError('inventory-read-android-secret')}
assert inventory('android')==androidIds.collectEntries{[(it):'configured']}; assert Provider.instance.store.writes==0
Provider.instance.store.values[androidIds[1]].scope=Scope.SYSTEM; assert inventory('android')[androidIds[1]]=='wrong-type'
// 接入与 Profile 刷新脚本也必须从同一个 Folder store 读取和写入。
def scriptReport={int index, Map input ->
    def script=loader.parseClass(new File(args[index]).text.replace('__PAYLOAD__', JsonOutput.toJson(input).bytes.encodeBase64().toString())).getDeclaredConstructor().newInstance()
    def output=new StringWriter(); script.binding=new Binding(out:new PrintWriter(output)); script.run()
    assert !output.toString().contains('private-material')
    output.toString()
}
def item=[id:ids[1],type:'Secret file',description:'profile',filename:'designated-release.p7b',value:'private-material'.bytes.encodeBase64().toString(),sha256:hash('private-material'.bytes)]
reset(); Provider.instance.store.values=[:]
assert scriptReport(3,[items:[item],apply:true]).contains('created')
assert new String(Provider.instance.store.values[item.id].data)=='private-material'; assert rootStore.values.isEmpty()
assert scriptReport(3,[items:[item],apply:false]).contains('matching')
reset(); Jenkins.instance.folder=null; assert scriptReport(3,[items:[item],apply:false]).contains('script-failed')
reset(); rootStore.values[item.id]=credential(item.id,'old-root'); assert scriptReport(3,[items:[item],apply:true]).contains('script-failed'); assert Provider.instance.store.writes==0
reset(); def refreshed=ids.drop(1).withIndex().collect{id,i -> item + [id:id,expectedSha256:hash(('old-'+(i+1)).bytes)]}
assert scriptReport(4,[namespace:ns,brand:'alpha',items:refreshed,apply:true]).contains('updated')
assert values()==['old-0','private-material','private-material']; assert rootStore.values.isEmpty()
reset(); Provider.instance.store.failUpdate=ids[2]
assert scriptReport(4,[namespace:ns,brand:'alpha',items:refreshed,apply:true]).contains('refresh-failed'); assert values()==['old-0','old-1','old-2']
reset(); Provider.folderStores=[rootStore]
assert scriptReport(4,[namespace:ns,brand:'alpha',items:refreshed,apply:true]).contains('script-failed'); assert rootStore.writes==0
reset(); rootStore.other['legacy']=[credential(ids[1],'old-root')]
assert scriptReport(4,[namespace:ns,brand:'alpha',items:refreshed,apply:true]).contains('script-failed'); assert Provider.instance.store.writes==0
println('Controller helper: 52 behavioral checks passed')
