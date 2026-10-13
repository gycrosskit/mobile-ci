import org.jenkinsci.plugins.scriptsecurity.scripts.ScriptApproval

// 仅批准仓库内 iOS Pipeline 解析自身 JSON 配置所需的最小签名。
def approval = ScriptApproval.get()
approval.approveSignature('new groovy.json.JsonSlurperClassic')
approval.approveSignature('method groovy.json.JsonSlurperClassic parseText java.lang.String')

// 只批准可选参数折叠的静态 HTML/CSS；内容变化后必须重新核对哈希，不开放通用脚本权限。
approval.approveScript('SHA512:d530c92edd3f9859eab0f2a8a9a2b4dc01a49cf8da7a57f86b1277aa89c6b240f09a5432dc9f88aaa2dab4cb9a06a29a8f3d91b61190f86aff3f1d11eafe0ce7')

// 精确收回旧选择器的全局签名；其他 Pipeline 审批与 ACL 审批保持不变。
def selectorSignatures = [
    'staticMethod jenkins.model.Jenkins get',
    'method jenkins.model.Jenkins getItemByFullName java.lang.String',
    'staticField net.uaznia.lukanus.hudson.plugins.gitparameter.SelectedValue NONE',
    'staticMethod net.uaznia.lukanus.hudson.plugins.gitparameter.jobs.JobWrapperFactory createJobWrapper hudson.model.Job',
    'new net.uaznia.lukanus.hudson.plugins.gitparameter.GitParameterDefinition java.lang.String java.lang.String java.lang.String java.lang.String java.lang.String java.lang.String java.lang.String net.uaznia.lukanus.hudson.plugins.gitparameter.SortMode net.uaznia.lukanus.hudson.plugins.gitparameter.SelectedValue java.lang.String java.lang.Boolean',
    'new hudson.plugins.git.UserRemoteConfig java.lang.String java.lang.String java.lang.String java.lang.String',
    'new hudson.plugins.git.GitSCM java.util.List java.util.List hudson.plugins.git.browser.GitRepositoryBrowser java.lang.String java.util.List',
    'method net.uaznia.lukanus.hudson.plugins.gitparameter.GitParameterDefinition generateContents net.uaznia.lukanus.hudson.plugins.gitparameter.jobs.JobWrapper java.util.List',
    'field net.uaznia.lukanus.hudson.plugins.gitparameter.model.ItemsErrorModel values',
    'field net.uaznia.lukanus.hudson.plugins.gitparameter.model.ItemsErrorModel$Option value',
]
synchronized (approval) {
    assert !approval.aclApprovedSignatures.any { it in selectorSignatures }: '选择器存在额外 ACL 签名审批，请先移除对应项'
    approval.setApprovedSignatures(approval.approvedSignatures.findAll { !(it in selectorSignatures) } as String[])
}

// 完整脚本包含固定 Job、仓库、凭据和 Build 权限检查；禁止从线上 Job 动态计算并批准哈希。
// BEGIN MANAGED SELECTOR HASHES
[
    'SHA512:94dbf49107466cdf6a5d2f20645a1d1fcd71a9799d7007fa11a298201c2ad462a48ec013e0a49f4571bf67728237fa9aec2e7de139b5d06fe546edc8f39044ea',
    'SHA512:c1514f7dee678040044c2a8754d1fa4c30875d1c5f7828dcadee7242afeaaed3bfdeccd6ad4280e18264b4c005ea4f80cb4cd5b6b7e903834fec7a70de57ec1f',
    'SHA512:ac973e0e5dac6a092eba1affb96731d3dd36cab4a8ae7f7c1852131e194d2fbdbf5665aa80389b16a8c42cbc7059227928cb6aa60f0e9f0b3675fb85e7857f27',
    'SHA512:5cf2a8a1652b1ee1cf4e4a90c2991f8a0598753633a3f58b213f227dab31c3995e7843fbad07407ff0bc939be89ef1ab82a67025969b64d44406a4ff2591d1d5',
    'SHA512:474a92afb856be1b858716925851972ee1727e76695f128ed82229ffc8eac254656c7853d4ed5baeea677f1158726bb78ea71c6c90def2927f3671d13bc648d4',
    'SHA512:507d58133312c1b9e33295ec8469c85cb7f08e9005f6f428334c90e63cb1baf2a06e1e68f31d0765bbc034a4e2922ae7aebcc010dcedb584337f5ad60c3c2b46',
].each { approval.approveScript(it) }
// END MANAGED SELECTOR HASHES
