// 执行真实 Pipeline 参数段，固定角色来自受信 profile，而不是请求参数。
def source = new File(args[0]).text
def modeStart = source.indexOf('                    if (!(params.UPLOAD_AND_NOTIFY')
def modeBlock = source.substring(modeStart, source.indexOf("\n                }", modeStart))
def parameterStart = source.indexOf("                    env.CI_BRAND = (params.BRAND")
def parameterBlock = source.substring(parameterStart, source.indexOf("\n                }", parameterStart))
def profile = [jobs:[harmony_test:[name:'mobile-apps/sxmqlive-ohos-test'],harmony_release:[name:'mobile-apps/sxmqlive-ohos-formal-release']]]
def evaluate = { job, values ->
    def environment = [JOB_NAME: job]
    def binding = new Binding([env:environment,params:values,ciProject:profile,error:{ message -> throw new IllegalStateException(message.toString()) }])
    def shell = new GroovyShell(binding)
    shell.evaluate(modeBlock); shell.evaluate(parameterBlock)
    environment
}
[true,false].each { mode ->
    ['mobile-apps/sxmqlive-ohos-test','mobile-apps/sxmqlive-ohos-formal-release'].each { job ->
        def result = evaluate(job,[UPLOAD_AND_NOTIFY:mode])
        assert result.CI_DRY_RUN == (mode ? 'false':'true')
        assert result.CI_NOTIFY == (mode ? 'true':'false')
        assert result.CI_BUILD_TYPE == (job.endsWith('formal-release') ? 'Release':'QA')
    }
}
[null,'true','false',0,1,''].each { bad ->
    try { evaluate(profile.jobs.harmony_test.name,[UPLOAD_AND_NOTIFY:bad]); assert false }
    catch (IllegalStateException expected) { assert expected.message.contains('UPLOAD_AND_NOTIFY') }
}
[[UPLOAD_AND_NOTIFY:true,DRY_RUN:false],[UPLOAD_AND_NOTIFY:false,NOTIFY:false],[:]].each { bad ->
    try { evaluate(profile.jobs.harmony_test.name,bad); assert false }
    catch (IllegalStateException expected) { assert expected.message.contains('UPLOAD_AND_NOTIFY') }
}
[[profile.jobs.harmony_test.name,[BUILD_TYPE:'Release']], [profile.jobs.harmony_release.name,[BUILD_TYPE:'QA']], ['temporary-job',[:]]].each { entry ->
    try { evaluate(entry[0],[UPLOAD_AND_NOTIFY:false]+entry[1]); assert false }
    catch (IllegalStateException expected) { assert expected.message }
}
profile.jobs.harmony_test.name='Other Folder/qa'
assert evaluate('Other Folder/qa',[UPLOAD_AND_NOTIFY:false,BUILD_TYPE:'Debug',GIT_BRANCH:'origin/main']).CI_BRANCH == 'main'
println('Harmony Job parameters self-check passed: strict unified mode, defaults, Job identity, source normalization.')

