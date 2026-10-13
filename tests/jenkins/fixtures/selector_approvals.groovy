// 执行真实初始化脚本，验证迁移范围和重复执行；不替代线上 Script Security 验收。
def source = new File(args[0]).text
def loader = new GroovyClassLoader()
def type = loader.parseClass('''
package org.jenkinsci.plugins.scriptsecurity.scripts
class ScriptApproval {
    static final INSTANCE = new ScriptApproval()
    static get() { INSTANCE }
    Set signatures = [] as Set
    Set aclSignatures = [] as Set
    Set hashes = [] as Set
    String[] getApprovedSignatures() { signatures as String[] }
    String[] getAclApprovedSignatures() { aclSignatures as String[] }
    void setApprovedSignatures(String[] values) { signatures = values as Set }
    void approveSignature(String value) { signatures.add(value) }
    void approveScript(String value) { hashes.add(value) }
}
''')
def approval = type.INSTANCE
def revoked = (source =~ /(?m)^    '((?:staticMethod|method|staticField|new|field) [^']+)',$/).collect { it[1] } as Set
assert revoked.size() == 10
def unrelated = 'method example.Service read'
approval.signatures.addAll(revoked + [unrelated])
approval.aclSignatures.add('method example.Service readWithAcl')
approval.hashes.add('existing-script-hash')
def shell = new GroovyShell(loader)
2.times { shell.evaluate(source) }
assert approval.signatures == ([unrelated, 'new groovy.json.JsonSlurperClassic',
    'method groovy.json.JsonSlurperClassic parseText java.lang.String'] as Set)
assert approval.aclSignatures == (['method example.Service readWithAcl'] as Set)
assert approval.hashes == (((source =~ /SHA512:[0-9a-f]{128}/).collect { it } + ['existing-script-hash']) as Set)
approval.signatures.addAll(revoked)
def before = approval.signatures.clone()
def hashesBefore = approval.hashes.clone()
approval.aclSignatures.add(revoked.first())
def aclBefore = approval.aclSignatures.clone()
def refused = false
try {
    shell.evaluate(source)
} catch (AssertionError expected) {
    assert expected.message.contains('选择器存在额外 ACL 签名审批')
    refused = true
}
assert refused: 'Expected extra ACL approval to block migration'
assert approval.signatures == before: 'Blocked migration changed global approvals'
assert approval.hashes == hashesBefore
assert approval.aclSignatures == aclBefore
println('Selector approval migration passed: exact revocation, unrelated approvals, fixed hashes, idempotence, ACL fail closed.')
