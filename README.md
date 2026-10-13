# 移动端 CI

复用 Android、iOS 和 HarmonyOS 的门禁、签名运维、交付与恢复工具，生产应用编排使用 Jenkins + Gitee，通用工具的公开目标是 `gycrosskit/mobile-ci`。
应用保留 Gradle/Xcode/Hvigor 工程、品牌事实和本地 Debug 工具；日常业务改动只更新应用仓库，消费固定 CI 版本。

| 能力 | 当前支持与边界 |
| --- | --- |
| Jenkins + Gitee | 实际支持：固定可信 MR 门禁、项目绑定的 Job/选择器、固定 CI SHA 升级 |
| Android | QA/Release 构建、严格验签、归档；授权后上传与通知 |
| iOS | Mac Agent Archive/Export、临时签名、验签、蒲公英/TestFlight/App Store Connect |
| HarmonyOS | QA/Debug 测试包与 Release 指定设备 HAP、验签；授权后蒲公英及通知；未实现市场提交 |
| 多项目 | 受信项目 JSON、薄构建适配、账户/回执/备份隔离；[第二项目示例](examples/second-project/README.md) |
| GitHub Actions | [工具安全自测](docs/GitHub迁移.md)；[独立应用 PR 参考](docs/GitHub接入.md) 可按需接入，私有应用无需新增 YAML |

当前按[纯源码发行范围](docs/开源准备.md#本轮验收范围纯源码发行)收尾验收，只准备中性 CI 工具源码、
文档和示例；应用继续留在私有 Gitee，不新增 YAML，现有生产 Jenkins 保持原位。
115 条未确认组件属于镜像发行阻塞，镜像/SDK/第三方二进制不随源码候选包发布。
源码技术验收与实际公开发布分开记录；[SECURITY](SECURITY.md) 已登记维护者授权的安全报告邮箱，2026-10-13 已完成一次自发自收，一次外部邮件已送达，经人工纠正垃圾分类后在收件箱；
工具迁入 `gycrosskit/mobile-ci` 的范围与步骤见 [GitHub 迁移](docs/GitHub迁移.md)。

| 目录 | 唯一职责 |
| --- | --- |
| `jenkins/` | Pipeline、Job 模板、Controller/Agent 运维与 Wiki 导航 |
| `delivery/` | 构建、验收、发布上下文、渠道上传与通知 |
| `signing/` | 证书、Profile、设备、凭据维护与加密备份 |
| `tests/` | 行为及安全契约测试 |
| `docs/`、`examples/` | 接入、运维、验收与可执行参考 |

## 工具链

本地示例检查需要 Git、Python 3.9+、JDK 17 或 21；生产 Jenkins/Agent 固定 JDK 21。
Groovy 行为检查使用与 Controller 相同的 Groovy 2.4.21，
由 `GROOVY_TEST_CLASSPATH` 绑定受信 jar，不能用 Gradle 自带版本代替。部署需 Docker 与 Compose v2、OpenSSH。
Android SDK/NDK 以 [Agent Dockerfile](jenkins/remote/android-agent/Dockerfile) 为准；iOS 需要 macOS、
[固定 Xcode](jenkins/local/.xcode-version)、[Brewfile](jenkins/local/Brewfile) 和
[固定 Fastlane](delivery/.fastlane-version)；鸿蒙还需项目对应的官方 SDK、Hvigor/ohpm 和签名工具。
鸿蒙可用[公开基础镜像与离线 CLT 安装器](docs/鸿蒙环境重建.md)重建；SDK 由获授权管理员获取，
不随源码或镜像分发。既有统一 Android/OHOS 节点使用显式审核的兼容基础，不自动迁移。

## Quickstart：先检查，再接入

公开工具先按 [贡献指南](CONTRIBUTING.md) 准备只读 Groovy JAR与运行时，再执行
`python3 jenkins/run_public_checks.py`；无需公司 profile、应用、SDK或凭据。
下面的生产项目命令需另行准备审核后的应用和私有配置。

在独占 CI Worktree 中，应用独立放在 `source/`，CI 工具放在 `automation/`，分别记录精确提交：

```text
workspace/
  automation/    CI 工具：已审 40 位 SHA
  source/        应用：选定分支、Tag 或 MR 的实际 SHA
```

已有项目的本地源码检查：

```bash
export CI_TOOL_ROOT="$PWD"
export APP_SOURCE_ROOT=/absolute/path/application
export PYTHONPATH="$CI_TOOL_ROOT"
export CI_PROJECT_ID='替换为已审项目ID'
export CI_AUTOMATION_COMMIT='替换为已审40位提交'
python3 jenkins/check_repository_contract.py
python3 jenkins/generate_jenkins_job_templates.py --project="$CI_PROJECT_ID" --check
python3 jenkins/sync_jenkins_jobs.py render --project="$CI_PROJECT_ID"
python3 -m unittest discover -s tests -t . -b
```

这些命令检查源码和渲染模板，不部署 Controller、不触发构建。新项目按
[接入命令链](docs/项目接入.md#第二项目可执行示例) 操作；首次真实构建明确设置 `UPLOAD_AND_NOTIFY=false`。
六个生产打包 Job 的默认值仍为 `true`，构建前核对所选值和当前操作授权。

验收至少留存应用 SHA、可信 Pipeline/CI SHA、项目/Job 身份、工具链、签名与产物报告；
Controller 实际 CPS、权限拒绝、审批持久化重启回读按 [运行验收](docs/运行验收.md) 单独完成。
普通 Python/Groovy 测试通过不等于实际 Pipeline、设备安装或供应商发布验收。

接入与日常操作由 [文档导航](docs/README.md) 进入；
[Linux Controller/Agent runbook](jenkins/remote/README.md) 与
[macOS Agent runbook](jenkins/local/README.md) 是各自唯一运维入口。
私有维护源保留已有部署兼容；公司配置、公网地址和私有运行材料按 [开源准备](docs/开源准备.md)
与中性发行包隔离，Docker 不内嵌实例项目配置。仓库采用 [Apache-2.0](LICENSE)，
许可证不授予外部发布、通知或生产维护权限。
凭据、私钥、P12、Profile、密码、Token 和恢复密钥不进入 Git。

新项目由[中性模板](docs/模板扩展.md)生成固定 Job 与审批文本，不依赖公司旧字符串替换。
公开发行前运行[历史秘密扫描与第三方许可核查](docs/第三方许可核查.md)；镜像/SDK 分发需独立核查，
源码许可证不会自动覆盖 Gitee 插件、厂商 SDK 或公开 CA 数据。
