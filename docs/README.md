# CI 文档入口

通用工具已公开于 [gycrosskit/mobile-ci](https://github.com/gycrosskit/mobile-ci)；应用继续私有 Gitee，消费管理员审核的固定工具 Commit。

1. [项目接入与复用](项目接入.md)：可信项目配置、薄构建适配和账户隔离。
2. [通用部署](部署.md)、[运行验收](运行验收.md)、[新 Mac 迁移](新Mac迁移.md)与[任务管理](任务管理.md)：统一锁版、独立节点、迁移方案和验收。
3. [GitHub 迁移与工具安全自测](GitHub迁移.md)：公共源码、Actions 与私有兼容边界。
4. [开源准备](开源准备.md)与[上游许可](../THIRD_PARTY.md)：源码范围及镜像/SDK阻塞。
5. [第二项目示例](../examples/second-project/README.md)与[可选应用 PR 入口](GitHub接入.md)：离线验证和按需接入。

[鸿蒙环境重建](鸿蒙环境重建.md)和[Apple证书获取](Apple证书获取.md)提供官方材料获取入口。
私有维护源的[发布流程](发布流程.md)、[仓库接线](仓库接线与切换.md)、[模板兼容](模板扩展.md)、
[Linux runbook](../jenkins/remote/README.md)、[Mac runbook](../jenkins/local/README.md)仍支持现有实例；
公开导出以中性入口替代公司操作及私有证据，不公开生产材料。
安全报告见 [SECURITY](../SECURITY.md)。旧镜像剩余 112 条 SDK/DWS 仍 UNKNOWN；默认基础镜像排除这些 payload，其他发行义务见[镜像许可核查](镜像许可核查.md)。完整镜像仍须独立验收；官方 CLT 已通过隔离运行预检、冷缓存实际应用 HAP 构建与严格签名验签，设备安装及供应商发行仍须独立验证。
