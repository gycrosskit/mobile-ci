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
def jenkins = Jenkins.get()
def agent = [
    name: env.getOrDefault('OHOS_MAINTENANCE_AGENT_NAME', 'signing-maintenance'),
    remoteFs: env.getOrDefault('OHOS_MAINTENANCE_AGENT_REMOTE_FS', '/home/jenkins/agent'),
    secretPath: Paths.get(env.getOrDefault('OHOS_MAINTENANCE_AGENT_SECRET_FILE', '/run/jenkins-ohos-maintenance-bootstrap/agent.secret')),
    label: 'signing-maintenance', description: '管理员 iOS/Harmony 签名维护节点，独占签名 state 与供应商访问密钥，不运行品牌源码。'
]
// 节点改名须先由管理员迁移；保留原容器和隔离卷，不能自动新建第二个维护入口。
if (agent.name != 'signing-maintenance' || jenkins.getNode('ohos-maintenance') != null) {
    throw new IllegalStateException('Signing maintenance requires signing-maintenance; migrate the legacy ohos-maintenance node before bootstrap.')
}
def node = jenkins.getNode(agent.name)
def remoteFs = agent.remoteFs
if (node != null && (!(node instanceof DumbSlave) || node.getRemoteFS() != remoteFs)) {
    throw new IllegalStateException('Existing OHOS agent conflicts with requested remoteFs/type; preserve it for administrator review.')
}
agent.changed = node != null && (node.getNumExecutors() != 1 || node.getMode() != Node.Mode.EXCLUSIVE ||
    node.getLabelString().trim() != agent.label || !(node.getLauncher() instanceof JNLPLauncher) ||
    !(node.getRetentionStrategy() instanceof RetentionStrategy.Always))
if (agent.changed) {
    def computer = node.toComputer()
    if (computer != null && !computer.isIdle()) {
        throw new IllegalStateException('Existing OHOS agent is busy; refuse configuration changes.')
    }
}

if (node == null) {
    node = new DumbSlave(agent.name, agent.description, agent.remoteFs, '1', Node.Mode.EXCLUSIVE,
        agent.label, new JNLPLauncher(), new RetentionStrategy.Always(), [])
    jenkins.addNode(node)
} else if (agent.changed) {
    node.setNumExecutors(1)
    node.setMode(Node.Mode.EXCLUSIVE)
    node.setLabelString(agent.label)
    node.setLauncher(new JNLPLauncher())
    node.setRetentionStrategy(new RetentionStrategy.Always())
    node.save()
}

def secretPath = agent.secretPath
Files.createDirectories(secretPath.getParent())
Files.setPosixFilePermissions(secretPath.getParent(), PosixFilePermissions.fromString('rwx------'))
def secret = JnlpAgentReceiver.DATABASE.getSecretOf(agent.name) + System.lineSeparator()
Files.write(secretPath, secret.getBytes(StandardCharsets.US_ASCII))
Files.setPosixFilePermissions(secretPath, PosixFilePermissions.fromString('rw-------'))
println('Configured isolated signing maintenance agent with one exclusive executor; secret was saved without logging.')
