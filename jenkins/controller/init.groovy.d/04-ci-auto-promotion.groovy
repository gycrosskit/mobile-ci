import com.cloudbees.plugins.credentials.CredentialsProvider
import com.gitee.jenkins.connection.GiteeApiToken
import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic
import groovy.xml.XmlUtil
import hudson.model.InvisibleAction
import hudson.model.RootAction
import hudson.model.TaskListener
import hudson.plugins.git.util.BuildData
import hudson.security.ACL
import jenkins.model.Jenkins
import jenkins.util.Timer
import java.nio.file.Files
import java.nio.file.attribute.PosixFilePermissions
import java.util.concurrent.Callable
import java.util.concurrent.TimeUnit

/** 管理员固定部署的 Controller 代码，不从候选 MR 加载或执行代码。 */
class SxmqliveCiAutoPromotion extends InvisibleAction implements RootAction, Runnable {
    final Class policy
    final File home
    final Map project
    final Map original
    def future
    Map cache = [:]

    SxmqliveCiAutoPromotion(Class policy, File home, Map project, Map original = null) { this.policy = policy; this.home = home; this.project = project; this.original = original }

    static void privateDirectory(File file) {
        Files.createDirectories(file.toPath())
        Files.setPosixFilePermissions(file.toPath(), PosixFilePermissions.fromString('rwx------'))
    }

    static void privateWrite(File file, String text) {
        privateDirectory(file.parentFile)
        def temporary = Files.createTempFile(file.parentFile.toPath(), '.promotion-', '.tmp')
        try {
            Files.setPosixFilePermissions(temporary, PosixFilePermissions.fromString('rw-------'))
            Files.write(temporary, text.getBytes('UTF-8'))
            Files.move(temporary, file.toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING,
                java.nio.file.StandardCopyOption.ATOMIC_MOVE)
        } finally { Files.deleteIfExists(temporary) }
    }

    def api(String path) {
        policy.requireGiteePromotion(project)
        if (cache.containsKey(path)) return cache[path]
        def gate = Jenkins.get().getItemByFullName(policy.gateName(project))
        if (!gate) throw new IllegalStateException('固定项目门禁缺失；拒绝无 Item 范围的凭据查找')
        def credential = CredentialsProvider.lookupCredentialsInItem(GiteeApiToken.class, gate,
            ACL.SYSTEM2, Collections.emptyList()).find { it.id == project.credentials.giteeApi }
        if (!credential) throw new IllegalStateException('缺少既有 Gitee 凭据')
        def connection = new URL('https://gitee.com/api/v5/repos/' + project.repositories.automation.identity + path).openConnection()
        connection.connectTimeout = 10000
        connection.readTimeout = 20000
        connection.instanceFollowRedirects = false
        connection.setRequestProperty('Authorization', 'Bearer ' + credential.apiToken.plainText)
        try {
            if (connection.responseCode != 200) throw new IllegalStateException('Gitee 只读核验失败')
            String body = connection.inputStream.withCloseable { stream ->
                byte[] bytes = stream.readNBytes(2 * 1024 * 1024 + 1)
                if (bytes.length > 2 * 1024 * 1024) throw new IllegalStateException('核验响应超限')
                new String(bytes, 'UTF-8')
            }
            cache[path] = new JsonSlurperClassic().parseText(body)
        } finally { connection.disconnect() }
    }

    boolean ancestor(String before, String after) {
        policy.requireGiteePromotion(project)
        policy.sha(before); policy.sha(after)
        before == after || api('/compare/' + before + '...' + after).merge_base_commit?.sha == before
    }

