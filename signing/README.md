# 签名工具与自动备份

品牌事实与签名备份接收材料属于应用仓库：`APP_SOURCE_ROOT/ci/brands.json` 和
`APP_SOURCE_ROOT/signing/backups/`。本目录只维护 CLI、公开 Apple CA 资源和备份加密实现，
不复制应用品牌 JSON、接收公钥、恢复私钥密文或离线恢复密钥。

自动快照只追加到应用仓库 `sxmqlive-android` 的独立 `backup/signing` 分支，不写 CI 仓库。
维护 apply 前置备份失败即阻断；后置失败仅保留待备份状态，定时任务只重试备份。
离线恢复步骤见[部署 runbook](../jenkins/remote/README.md#凭据加密快照与-git)。

快照归档前仅清空 Active Choices `CascadeChoiceParameter`、`DynamicReferenceParameter` 中
`parameters class="linked-hash-map"` 的页面选择缓存；不改线上 Job XML。真实参数 choices、脚本、
Pipeline 提交、ACL、启停、凭据、节点和签名材料仍参与归档与摘要。普通业务仅改变应用提交或构建号，
以及页面切换品牌/分支，不会因此追加 `backup/signing` 快照；真实恢复配置或材料改变仍追加快照。
首次部署此规则可能产生一次稳定化后的新基线；旧快照和恢复协议保持兼容。
