# 第二项目：原生 Android 接入验收

在 CI 工具仓库根运行：

```bash
export GROOVY_TEST_CLASSPATH=/absolute/path/to/groovy-all-2.4.21.jar
python3 examples/second-project/validate.py
```

需要 Python 3.9+、git、JDK 和已有 Groovy jar。未设置 classpath、jar 不存在或命令执行失败均返回非零，不通过
skip 冒充成功。脚本不下载工具，不需要私有应用、Jenkins、生产凭据或供应商账号。验收通常约 5 秒；
所有检出、临时 Git 提交、派生模板、合成产物和回执位于临时目录，退出时删除，不改当前仓库 Git 状态。

| 文件 | 职责 |
| --- | --- |
| [project.json](project.json) | 经审核的静态示例配置，项目 `example-mobile`、`testOnly: true` |
| [application/](application/) | 独立 native Android 工程；`main` 分支、`mobile` 模块、`org.example.mobile` |
| [application/ci/brands.json](application/ci/brands.json) | 示例品牌与凭据 ID；全零证书摘要是假材料，不能用于签名 |
| [validate.py](validate.py) | 只在临时工具根安装固定 profile，并调用真实入口验证接线 |

应用仓库是 `example/native-mobile`，CI 仓库是 `example/mobile-automation`；Folder 是 `Example Mobile`，
四个 Job 分别为 `android-debug`、`android-release`、`application-pr`、`automation-pr`。
供应商账户和 SCM、API、Webhook、签名凭据均为 `example-mobile-*`，没有凭据值。
这组远端地址仅用作身份 fixture，脚本不访问 Gitee。

## 默认验收覆盖

1. 将静态 profile 安装到临时 CI 的固定 `projects/example-mobile.json`，通过 `load_project` 读取；候选应用的
   `CI_PROJECT_CONFIG` 路径不能替换配置。临时 application 和 automation 仓库各生成真实 40 位 SHA。
2. 调用 `check_repository_contract.py`，运行应用 manifest 测试与真实 Git diff 的受影响任务规划；调用
   `generate_jenkins_job_templates.synchronize` 和 `sync_jenkins_jobs.render_all`，检查 Folder/Job、仓库、
   默认 `main`、`native` 和签名凭据 ID；`android_ci.sh describe` 解析 `lintDebug` / `assembleDebug`。
3. 执行真实 Jenkinsfile 中的 Groovy Webhook 与 SHA 证据校验，并复用 Jenkins DSL 替身确认
   `UPLOAD_AND_NOTIFY=false` 在成功、失败和 unstable 时均不执行通知或绑定通知凭据。
4. 拒绝越界配置 ID、越界产物路径、外来 Job/仓库、非法及不匹配 SHA、旧项目 namespace，
   并拒绝跨供应商账号读取回执或在旧账号 pending 时重发；两种 Job apply 入口和 Controller loader
   均必须拒绝 `testOnly` 示例，部署函数调用次数为 0。
5. 用明确标注的**合成 ZIP fixture** 调用 `release.py` 的 prepare、upload dry-run、notify dry-run 实际处理函数，
   对供应商函数作 mock 并断言调用次数为 0。没有真实 SDK 编译、Jenkins 凭据读取、签名验收、上传、通知或部署。

这里的 APK fixture 无法安装，不是编译或验签证据。Job XML 与假签名配置也不是生产部署材料。
`testOnly` 示例由两种 Job apply 入口和 Controller loader 拒绝部署；正式接入必须由管理员另行审核可信 profile、
品牌事实、证书摘要、Folder 凭据和固定 CI SHA。生产签名、设备安装、供应商发布仍需独立验收。

## 可选真实 Debug 构建

```bash
python3 examples/second-project/validate.py --build
```

该参数执行 `android_ci.sh verified-build example test native`，使用原生 Android SDK 工程，随后用真实 Debug
APK 代替合成 fixture 进行本地 prepare/dry-run 验证。需要预装 JDK 17、AGP 8.9.2 兼容的 Gradle 8.x
（至少 8.11.1）、Android SDK platform 36 和对应 build-tools。`application/gradlew` 是诚实的本地 Gradle
启动器，不是 Gradle Wrapper；Gradle 会按工程配置解析 AGP，首次构建可能需要联网下载依赖。
缺 SDK 或 Gradle 时明确失败，默认命令仍可完整执行工具接线验收。

真实 Debug 构建使用 Android 本地默认 debug key，不消费示例签名凭据，不构成 Jenkins 品牌证书验收。
脚本不部署 XML，也不运行发布 Job，供应商函数依然被 mock 且 dry-run。

最小回归：

```bash
python3 -m unittest tests.jenkins.test_second_project_example -v
```
