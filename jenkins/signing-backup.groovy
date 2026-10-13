import com.cloudbees.groovy.cps.NonCPS
import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic
import hudson.FilePath
import hudson.model.Cause
import hudson.model.User
import hudson.triggers.TimerTrigger
import jenkins.model.Jenkins
import java.nio.file.Files
import java.nio.file.LinkOption
import java.security.KeyFactory
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.spec.MGF1ParameterSpec
import java.security.spec.X509EncodedKeySpec
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.OAEPParameterSpec
import javax.crypto.spec.PSource
import javax.crypto.spec.SecretKeySpec
import javax.xml.parsers.DocumentBuilderFactory
import javax.xml.transform.TransformerFactory
import javax.xml.transform.dom.DOMSource
import javax.xml.transform.stream.StreamResult

@NonCPS
Map projectFor(def run) {
    def home = Jenkins.get().rootDir
    def directory = new File(home, 'ci-projects')
    // Pipeline 的 GroovyClassLoader 重载会继承 CPS 编译器；同步策略必须使用独立编译配置。
    def loader = new GroovyClassLoader(Jenkins.class.classLoader,
                                     new org.codehaus.groovy.control.CompilerConfiguration())
    try {
        def policy = loader.parseClass(new File(home, 'ci-auto-promotion/CiAutoPromotion.groovy'))
        def matches = []
        directory.listFiles()?.findAll { it.name ==~ /[a-z][a-z0-9-]{0,63}\.json/ }?.sort { it.name }?.each { file ->
            def profile = policy.loadProject(directory, file.name.substring(0, file.name.length() - 5))
            if (['signing_backup', 'signing_maintenance'].any { role -> profile.jobs[role]?.name == run.parent.fullName }) {
                matches << profile
            }
        }
        if (matches.size() != 1) throw new IllegalStateException('实际任务未唯一绑定受信签名备份项目')
        return matches[0]
    } finally { loader.close() }
}

@NonCPS
Map authorize(def run) {
    def profile = projectFor(run)
    def name = run.parent.fullName
    def cause = run.getCause(Cause.UserIdCause.class)
    def user = cause?.userId ? User.getById(cause.userId, false) : null
    def admin = user != null && Jenkins.get().ACL.hasPermission2(user.impersonate2(), Jenkins.ADMINISTER)
    def timer = name == profile.jobs.signing_backup?.name && run.getCause(TimerTrigger.TimerTriggerCause.class) != null
    if (!admin && !timer) { throw new IllegalStateException('备份仅接受管理员或固定定时触发') }
    profile
}

@NonCPS
String digest(byte[] bytes) {
    MessageDigest.getInstance('SHA-256').digest(bytes).encodeHex().toString()
}

@NonCPS
byte[] backupJobConfig(byte[] bytes) {
    if (!new String(bytes, 'UTF-8').contains('org.biouno.unochoice.')) return bytes
    def factory = DocumentBuilderFactory.newInstance()
    factory.setFeature('http://apache.org/xml/features/disallow-doctype-decl', true)
    def document = factory.newDocumentBuilder().parse(new ByteArrayInputStream(bytes))
    def parameters = document.getElementsByTagName('parameters')
    boolean hasCache = false
    for (int index = 0; index < parameters.length; index++) {
        def node = parameters.item(index)
        if (node.getAttribute('class') == 'linked-hash-map' && node.parentNode.nodeName in [
            'org.biouno.unochoice.CascadeChoiceParameter', 'org.biouno.unochoice.DynamicReferenceParameter']) {
            // 页面最后选择不是恢复配置；真实 choices、脚本、权限与 Pipeline 定义仍完整保存。
            while (node.hasChildNodes()) node.removeChild(node.firstChild)
            hasCache = true
        }
    }
    if (!hasCache) return bytes
    def output = new ByteArrayOutputStream()
    TransformerFactory.newInstance().newTransformer().transform(new DOMSource(document), new StreamResult(output))
    output.toByteArray()
}

