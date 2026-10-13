import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic
import hudson.model.*
import hudson.util.Secret
import hudson.FilePath
import java.util.concurrent.Callable
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.concurrent.atomic.AtomicBoolean
import jenkins.model.Jenkins
import org.kohsuke.stapler.StaplerRequest2
import org.kohsuke.stapler.StaplerResponse2
import org.kohsuke.stapler.interceptor.RequirePOST
import java.nio.charset.StandardCharsets
import java.security.MessageDigest

// stdout/stderr 只进当前请求的受限内存，不使用构建或脚本审计日志。
class SigningCaptureOutput extends ByteArrayOutputStream {
    boolean oversized = false
    @Override synchronized void write(int value) {
        if (size() < 16384) { super.write(value) } else { oversized = true }
    }
    @Override synchronized void write(byte[] bytes, int offset, int length) {
        if (size() + length <= 16384) { super.write(bytes, offset, length) } else { oversized = true }
    }
}

// 页面与接口共享 Jenkins 会话；维护只调度固定 Job，采集只在固定 Mac 节点读取设备。
class SxmqliveSigningPortal implements RootAction {
    static final String INVENTORY = 'build/signing-inventory/inventory.json'
    final String jobName
    final String macNode
    final String hdcPath
    final String backupUrl
    final String projectId
    final String backupScope
    final String recipientSha256
    final String legacyRecipientSha256

    SxmqliveSigningPortal(Map project) {
        projectId = project.id
        backupScope = project.signingBackup?.scope
        recipientSha256 = project.signingBackup?.recipientSha256
        legacyRecipientSha256 = project.signingBackup?.legacyRecipientSha256
        jobName = project.jobs?.signing_maintenance?.name
        macNode = project.portal?.macNode
        hdcPath = project.portal?.hdcPath
        backupUrl = project.portal?.backupUrl
        if (!(projectId ==~ /[a-z][a-z0-9-]{0,63}/) ||
            !(backupScope in ['controller_dr', 'project_signing']) ||
            !(recipientSha256 ==~ /[0-9a-f]{64}/) ||
            !(jobName instanceof String) || jobName.split('/').any {
                !(it ==~ /[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_. \u4e00-\u9fff-]*/) } ||
            jobName.tokenize('/').any { it in ['.', '..'] } ||
            !(macNode ==~ /[A-Za-z0-9_.-]+/) ||
            !(hdcPath instanceof String) || !new File(hdcPath).isAbsolute() ||
            hdcPath.tokenize('/').any { it in ['.', '..'] } ||
            !hdcPath.endsWith('/hdc') || hdcPath.any { Character.isISOControl((char) it) } ||
            !(backupUrl instanceof String)) {
            throw new IllegalArgumentException('管理员门户策略无效')
        }
        def uri = new URI(backupUrl)
        if (uri.scheme != 'https' || !uri.host || uri.userInfo || uri.fragment) {
            throw new IllegalArgumentException('备份链接必须是受信 HTTPS 地址')
        }
    }
    private final Map requests = new LinkedHashMap()
    private final AtomicBoolean capturing = new AtomicBoolean(false)

    String getIconFileName() { Jenkins.get().hasPermission(Jenkins.READ) ? 'symbol-device-mobile' : null }
    String getDisplayName() { '品牌签名与设备管理' }
    String getUrlName() { 'signing' }

    def maintenanceJob() {
        Jenkins.get().checkPermission(Jenkins.READ)
        def job = Jenkins.get().getItemByFullName(jobName)
        if (job == null) { throw new IllegalArgumentException('签名维护任务尚未配置') }
        job.checkPermission(Item.READ)
        return job
    }

    static void headers(StaplerResponse2 rsp) {
        rsp.setHeader('Cache-Control', 'no-store')
        rsp.setHeader('X-Content-Type-Options', 'nosniff')
        rsp.setHeader('Referrer-Policy', 'same-origin')
        rsp.setHeader('X-Frame-Options', 'DENY')
    }

