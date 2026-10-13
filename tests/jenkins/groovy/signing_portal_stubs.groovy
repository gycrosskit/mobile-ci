// 仅用于本地校验解析和进度逻辑；真实登录、权限、CSRF 必须在 Controller 验证。
def loader = new GroovyClassLoader(getClass().classLoader)
loader.parseClass('''package hudson.model
interface RootAction {}
class Item { static READ = 'read'; static BUILD = 'build' }
class Computer { static threadPoolForRemoting }
class TaskListener { static final NULL = new Object() }
class HdcLauncher {
    static List commands = []; static List outputs = []
    static String expectedHdc = '/Users/Shared/sxmqlive-jenkins-pr/tools/harmony/hdc'
    def launch() { new HdcStarter() }
}
class HdcStarter {
    def output; def error; boolean quietFlag
    def cmds(List command) { assert command[0] == HdcLauncher.expectedHdc; HdcLauncher.commands << command; this }
    def quiet(boolean value) { quietFlag = value; this }
    def stdout(value) { output = value; this }
    def stderr(value) { error = value; this }
    def start() { assert quietFlag && output.is(error); new HdcProc(output:output, result:HdcLauncher.outputs.remove(0)) }
}
class HdcProc {
    static boolean killed = false
    def output; Map result
    void kill() { killed = true }
    int joinWithTimeout(long seconds, unit, listener) {
        assert seconds == 3 && listener.is(TaskListener.NULL)
        if (result.timeout) { Thread.sleep(3000); killed = true; return 137 }
        if (result.interrupted) { throw new InterruptedException() }
        if (result.exception) { throw new IOException('private device output') }
        output.write((result.text ?: '').getBytes('UTF-8'))
        result.exit ?: 0
    }
}
class CaptureFuture {
    static boolean timeout = false; static boolean cancelled = false
    java.util.concurrent.Callable task
    def get(long seconds, java.util.concurrent.TimeUnit unit) {
        assert seconds <= 15
        if (timeout) { throw new java.util.concurrent.TimeoutException() }
        task.call()
    }
    def cancel(boolean interrupt) { cancelled = interrupt }
}
class Run {}
class Cause { static class UserIdCause {} }
class StringParameterValue { StringParameterValue(String name, value) {} }
class BooleanParameterValue { BooleanParameterValue(String name, boolean value) {} }
class PasswordParameterValue { PasswordParameterValue(String name, value, String description) {} }
class CauseAction { CauseAction(cause) {} }
class ParametersAction { ParametersAction(values) {} }
''')
loader.parseClass('''package hudson
class FilePath {
    static boolean present = true
    FilePath(channel, String path) { assert path == hudson.model.HdcLauncher.expectedHdc }
    boolean exists() { present }
}
''')
loader.parseClass('''package hudson.util
class Secret { static fromString(String value) { new Secret() } }

''')
loader.parseClass('''package jenkins.model
class Jenkins {
    static READ = 'read'; static ADMINISTER = 'admin'; static instance
    static get() { instance }
}
''')
loader.parseClass('''package org.kohsuke.stapler
interface StaplerRequest2 { String getContentType(); InputStream getInputStream() }
interface StaplerResponse2 {
    void setHeader(String key, String value); void setStatus(int status)
    void setContentType(String value); Writer getWriter()
}
''')
loader.parseClass('''package org.kohsuke.stapler.interceptor
import java.lang.annotation.*
@Retention(RetentionPolicy.RUNTIME)
@Target([ElementType.METHOD])
@interface RequirePOST {}
''')
def source = new File(args[0]).text.split('\ndef controller =')[0]
def tests = new File(args[1]).text
new GroovyShell(loader).evaluate(source + '\n' + tests)
