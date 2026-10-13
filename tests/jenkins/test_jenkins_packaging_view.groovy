// 使用 GroovyShell 替身执行同一份初始化脚本，无需连接 Jenkins。
def loader = new GroovyClassLoader()
def viewClass = loader.parseClass('''
package hudson.model
class ListView {
    String name
    def owner
    Set jobNames = [] as Set
    def columns = ['custom-column']
    def jobFilters = ['custom-filter']
    String includeRegex = 'custom-.*'
    boolean recurse = false
    int saves = 0
    boolean isRecurse(){recurse}
    void setRecurse(boolean value){recurse=value}
    void save(){saves++}
    ListView(String name, owner) { this.name = name; this.owner = owner }
    void add(item) { jobNames.add(item.fullName); saves++ }
}
''')
def jenkinsClass = loader.parseClass('''
package jenkins.model
class Jenkins {
    static Jenkins instance = new Jenkins()
    static Jenkins get() { instance }
    def view
    Map views = [:]
    Map jobs = [:]
    int addedViews = 0
    def getView(String name) { name == '打包任务' ? view : views[name] }
    def getItemByFullName(String name) { jobs[name] }
    void addView(value) {
        if (value.name == '打包任务') view = value
        else views[value.name] = value
        addedViews++
    }
}
''')
def shell = new GroovyShell(loader)
def source = new File(args[0]).text
def jenkins = jenkinsClass.get()
def jobs = ['mobile-apps/sxmqlive-android-test', 'mobile-apps/sxmqlive-android-formal-release',
    'mobile-apps/sxmqlive-ios-test', 'mobile-apps/sxmqlive-ios-formal-release', 'mobile-apps/sxmqlive-ohos-test', 'mobile-apps/sxmqlive-ohos-formal-release']
jenkins.jobs = jobs.collectEntries { [(it): [name: it.tokenize("/").last(), fullName: it]] }
def roles = ['代码门禁': ['sxmqlive-pr-validation', 'sxmqlive-ci-pr-validation'],
    '管理员维护': ['mobile-apps/sxmqlive-signing-maintenance', '运维/sxmqlive-signing-backup',
               '运维/sxmqlive-wiki-navigation', '运维/Jenkins 失败告警验收'],
    '历史验收': ['sxmqlive-pr-validation-candidate-144', 'mobile-apps/cihang-qa-delivery-19',
              'sxmqlive-auxiliary-review-20261012']]
roles.values().flatten().each { jenkins.jobs[it] = [fullName: it] }

// 缺失视图创建并持久追加；重复初始化不再写入。
shell.evaluate(source)
assert jenkins.addedViews == 4
roles.each { name, members -> assert jenkins.views[name].jobNames == members as Set }
assert jenkins.view.jobNames == jobs as Set
assert jenkins.view.isRecurse()
assert jenkins.view.saves == 7
shell.evaluate(source)
assert jenkins.view.saves == 7
assert jenkins.addedViews == 4
roles.each { name, members -> assert jenkins.views[name].saves == members.size() + 1 }

// 已有成员和设置保持原值；不存在的 Job 不产生空入口。
def existing = viewClass.newInstance('打包任务', jenkins)
existing.jobNames.addAll(jobs[0..3] + ['custom-job'])
jenkins.view = existing
jenkins.jobs.remove('mobile-apps/sxmqlive-ios-test')
shell.evaluate(source)
assert jenkins.view.is(existing)
assert existing.jobNames == (jobs + ['custom-job']) as Set
assert existing.columns == ['custom-column']
assert existing.jobFilters == ['custom-filter']
assert existing.includeRegex == 'custom-.*'
assert existing.saves == 3
assert jenkins.addedViews == 4

jenkins.view = null
shell.evaluate(source)
assert jenkins.view.jobNames == (jobs - ['mobile-apps/sxmqlive-ios-test']) as Set
assert jenkins.view.saves == 6
assert jenkins.addedViews == 5

// 同名非 ListView 必须停止，不能替换用户配置。
jenkins.view = [name: '打包任务']
def conflicting = jenkins.view
try {
    shell.evaluate(source)
    assert false: 'Expected conflicting view type to fail'
} catch (IllegalStateException expected) {
    assert expected.message.contains('not a ListView')
}
assert jenkins.view.is(conflicting)
assert jenkins.addedViews == 5
println('Packaging view self-check passed: create, additive preservation, idempotence, missing Job, type conflict.')