// 执行正式模板的实际 Active Choices 脚本，替身只提供远程 Tag 清单。
def template = new File(args.length > 1 ? args[1] : 'jenkins/local/ohos-release-job.xml.template').text
def selectorScript = (template =~ /(?s)<script>(import net.*?)<\/script>/)[0][1]
def fallbackScript = (template =~ /(?s)<secureFallbackScript>\s*<script>(.*?)<\/script>/)[0][1]
def loader = new GroovyClassLoader()
loader.parseClass('''
package hudson.model
class Item { static final String BUILD = 'Job.Build' }
''')
loader.parseClass('''
package org.biouno.unochoice.util
class SafeHtmlExtendedMarkupFormatter {
    static final INSTANCE = new SafeHtmlExtendedMarkupFormatter()
    String translate(String value) { value.replaceAll(/<[^>]*>/, '') }
}
''')
loader.parseClass('''
package hudson.plugins.git
class UserRemoteConfig {
    String url, name, refspec, credentialsId
    UserRemoteConfig(String url, String name, String refspec, String credentialsId) {
        this.url = url; this.name = name; this.refspec = refspec; this.credentialsId = credentialsId
    }
}
class BranchSpec {
    String name
    BranchSpec(String name) { this.name = name }
}
class GitSCM {
    List userRemoteConfigs, branches
    GitSCM(List remotes, List branches, Object browser, String gitTool, List extensions) {
        assert browser == null && gitTool == null && extensions.empty
        this.userRemoteConfigs = remotes; this.branches = branches ?: [new BranchSpec('*/master')]
    }
}
''')
def parameterClass = loader.parseClass('''
package net.uaznia.lukanus.hudson.plugins.gitparameter
class GitParameterDefinition {
    static List tags = []
    static boolean fail = false
    static int queries = 0
    GitParameterDefinition(Object... ignored) {}
    def generateContents(wrapper, scms) {
        queries++
        assert scms.size() == 1
        assert scms[0] instanceof hudson.plugins.git.GitSCM
        assert scms[0].userRemoteConfigs.size() == 1
        def remote = scms[0].userRemoteConfigs[0]
        assert remote.url == System.getProperty('ci.test.applicationSsh')
        assert remote.credentialsId == System.getProperty('ci.test.scmCredential') && remote.name == 'origin'
        assert remote.refspec == '+refs/heads/*:refs/remotes/origin/*'
        assert scms[0].branches.size() == 1 && scms[0].branches[0].name == '*/master'
        if (fail) throw new IllegalStateException('remote refs unavailable')
        [values: tags.collect { [value: it] }]
    }
}
enum SelectedValue { NONE }
''')
loader.parseClass('''
package net.uaznia.lukanus.hudson.plugins.gitparameter.jobs
class JobWrapperFactory { static createJobWrapper(job) { job } }
''')
def jenkinsClass = loader.parseClass('''
package jenkins.model
class Jenkins {
    static Jenkins instance = new Jenkins()
    static Jenkins get() { instance }
    def job = [checkPermission: { permission -> assert permission == 'Job.Build' }]
    def getItemByFullName(String name) { job }
}
''')
parameterClass.tags = ['jimin-ohos-v2.0.0', 'cihang-ios-v1.0.0', 'cihang-v1.0.0',
    'cihang-ohos-v1.0.0', 'cihang-ohos-v1.1.0']
def selector = new GroovyShell(loader, new Binding([BRAND: 'cihang']))
assert selector.evaluate(selectorScript) == ['cihang-ohos-v1.1.0', 'cihang-ohos-v1.0.0']
assert new GroovyShell(loader).evaluate(selectorScript) == ['cihang-ohos-v1.1.0', 'cihang-ohos-v1.0.0']
assert new GroovyShell(loader, new Binding([BRAND: 'jimin'])).evaluate(selectorScript) == ['请先选择品牌:selected']
def queries = parameterClass.queries
jenkinsClass.get().job = [checkPermission: { permission -> throw new SecurityException('Job.Build denied') }]
try {
    selector.evaluate(selectorScript)
    assert false: 'Expected Job.Build rejection before credential lookup'
} catch (SecurityException expected) {
    assert parameterClass.queries == queries
}
jenkinsClass.get().job = [checkPermission: { permission -> assert permission == 'Job.Build' }]
parameterClass.tags = ['cihang-ohos-v1.0.0<img src=x onerror=alert(1)>']
assert selector.evaluate(selectorScript) == ['cihang-ohos-v1.0.0']
parameterClass.tags = ['jimin-ohos-v1.0.0']
assert selector.evaluate(selectorScript) == ['该品牌暂无鸿蒙正式 Tag:selected']
jenkinsClass.get().job = null
assert selector.evaluate(selectorScript) == ['正式任务不存在:selected']
jenkinsClass.get().job = [checkPermission: { permission -> assert permission == 'Job.Build' }]
parameterClass.fail = true
try {
    selector.evaluate(selectorScript)
    assert false: 'Expected remote query failure'
} catch (IllegalStateException expected) {
    assert selector.evaluate(fallbackScript) == ['Tag 加载失败，请联系 Jenkins 管理员:selected']
}
println('Harmony formal Tag selector self-check passed: explicit application SCM, brand filter, ordering, first render, invalid brand, empty tags, absent Job, remote fallback.')