    String pipeline(String commit, String role = 'ci_pr') {
        policy.sha(commit)
        if (!(role in ['ci_pr','app_pr']) || role == 'app_pr' && project.id == 'sxmqlive') throw new IllegalArgumentException('不支持的固定内联门禁角色')
        String path = role == 'app_pr' ? 'jenkins/templates/pipelines/Jenkinsfile.project-pr-validation' :
            project.id == 'sxmqlive' ? 'jenkins/Jenkinsfile.ci-pr-validation' : 'jenkins/templates/pipelines/Jenkinsfile.ci-pr-validation'
        def result = api('/contents/' + path + '?ref=' + commit)
        if (result.type != 'file' || result.encoding != 'base64' || !(result.content instanceof String)) {
            throw new IllegalStateException('可信门禁源文本不可用')
        }
        policy.renderProjectText(new String(result.content.replaceAll(/\s/, '').decodeBase64(), 'UTF-8'), project, original, role)
    }

    Map proof(String commit) {
        policy.requireGiteePromotion(project)
        String tree = policy.sha(api('/commits/' + commit).commit.tree.sha)
        def gate = Jenkins.get().getItemByFullName(policy.gateName(project))
        if (!gate) throw new IllegalStateException('固定项目门禁缺失')
        for (def run : gate.builds.take(30)) {
            if (run.isBuilding() || run.result?.toString() != 'SUCCESS') continue
            def revisions = run.getActions(BuildData.class).findAll { it.remoteUrls.contains(policy.repository(project)) }
            if (revisions.size() != 1) continue
            String testedMerge = policy.sha(revisions[0].lastBuiltRevision.sha1.name())
            String testedTree = policy.sha(api('/commits/' + testedMerge).commit.tree.sha)
            if (tree != testedTree) continue
            def environment = run.getEnvironment(TaskListener.NULL)
            def event = new JsonSlurperClassic().parseText(environment.jsonBody ?: '{}')
            String iid = environment.giteePullRequestIid
            if (!(iid ==~ /[1-9][0-9]*/)) continue
            def mr = api('/pulls/' + iid)
            String head = policy.sha(event.pull_request?.head?.sha)
            if (mr.state != 'merged' || mr.base?.ref != project.repositories.automation.defaultBranch || mr.head?.sha != head ||
                environment.giteeTargetBranch != project.repositories.automation.defaultBranch ||
                environment.giteeTargetNamespace != project.repositories.automation.identity.split('/')[0] ||
                environment.giteeTargetRepoName != project.repositories.automation.identity.split('/')[1]) continue
            String executed = run.execution.script
            def matcher = executed =~ /CI_AUTOMATION_COMMIT = '([0-9a-f]{40})'/
            if (matcher.count != 1) continue
            String trusted = policy.sha(matcher[0][1])
            if (!ancestor(trusted, commit) || executed != pipeline(trusted).replace('__CI_AUTOMATION_COMMIT__', trusted)) continue
            def result = [commit: commit, tree: tree, testedTree: testedTree, testedMerge: testedMerge,
                result: 'SUCCESS', building: false, repository: project.repositories.automation.identity,
                target: project.repositories.automation.defaultBranch, mrState: mr.state, head: head, mergedHead: mr.head.sha,
                trustedScript: true, gateBuild: run.number, mr: iid]
            policy.verifyProof(result, project)
            return result
        }
        throw new IllegalStateException('没有与 master 内容对应的已合并成功可信门禁')
    }

    static boolean busy(def job, def queue) {
        job.isBuilding() || queue.items.any { it.task.ownerTask == job }
    }

    Map adapter(def job, String old, String commit, boolean isAncestor) {
        def read = { job.configFile.asString() }
        [lock: { Closure operation -> hudson.model.Queue.withLock(operation as Callable) },
         busy: { busy(job, Jenkins.get().queue) }, read: read,
         ancestor: { from, to -> from == old && to == commit && isAncestor },
         backup: { xml ->
             policy.backupSnapshot(home, job.fullName, commit, xml, project)
         },
         completedBackup: { backup, verified ->
             if (read() != verified) throw new IllegalStateException('管理员已并发修改配置；保留全部备份')
             policy.retainBackup(home, job.fullName, commit, backup, project)
         },
         write: { xml ->
             // 只替换定义，避免覆盖管理员同时修改的权限、参数、触发器和启停状态。
             String definition = XmlUtil.serialize(policy.parse(xml).definition[0])
             def value = Jenkins.XSTREAM2.fromXML(definition)
             if (!(value instanceof org.jenkinsci.plugins.workflow.flow.FlowDefinition)) {
                 throw new IllegalStateException('不支持的 Pipeline 定义')
             }
             job.setDefinition(value)
         },
         sameDefinition: { left, right ->
             XmlUtil.serialize(policy.parse(left).definition[0]) == XmlUtil.serialize(policy.parse(right).definition[0])
         }]
    }

