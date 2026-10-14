import groovy.json.JsonSlurperClassic
import hudson.model.Node
import hudson.slaves.DumbSlave
import hudson.slaves.JNLPLauncher
import hudson.slaves.RetentionStrategy
import jenkins.model.Jenkins
import jenkins.slaves.JnlpAgentReceiver
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Paths
import java.nio.file.StandardCopyOption
import java.nio.file.StandardOpenOption
import java.nio.file.attribute.PosixFilePermissions
import java.security.MessageDigest

def filename = System.getenv('MOBILE_CI_DEPLOYMENT_FILE')
if (!filename) { return }
if (filename != '/var/jenkins_home/ci-deployment/nodes.json') {
    throw new IllegalStateException('Deployment nodes must use the fixed administrator mount.')
}
def checkPrivate = { p, directory ->
    if (Files.isSymbolicLink(p) || !(directory ? Files.isDirectory(p, LinkOption.NOFOLLOW_LINKS) : Files.isRegularFile(p, LinkOption.NOFOLLOW_LINKS)) ||
        Files.getAttribute(p, 'unix:uid') != 1000 ||
        Files.getPosixFilePermissions(p) != PosixFilePermissions.fromString(directory ? 'rwx------' : 'rw-------') ||
        (!directory && (Files.getAttribute(p, 'unix:nlink') != 1 || Files.size(p) > 65536L))) {
        throw new IllegalStateException('Deployment state must be isolated 1000-owned private regular state.')
    }
}
def source = Paths.get(filename)
checkPrivate(source.parent, true)
checkPrivate(source.parent.parent, true)
checkPrivate(source, false)
def bytes = Files.readAllBytes(source)
def digest = { content -> MessageDigest.getInstance('SHA-256').digest(content).encodeHex().toString() }
def expectedNodes = System.getenv('MOBILE_CI_NODES_SHA256')
def expectedDeployment = System.getenv('MOBILE_CI_DEPLOYMENT_SHA256')
if (!(expectedNodes ==~ /[0-9a-f]{64}/) || !(expectedDeployment ==~ /[0-9a-f]{64}/) || digest(bytes) != expectedNodes) {
    throw new IllegalStateException('Deployment nodes differ from the administrator-rendered Compose digest.')
}
def deploymentSource = source.parent.resolve('deployment.json')
checkPrivate(deploymentSource, false)
if (digest(Files.readAllBytes(deploymentSource)) != expectedDeployment) {
    throw new IllegalStateException('Deployment manifest digest differs from the rendered nodes.')
}
def manifest = new JsonSlurperClassic().parseText(new String(bytes, StandardCharsets.UTF_8))
if (!(manifest instanceof Map) || manifest.keySet() != ['version', 'deploymentId', 'deploymentSha256', 'nodes'] as Set ||
    manifest.version != 1 || !(manifest.deploymentId ==~ /[a-z][a-z0-9-]{0,63}/) || manifest.deploymentSha256 != expectedDeployment ||
    !(manifest.nodes instanceof List) || manifest.nodes.isEmpty()) {
    throw new IllegalStateException('Unsupported derived deployment nodes schema.')
}
def jenkins = Jenkins.get()
def checked = []
def ids = [] as Set
def names = [] as Set
manifest.nodes.each { agent ->
    if (!(agent instanceof Map) || agent.keySet() != ['id', 'name', 'labels', 'capabilities', 'target', 'remoteFS', 'executors'] as Set ||
        !(agent.name instanceof String) || !(agent.name ==~ /[A-Za-z0-9_.-]{1,64}/) || agent.name in ['.', '..'] ||
        !(agent.id instanceof String) || !(agent.id ==~ /[a-z][a-z0-9-]{0,63}/) || ids.contains(agent.id) || names.contains(agent.name) ||
        agent.executors != 1 || !(agent.labels instanceof List) || agent.labels.isEmpty() ||
        agent.labels.any { !(it instanceof String) || !(it ==~ /[A-Za-z0-9_-]+/) } || agent.labels.toSet().size() != agent.labels.size() ||
        !(agent.capabilities instanceof List) || agent.capabilities.isEmpty() || agent.capabilities.toSet().size() != agent.capabilities.size() ||
        agent.capabilities.any { !(it in ['android', 'ios', 'harmony', 'ci', 'maintenance']) } ||
        !(agent.target instanceof Map) || agent.target.keySet() != ['os', 'arch'] as Set ||
        !(agent.target.os in ['linux', 'darwin']) || !(agent.target.arch in ['amd64', 'arm64']) ||
        ('ios' in agent.capabilities && agent.target.os != 'darwin') ||
        ('harmony' in agent.capabilities && (agent.target.os != 'linux' || agent.target.arch != 'amd64')) ||
        ('android' in agent.capabilities && agent.target.os == 'linux' && agent.target.arch != 'amd64') ||
        ('pr-builder' in agent.labels && agent.labels.any { it in ['android-builder', 'mac-build', 'linux-build', 'signing-maintenance'] }) ||
        !(agent.remoteFS instanceof String) || !(agent.remoteFS ==~ /\/[A-Za-z0-9_.\/-]+/) ||
        agent.remoteFS.split('/').drop(1).any { it in ['.', '..', ''] } ||
        !(agent.target.os == 'linux' ? agent.remoteFS.startsWith('/home/jenkins/') : (agent.remoteFS.startsWith('/Users/Shared/') || agent.remoteFS.startsWith('/private/var/lib/mobile-ci/')))) {
        throw new IllegalStateException('Invalid derived agent identity/capability/path/isolation.')
    }
    ids.add(agent.id); names.add(agent.name)
    def node = jenkins.getNode(agent.name)
    def labels = agent.labels.join(' ')
    if (node != null && (!(node instanceof DumbSlave) || node.getRemoteFS() != agent.remoteFS)) {
        throw new IllegalStateException('Existing agent conflicts with requested identity/type/remoteFs; preserve it.')
    }
    def changed = node != null && (node.getNumExecutors() != 1 || node.getMode() != Node.Mode.EXCLUSIVE ||
        node.getLabelString().trim() != labels || !(node.getLauncher() instanceof JNLPLauncher) ||
        !(node.getRetentionStrategy() instanceof RetentionStrategy.Always))
    if (changed && node.toComputer()?.isIdle() == false) {
        throw new IllegalStateException('Existing agent is busy; refuse configuration changes.')
    }
    def secret = Paths.get('/run/mobile-ci', agent.id, 'agent.secret')
    checkPrivate(secret.parent, true)
    def secretValue = (JnlpAgentReceiver.DATABASE.getSecretOf(agent.name) + System.lineSeparator()).getBytes(StandardCharsets.US_ASCII)
    if (Files.exists(secret, LinkOption.NOFOLLOW_LINKS)) {
        checkPrivate(secret, false)
        if (!Arrays.equals(Files.readAllBytes(secret), secretValue)) {
            throw new IllegalStateException('Existing agent secret differs; explicit administrator migration required.')
        }
    }
    checked << [agent: agent, node: node, changed: changed, labels: labels, secret: secret, secretValue: secretValue]
}
// 全部配置、已有节点和私有材料预检通过后才更改；不删除清单以外的节点。
if (jenkins.getNumExecutors() != 0 && jenkins.toComputer()?.isIdle() == false) {
    throw new IllegalStateException('Controller is busy; preserve it for administrator review.')
}
jenkins.setNumExecutors(0)
checked.each { item ->
    def node = item.node
    if (node == null) {
        node = new DumbSlave(item.agent.name, '管理员清单独立节点', item.agent.remoteFS, '1',
            Node.Mode.EXCLUSIVE, item.labels, new JNLPLauncher(), new RetentionStrategy.Always(), [])
        jenkins.addNode(node)
    } else if (item.changed) {
        node.setNumExecutors(1); node.setMode(Node.Mode.EXCLUSIVE); node.setLabelString(item.labels)
        node.setLauncher(new JNLPLauncher()); node.setRetentionStrategy(new RetentionStrategy.Always()); node.save()
    }
    if (!Files.exists(item.secret, LinkOption.NOFOLLOW_LINKS)) {
        def temp = Files.createTempFile(item.secret.parent, '.agent-secret-', '.tmp', PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString('rw-------')))
        try {
            Files.write(temp, item.secretValue, StandardOpenOption.WRITE, LinkOption.NOFOLLOW_LINKS)
            // 私有目录只有Controller可写；无覆盖move保留任何竞争创建的材料。
            Files.move(temp, item.secret)
        } finally {
            Files.deleteIfExists(temp)
        }
    }
}
jenkins.save()
println('Configured administrator deployment agents without deleting existing nodes.')
