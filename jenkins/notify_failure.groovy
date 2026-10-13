def send(String platform, String result, String failureStage = '') {
    boolean controlledAlert = params.CONFIRM_FAILURE_ALERT == true && params.CONFIRM_TEXT == 'SEND_CONTROLLED_FAILURE'
    if (!controlledAlert && (params.containsKey('DRY_RUN') || params.containsKey('NOTIFY') || !(params.UPLOAD_AND_NOTIFY instanceof Boolean) || params.UPLOAD_AND_NOTIFY != true || env.CI_NOTIFY != 'true')) {
        echo '上传与通知关闭或未经校验：跳过失败通知。'
        return
    }
    def projectText = sh(script: 'python3 "$CI_TOOL_ROOT/project_config.py" --job "$JOB_NAME"', returnStdout: true).trim()
    def project = new groovy.json.JsonSlurperClassic().parseText(projectText)
    def role = project.jobs.find { key, job -> job.name == env.JOB_NAME }?.key
    if (!(role in ['android_test','android_release','ios_test','ios_release','harmony_test','harmony_release']) &&
        !(role == 'alert' && controlledAlert)) { error '当前 Job 无权使用交付失败通知' }
    if (role != 'alert' && (env.CI_NOTIFY != 'true' || params.UPLOAD_AND_NOTIFY != true)) { return }
    def provider = project.providers.dingtalk
    if (!provider?.webhookCredentialId || !provider?.secretCredentialId) { error '缺少受信项目通知绑定' }
    def brand = env.CI_BRAND ?: params.get('BRAND') ?: ''
    def uiEngine = env.CI_UI_ENGINE ?: params.get('UI_ENGINE') ?: ''
    def gitRef = env.CI_BRANCH ?: env.CI_TAG ?: params.get('GIT_BRANCH') ?: params.get('GIT_TAG') ?: 'master'
    def workItemType = env.CI_WORK_ITEM_TYPE ?: params.get('WORK_ITEM_TYPE') ?: ''
    def workItemId = env.CI_WORK_ITEM_ID ?: params.get('WORK_ITEM_ID') ?: ''
    def workItemUrl = env.CI_WORK_ITEM_URL ?: params.get('WORK_ITEM_URL') ?: ''
    withEnv([
        "CI_FAILURE_PLATFORM=${platform}",
        "CI_FAILURE_RESULT=${result}",
        "CI_FAILURE_BRAND=${brand}",
        "CI_FAILURE_UI_ENGINE=${uiEngine}",
        "CI_FAILURE_GIT_REF=${gitRef}",
        "CI_FAILURE_WORK_ITEM_TYPE=${workItemType}",
        "CI_FAILURE_WORK_ITEM_ID=${workItemId}",
        "CI_FAILURE_WORK_ITEM_URL=${workItemUrl}",
        "CI_FAILURE_STAGE=${failureStage ?: env.CI_FAILURE_STAGE ?: 'Pipeline'}"
    ]) {
        withCredentials([
            string(credentialsId: provider.webhookCredentialId, variable: 'DINGTALK_WEBHOOK'),
            string(credentialsId: provider.secretCredentialId, variable: 'DINGTALK_SECRET')
        ]) {
            sh '''
                PYTHONPATH="$CI_TOOL_ROOT" python3 "$CI_TOOL_ROOT/jenkins/jenkins_failure_notify.py" \
                    --job="$JOB_NAME" \
                    --build-number="$BUILD_NUMBER" \
                    --build-url="$BUILD_URL" \
                    --result="$CI_FAILURE_RESULT" \
                    --stage="${CI_FAILURE_STAGE:-Pipeline}" \
                    --platform="$CI_FAILURE_PLATFORM" \
                    --brand="$CI_FAILURE_BRAND" \
                    --ui-engine="$CI_FAILURE_UI_ENGINE" \
                    --git-ref="$CI_FAILURE_GIT_REF" \
                    --work-item-type="$CI_FAILURE_WORK_ITEM_TYPE" \
                    --work-item-id="$CI_FAILURE_WORK_ITEM_ID" \
                    --work-item-url="$CI_FAILURE_WORK_ITEM_URL"
            '''
        }
    }
}

return this
