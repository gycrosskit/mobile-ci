import groovy.xml.XmlUtil
import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import java.nio.file.StandardCopyOption
import java.nio.file.attribute.PosixFilePermissions
import java.security.MessageDigest

/** 只更新已存在 Job 的执行版本，完整保留其余运行配置。 */
class CiAutoPromotion {
    /** 模板语法标记来自固定部署代码；不需要另一公司的项目配置。 */
    static Map loadTemplateBindings(File home) {
        Path path = new File(home, 'template-bindings.json').toPath().toAbsolutePath().normalize()
        if (path.toFile().canonicalFile.toPath() != path || !Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS) ||
            Files.getAttribute(path, 'unix:nlink', LinkOption.NOFOLLOW_LINKS) != 1 || Files.size(path) > 65536 ||
            Files.getPosixFilePermissions(path).any { it.toString() in ['GROUP_WRITE','OTHERS_WRITE'] }) {
            throw new IllegalStateException('模板标记必须为固定部署的有界单链接文件且不可由其他用户写入')
        }
        byte[] bytes
        def input = Files.newInputStream(path, LinkOption.NOFOLLOW_LINKS)
        try { bytes = input.readNBytes(65537) } finally { input.close() }
        if (bytes.length > 65536) throw new IllegalStateException('模板标记超过大小上限')
        def bindings = new JsonSlurperClassic().parseText(new String(bytes, 'UTF-8'))
        if (!(bindings instanceof Map) || bindings.keySet() != ['repositories','credentials','jobs'] as Set) {
            throw new IllegalStateException('模板标记只包含语法绑定，不能携带项目运行策略')
        }
        bindings
    }

    /** 与 Python loader 消费同一管理员部署 JSON；候选 MR 与 HTTP 请求不提供路径。 */
    static Map loadProject(File directory, String id = 'sxmqlive') {
        if (!(id ==~ /[a-z][a-z0-9-]{0,63}/)) throw new IllegalArgumentException('项目必须是固定配置 ID')
        Path root = directory.toPath().toAbsolutePath().normalize()
        if (directory.canonicalFile.toPath() != root) throw new IllegalStateException('项目配置目录不允许符号链接')
        backupDirectory(root, false)
        if (Files.getOwner(root).name != System.getProperty('user.name')) throw new IllegalStateException('项目配置目录所有者无效')
        Path file = root.resolve(id + '.json')
        byte[] bytes = privateBackupBytes(file, 65536)
        if (bytes.length > 65536 || Files.getOwner(file).name != System.getProperty('user.name')) throw new IllegalStateException('项目配置文件所有者或大小无效')
        def value = new JsonSlurperClassic().parseText(new String(bytes, 'UTF-8'))
        if (!(value instanceof Map) || !(value.version instanceof Integer) || value.version != 1 || value.id != id ||
            !(value.repositories instanceof Map) || !(value.jobs instanceof Map) || !(value.platforms instanceof Map)) {
            throw new IllegalArgumentException('无效可信项目配置')
        }
        def allowed = ['version','id','repositories','credentials','providers','platforms','jobs','contract','runtime','backup','signingBackup','portal','testOnly','brands','scm']
        if (value.containsKey('scm') && (!(value.scm instanceof Map) || value.scm.keySet() != ['provider'] as Set || value.scm.provider != 'gitee')) {
            throw new IllegalArgumentException('Controller 只接受 Gitee 应用项目；GitHub 应用 PR 运行于独立 Actions 入口')
        }
        if (value.containsKey('testOnly') && !(value.testOnly instanceof Boolean)) throw new IllegalArgumentException('testOnly 必须为布尔值')
        if (value.testOnly == true) throw new IllegalArgumentException('testOnly 示例不能安装为 Controller 受信项目')
        if (!allowed.containsAll(value.keySet())) throw new IllegalArgumentException('未知项目字段')
        String applicationProvider = value.scm?.provider ?: 'gitee'
        ['application', 'automation'].each { role ->
            def repo = value.repositories[role]
            def provider = repo instanceof Map ? (repo.containsKey('provider') ? repo.provider : applicationProvider) : null
            String domain = provider == 'github' ? 'github.com' : 'gitee.com'
            if (!(repo instanceof Map) || !['identity','ssh','https','defaultBranch','trustRef','provider'].containsAll(repo.keySet()) ||
                !(provider in ['gitee','github']) || (role == 'application' && provider != applicationProvider) ||
                !(repo.identity ==~ (provider == 'github' ? /[A-Za-z0-9_-]+\/[A-Za-z0-9_.-]+/ : /[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+/)) ||
                (provider == 'github' && repo.identity.split('/')[1] in ['.','..']) ||
                repo.ssh != 'git@' + domain + ':' + repo.identity + '.git' || repo.https != 'https://' + domain + '/' + repo.identity + '.git' ||
                !(repo.defaultBranch ==~ /[A-Za-z0-9][A-Za-z0-9_.\/-]*/) || repo.defaultBranch.contains('..') ||
                repo.trustRef != 'refs/heads/' + repo.defaultBranch) throw new IllegalArgumentException('仓库身份与可信引用不一致')
        }
        if (value.repositories.keySet() != ['application','automation'] as Set) throw new IllegalArgumentException('未知仓库角色')
        if (!(value.credentials instanceof Map) || value.credentials.keySet() != ['scm','giteeApi','giteeWebhook','giteeConnection'] as Set ||
            !value.credentials.values().every { it instanceof String && it ==~ /[A-Za-z0-9_.-]+/ }) throw new IllegalArgumentException('无效固定凭据')
        if (!(value.providers instanceof Map) || !['pgyer','dingtalk'].containsAll(value.providers.keySet())) throw new IllegalArgumentException('无效供应商')
        value.providers.each { name, binding ->
            def fields = ['accountId','credentialId','webhookCredentialId','secretCredentialId','testerCredentialId','legacyNamespace']
            if (!(binding instanceof Map) || !fields.containsAll(binding.keySet()) || !(binding.accountId ==~ /[a-z][a-z0-9_.-]{0,127}/) ||
                (binding.containsKey('legacyNamespace') && !(binding.legacyNamespace instanceof Boolean)) ||
                (binding.legacyNamespace && (id != 'sxmqlive' || binding.accountId != 'sxmqlive-' + name))) throw new IllegalArgumentException('供应商不能认领外部台账')
            def required = name == 'pgyer' ? ['credentialId'] : ['webhookCredentialId','secretCredentialId']
            if (!required.every { binding[it] instanceof String && binding[it] ==~ /[A-Za-z0-9_.-]+/ } ||
                !binding.findAll { key, ignored -> key.endsWith('CredentialId') || key == 'credentialId' }.values().every { it instanceof String && it ==~ /[A-Za-z0-9_.-]+/ }) throw new IllegalArgumentException('无效供应商凭据')
        }
        if (!value.platforms || !['android','ios','harmony'].containsAll(value.platforms.keySet())) throw new IllegalArgumentException('无效平台')
        value.platforms.each { platform, config ->
            def fields = ['adapter','engines','modules','buildTypes','variants','applicationId','artifact','mapping','hostDirs','workspace','project','scheme','xcconfig','bundleId','configuration','distribution','renderer','module','product','appIdentifier','metadata']
            if (!(config instanceof Map) || !fields.containsAll(config.keySet()) || !(config.adapter in ['sxmqlive',[android:'gradle',ios:'xcode',harmony:'hvigor'][platform]])) throw new IllegalArgumentException('无效平台字段')
            if (!(config.engines instanceof List) || !config.engines || !['cmp','kuikly','native'].containsAll(config.engines) || config.engines.unique(false).size() != config.engines.size()) throw new IllegalArgumentException('无效引擎')
            if (config.adapter != 'sxmqlive' && config.engines != ['native']) throw new IllegalArgumentException('原生适配器必须绑定 native')
            if (platform == 'android' && (!(config.modules instanceof Map) || config.modules.keySet() != config.engines as Set)) throw new IllegalArgumentException('Android module 必须逐引擎绑定')
            if (platform == 'ios' && config.adapter == 'xcode' && ((config.workspace as boolean) == (config.project as boolean))) throw new IllegalArgumentException('原生 iOS 必须固定 workspace 或 project')
            config.each { key, item ->
                if (item instanceof Map) {
                    def expected = key in ['modules','hostDirs','workspace','project','scheme','configuration'] ? config.engines : key in ['buildTypes','variants','xcconfig'] ? ['test','release'] : null
                    if (expected == null || !item || !expected.containsAll(item.keySet())) throw new IllegalArgumentException('平台映射只接受固定角色')
                }
                if (key in ['modules','buildTypes','variants','hostDirs'] && !(item instanceof Map)) throw new IllegalArgumentException('平台字段必须为固定映射')
            }
            config.values().each { item ->
                def items = item instanceof Map ? item.values() : item instanceof List ? item : [item]
                items.each { text ->
                    if (text instanceof String) {
                        def matcher = text =~ /\{([^{}]*)\}/
                        def names = matcher.collect { match -> match[1] }
                        def placeholders = ['brand','brandPascal','buildType','buildTypeLower','variant','module','product','engine','distribution','environment','scheme','pipeline']
                        if (!placeholders.containsAll(names) || text.replaceAll(/\{[^{}]*\}/,'').contains('{') || text.replaceAll(/\{[^{}]*\}/,'').contains('}')) throw new IllegalArgumentException('构建模板只接受固定字段')
                    }
                }
            }
            config.findAll { key, ignored -> !(key in ['adapter','engines']) }.each { key, item ->
                def items = item instanceof Map ? item.values() : [item]
                if (!items.every { it instanceof String && it ==~ /[A-Za-z0-9_ .\/{}*-]+/ && !it.contains('..') && !it.startsWith('/') && !it.contains('\\') }) throw new IllegalArgumentException('平台只接受固定数据')
                if (key in ['artifact','mapping','metadata','xcconfig','workspace','project','hostDirs'] && !items.every { !it.split('/',-1).any { part -> part in ['.','..',''] } }) throw new IllegalArgumentException('平台路径不能越界或包含空分段')
            }
        }
        if (value.platforms.values().any { it.adapter != 'sxmqlive' } && (!(value.brands instanceof List) || !value.brands || !value.brands.every { it ==~ /[a-z][a-z0-9_]*/ } || value.brands.unique(false).size() != value.brands.size())) throw new IllegalArgumentException('原生项目必须声明固定品牌')
        def roles = [android_test:'jenkins/Jenkinsfile.test', android_release:'jenkins/Jenkinsfile.release',
            ios_test:'jenkins/Jenkinsfile.ios.test', ios_release:'jenkins/Jenkinsfile.ios.release',
            harmony_test:'jenkins/Jenkinsfile.ohos.test', harmony_release:'jenkins/Jenkinsfile.ohos.test',
            app_pr:'jenkins/Jenkinsfile.pr-validation', ci_pr:'jenkins/Jenkinsfile.ci-pr-validation', wiki:'jenkins/Jenkinsfile.wiki-navigation',
            signing_maintenance:'jenkins/Jenkinsfile.signing.maintenance', signing_backup:'jenkins/Jenkinsfile.signing.backup', alert:'jenkins/Jenkinsfile.alert-test']
        value.jobs.each { role, job ->
            if (!(job instanceof Map) || !roles.containsKey(role) || job.pipeline != roles[role] ||
                job.keySet() != ['name','pipeline'] as Set || !(job.name instanceof String) ||
                !job.name.split('/').every { it ==~ /[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_. \u4e00-\u9fff-]*/ }) {
                throw new IllegalArgumentException('Job 必须绑定固定角色和 Pipeline')
            }
        }
        def names = value.jobs.values().collect { it.name }
        if (names.unique(false).size() != names.size()) throw new IllegalArgumentException('重复项目 Job')
        if (value.signingBackup) {
            def backup = value.signingBackup
            if (!(backup instanceof Map) || !['scope','node','root','recipientPath','recipientSha256','legacyRecipientSha256','repository','branch','prefixes'].containsAll(backup.keySet()) ||
                !value.repositories.containsKey(backup.repository) || !(backup.branch ==~ /[A-Za-z0-9][A-Za-z0-9_.\/-]*/) || backup.branch.contains('..')) throw new IllegalArgumentException('无效签名备份接线')
            if (!(backup.scope in ['controller_dr','project_signing']) || !(backup.node ==~ /[A-Za-z0-9_.-]+/) ||
                !(backup.root ==~ /\/[A-Za-z0-9_.\/ -]+/) || backup.root.split('/').contains('..') ||
                !(backup.recipientSha256 ==~ /[0-9a-f]{64}/) || !(backup.prefixes instanceof List) ||
                (backup.scope == 'project_signing' && !backup.prefixes)) throw new IllegalArgumentException('无效签名备份范围')
            ([backup.recipientPath] + backup.prefixes).each { path ->
                if (!(path instanceof String) || !path || path.startsWith('/') || path.contains('\\') || path.split('/').any { it in ['.','..',''] }) {
                    throw new IllegalArgumentException('签名材料路径越界')
                }
            }
            if (backup.prefixes.any { it == 'controller' || it.startsWith('controller/') } ||
                backup.prefixes.unique(false).size() != backup.prefixes.size() ||
                backup.prefixes.any { a -> backup.prefixes.any { b -> a != b && b.startsWith(a + '/') } }) {
                throw new IllegalArgumentException('项目材料前缀重复、重叠或包含 Controller')
            }
            if (backup.legacyRecipientSha256 && (id != 'sxmqlive' || backup.scope != 'controller_dr' ||
                backup.legacyRecipientSha256 != '2387e446e9d15b45ebb0d67684550a61598515dc5eceb899d7ec31edcf3219bf' ||
                backup.recipientSha256 != backup.legacyRecipientSha256)) throw new IllegalArgumentException('新接收人不能认领旧备份')
        }
        [android:['android_test','android_release'],ios:['ios_test','ios_release'],harmony:['harmony_test','harmony_release']].each { name, required ->
            if (value.platforms.containsKey(name) ? !value.jobs.keySet().containsAll(required) : required.any { value.jobs.containsKey(it) }) throw new IllegalArgumentException('平台与 Job 不一致')
        }
        def contract = value.contract ?: [:]
        if (!(contract instanceof Map) || !['toolDir','testDir','brandsFile','manifestTest','buildSystem','requiredTools'].containsAll(contract.keySet()) ||
            !contract.brandsFile || !(contract.get('buildSystem','gradle') in ['gradle','xcode','hvigor','none'])) throw new IllegalArgumentException('无效平台检查契约')
        ['toolDir','testDir','brandsFile','manifestTest'].each { key ->
            if (contract.containsKey(key) && (!(contract[key] instanceof String) || !(contract[key] ==~ /[A-Za-z0-9_ .\/-]+/) || contract[key].startsWith('/') || contract[key].split('/',-1).any { it in ['.','..',''] })) throw new IllegalArgumentException('检查契约路径越界')
        }
        def requiredTools = contract.requiredTools ?: [:]
        if (!(requiredTools instanceof Map) || !value.platforms.keySet().containsAll(requiredTools.keySet())) throw new IllegalArgumentException('检查入口必须属于受信平台')
        requiredTools.each { platform, paths ->
            if (!(paths instanceof List) || !paths.every { it instanceof String && it ==~ /[A-Za-z0-9_ .\/-]+/ && !it.startsWith('/') && !it.split('/',-1).any { part -> part in ['.','..',''] } }) throw new IllegalArgumentException('无效固定检查入口')
        }
        if (value.runtime && (!(value.runtime instanceof Map) || !['roles','linuxUid','macUid'].containsAll(value.runtime.keySet()) ||
            value.runtime.roles?.keySet() != ['controller','android','pr','maintenance'] as Set ||
            !value.runtime.roles.values().every { it instanceof String && it ==~ /[a-z][a-z0-9-]*/ } || value.runtime.roles.values().unique(false).size() != 4 ||
            !['linuxUid','macUid'].every { value.runtime[it] instanceof Integer && value.runtime[it] > 0 })) throw new IllegalArgumentException('无效运行边界')
        if (value.backup && (!(value.backup instanceof Map) || !['scope','recipientAuthority','repository','branch'].containsAll(value.backup.keySet()) ||
            !(value.backup.scope in ['controller','project']) || !(value.backup.recipientAuthority in ['controller-admin','project-admin']) || !value.repositories.containsKey(value.backup.repository) ||
            !(value.backup.branch ==~ /[A-Za-z0-9][A-Za-z0-9_.\/-]*/) || value.backup.branch.contains('..'))) throw new IllegalArgumentException('无效备份责任边界')
        if (value.portal && (!(value.portal instanceof Map) || !['macNode','hdcPath','backupUrl'].containsAll(value.portal.keySet()) ||
            !(value.portal.macNode ==~ /[A-Za-z0-9_.-]+/) || !(value.portal.hdcPath ==~ /\/[A-Za-z0-9_.\/ -]+/) || value.portal.hdcPath.split('/').contains('..') ||
            value.portal.backupUrl != value.repositories[value.signingBackup?.repository ?: 'application'].https[0..-5] + '/tree/' + (value.signingBackup?.branch ?: 'backup/signing'))) throw new IllegalArgumentException('无效门户接线')
        value.asImmutable()
    }

    static String renderProjectText(String source, Map project, Map original = null, String role = 'ci_pr') {
        boolean githubTools = automationProvider(project) == 'github'
        if (githubTools && role == 'ci_pr') throw new IllegalArgumentException('GitHub 工具 PR 使用 Actions；不能渲染 Gitee CI 门禁')
        Map values = [PROJECT_ID: project.id]
        values.CI_CHECKOUT_URL = githubTools ? project.repositories.automation.https : project.repositories.automation.ssh
        values.CI_CHECKOUT_CREDENTIAL = githubTools ? '' : project.credentials.scm
        values.CI_BROWSER_CLASS = githubTools ? 'hudson.plugins.git.browser.GithubWeb' : 'hudson.plugins.git.browser.Gitee'
        [application:'APP', automation:'CI'].each { name, prefix ->
            ['ssh','https','identity'].each { field -> values[prefix + '_' + field.toUpperCase()] = project.repositories[name][field] }
            values[prefix + '_WEB'] = project.repositories[name].https[0..-5]
            values[prefix + '_BRANCH'] = project.repositories[name].defaultBranch
        }
        values.APP_REPO_PATTERN = '.*' + project.repositories.application.identity.split('/')[1] + '.git'
        Map repository = project.repositories[role == 'ci_pr' ? 'automation' : 'application']
        values.ROLE_BRANCH = repository.defaultBranch
        values.ROLE_NAMESPACE = repository.identity.split('/')[0]
        project.jobs.each { name, job -> values['JOB_' + name.toUpperCase()] = job.name }
        project.credentials.each { name, credential ->
            values['CREDENTIAL_' + name.replaceAll(/([a-z])([A-Z])/,'$1_$2').toUpperCase()] = credential
        }
        if (project.jobs.signing_maintenance) {
            String name = project.jobs.signing_maintenance.name
            int split = name.lastIndexOf('/')
            if (split < 1) throw new IllegalArgumentException('签名维护必须绑定固定 Folder')
            values.SIGNING_FOLDER = name.substring(0, split)
        }
        if (project.signingBackup) {
            values.SIGNING_NODE = project.signingBackup.node
            values.SIGNING_ROOT = project.signingBackup.root
        }
        String rendered = source.replaceAll(/\{\{CI_([A-Z][A-Z0-9_]*)\}\}/) { match, field ->
            if (!values.containsKey(field)) throw new IllegalArgumentException('模板含未知或未绑定占位符：' + field)
            values[field]
        }
        if (rendered.contains('{{CI_')) throw new IllegalArgumentException('模板仍含未渲染或畸形占位符')
        rendered
    }

    static Map jobMap(Map project = null) {
        if (project == null) return JOBS
        def roles = ['wiki','app_pr','ci_pr','android_test','android_release','ios_test','ios_release','harmony_test','harmony_release']
        project.jobs.findAll { role, ignored -> role in roles }.collectEntries { role, job -> [(job.name): job.pipeline] }
    }

    static String repository(Map project = null) { project ? project.repositories.automation.ssh : REPOSITORY }
    static String automationProvider(Map project = null) { project?.repositories?.automation?.containsKey('provider') ? project.repositories.automation.provider : project?.scm?.provider ?: 'gitee' }
    static void requireGiteePromotion(Map project = null) {
        // GitHub 来源没有 Gitee MR 证明，不能沿用旧身份和祖先校验自动放行。
        if (automationProvider(project) != 'gitee') throw new IllegalStateException('自动晋级只支持 Gitee 工具来源；GitHub 必须由管理员固定已审版本')
    }
    static String gateName(Map project = null) { project ? project.jobs.ci_pr.name : 'sxmqlive-ci-pr-validation' }
    static String inlineRole(String name, Map project = null) {
        if (name == gateName(project)) return 'ci_pr'
        if (project && project.id != 'sxmqlive' && name == project.jobs.app_pr?.name) return 'app_pr'
        null
    }
    static final String REPOSITORY = 'git@gitee.com:example/legacy-automation.git'
    static final Map JOBS = [
        '运维/sxmqlive-wiki-navigation': 'jenkins/Jenkinsfile.wiki-navigation',
        'sxmqlive-pr-validation': 'jenkins/Jenkinsfile.pr-validation',
        'sxmqlive-ci-pr-validation': 'jenkins/Jenkinsfile.ci-pr-validation',
        'mobile-apps/sxmqlive-android-test': 'jenkins/Jenkinsfile.test',
        'mobile-apps/sxmqlive-android-formal-release': 'jenkins/Jenkinsfile.release',
        'mobile-apps/sxmqlive-ios-test': 'jenkins/Jenkinsfile.ios.test',
        'mobile-apps/sxmqlive-ios-formal-release': 'jenkins/Jenkinsfile.ios.release',
        'mobile-apps/sxmqlive-ohos-test': 'jenkins/Jenkinsfile.ohos.test',
        'mobile-apps/sxmqlive-ohos-formal-release': 'jenkins/Jenkinsfile.ohos.test'
    ].asImmutable()

    static String sha(Object value) {
        if (!(value instanceof String) || !(value ==~ /[0-9a-f]{40}/)) {
            throw new IllegalArgumentException('版本必须为精确 40 位提交')
        }
        value
    }

    static void verifyProof(Map proof, Map project = null) {
        requireGiteePromotion(project)
        sha(proof.commit)
        sha(proof.tree)
        sha(proof.testedTree)
        sha(proof.testedMerge)
        sha(proof.mergedHead)
        if (proof.tree != proof.testedTree || proof.result != 'SUCCESS' || proof.building != false ||
            proof.repository != (project ? project.repositories.automation.identity : 'example/legacy-automation') ||
            proof.target != (project ? project.repositories.automation.defaultBranch : 'master') ||
            proof.mrState != 'merged' || proof.head != proof.mergedHead || proof.trustedScript != true) {
            throw new IllegalArgumentException('master 内容必须与已合并 MR 的成功可信门禁一致')
        }
        sha(proof.head)
    }

    static Node parse(String source) {
        // 禁止 DOCTYPE，配置中的凭据只能原样保留，不能解析外部实体。
        new XmlParser(false, false).parseText(source)
    }

    static String pinnedCommit(String source, String name, Map project = null) {
        if (source.contains('{{CI_')) throw new IllegalArgumentException('受管 Job 含未渲染项目占位符')
        Map jobs = jobMap(project)
        if (!jobs.containsKey(name)) throw new IllegalArgumentException('不受管 Job')
        def definition = parse(source).definition[0]
        if (inlineRole(name, project)) {
            if (definition?.attribute('class') != 'org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition' ||
                definition.sandbox.text() != 'true') throw new IllegalArgumentException('自身门禁必须为固定沙箱文本')
            def match = definition.script.text() =~ /CI_AUTOMATION_COMMIT = '([0-9a-f]{40})'/
            if (match.count != 1) throw new IllegalArgumentException('自身门禁缺少唯一版本记录')
            return sha(match[0][1])
        }
        if (definition?.attribute('class') != 'org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition' ||
            definition.scriptPath.text() != jobs[name] || definition.scm.size() != 1 ||
            definition.lightweight.size() != 1 || definition.lightweight.text() != 'false' ||
            definition.scm[0].attribute('class') != 'hudson.plugins.git.GitSCM' ||
            definition.scm[0].userRemoteConfigs[0]?.children()?.size() != 1 ||
            definition.scm[0].userRemoteConfigs[0].children()[0].url.text() != repository(project) ||
            definition.scm[0].branches[0]?.children()?.size() != 1) {
            throw new IllegalArgumentException('受管任务的仓库、Pipeline 路径或 SCM 发生变化')
        }
        sha(definition.scm[0].branches[0].children()[0].name.text())
    }

    static String updatedConfig(String source, String name, String commit, String pipeline = null, Map project = null) {
        String before = pinnedCommit(source, name, project)
        sha(commit)
        if (before == commit) return source
        def root = parse(source)
        def definition = root.definition[0]
        if (inlineRole(name, project)) {
            if (!pipeline || !pipeline.contains("CI_AUTOMATION_COMMIT = '__CI_AUTOMATION_COMMIT__'")) {
                throw new IllegalArgumentException('自身门禁源文本缺少固定版本占位符')
            }
            if (pipeline.contains('{{CI_')) throw new IllegalArgumentException('自身门禁含未渲染项目占位符')
            definition.script[0].setValue(pipeline.replace('__CI_AUTOMATION_COMMIT__', commit))
        } else {
            definition.scm[0].branches[0].children()[0].name[0].setValue(commit)
        }
        String result = XmlUtil.serialize(root)
        if (pinnedCommit(result, name, project) != commit) throw new IllegalStateException('版本更新回读失败')
        result
    }

    static void backupDirectory(Path path, boolean create = true) {
        if (create && !Files.exists(path, LinkOption.NOFOLLOW_LINKS)) {
            Files.createDirectory(path, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString('rwx------')))
        }
        if (!Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS) || Files.isSymbolicLink(path) ||
            Files.getPosixFilePermissions(path, LinkOption.NOFOLLOW_LINKS) != PosixFilePermissions.fromString('rwx------')) {
            throw new IllegalStateException('自动升级备份目录必须为私有真实目录')
        }
    }

    static byte[] privateBackupBytes(Path path, Integer maximum = null) {
        if (!Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS) || Files.isSymbolicLink(path) ||
            Files.getAttribute(path, 'unix:nlink', LinkOption.NOFOLLOW_LINKS) != 1 ||
            Files.getPosixFilePermissions(path, LinkOption.NOFOLLOW_LINKS) != PosixFilePermissions.fromString('rw-------')) {
            throw new IllegalStateException('自动升级备份必须为私有单链接普通文件')
        }
        Files.newInputStream(path, LinkOption.NOFOLLOW_LINKS).withCloseable { stream ->
            byte[] bytes = maximum == null ? stream.bytes : stream.readNBytes(maximum + 1)
            if (maximum != null && bytes.length > maximum) throw new IllegalStateException('私有配置超出大小限制')
            bytes
        }
    }

    static String backupDigest(byte[] bytes) {
        MessageDigest.getInstance('SHA-256').digest(bytes).encodeHex().toString()
    }

    static Path backupJobDirectory(File home, String name, boolean create = true, Map project = null) {
        if (!jobMap(project).containsKey(name)) throw new IllegalArgumentException('不受管 Job')
        Path base = home.toPath().toAbsolutePath().normalize()
        backupDirectory(base, create)
        // v1 UUID 快照与未知人工数据不进入本格式的保留/清理范围。
        Path root = base.resolve('backups-v2'); backupDirectory(root, create)
        Path job = root.resolve(name.replace('/', '__')); backupDirectory(job, create)
        job
    }

    static Path backupRecord(Path job, String record) {
        if (!(record ==~ /[0-9a-f]{40}\/[0-9a-f]{64}/)) throw new IllegalStateException('无效自动升级备份记录')
        Path target = job.resolve(record.split('/')[0]); backupDirectory(target, false)
        Path directory = job.resolve(record); backupDirectory(directory, false)
        Path file = directory.resolve('config.xml')
        if (backupDigest(privateBackupBytes(file)) != record.split('/')[1]) {
            throw new IllegalStateException('自动升级备份内容哈希不匹配')
        }
        file
    }

    static String backupSnapshot(File home, String name, String target, String source, Map project = null) {
        sha(target); pinnedCommit(source, name, project)
        Path job = backupJobDirectory(home, name, true, project)
        byte[] bytes = source.getBytes('UTF-8')
        String record = target + '/' + backupDigest(bytes)
        Path version = job.resolve(target); backupDirectory(version)
        Path directory = job.resolve(record); backupDirectory(directory)
        Path file = directory.resolve('config.xml')
        if (!Files.exists(file, LinkOption.NOFOLLOW_LINKS)) {
            Files.createFile(file, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString('rw-------')))
            Files.write(file, bytes, StandardOpenOption.WRITE, LinkOption.NOFOLLOW_LINKS)
        }
        backupRecord(job, record).toString()
    }

    static void retainBackup(File home, String name, String target, String backup, Map project = null) {
        sha(target)
        Path job = backupJobDirectory(home, name, false, project)
        Path file = new File(backup).toPath().toAbsolutePath().normalize()
        String record = job.relativize(file).toString().replace(File.separator, '/')
        if (!record.endsWith('/config.xml')) throw new IllegalStateException('无效自动升级备份路径')
        record = record.substring(0, record.length() - '/config.xml'.length())
        if (!record.startsWith(target + '/') || backupRecord(job, record) != file) {
            throw new IllegalStateException('自动升级备份不属于当前目标')
        }
        Path index = job.resolve('retained.json')
        List previous = []
        if (Files.exists(index, LinkOption.NOFOLLOW_LINKS)) {
            def data = new JsonSlurperClassic().parseText(new String(privateBackupBytes(index), 'UTF-8'))
            if (!(data instanceof Map) || data.format != 'ci-auto-promotion-v2' ||
                !(data.records instanceof List) || data.records.size() > 2 ||
                data.records.unique(false).size() != data.records.size() || !data.records.every { it instanceof String }) {
                throw new IllegalStateException('自动升级备份保留记录无效')
            }
            previous = data.records
            previous.each { backupRecord(job, it) }
        }
        List ordered = [record] + previous.findAll { it != record }
        List keep = ordered.take(2)
        // 仅成功升级登记的旧记录可清理；失败、未登记、UUID 与人工快照全部保留。
        Path temporary = Files.createTempFile(job, '.retained-', '.tmp',
            PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString('rw-------')))
        try {
            Files.write(temporary, JsonOutput.toJson([format: 'ci-auto-promotion-v2', records: keep]).getBytes('UTF-8'))
            if (Files.exists(index, LinkOption.NOFOLLOW_LINKS)) privateBackupBytes(index)
            Files.move(temporary, index, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
        } finally { Files.deleteIfExists(temporary) }
        ordered.drop(2).each { oldRecord ->
            Path oldFile = backupRecord(job, oldRecord)
            Files.delete(oldFile)
            // 目录中有未知文件时保留它们，不进行递归删除。
            try { Files.delete(oldFile.parent) } catch (java.nio.file.DirectoryNotEmptyException ignored) { }
            try { Files.delete(oldFile.parent.parent) } catch (java.nio.file.DirectoryNotEmptyException ignored) { }
        }
    }

    static Map promote(String name, Map proof, String pipeline, Map io, Map project = null) {
        verifyProof(proof, project)
        if (!jobMap(project).containsKey(name)) throw new IllegalArgumentException('不受管 Job')
        // 调用方用 Queue 锁覆盖繁忙检查、配置 CAS、备份与更新，避免排队检查竞态。
        io.lock {
            if (io.busy()) return [job: name, status: 'busy']
            String before = io.read()
            String old = pinnedCommit(before, name, project)
            if (old == proof.commit) return [job: name, status: 'unchanged', commit: old]
            if (io.ancestor(old, proof.commit) != true) throw new IllegalStateException('拒绝倒退或分叉版本')
            String after = updatedConfig(before, name, proof.commit, pipeline, project)
            if (io.read() != before) return [job: name, status: 'changed-externally']
            String backup = io.backup(before)
            String readback
            try {
                io.write(after)
                readback = io.read()
                if (pinnedCommit(readback, name, project) != proof.commit) throw new IllegalStateException('线上版本回读失败')
            } catch (Exception failure) {
                // 仅恢复本任务写入的定义；若管理员已修改配置，保留备份供人工恢复。
                String current = io.read()
                if (current == after || io.sameDefinition(current, after)) {
                    def restored = parse(current)
                    restored.definition[0].replaceNode(parse(before).definition[0])
                    io.write(XmlUtil.serialize(restored))
                }
                throw new IllegalStateException('升级失败；已保留配置备份', failure)
            }
            def result = [job: name, status: 'updated', before: old, commit: proof.commit, backup: backup]
            if (XmlUtil.serialize(parse(readback)) == XmlUtil.serialize(parse(after))) {
                try { io.completedBackup(backup, readback) }
                catch (Exception ignored) { result.backupRetention = 'blocked' }
            } else { result.backupRetention = 'concurrent-change' }
            result
        }
    }
}
