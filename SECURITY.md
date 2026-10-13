# 安全问题报告

请勿在公开 Issue、MR 或日志附上 Token、Cookie、密码、Webhook、私钥、P12、Profile、完整 UDID、
Jenkins 解密材料或供应商完整响应。

## 私有报告渠道

优先使用已启用的 [GitHub 私有漏洞报告](https://github.com/gycrosskit/mobile-ci/security/advisories/new)。
入口启用不代表已验证真实报告的完整处理流程。

也可将安全漏洞私下发送至 [guoyanggit@gmail.com](mailto:guoyanggit@gmail.com)，不要在公开 Issue、MR 或日志披露
完整漏洞利用细节。建议邮件主题为 `CI 安全报告：简要描述`；初次联系只提供脱敏材料，不附带真实凭据或用户数据。

维护者已于 2026-10-13 授权此邮箱并完成一次自发自收及一封外部测试投递。
外部邮件初次被归入垃圾箱，经人工纠正后已在收件箱；不保证其他发件方自动归入收件箱，
维护者仍需检查垃圾箱，不承诺响应时限。私有投递回执不随公开源码发布。
邮箱没有已公布的加密公钥；传递敏感证据前请先约定受限接收方式。

报告渠道配置见 [GitHub 官方说明](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)。
应用与公司实例继续留在私有 Gitee；公开工具的报告入口不要求迁移应用。

## 报告内容与处理边界

报告应包含受影响的完整 CI Commit、入口、平台、脱敏复现步骤、影响和可选修复建议。
证据留在受限目录；不得通过上传安装包、发送业务通知、读取生产秘密或改变生产配置来复现安全问题。
维护者通过已经确认的私有渠道核对范围与修复，不在公开讨论中回填敏感原文，也不凭报告自动部署或发布。

## 安全边界

- 候选应用只能提供构建源码，不能决定 Controller 策略、签名/发布凭据、运行节点或受信项目配置路径。
- CI 工具及 workflow 使用已审完整 Commit；实际检出 SHA 与上下文、签名报告和回执一致才允许交付。
- 示例 `testOnly=true` 可以离线验证与渲染，部署 CLI 和 Controller 拒绝安装；移除标记不能代替真实接入审核。
- 维护、PR 与打包节点隔离；未受信 PR 不读取签名、上传或通知凭据。公开参考 workflow 只用 GitHub-hosted runner。
- 未知上传/通知状态保留回执并阻断重发，不通过换目录、改账号或删除状态绕过。目录与文件权限、NOFOLLOW、
  账号、接收人、公钥摘要、验签及审批约束继续生效。

运行与重启验收见 [运行验收](docs/运行验收.md)，开源发行边界见 [开源准备](docs/开源准备.md)。