    synchronized Map check() {
        cache = [:]
        def config = new JsonSlurperClassic().parse(new File(home, 'config.json'))
        if (config.enabled != true) return [status: 'disabled']
        policy.requireGiteePromotion(project)
        if (!(config.jobs instanceof List) || !config.jobs || config.jobs.unique(false).size() != config.jobs.size() ||
            !config.jobs.every { policy.jobMap(project).containsKey(it) }) throw new IllegalStateException('非法升级白名单')
        String branchPath = '/branches/' + java.net.URLEncoder.encode(project.repositories.automation.defaultBranch, 'UTF-8')
        String commit = policy.sha(api(branchPath).commit.sha)
        Map evidence = proof(commit)
        String gatePipeline = config.jobs.contains(policy.gateName(project)) ? pipeline(commit) : null
        def results = []
        for (String name : config.jobs) {
            try {
                def job = Jenkins.get().getItemByFullName(name)
                if (!job) throw new IllegalStateException('受管 Job 缺失')
                String old = policy.pinnedCommit(job.configFile.asString(), name, project)
                boolean isAncestor = ancestor(old, commit)
                String role = policy.inlineRole(name, project)
                String candidatePipeline = role == 'ci_pr' ? gatePipeline : role == 'app_pr' ? pipeline(commit, role) : null
                cache.remove(branchPath)
                if (api(branchPath).commit.sha != commit) throw new IllegalStateException('默认分支核验后已变化')
                results << policy.promote(name, evidence, candidatePipeline, adapter(job, old, commit, isAncestor), project)
            } catch (Exception error) { results << [job: name, status: 'failed', error: error.class.simpleName] }
        }
        Map report = [commit: commit, gateBuild: evidence.gateBuild, mr: evidence.mr, jobs: results]
        privateWrite(new File(home, 'status.json'), JsonOutput.prettyPrint(JsonOutput.toJson(report)))
        report
    }

    void run() {
        try {
            def context = ACL.as2(ACL.SYSTEM2)
            try { check() } finally { context.close() }
        }
        catch (Exception error) {
            Map blocked = [status: 'blocked', error: error.class.simpleName]
            if (policy.automationProvider(project) != 'gitee') blocked.reason = 'unsupported-automation-provider'
            privateWrite(new File(home, 'status.json'), JsonOutput.toJson(blocked))
        }
    }
}

def jenkins = Jenkins.get()
def home = new File(jenkins.rootDir, 'ci-auto-promotion')
// 没有管理员显式配置时不注册，镜像升级不会自行启用升级器。
if (!new File(home, 'config.json').isFile()) return
def loader = new GroovyClassLoader(jenkins.pluginManager.uberClassLoader)
def policy = loader.parseClass(new File(home, 'CiAutoPromotion.groovy'))
def actions = jenkins.getExtensionList(RootAction.class)
def previous = actions.find { it.class.name == 'SxmqliveCiAutoPromotion' }
if (previous) { previous.future?.cancel(false); actions.remove(previous) }
def worker = new SxmqliveCiAutoPromotion(policy, home, policy.loadProject(new File(jenkins.rootDir, 'ci-projects'), System.getenv('JENKINS_CI_PROJECT') ?: 'sxmqlive'))
actions.add(worker)
worker.future = Timer.get().scheduleWithFixedDelay(worker, 15, 120, TimeUnit.SECONDS)
