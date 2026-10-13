# 贡献指南

修改范围先读 [文档入口](docs/README.md)。应用源码、品牌事实和项目生产配置由各应用仓库维护；
本仓库只维护通用 CI 工具、受信边界、中性示例和回归。请勿提交 SDK、签名材料、供应商响应或生产凭据。

1. 在独占工作区修改相关实现和调用方，保留校验、拒绝路径与错误恢复。
2. 准备 Git、Python、JDK 21、Node、Ruby/Psych、Docker Compose CLI、OpenSSL 3 和摘要锁定的 Groovy 2.4.21。
3. 执行 `python3 jenkins/run_public_checks.py`。它使用临时树与合成 fixture，不部署、不上传、不通知。
4. 涉及旧实例兼容时另在受限环境执行完整私有回归。公共与私有验收范围见 [GitHub 迁移](docs/GitHub迁移.md)。
5. PR 说明变更原因、实际验证和剩余限制。生产升级、外部交付与镜像发行另行授权。

安全问题按 [SECURITY](SECURITY.md) 私下报告。原创代码采用 [Apache-2.0](LICENSE)，保留
[NOTICE](NOTICE) 与 [上游声明](THIRD_PARTY.md)；新增第三方材料必须提供来源和许可。