    static void json(StaplerResponse2 rsp, def value, int status = 200) {
        headers(rsp)
        rsp.setStatus(status)
        rsp.setContentType('application/json;charset=UTF-8')
        rsp.writer.write(JsonOutput.toJson(value))
    }

    void asset(StaplerResponse2 rsp, String name) {
        maintenanceJob()
        def types = ['index.html':'text/html', 'app.js':'text/javascript', 'style.css':'text/css']
        if (!types.containsKey(name)) { rsp.sendError(404); return }
        def root = new File(Jenkins.get().rootDir, 'signing-portal').canonicalFile
        def file = new File(root, name)
        if (!file.isFile() || file.canonicalFile.parentFile != root || java.nio.file.Files.isSymbolicLink(file.toPath())) {
            rsp.sendError(503, '管理页面尚未安装'); return
        }
        headers(rsp)
        rsp.setHeader('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        rsp.setContentType(types[name] + ';charset=UTF-8')
        rsp.outputStream.write(file.bytes)
    }

    void doIndex(StaplerRequest2 req, StaplerResponse2 rsp) {
        if (!req.requestURI.endsWith('/')) { rsp.sendRedirect(req.contextPath + '/signing/'); return }
        asset(rsp, 'index.html')
    }

    void doDynamic(StaplerRequest2 req, StaplerResponse2 rsp) {
        asset(rsp, req.restOfPath.replaceFirst('^/', ''))
    }

    Map snapshot(def job) {
        // plan 构建没有查询产物，不能把 lastSuccessfulBuild 当作最新有效快照。
        def skipped = []
        for (def run : job.builds.limit(30)) {
            if (run.isBuilding()) { continue }
            try {
                def file = run.artifactManager.root().child(INVENTORY)
                if (!file.isFile()) { continue }
                byte[] bytes
                file.open().withCloseable { bytes = it.readNBytes(2 * 1024 * 1024 + 1) }
                if (bytes.length > 2 * 1024 * 1024) { throw new IllegalArgumentException() }
                def document = new JsonSlurperClassic().parseText(new String(bytes, StandardCharsets.UTF_8))
                if (!(document instanceof Map) || !(document.brands instanceof List) ||
                    !(document.devices instanceof Map) || !(document.devices.ios instanceof List) ||
                    !(document.devices.harmony instanceof List)) { throw new IllegalArgumentException() }
                return [inventory:document, snapshot:[build:run.number, url:run.url + 'artifact/' + INVENTORY], skippedSnapshots:skipped.size()]
            } catch (Exception ignored) {
                // 损坏的查询记录不能阻断读取旧快照或重新查询，也不回传包含原始内容的解析异常。
                skipped << run.number
            }
        }
        return [inventory:[generatedAt:null, brands:[], devices:[ios:[], harmony:[]]], snapshot:null, skippedSnapshots:skipped.size()]
    }

    boolean busy() {
        Jenkins.get().queue.items.length > 0 || Jenkins.get().computers.any { it.countBusy() > 0 }
    }

    Map backupStatus() {
        def unavailable = [status:'pending', checkedAt:null, lastSuccess:null]
        def file = new File(Jenkins.get().rootDir, projectId == 'sxmqlive' ? 'signing-backup-status.json' : 'signing-backup-status-' + projectId + '.json')
        try {
            def path = file.toPath()
            def nofollow = java.nio.file.LinkOption.NOFOLLOW_LINKS
            String attributes = 'unix:dev,ino,uid,nlink,mode,size,lastModifiedTime,ctime,isRegularFile'
            def before
            try { before = java.nio.file.Files.readAttributes(path, attributes, nofollow) }
            catch (java.nio.file.NoSuchFileException absent) { return [status:'none', checkedAt:null, lastSuccess:null] }
            def owner = java.nio.file.Files.getAttribute(Jenkins.get().rootDir.toPath(), 'unix:uid', nofollow)
            if (!before.isRegularFile || before.uid != owner || before.nlink != 1 ||
                (before.mode & 07777) != 0600 || before.size > 16384) { return unavailable }
            byte[] bytes
            java.nio.file.Files.newInputStream(path, nofollow)
                .withCloseable { bytes = it.readNBytes(16385) }
            // 状态由原子 writer 更新；读到跨版本文件或权限变更时只显示 pending。
            def after = java.nio.file.Files.readAttributes(path, attributes, nofollow)
            if (before != after || bytes.length != before.size || bytes.length > 16384) { return unavailable }
            def document = new JsonSlurperClassic().parseText(new String(bytes, StandardCharsets.UTF_8))
            if (!(document instanceof Map) || !(document.status in ['synced','unchanged','pending','running'])) {
                return unavailable
            }
            boolean legacy = !['projectId', 'scope', 'recipientSha256'].any { document.containsKey(it) }
            if (legacy ? !(projectId == 'sxmqlive' && backupScope == 'controller_dr' && recipientSha256 == legacyRecipientSha256) :
                document.projectId != projectId || document.scope != backupScope || document.recipientSha256 != recipientSha256) {
                return unavailable
            }
            def result = [status:document.status, checkedAt:java.time.Instant.parse(document.checkedAt).toString(), lastSuccess:null]
            if (document.lastSuccess != null) {
                def success = document.lastSuccess
                if (!(success instanceof Map) || !(success.gitCommit instanceof String) ||
                    !(success.gitCommit ==~ /[0-9a-f]{40}/) || !(success.snapshot instanceof String) ||
                    !(success.snapshot ==~ /snapshots\/[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}\.enc\.json/)) { return unavailable }
                // 只返回公开恢复证据，状态文件的错误、摘要及凭据字段不透传到 H5。
                result.lastSuccess = [at:java.time.Instant.parse(success.at).toString(), gitCommit:success.gitCommit,
                    snapshot:success.snapshot, url:backupUrl]
            }
            if (result.status in ['synced','unchanged'] && result.lastSuccess == null) { return unavailable }
            return result
        } catch (Exception ignored) { return unavailable }
    }

    void doState(StaplerRequest2 req, StaplerResponse2 rsp) {
        def job = maintenanceJob()
        try {
            def state = snapshot(job)
            json(rsp, state + [user:Jenkins.getAuthentication2().name,
                              canMaintain:Jenkins.get().hasPermission(Jenkins.ADMINISTER) && job.hasPermission(Item.BUILD),
                              busy:busy(), capture:captureCapabilities(job), backup:backupStatus()])
        } catch (IllegalArgumentException ex) { json(rsp, [error:ex.message], 503) }
    }

    Map captureCapabilities(def job) {
        boolean allowed = Jenkins.get().hasPermission(Jenkins.ADMINISTER) && job.hasPermission(Item.BUILD)
        def computer = allowed ? Jenkins.get().getComputer(macNode) : null
        boolean online = computer != null && computer.isOnline() && computer.channel != null
        // state 不执行 Agent 命令；在线表示可尝试采集，HDC 和手机授权由实际采集核验。
        return [iosAvailable:false, iosReason:'https-required', harmonyAvailable:allowed && online,
                harmonyReason:!allowed ? 'permission-required' : online ? null : 'agent-offline']
    }

    static String validateCapture(Map input) {
        if (input.keySet().any { !(it in ['platform','target']) } || !(input.platform in ['ios','harmony'])) {
            throw new IllegalArgumentException('请选择有效的设备平台')
        }
        if (!input.containsKey('target')) { return '' }
        if (!(input.target instanceof String)) { throw new IllegalArgumentException('无线设备地址格式不正确') }
        String target = input.target.trim()
        def match = target =~ /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3}):(\d{1,5})$/
        if (!match.matches()) { throw new IllegalArgumentException('请填写已连接手机的局域网 IPv4 和端口') }
        def parts = (1..4).collect { match[0][it].toInteger() }
        int port = match[0][5].toInteger()
        if (parts.any { it > 255 } || port < 1 || port > 65535 ||
            !(parts[0] == 10 || (parts[0] == 172 && parts[1] in 16..31) || (parts[0] == 192 && parts[1] == 168))) {
            throw new IllegalArgumentException('仅允许已连接手机的局域网 IPv4 和有效端口')
        }
        return parts.join('.') + ':' + port
    }

    static Map runHdc(def launcher, List arguments, String hdcPath) {
        def output = new SigningCaptureOutput()
        def process = launcher.launch().cmds(([hdcPath] + arguments).collect { it.toString() })
            .quiet(true).stdout(output).stderr(output).start()
        long started = System.nanoTime()
        int exit
        try { exit = process.joinWithTimeout(3, TimeUnit.SECONDS, TaskListener.NULL) }
        catch (InterruptedException ex) {
            // 全局超时取消 worker 时也终止本次 HDC；清理异常不能暴露设备输出。
            try { process.kill() } catch (Exception ignored) { }
            throw ex
        }
        if (exit != 0) {
            return [code:System.nanoTime() - started >= 3000000000L ? 'capture-timeout' : 'hdc-failed']
        }
        if (output.oversized) { return [code:'hdc-failed'] }
        return [text:output.toString('UTF-8')]
    }

    static List captureTargets(String text) {
        text.readLines().collect { it.trim() }.findAll {
            it != 'Empty' && it != '[Empty]' && it ==~ /[a-zA-Z0-9._:-]{1,100}/
        }.unique(false)
    }

    static String captureUdid(String text) {
        String value = text.trim().replaceFirst(/(?i)^udid(?: of current device is)?\s*:\s*/, '')
        value ==~ /[a-zA-Z0-9-]{16,64}/ ? value : null
    }

    static Map captureHarmony(def computer, String target, String hdcPath) {
        if (!new FilePath(computer.channel, hdcPath).exists()) { return [code:'hdc-unavailable'] }
        // Jenkins 标准 Launcher 已处理 Agent 通道，不在远端动态编译 Groovy 或审计完整 UDID。
        def launcher = computer.node.createLauncher(TaskListener.NULL)
        def list = runHdc(launcher, ['list','targets'], hdcPath)
        if (list.code) { return list }
        def connected = captureTargets(list.text)
        if (!connected) { return [code:'device-not-connected'] }
        if (target && !connected.contains(target)) { return [code:'target-not-connected'] }
        if (!target && connected.size() != 1) { return [code:'multiple-devices'] }
        String selected = target ?: connected[0]
        def identity = runHdc(launcher, ['-t',selected,'shell','bm','get','-u'], hdcPath)
        if (identity.code) { return identity }
        String value = captureUdid(identity.text)
        if (!value) { return [code:'invalid-device-identity'] }
        def model = runHdc(launcher, ['-t',selected,'shell','param','get','const.product.model'], hdcPath)
        if (model.code) { return model }
        String name = model.text.trim()
        if (!name || name.length() > 100 || name.any { Character.isISOControl((char) it) }) {
            return [code:'invalid-device-name']
        }
        return [name:name, udid:value]
    }

    static final Map CAPTURE_ERRORS = [
        'https-required':'iOS 自动采集需要手机可访问的 HTTPS 域名；当前请手动填写 UDID',
        'agent-offline':'研发 Mac 未连接 Jenkins，请联系研发检查配置的 Mac 节点',
        'capture-busy':'已有设备正在采集，请稍后再试',
        'hdc-unavailable':'研发 Mac 的服务账号无法运行 HDC，请联系研发检查 DevEco 安装',
        'device-not-connected':'研发 Mac 尚未连接手机，请用 USB 或在研发电脑授权无线调试后重试',
        'target-not-connected':'该无线设备尚未连接研发 Mac，请先在研发电脑完成 HDC 连接和手机授权',
        'multiple-devices':'研发 Mac 已连接多台手机，请只保留一台或填写已连接手机的无线地址',
        'invalid-device-identity':'未能读取有效 UDID，请确认手机已授权调试，或手动填写',
        'invalid-device-name':'未能读取设备名称，请手动填写设备名称和 UDID',
        'hdc-failed':'HDC 读取失败，请检查手机调试授权，或手动填写',
        'capture-timeout':'读取设备超时，请检查连接后重试，或手动填写'
    ]

    static void captureFailure(StaplerResponse2 rsp, String platform, String code, int status = 503) {
        json(rsp, [state:'unavailable', platform:platform, code:code, message:CAPTURE_ERRORS[code]], status)
    }

    @RequirePOST
    void doCapture(StaplerRequest2 req, StaplerResponse2 rsp) {
        def job = maintenanceJob()
        Jenkins.get().checkPermission(Jenkins.ADMINISTER)
        job.checkPermission(Item.BUILD)
        if (!req.contentType?.toLowerCase()?.startsWith('application/json')) {
            json(rsp, [state:'unavailable',code:'invalid-request',message:'请求格式不正确'], 415); return
        }
        def bytes = req.inputStream.readNBytes(1025)
        if (bytes.length > 1024) {
            json(rsp, [state:'unavailable',code:'invalid-request',message:'请求超出大小限制'], 413); return
        }
        Map input
        String target
        try {
            def parsed = new JsonSlurperClassic().parseText(new String(bytes, StandardCharsets.UTF_8))
            if (!(parsed instanceof Map)) { throw new IllegalArgumentException('设备采集请求无效') }
            input = parsed
            target = validateCapture(input)
        } catch (Exception ignored) {
            json(rsp, [state:'unavailable',code:'invalid-request',message:'设备平台或无线地址无效'], 400); return
        }
        if (input.platform == 'ios') { captureFailure(rsp, 'ios', 'https-required'); return }
        def computer = Jenkins.get().getComputer(macNode)
        def channel = computer?.channel
        if (computer == null || !computer.isOnline() || channel == null) {
            captureFailure(rsp, 'harmony', 'agent-offline'); return
        }
        if (!capturing.compareAndSet(false, true)) { captureFailure(rsp, 'harmony', 'capture-busy', 409); return }
        def future
        try {
            future = Computer.threadPoolForRemoting.submit({ captureHarmony(computer, target, hdcPath) } as Callable)
            // Agent 文件检查及全部读取整体最多等待 13 秒；各固定 HDC 命令另有 3 秒进程超时。
            def captured = future.get(13, TimeUnit.SECONDS)
            if (CAPTURE_ERRORS.containsKey(captured.code)) {
                captureFailure(rsp, 'harmony', captured.code, captured.code == 'capture-timeout' ? 504 : 503); return
            }
            // 完整 UDID 只回传当前授权请求，不保存到 Job、日志、产物或全局页面状态。
            if (!(captured.udid instanceof String) || !(captured.udid ==~ /[a-zA-Z0-9-]{16,64}/) ||
                !(captured.name instanceof String) || !captured.name || captured.name.length() > 100 ||
                captured.name.any { Character.isISOControl((char) it) }) { throw new IllegalArgumentException() }
            json(rsp, [state:'ready',platform:'harmony',name:captured.name,udid:captured.udid])
        } catch (TimeoutException ignored) {
            future?.cancel(true)
            captureFailure(rsp, 'harmony', 'capture-timeout', 504)
        } catch (InterruptedException ignored) {
            future?.cancel(true)
            Thread.currentThread().interrupt()
            captureFailure(rsp, 'harmony', 'hdc-failed')
        } catch (Exception ignored) {
            // Agent 异常可能含命令输出；不可把异常内容写日志或返回浏览器。
            captureFailure(rsp, 'harmony', 'hdc-failed')
        } finally { capturing.set(false) }
    }

    static boolean eligible(Map row, String platform) {
        row.platform == platform && row.configurationStatus == 'configured' &&
            row.channelEnabled?.get(platform == 'ios' ? 'pgyer' : 'designated') == true
    }

    static Map validate(Map input, Map inventory) {
        def keys = ['operation','platform','source','brands','deviceIds','name','udid','mode','requestId','recoverOnly']
        if (input.keySet().any { !(it in keys) } || !(input.requestId instanceof String) ||
            !(input.requestId ==~ /[a-zA-Z0-9-]{16,80}/) ||
            (input.containsKey('recoverOnly') && !(input.recoverOnly instanceof Boolean))) {
            throw new IllegalArgumentException('操作请求无效，请重新打开页面')
        }
        if (input.operation == 'inventory') {
            if (input.mode != 'plan') { throw new IllegalArgumentException('刷新查询仅允许读取状态') }
            return [operation:'inventory', platform:'all', mode:'plan', source:'shared', brands:'all', deviceIds:'', name:'', udid:'']
        }
        if (input.operation != 'devices' || !(input.platform in ['ios','harmony']) ||
            !(input.mode in ['plan','apply']) || !(input.source in ['shared','manual'])) {
            throw new IllegalArgumentException('请选择有效平台和设备操作')
        }
        if (!(input.brands instanceof List) || !input.brands || input.brands.size() > 20 ||
            input.brands.any { !(it instanceof String) || !(it ==~ /[a-z][a-z0-9_]*/) } ||
            input.brands.unique(false).size() != input.brands.size()) { throw new IllegalArgumentException('请选择需要同步的品牌') }
        def brands = input.brands.collect { brand -> inventory.brands.find { it.brand == brand && eligible(it, input.platform) } }
        if (brands.any { it == null }) { throw new IllegalArgumentException('选中品牌尚未接入设备签名，请先完成品牌接入') }
        def ids = input.deviceIds ?: []
        String name = input.name ?: ''
        String udid = input.udid ?: ''
        if (input.source == 'shared') {
            if (!(ids instanceof List) || !ids || ids.size() > 100 ||
                ids.any { !(it instanceof String) || !(it ==~ /[a-f0-9]{20}/) } ||
                ids.unique(false).size() != ids.size() || name || udid ||
                ids.any { id -> !inventory.devices[input.platform]?.any { it.id == id } }) {
                throw new IllegalArgumentException('请选择该平台已有的测试设备')
            }
        } else {
            if (!(input.name instanceof String) || !(input.udid instanceof String) || ids) {
                throw new IllegalArgumentException('请填写设备名称和 UDID')
            }
            name = name.trim(); udid = udid.trim()
            def pattern = input.platform == 'ios' ? /(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{8}-[a-fA-F0-9]{16})/ : /[a-zA-Z0-9-]{16,64}/
            if (!name || name.length() > (input.platform == 'ios' ? 80 : 100) ||
                name.any { Character.isISOControl((char) it) } || !(udid ==~ pattern)) {
                throw new IllegalArgumentException('设备名称或 UDID 格式不正确')
            }
        }
        return [operation:'devices', platform:input.platform, mode:input.mode, source:input.source,
                brands:input.brands.sort(false).join(','), deviceIds:ids.sort(false).join(','), name:name, udid:udid]
    }

    @RequirePOST
    void doOperation(StaplerRequest2 req, StaplerResponse2 rsp) {
        def job = maintenanceJob()
        Jenkins.get().checkPermission(Jenkins.ADMINISTER)
        job.checkPermission(Item.BUILD)
        if (!req.contentType?.toLowerCase()?.startsWith('application/json')) { json(rsp, [error:'请求格式不正确'], 415); return }
        def bytes = req.inputStream.readNBytes(8193)
        if (bytes.length > 8192) { json(rsp, [error:'请求超出大小限制'], 413); return }
        try {
            def input = new JsonSlurperClassic().parseText(new String(bytes, StandardCharsets.UTF_8))
            if (!(input instanceof Map)) { throw new IllegalArgumentException('操作请求无效') }
            def request = validate(input, input.operation == 'inventory' ? [:] : snapshot(job).inventory)
            String owner = Jenkins.getAuthentication2().name
            String digest = MessageDigest.getInstance('SHA-256').digest(JsonOutput.toJson(request).getBytes(StandardCharsets.UTF_8)).encodeHex().toString()
            synchronized (requests) {
                long now = System.currentTimeMillis()
                requests.entrySet().removeIf { now - it.value.created > 3600000L }
                def existing = requests[input.requestId]
                if (existing == null) {
                    // 标记随标准 ParametersAction 落盘；重启后仍可找回，不保存额外秘密文件。
                    def candidates = Jenkins.get().queue.items.findAll { it.task == job } + job.builds.limit(100).toList()
                    def previous = candidates.find { candidate ->
                        candidate.getAction(ParametersAction.class)?.allParameters?.find {
                            it.name == 'H5_REQUEST_ID' && it.value == input.requestId
                        }
                    }
                    if (previous != null) {
                        def parameters = previous.getAction(ParametersAction.class).allParameters
                        def cause = previous instanceof Run ? previous.getCause(Cause.UserIdCause.class) :
                            previous.getAction(CauseAction.class)?.causes?.find { it instanceof Cause.UserIdCause }
                        existing = [owner:parameters.find { it.name == 'H5_REQUEST_OWNER' }?.value,
                                    digest:parameters.find { it.name == 'H5_REQUEST_DIGEST' }?.value,
                                    queueId:previous instanceof Run ? previous.queueId : previous.id]
                        if (cause?.userId != existing.owner) {
                            json(rsp, [error:'原任务发起者不匹配，请在 Jenkins 中核验'], 409); return
                        }
                    }
                }
                if (existing != null) {
                    if (existing.owner != owner || existing.digest != digest) {
                        json(rsp, [error:'请求标识已使用，请重新发起操作'], 409); return
                    }
                    json(rsp, [queueId:existing.queueId, reused:true], 202); return
                }
                if (input.recoverOnly == true) {
                    json(rsp, [code:'recovery-unavailable', error:'尚未找到原提交记录，请在 Jenkins 中核验；本次不会重新提交'], 409); return
                }
                if (requests.size() >= 128) { json(rsp, [error:'操作过多，请稍后再试'], 429); return }
                if (request.mode == 'apply' && busy()) {
                    json(rsp, [error:'Jenkins 正在执行其他任务，请结束后再确认同步'], 409); return
                }
                if (!job.isBuildable()) { json(rsp, [error:'维护任务当前不可执行'], 409); return }
                def values = [new StringParameterValue('PLATFORM',request.platform),
                    new StringParameterValue('OPERATION',request.operation), new StringParameterValue('MODE',request.mode),
                    new StringParameterValue('TARGET_BRANDS',request.brands), new StringParameterValue('DEVICE_SOURCE',request.source),
                    new StringParameterValue('DEVICE_IDS',request.deviceIds), new StringParameterValue('DEVICE_NAME',request.name),
                    new PasswordParameterValue('DEVICE_UDID',Secret.fromString(request.udid), ''),
                    new BooleanParameterValue('CONFIRM_APPLY',request.mode == 'apply'), new BooleanParameterValue('NOTIFY',false),
                    new StringParameterValue('H5_REQUEST_ID',input.requestId), new StringParameterValue('H5_REQUEST_OWNER',owner),
                    new StringParameterValue('H5_REQUEST_DIGEST',digest)]
                def item = Jenkins.get().queue.schedule2(job, 0, [new CauseAction(new Cause.UserIdCause()), new ParametersAction(values)]).item
                if (item == null) { json(rsp, [error:'维护任务未进入队列，请检查 Jenkins 状态'], 409); return }
                // 去重只保存摘要和队列号；设备完整 UDID 不保留在 H5 状态中。
                requests[input.requestId] = [owner:owner, digest:digest, queueId:item.id, created:now]
                json(rsp, [queueId:item.id], 202)
            }
        } catch (IllegalArgumentException ex) { json(rsp, [error:ex.message], 400) }
          catch (groovy.json.JsonException ex) { json(rsp, [error:'操作请求无效'], 400) }
    }

    static List progress(List selected, String platform, String text, boolean finished, String result) {
        def states = selected.collectEntries { [(it):'not-run'] }
        String active = null
        text.eachLine { line ->
            def start = line =~ /^维护品牌 ([a-z][a-z0-9_]*) \/ Team [a-z][a-z0-9_-]* \/ (?:plan|apply)$/
            if (start.matches() && states.containsKey(start[0][1])) {
                if (active != null) { states[active] = 'success' }
                active = start[0][1]; states[active] = 'running'
            }
            def done = line =~ /^品牌 ([a-z][a-z0-9_]*) 的 Jenkins 签名材料已核对；更新 [0-9]+ 项$/
            if (done.matches() && states.containsKey(done[0][1])) { states[done[0][1]] = 'success' }
        }
        if (finished) {
            if (result == 'SUCCESS' || result == 'UNSTABLE') {
                states.replaceAll { key, value -> 'success' }
            } else if (active != null && states[active] != 'success') { states[active] = 'unknown' }
        }
        states.collect { brand, status -> [brand:brand, platform:platform, status:status] }
    }

    void doTask(StaplerRequest2 req, StaplerResponse2 rsp) {
        def job = maintenanceJob()
        String id = req.getParameter('id') ?: ''
        if (!(id ==~ /[0-9]{1,18}/)) { json(rsp, [error:'任务标识无效'], 400); return }
        long queueId = id.toLong()
        def run = job.builds.limit(100).find { it.queueId == queueId }
        if (run != null) {
            def parameters = run.getAction(ParametersAction.class)
            def selected = parameters?.getParameter('OPERATION')?.value == 'devices' ?
                (parameters.getParameter('TARGET_BRANDS')?.value ?: '').toString().split(',').findAll { it ==~ /[a-z][a-z0-9_]*/ } : []
            def platform = parameters?.getParameter('PLATFORM')?.value?.toString()
            String text = ''
            run.getLogReader().withCloseable { reader ->
                char[] buffer = new char[8192]
                def output = new StringBuilder()
                int size
                while (output.length() < 2 * 1024 * 1024 && (size = reader.read(buffer)) > 0) { output.append(buffer, 0, size) }
                text = output.toString()
            }
            def result = run.result?.toString()
            json(rsp, [state:run.isBuilding() ? 'running':'finished', build:run.number, result:result,
                       url:run.url, brands:progress(selected, platform, text, !run.isBuilding(), result)])
            return
        }
        def queued = Jenkins.get().queue.getItem(queueId)
        if (queued != null && queued.task == job) { json(rsp, [state:'queued', build:null, result:null, url:job.url, brands:[]]); return }
        json(rsp, [state:'cancelled', build:null, result:null, brands:[]])
    }
}

def controller = Jenkins.get()
// 项目策略由管理员固定部署；网页请求和候选 MR 无法选择 Job、节点或命令。
def policyLoader = new GroovyClassLoader(controller.pluginManager.uberClassLoader)
def policy = policyLoader.parseClass(new File(controller.rootDir, 'ci-auto-promotion/CiAutoPromotion.groovy'))
def project = policy.loadProject(new File(controller.rootDir, 'ci-projects'), System.getenv('JENKINS_CI_PROJECT') ?: 'sxmqlive')
if (!project.jobs.signing_maintenance) return

def extensions = controller.getExtensionList(RootAction.class)
extensions.findAll { it.class.name == 'SxmqliveSigningPortal' }.each { extensions.remove(it) }
def actions = Jenkins.get().actions
actions.findAll { it.class.name == 'SxmqliveSigningPortal' }.each { actions.remove(it) }
def portal = new SxmqliveSigningPortal(project)
extensions.add(portal)
// Jenkins 启动时才把 RootAction 放进路由列表；热加载也须更新该列表，无须重启。
actions.add(portal)
java.util.logging.Logger.getLogger('sxmqlive.signing.portal').info('品牌签名与设备管理入口已注册：/signing/')