@NonCPS
Map collectMaterials(def computer, Map profile) {
    def policy = profile.signingBackup
    def home = Jenkins.get().rootDir.toPath().toAbsolutePath().normalize()
    Map files = new TreeMap()
    long size = 0
    def put = { String name, byte[] bytes ->
        size += bytes.length
        if (files.containsKey(name) || files.size() >= 2048 || size > 64 * 1024 * 1024) {
            throw new IllegalStateException('签名快照重复或超过大小限制')
        }
        files[name] = bytes
    }
    def local = { String relative, boolean jobConfig = false ->
        def path = home.resolve(relative).normalize()
        if (!path.startsWith(home)) { throw new IllegalStateException('Controller 快照路径越界') }
        for (def cursor = path; cursor != home; cursor = cursor.parent) {
            if (Files.isSymbolicLink(cursor)) { throw new IllegalStateException('Controller 快照拒绝符号链接') }
        }
        if (!Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS) || Files.size(path) > 64 * 1024 * 1024) {
            throw new IllegalStateException('Controller 快照只接受有界常规文件') }
        byte[] bytes = Files.readAllBytes(path)
        put('controller/' + relative, jobConfig ? backupJobConfig(bytes) : bytes)
    }
    if (policy.scope == 'controller_dr') {
    ['config.xml', 'credentials.xml'].each { local(it) }
    if (Files.exists(home.resolve('scriptApproval.xml'))) { local('scriptApproval.xml') }
    def secretPaths = Files.list(home.resolve('secrets'))
    try {
        secretPaths.sorted().forEach { path -> local('secrets/' + path.fileName.toString()) }
    } finally { secretPaths.close() }
    Jenkins.get().getAllItems(hudson.model.AbstractItem.class).each { item ->
        def path = item.rootDir.toPath().toAbsolutePath().normalize().resolve('config.xml')
        local(home.relativize(path).toString().replace(File.separator, '/'), true)
    }
    Jenkins.get().nodes.each { node -> local('nodes/' + node.nodeName + '/config.xml') }
    def users = home.resolve('users')
    if (Files.isDirectory(users, LinkOption.NOFOLLOW_LINKS)) {
        def userPaths = Files.walk(users, 2)
        try {
            userPaths.filter { it.fileName.toString() in ['config.xml', 'users.xml'] }.sorted().forEach { path ->
                local(home.relativize(path).toString().replace(File.separator, '/'))
            }
        } finally { userPaths.close() }
    }
    ['controller/secrets/master.key', 'controller/secrets/hudson.util.Secret',
     'controller/config.xml', 'controller/credentials.xml'].each {
        if (!files.containsKey(it)) { throw new IllegalStateException('凭据恢复所需文件缺失') }
    }
    }
    if (!(policy.scope in ['controller_dr', 'project_signing'])) throw new IllegalStateException('备份范围无效')
    def root = new FilePath(computer.channel, policy.root)
    def pending = policy.scope == 'controller_dr' ? [[path: root, name: 'signing-state']] : []
    if (policy.scope == 'project_signing') {
        if (!policy.prefixes) throw new IllegalStateException('项目签名范围必须显式声明材料前缀')
        policy.prefixes.each { prefix ->
            // 只遍历已审材料根；绝不先收全局材料再过滤。
            def cursor = root
            prefix.tokenize('/').each { segment ->
                if (cursor.readLink() != null || !cursor.isDirectory() || (cursor.mode() & 0777) != 0700) {
                    throw new IllegalStateException('项目签名范围父目录必须私有且无链接') }
                cursor = cursor.child(segment)
            }
            pending << [path: cursor, name: 'signing-state/' + prefix]
        }
    }
    int visited = 0
    while (pending) {
        def entry = pending.remove(0)
        def path = entry.path
        if (++visited > 4096 || path.readLink() != null) { throw new IllegalStateException('签名状态存在链接或过多文件') }
        if (path.isDirectory()) {
            if ((path.mode() & 0777) != 0700) { throw new IllegalStateException('签名目录必须为 0700') }
            path.list().each { child ->
                def name = child.name
                if (name in ['.', '..'] || name.contains('/') || name.contains('\\')) {
                    throw new IllegalStateException('签名状态文件名无效') }
                pending << [path: child, name: entry.name + '/' + name]
            }
        } else {
            if ((path.mode() & 0777) != 0600 || path.length() > 64 * 1024 * 1024) {
                throw new IllegalStateException('签名文件必须为受限小文件') }
            path.read().withCloseable { input ->
                byte[] bytes = input.readNBytes(64 * 1024 * 1024 + 1)
                if (bytes.length > 64 * 1024 * 1024) { throw new IllegalStateException('签名文件过大') }
                put(entry.name, bytes)
            }
        }
    }
    if (!files) throw new IllegalStateException('签名快照不能为空')
    put('backup-scope.json', JsonOutput.toJson([projectId: profile.id, scope: policy.scope,
        recipientSha256: policy.recipientSha256]).getBytes('UTF-8'))
    files
}

@NonCPS
byte[] archive(Map files) {
    def output = new ByteArrayOutputStream()
    def manifest = new TreeMap()
    files.each { name, bytes -> manifest[name] = [bytes: bytes.length, sha256: digest(bytes)] }
    def content = new TreeMap(files)
    content['manifest.json'] = JsonOutput.toJson(manifest).getBytes('UTF-8')
    new ZipOutputStream(output).withCloseable { zip ->
        content.each { name, bytes ->
            def entry = new ZipEntry(name); entry.setTime(0)
            zip.putNextEntry(entry); zip.write(bytes); zip.closeEntry()
        }
    }
    output.toByteArray()
}

