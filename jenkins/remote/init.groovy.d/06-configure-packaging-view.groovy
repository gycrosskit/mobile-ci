import hudson.model.ListView
import jenkins.model.Jenkins

def jenkins = Jenkins.get()
def view = jenkins.getView('打包任务')
if (view != null && !(view instanceof ListView)) {
    throw new IllegalStateException('Existing packaging view is not a ListView; preserve it for administrator review.')
}
if (view == null) {
    view = new ListView('打包任务', jenkins)
    jenkins.addView(view)
}

// 根视图须递归显示 Folder 内 Job；保留已有成员、列与过滤规则。
if (!view.isRecurse()) {
    view.setRecurse(true)
    view.save()
}

// 只追加已存在的打包 Job，保留管理员配置的其他成员、列与过滤规则。
[
    'mobile-apps/sxmqlive-android-test',
    'mobile-apps/sxmqlive-android-formal-release',
    'mobile-apps/sxmqlive-ios-test',
    'mobile-apps/sxmqlive-ios-formal-release',
    'mobile-apps/sxmqlive-ohos-test',
    'mobile-apps/sxmqlive-ohos-formal-release'
].each { jobName ->
    def item = jenkins.getItemByFullName(jobName)
    if (item != null && !view.jobNames.contains(jobName)) {
        view.add(item)
    }
}
println('Configured packaging view without replacing existing members or settings.')

// 固定职责入口只引用已有 Job；视图不创建任务、不调整权限，也不改变 Webhook 身份。
[
    '代码门禁': ['sxmqlive-pr-validation', 'sxmqlive-ci-pr-validation'],
    '管理员维护': ['mobile-apps/sxmqlive-signing-maintenance', '运维/sxmqlive-signing-backup',
               '运维/sxmqlive-wiki-navigation', '运维/Jenkins 失败告警验收'],
    '历史验收': ['sxmqlive-pr-validation-candidate-144', 'mobile-apps/cihang-qa-delivery-19',
              'sxmqlive-auxiliary-review-20261012']
].each { name, members ->
    def roleView = jenkins.getView(name)
    if (roleView != null && !(roleView instanceof ListView)) {
        throw new IllegalStateException('Existing role view is not a ListView; preserve it for administrator review.')
    }
    if (roleView == null) {
        roleView = new ListView(name, jenkins)
        jenkins.addView(roleView)
    }
    if (!roleView.isRecurse()) { roleView.setRecurse(true); roleView.save() }
    members.each { member ->
        def item = jenkins.getItemByFullName(member)
        if (item != null && !roleView.jobNames.contains(member)) { roleView.add(item) }
    }
}
