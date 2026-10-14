import com.cloudbees.plugins.credentials.CredentialsProvider
import com.cloudbees.plugins.credentials.common.StandardCredentials
import com.gitee.jenkins.connection.GiteeConnection
import com.gitee.jenkins.connection.GiteeConnectionConfig
import com.gitee.jenkins.connection.GiteeConnectionProperty
import com.gitee.jenkins.connection.GiteeApiToken
import com.gitee.jenkins.trigger.GiteePushTrigger
import com.gitee.jenkins.trigger.filter.BranchFilterType
import com.gitee.jenkins.trigger.filter.BuildInstructionFilterType
import hudson.security.ACL
import jenkins.model.Jenkins
import org.jenkinsci.plugins.plaincredentials.StringCredentials

def logger = java.util.logging.Logger.getLogger('sxmqlive.gitee.bootstrap')
def jenkins = Jenkins.get()
def loader = new GroovyClassLoader(jenkins.pluginManager.uberClassLoader)
def policy = loader.parseClass(new File(jenkins.rootDir, 'ci-auto-promotion/CiAutoPromotion.groovy'))
def project = policy.loadProject(new File(jenkins.rootDir, 'ci-projects'), System.getenv('JENKINS_CI_PROJECT') ?: 'sxmqlive')
def gateNames = ['app_pr','ci_pr'].findAll { role ->
    String repositoryRole = role == 'app_pr' ? 'application' : 'automation'
    def repository = project.repositories[repositoryRole]
    def provider = repository.containsKey('provider') ? repository.provider : project.scm?.provider ?: 'gitee'
    project.jobs[role] && provider == 'gitee'
}.collect { project.jobs[it].name }
def jobs = gateNames
    .collect { jenkins.getItemByFullName(it) }.findAll { it != null }
if (!jobs) {
    logger.info('MR 门禁尚未导入，跳过 Gitee 初始化。')
    return
}

jobs.each { job ->
    def credentials = CredentialsProvider.lookupCredentialsInItem(
        StandardCredentials.class,
        job,
        ACL.SYSTEM2,
        Collections.emptyList()
    )
    def findCredential = { String id -> credentials.find { it.id == id } }
    def apiToken = findCredential(project.credentials.giteeApi)
    def webhookSecret = findCredential(project.credentials.giteeWebhook)

    if (apiToken instanceof GiteeApiToken) {
        def config = jenkins.getDescriptor(GiteeConnectionConfig.class)
        def retained = config.connections.findAll { it.name != project.credentials.giteeConnection }
        retained.add(new GiteeConnection(
            project.credentials.giteeConnection,
            'https://gitee.com',
            project.credentials.giteeApi,
            'v5',
            false,
            10,
            20
        ))
        config.setConnections(retained)
        config.save()
        def current = job.getProperty(GiteeConnectionProperty.class)
        if (current == null || current.giteeConnection != project.credentials.giteeConnection) {
            if (current != null) {
                job.removeProperty(GiteeConnectionProperty.class)
            }
            job.addProperty(new GiteeConnectionProperty(project.credentials.giteeConnection))
        }
    }

    if (webhookSecret instanceof StringCredentials) {
        def trigger = job.getTriggers().values().find { candidate -> candidate instanceof GiteePushTrigger }
        if (trigger == null) {
            trigger = new GiteePushTrigger()
            job.addTrigger(trigger)
        }
        trigger.setTriggerOnPush(false)
        trigger.setTriggerOnCommitComment(false)
        trigger.setTriggerOnOpenPullRequest(true)
        trigger.setTriggerOnUpdatePullRequest('1')
        trigger.setTriggerOnAcceptedPullRequest(false)
        trigger.setTriggerOnClosedPullRequest(false)
        trigger.setTriggerOnApprovedPullRequest(false)
        trigger.setTriggerOnTestedPullRequest(false)
        trigger.setTriggerOnNoteRequest(false)
        trigger.setBuildInstructionFilterType(BuildInstructionFilterType.CI_SKIP)
        trigger.setSkipWorkInProgressPullRequest(true)
        trigger.setCancelIncompleteBuildOnSamePullRequest(true)
        trigger.setBranchFilterType(BranchFilterType.NameBasedFilter)
        trigger.setIncludeBranchesSpec(job.fullName == project.jobs.ci_pr?.name ? project.repositories.automation.defaultBranch : project.repositories.application.defaultBranch)
        trigger.setExcludeBranchesSpec('')
        trigger.setSecretToken(webhookSecret.secret.plainText)
    }

    def ready = apiToken instanceof GiteeApiToken && webhookSecret instanceof StringCredentials
    def jobConfig = project.jobs.values().find { it.name == job.fullName }
    def enabled = ready && jobConfig.enabled != false
    job.setDisabled(!enabled)
    job.save()
    if (enabled) {
        logger.info('Gitee MR 门禁已配置并启用。')
    } else if (ready) {
        logger.info('Gitee MR 门禁已配置，按受信项目声明保持禁用。')
    } else {
        def missing = []
        if (!(apiToken instanceof GiteeApiToken)) missing.add('gitee-api-token (Gitee API Token)')
        if (!(webhookSecret instanceof StringCredentials)) missing.add(project.credentials.giteeWebhook)
        logger.warning('Gitee MR 门禁保持禁用，缺少 Credential ID：' + missing.join(', '))
    }

}