@NonCPS
Map seal(byte[] bytes, String publicPem) {
    String format = 'sxmqlive-signing-backup-v2'
    if (!bytes.length || bytes.length > 64 * 1024 * 1024 || publicPem.length() > 8192) {
        throw new IllegalStateException('备份或公钥大小无效') }
    def der = Base64.decoder.decode(publicPem.replace('-----BEGIN PUBLIC KEY-----', '')
        .replace('-----END PUBLIC KEY-----', '').replaceAll(/\s/, ''))
    def publicKey = KeyFactory.getInstance('RSA').generatePublic(new X509EncodedKeySpec(der))
    if (publicKey.modulus.bitLength() != 3072 || publicKey.publicExponent != 65537G) {
        throw new IllegalStateException('备份公钥必须为 RSA3072 / 65537') }
    String recipient = digest(publicKey.encoded)
    byte[] key = new byte[32], nonce = new byte[12]
    def random = new SecureRandom(); random.nextBytes(key); random.nextBytes(nonce)
    try {
        def aad = (format + ':' + recipient).getBytes('UTF-8')
        def aes = Cipher.getInstance('AES/GCM/NoPadding')
        aes.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, 'AES'), new GCMParameterSpec(128, nonce))
        aes.updateAAD(aad)
        byte[] encrypted = aes.doFinal(bytes)
        // 当场用临时数据密钥验证 AES 字节往返；RSA 恢复私钥始终不进入服务器。
        def verify = Cipher.getInstance('AES/GCM/NoPadding')
        verify.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, 'AES'), new GCMParameterSpec(128, nonce))
        verify.updateAAD(aad)
        if (!Arrays.equals(bytes, verify.doFinal(encrypted))) { throw new IllegalStateException('备份加密校验失败') }
        def rsa = Cipher.getInstance('RSA/ECB/OAEPPadding')
        rsa.init(Cipher.ENCRYPT_MODE, publicKey,
            new OAEPParameterSpec('SHA-256', 'MGF1', MGF1ParameterSpec.SHA256, PSource.PSpecified.DEFAULT))
        def b64 = { byte[] value -> Base64.encoder.encodeToString(value) }
        return [format: format, keyAlgorithm: 'RSA-OAEP-SHA256', recipientSha256: recipient,
            wrappedKey: b64(rsa.doFinal(key)), nonce: b64(nonce),
            tag: b64(Arrays.copyOfRange(encrypted, encrypted.length - 16, encrypted.length)),
            ciphertext: b64(Arrays.copyOf(encrypted, encrypted.length - 16))]
    } finally { Arrays.fill(key, (byte) 0) }
}

@NonCPS
void status(def run, Map value) {
    def profile = authorize(run)
    def policy = profile.signingBackup
    def identity = [projectId: profile.id, scope: policy.scope, recipientSha256: policy.recipientSha256]
    def url = profile.repositories[policy.repository].https.replaceAll(/\.git$/, '') + '/tree/' + policy.branch
    if (!(value.status in ['running', 'pending', 'synced', 'unchanged'])) {
        throw new IllegalStateException('备份状态无效') }
    def file = new File(Jenkins.get().rootDir, profile.id == 'sxmqlive' ? 'signing-backup-status.json' : 'signing-backup-status-' + profile.id + '.json')
    if (Files.isSymbolicLink(file.toPath())) { throw new IllegalStateException('备份状态拒绝符号链接') }
    def previous = [:]
    try {
        if (file.isFile() && file.length() < 16384) { previous = new JsonSlurperClassic().parseText(file.getText('UTF-8')) }
    } catch (Exception ignored) { /* 损坏的公开状态不阻止下一次完整备份。 */ }
    boolean sameIdentity = previous instanceof Map && identity.every { key, expected -> previous[key] == expected }
    boolean legacy = previous instanceof Map && !identity.keySet().any { previous.containsKey(it) } && profile.id == 'sxmqlive' &&
        policy.scope == 'controller_dr' && policy.legacyRecipientSha256 == policy.recipientSha256
    def prior = sameIdentity || legacy ? previous.lastSuccess : null
    def lastSuccess = prior instanceof Map && prior.gitCommit ==~ /[a-f0-9]{40}/ &&
        prior.snapshot ==~ /snapshots\/[A-Za-z0-9_-]+\.enc\.json/ ?
        [at: prior.at, gitCommit: prior.gitCommit, snapshot: prior.snapshot,
         url: url] : null
    def next = identity + [status: value.status, checkedAt: new Date().format("yyyy-MM-dd'T'HH:mm:ssXXX", TimeZone.getTimeZone('UTC')),
        jobUrl: run.parent.url, build: run.number, lastSuccess: lastSuccess]
    if (value.status in ['synced', 'unchanged']) {
        if (!identity.every { key, expected -> value[key] == expected } || value.branch != policy.branch || !(value.gitCommit ==~ /[a-f0-9]{40}/) ||
            !(value.snapshot ==~ /snapshots\/[A-Za-z0-9_-]+\.enc\.json/)) {
            throw new IllegalStateException('Git 备份结果无效') }
        next.lastSuccess = [at: next.checkedAt, gitCommit: value.gitCommit, snapshot: value.snapshot,
            url: url]
    }
    new hudson.util.AtomicFileWriter(file, 'UTF-8').withCloseable { writer ->
        writer.write(JsonOutput.toJson(next)); writer.commit()
    }
}

