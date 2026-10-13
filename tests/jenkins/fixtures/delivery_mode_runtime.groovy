// 运行真实 Jenkinsfile；仅替换 Jenkins DSL 和外部步骤，不执行构建或网络。
abstract class ModePipeline extends Script {
    boolean enabled = true
    List credentials = []
    List commands = []
    List notifications = []
    String selectedPost = ''
    String requestedStage = ''
    List checkouts = []
    String checkoutCommit = 'a'*40
    def invokeMethod(String name, Object arguments) {
        def values = arguments instanceof Object[] ? arguments as List : [arguments]
        def body = values.find { it instanceof Closure }
        switch (name) {
            case 'pipeline': case 'stages': case 'script': case 'dir': case 'catchError':
                return body?.call()
            case 'stage':
                if (!(values[0] in ['Validate Delivery Mode','Notify DingTalk', requestedStage])) return
                env.STAGE_NAME = values[0]; enabled = true
                return body.call()
            case 'when': return body.call()
            case 'expression': enabled = body.call(); return
            case 'steps': if (enabled) return body.call(); return
            case 'post': return body.call()
            case 'failure': case 'unstable': if (selectedPost == name) return body.call(); return
            case 'always': case 'success': case 'unsuccessful': case 'options': case 'environment': case 'agent': return
            case 'withCredentials': credentials.addAll(values[0]); return body.call()
            case 'string': return values[0]
            case 'checkout': checkouts << values[0]; return [GIT_COMMIT:checkoutCommit]
            case 'deleteDir': return
            case 'sh': commands << values[0]; return ''
            case 'echo': return
            case 'fileExists': return true
            case 'load': return [send:{ Object... fields -> this.notifications.add(fields.toList()) }]
            case 'error': throw new IllegalStateException(values[0].toString())
            default: return super.invokeMethod(name, arguments)
        }
    }
}

def loader = new GroovyClassLoader(this.class.classLoader)
loader.parseClass('package org.jenkinsci.plugins.workflow.steps; class FlowInterruptedException extends Exception {}')
def config = new org.codehaus.groovy.control.CompilerConfiguration(scriptBaseClass: ModePipeline.name)
args.each { path ->
    def source = new File(path).getText('UTF-8')
    def run = { values, post = '', requestedStage = '', checkoutCommit = 'a'*40 ->
        def env = [CI_DEFAULT_BRANCH:'main', CI_TAG:'example-v1', CI_SCM_CREDENTIAL_ID:'fixed-scm', APP_REPOSITORY_URL:'git@gitee.com:example/app.git', CI_NOTIFY:'', CI_TOOL_ROOT:'/trusted-ci', WORKSPACE:'/workspace', CI_COMMIT:'a'*40,
            CI_DINGTALK_WEBHOOK_CREDENTIAL_ID:'fixed-webhook',CI_DINGTALK_SECRET_CREDENTIAL_ID:'fixed-secret',CI_DINGTALK_TESTER_CREDENTIAL_ID:'']
        def shell = new GroovyShell(loader,new Binding([params:values,env:env,currentBuild:[currentResult:'FAILURE']]),config)
        def script = shell.parse(source)
        script.setCheckoutCommit(checkoutCommit)
        if (requestedStage == 'Checkout Selected Source') env.remove('CI_COMMIT')
        script.setSelectedPost(post); script.setRequestedStage(requestedStage); script.run(); script
    }
    if (path.endsWith('Jenkinsfile.ohos.test')) {
        def selected = run([UPLOAD_AND_NOTIFY:false], '', 'Checkout Selected Source')
        assert selected.env.CI_COMMIT == 'a'*40
        [null, '', 'invalid'].each { bad ->
            try { run([UPLOAD_AND_NOTIFY:false], '', 'Checkout Selected Source', bad); assert false }
            catch (IllegalStateException expected) { assert expected.message.contains('Commit') }
        }
    }
    def providerStart = source.indexOf("                        if (env.CI_NOTIFY == 'true' && (")
    assert providerStart >= 0
    def providerEnd = source.indexOf("\n                        }", providerStart) + "\n                        }".length()
    def providerBlock = source.substring(providerStart, providerEnd)
    def providerCheck = { notify, channel, providers ->
        new GroovyShell(new Binding([env:[CI_NOTIFY:notify], params:[TEST_CHANNEL:channel], ciProject:[providers:providers], error:{ message -> throw new IllegalStateException(message.toString()) }])).evaluate(providerBlock)
    }
    providerCheck('false','pgyer',[:])
    try { providerCheck('true','pgyer',[:]); assert false }
    catch (IllegalStateException expected) { assert expected.message }
    if (path.contains('Jenkinsfile.ios.')) {
        if (path.endsWith('ios.release')) providerCheck('true','', [dingtalk:[accountId:'fixed-dingtalk']])
        else {
            providerCheck('true','testflight',[dingtalk:[accountId:'fixed-dingtalk']])
            try { providerCheck('true','pgyer',[dingtalk:[accountId:'fixed-dingtalk']]); assert false }
            catch (IllegalStateException expected) { assert expected.message }
        }
    }
    if (path.endsWith('Jenkinsfile.release') || path.endsWith('Jenkinsfile.ios.release')) {
        def selected = run([UPLOAD_AND_NOTIFY:false], '', 'Checkout Selected Tag')
        assert selected.checkouts.size() == 1
        assert selected.checkouts[0].userRemoteConfigs[0].refspec == '+refs/heads/main:refs/remotes/origin/main +refs/tags/*:refs/tags/*'
        assert selected.checkouts[0].branches[0].name == 'refs/tags/example-v1'
    }
    [true,false].each { mode ->
        def normal = run([UPLOAD_AND_NOTIFY:mode])
        assert normal.env.CI_DRY_RUN == (mode ? 'false':'true')
        assert normal.env.CI_NOTIFY == (mode ? 'true':'false')
        if (mode) {
            assert normal.credentials*.credentialsId == ['fixed-webhook','fixed-secret']
            assert normal.commands.size() == 1 && normal.commands[0].contains(' notify')
        } else { assert normal.credentials.empty && normal.commands.empty }
        ['failure','unstable'].each { post ->
            def result = run([UPLOAD_AND_NOTIFY:mode], post)
            assert result.notifications.size() == (mode ? 1:0)
            if (!mode) assert result.credentials.empty && result.commands.empty
        }
    }
    [[:],[UPLOAD_AND_NOTIFY:null],[UPLOAD_AND_NOTIFY:'true'],[UPLOAD_AND_NOTIFY:'false'],[UPLOAD_AND_NOTIFY:0],
     [UPLOAD_AND_NOTIFY:1],[UPLOAD_AND_NOTIFY:true,DRY_RUN:false],[UPLOAD_AND_NOTIFY:false,NOTIFY:true]].each { bad ->
        try { run(bad); assert false: 'invalid form accepted' }
        catch (IllegalStateException expected) { assert expected.message.contains('UPLOAD_AND_NOTIFY') }
    }
}
println('Actual packaging modes passed: strict form, publish mapping, quiet success/failure, fixed notification credentials.')
