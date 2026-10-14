# 第三方材料与依赖

根 LICENSE 适用于本仓库原创源码及文档。外部工具、插件、镜像和证书数据保留各自来源与许可；
本声明不为第三方材料重新授权，也不代表容器镜像及其传递依赖已经完成发行许可审计。

当前发行候选仅包含中性 CI 原创源码、文档、示例及构建说明；不分发预构建镜像、SDK 或第三方二进制。
旧镜像剩余 112 条 SDK/DWS 记录及完整对应源码/NOTICE 缺口继续列为镜像发行阻塞，不阻断排除这些材料后的源码候选包；
不得因此把 UNKNOWN 改为已授权。具体范围与实际公开发布前置条件见 [开源准备](docs/开源准备.md)。

| 材料 | 来源与现有锁定方式 | 发行边界 |
| --- | --- | --- |
| Apple 公开中间证书 | [Apple PKI](https://www.apple.com/certificateauthority/)；固定 manifest 记录官方来源和 DER SHA-256 | 公开源码包排除 PEM，管理员按固定来源获取；私有生产 bundle 保留，不按 Apache-2.0 重新授权 |
| Jenkins 与插件 | [Jenkins 许可](https://www.jenkins.io/license/)；Controller Dockerfile 固定基础版本，`plugins.txt` 固定直接插件版本，镜像保留 resolved 清单 | 发布构建镜像时另核对实际解析的全部插件与系统包许可；不将插件二进制复制进源码发行包 |
| Groovy | [Apache Groovy](https://groovy-lang.org/license.html)；门禁锁定 2.4.21 与 SHA-256 | 源码包不含 JAR；参考 workflow 校验下载摘要 |
| Python、JDK、Node、Ruby、Docker、Gradle | 各平台官方发行与现有 Dockerfile/runbook | 属于外部执行工具；部署和二进制分发须保留其许可与来源 |
| Fastlane 与 Ruby gems | `delivery/fastlane/Fastfile` 与运行环境的安装清单 | 构建时下载；发行包含 gems 的镜像时按实际安装版本核对包许可 |
| Android、Xcode、HarmonyOS SDK | 厂商安装与 SDK 协议 | 不包含在公开源码包，也不随 Apache-2.0 重新授权 |
| GitHub Actions | `examples/github-actions/reusable-check.yml` 按完整 Commit 固定官方 Actions | 运行时由 GitHub 获取，不复制 Action 源码；新增 Action 应复核来源和许可 |

公开源码导出只包含版本控制内的源码和公开示例，不复制依赖缓存、SDK、JAR、APK/IPA/HAP 或运行材料。
第三方依赖升级由 CI MR 审核，不由日常业务 MR 自动改变；本表不是扫描全部依赖的替代品。

2026-10-13 已逐版核对 98 个插件、102 个本机 Fastlane 运行依赖及固定外部工具的许可证据，见
[详细核查](docs/第三方许可核查.md)与[机器清单](docs/audits/third-party-inventory.json)。
Fastlane 2.238.0 为 MIT。DWS 已从当前工具链移除；其外层声明不作为历史内嵌私有模块的授权。Gitee 插件声明 GPL v2，另有 EPL/CDDL 依赖，
它们没有随源码包分发。含依赖的镜像发行仍被核查入口拒绝，完整镜像 SBOM 和补证记录见[镜像许可核查](docs/镜像许可核查.md)；未确认组件、通知和适用的对应源码义务仍未消除。