@NonCPS
Map exportSnapshot(def run, String nodeName, String workspace) {
    def profile = authorize(run)
    def policy = profile.signingBackup
    if (nodeName != policy.node) { throw new IllegalStateException('备份仅使用现有签名维护节点') }
    def computer = Jenkins.get().getComputer(nodeName)
    if (computer?.channel == null) { throw new IllegalStateException('签名维护节点未连接') }
    def base = new FilePath(computer.channel, workspace)
    def recipient = base.child(policy.recipientPath)
    if (recipient.readLink() != null || recipient.length() > 8192) { throw new IllegalStateException('备份公钥无效') }
    String publicPem = recipient.readToString()
    def der = Base64.decoder.decode(publicPem.replace('-----BEGIN PUBLIC KEY-----', '')
        .replace('-----END PUBLIC KEY-----', '').replaceAll(/\s/, ''))
    def key = KeyFactory.getInstance('RSA').generatePublic(new X509EncodedKeySpec(der))
    if (digest(key.encoded) != policy.recipientSha256) throw new IllegalStateException('恢复接收人未获可信项目授权')
    def first = collectMaterials(computer, profile)
    def second = collectMaterials(computer, profile)
    if (first.keySet() != second.keySet() || first.any { name, bytes -> !Arrays.equals(bytes, second[name]) }) {
        throw new IllegalStateException('快照期间材料发生变化，保留待备份状态') }
    byte[] bytes = archive(first)
    def envelope = seal(bytes, publicPem)
    def directory = base.child('build/signing-backup'); directory.mkdirs()
    directory.child('snapshot.enc.json').write(JsonOutput.toJson(envelope) + '\n', 'UTF-8')
    // 仅密文和整个复合快照摘要离开 NonCPS，不返回单项私钥/密码摘要。
    [contentSha256: digest(bytes), entries: first.size(), projectId: profile.id, scope: policy.scope, recipientSha256: policy.recipientSha256]
}

def publish() {
    def project = authorize(currentBuild.rawBuild)
    dir(env.APP_SOURCE_ROOT) {
        status(currentBuild.rawBuild, [status: 'running'])
        try {
            def sourceCommit = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
            if (!(sourceCommit ==~ /[a-f0-9]{40}/)) { error '实际检出的应用提交无效' }
            def snapshot = exportSnapshot(currentBuild.rawBuild, env.NODE_NAME, env.APP_SOURCE_ROOT)
            withCredentials([sshUserPrivateKey(credentialsId: project.credentials.scm, keyFileVariable: 'BACKUP_SSH_KEY')]) {
                withEnv(["BACKUP_CONTENT_SHA256=${snapshot.contentSha256}", "BACKUP_SOURCE_COMMIT=${sourceCommit}", "BACKUP_PROJECT_ID=${project.id}"]) {
                    sh '''set +x; set -eu; umask 077
                        export GIT_SSH_COMMAND="ssh -i \\"$BACKUP_SSH_KEY\\" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes"
                        python3 "$CI_TOOL_ROOT/signing/publish_signing_backup.py" --project "$BACKUP_PROJECT_ID" --snapshot build/signing-backup/snapshot.enc.json \
                            --recipient-recovery signing/backups/recipient-recovery.enc.json \
                            --content-sha256 "$BACKUP_CONTENT_SHA256" --source-commit "$BACKUP_SOURCE_COMMIT" \
                            --build-number "$BUILD_NUMBER" --report build/signing-backup/report.json
                    '''
                }
            }
            def reportText = readFile('build/signing-backup/report.json')
            def report = new JsonSlurperClassic().parseText(reportText)
            status(currentBuild.rawBuild, report)
            archiveArtifacts artifacts: 'build/signing-backup/report.json', fingerprint: true
            echo "签名加密备份已同步 backup/signing：${report.gitCommit}"
        } catch (Exception failure) {
            echo "签名备份步骤失败：${failure.class.name}"
            status(currentBuild.rawBuild, [status: 'pending'])
            // 外部签名变更无法回滚；只重试加密备份，不重跑供应商写入。
            error '签名备份未同步 Git，已保留待备份状态；定时备份会重试，不会重复注册设备或更新证书'
        }
    }
}

return this
