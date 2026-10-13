import hudson.model.Node
import hudson.slaves.DumbSlave
import hudson.slaves.JNLPLauncher
import hudson.slaves.RetentionStrategy
import jenkins.model.Jenkins
import jenkins.slaves.JnlpAgentReceiver

import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Paths
import java.nio.file.attribute.PosixFilePermissions

def env = System.getenv()
def agentName = env.getOrDefault('PR_AGENT_NAME', 'pr-linux')
def labels = env.getOrDefault('PR_AGENT_LABELS', 'pr-builder')
def remoteFs = env.getOrDefault('PR_AGENT_REMOTE_FS', '/home/jenkins/agent')
def secretPath = Paths.get(
    env.getOrDefault('PR_AGENT_SECRET_FILE', '/run/jenkins-pr-agent-bootstrap/agent.secret')
)
def jenkins = Jenkins.get()
env.getOrDefault('PR_AGENT_REMOVE_NAMES', '').split(',').each { obsoleteName ->
    def normalizedName = obsoleteName.trim()
    if (normalizedName && normalizedName != agentName) {
        def obsoleteNode = jenkins.getNode(normalizedName)
        if (obsoleteNode != null) {
            jenkins.removeNode(obsoleteNode)
            println("Removed obsolete isolated Jenkins PR agent '${normalizedName}'.")
        }
    }
}
def node = jenkins.getNode(agentName)

if (node != null && node.getRemoteFS() != remoteFs) {
    jenkins.removeNode(node)
    node = null
}

if (node == null) {
    node = new DumbSlave(
        agentName,
        '隔离的 Gitee MR 构建节点，不挂载 DWS、发布或签名 Credential 状态。',
        remoteFs,
        '1',
        Node.Mode.EXCLUSIVE,
        labels,
        new JNLPLauncher(),
        new RetentionStrategy.Always(),
        []
    )
    jenkins.addNode(node)
} else {
    node.setNumExecutors(1)
    node.setMode(Node.Mode.EXCLUSIVE)
    node.setLabelString(labels)
    node.setLauncher(new JNLPLauncher())
    node.setRetentionStrategy(new RetentionStrategy.Always())
    node.save()
}

Files.createDirectories(secretPath.getParent())
def secret = JnlpAgentReceiver.DATABASE.getSecretOf(agentName) + System.lineSeparator()
Files.write(secretPath, secret.getBytes(StandardCharsets.US_ASCII))
try {
    Files.setPosixFilePermissions(secretPath, PosixFilePermissions.fromString('rw-------'))
} catch (UnsupportedOperationException ignored) {
    // Linux Volume 支持 POSIX 权限；保留兼容分支用于静态验证。
}
println("Configured isolated Jenkins PR agent '${agentName}' with labels '${labels}'.")
