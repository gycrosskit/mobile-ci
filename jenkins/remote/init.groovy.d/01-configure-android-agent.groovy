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
def agentName = env.getOrDefault('ANDROID_AGENT_NAME', 'linux-build')
def labels = env.getOrDefault(
    'ANDROID_AGENT_LABELS',
    'android-builder ohos-linux linux-build'
)
def remoteFs = env.getOrDefault('ANDROID_AGENT_REMOTE_FS', '/home/jenkins/agent')
def secretPath = Paths.get(
    env.getOrDefault(
        'ANDROID_AGENT_SECRET_FILE',
        '/run/jenkins-agent-bootstrap/agent.secret'
    )
)

def jenkins = Jenkins.get()
// 旧节点身份须由管理员迁移，启动脚本不能创建第二个构建入口或删除已有节点。
if (agentName != 'linux-build' || ['android-linux', 'ohos-linux'].any { jenkins.getNode(it) != null }) {
    throw new IllegalStateException('Linux build requires linux-build; migrate legacy android-linux/ohos-linux nodes before bootstrap.')
}
def node = jenkins.getNode(agentName)
if (node != null && (!(node instanceof DumbSlave) || node.getRemoteFS() != remoteFs)) {
    throw new IllegalStateException('Existing Linux build agent conflicts with requested remoteFs/type; preserve it for administrator review.')
}
def changed = node != null && (node.getNumExecutors() != 1 || node.getMode() != Node.Mode.EXCLUSIVE ||
    node.getLabelString().trim() != labels || !(node.getLauncher() instanceof JNLPLauncher) ||
    !(node.getRetentionStrategy() instanceof RetentionStrategy.Always))
if (changed) {
    def computer = node.toComputer()
    if (computer != null && !computer.isIdle()) {
        throw new IllegalStateException('Existing Linux build agent is busy; refuse configuration changes.')
    }
}

if (node == null) {
    node = new DumbSlave(
        agentName,
        'Linux Docker Android/Harmony 构建节点，单 Executor 串行运行，两平台缓存隔离。',
        remoteFs,
        '1',
        Node.Mode.EXCLUSIVE,
        labels,
        new JNLPLauncher(),
        new RetentionStrategy.Always(),
        []
    )
    jenkins.addNode(node)
} else if (changed) {
    node.setNumExecutors(1)
    node.setMode(Node.Mode.EXCLUSIVE)
    node.setLabelString(labels)
    node.setLauncher(new JNLPLauncher())
    node.setRetentionStrategy(new RetentionStrategy.Always())
    node.save()
}

Files.createDirectories(secretPath.getParent())
Files.setPosixFilePermissions(secretPath.getParent(), PosixFilePermissions.fromString('rwx------'))
def secret = JnlpAgentReceiver.DATABASE.getSecretOf(agentName) + System.lineSeparator()
Files.write(secretPath, secret.getBytes(StandardCharsets.US_ASCII))
try {
    Files.setPosixFilePermissions(secretPath, PosixFilePermissions.fromString('rw-------'))
} catch (UnsupportedOperationException ignored) {
    // Docker 的 Linux Volume 支持 POSIX 权限；保留兼容分支便于静态验证。
}

println("Configured Jenkins Linux build agent '${agentName}' with labels '${labels}'.")
