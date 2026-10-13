# GitHub Actions 参考入口

本目录与本仓库 `.github/workflows` 工具自测同步。入口在干净公开源码树编排 Jenkins/Gitee 核心 CLI 与安全回归；
它与下方新增的 GitHub 应用 PR 入口分开。这里使用 GitHub Actions 编排现有公开 CLI 与
纯本地回归，不表示 GitHub PR 状态回写、签名、打包、上传或通知已完成适配或远端验收。

## 复制和修改

1. 将 `check.yml` 和 `reusable-check.yml` 一起复制到承载公开 CI 工具与示例的 GitHub 仓库
   `.github/workflows/`，先在 GitHub-hosted runner 上验收。`check.yml` 的同仓相对 `uses` 由 GitHub
   固定到 caller 同一 commit，因此没有 `@tag`；两个文件的变更应一起评审。
2. 在自己的公开工具仓库完成验收后，其他仓库可把 job 的 `uses` 改为
   `YOUR_ORG/YOUR_CI_REPO/.github/workflows/reusable-check.yml@<已审完整40位commit>`。
   GitHub 不允许这个 `uses` 使用表达式；不要改成分支、可移动 tag 或动态参数。
   当前 reusable checkout 的是 caller 仓库，跨仓调用前应先把 checkout 改为受信的
   `repository: YOUR_ORG/YOUR_CI_REPO` 和同一固定 `ref`，且 caller 不能覆盖这个 revision。
3. `example-mobile` 是固定公开测试项目，不能简单改成自己的业务项目名称。真实接入先按照
   [项目接入文档](../../docs/项目接入.md)准备可信配置；替换示例 organization/repository、品牌、
   Job、账户标识后分别验证。原始第二项目示例仍使用 Gitee URL；GitHub profile 与独立应用入口按下方接入文档配置。

## 固定版本与权限

Action 使用官方仓库完整 commit：checkout v4.2.2、setup-python v5.6.0、setup-java v4.7.1。
Python 固定 3.11.9，Temurin JDK 固定 21.0.11+10；Groovy 2.4.21 从 Maven Central 下载，并核对与现有
Jenkins 测试运行时一致的 SHA-256。更新版本时同步更新 YAML、测试常量和校验值，重新做最小验收。
`ubuntu-24.04` 是托管镜像系列，不是不可变镜像摘要；这些锁不能代替真实远端验收。

PR 与手动 caller 均只授予 `contents: read`，显式零 secret，checkout 不保留 token。
fork PR 运行候选代码只使用 GitHub-hosted runner；禁止 `pull_request_target`、self-hosted runner、
`secrets: inherit`、动态 `uses`、可注入命令/hook、发布/通知开关进入这个检查入口。
后续特权流程须独立设计受信触发入口，按实际用途声明并逐项传递最小 secret，不能给此 PR workflow 加权限。

## 本地验证

先按[公开第二项目说明](../second-project/README.md)准备 JDK/Groovy，然后执行：

```bash
python3 jenkins/run_public_checks.py
```

公共 runner 校验运行时、拒绝测试 skip，在临时树安装 `testOnly=true` 合成 fixture 以运行安全核心；
精确私有兼容用例单独列明，仍在私有 Jenkins 检查，详见 [迁移边界](../../docs/GitHub迁移.md)。
YAML 静态测试使用现有 Ruby/Psych 标准解析器，缺少 Ruby 时明确失败；不安装额外 Python 运行依赖。
全部 `on` key 加引号，避免 PyYAML YAML 1.1 把它当作 boolean。静态测试覆盖权限、runner、触发器、
Action SHA 和固定命令；它不等于 GitHub 官方执行器完整语义校验或真实 Actions 运行通过。

官方依据：[workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)、
[reusing workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)。
Action SHA 来源：[checkout](https://github.com/actions/checkout/releases/tag/v4.2.2)、
[setup-python](https://github.com/actions/setup-python/releases/tag/v5.6.0)、
[setup-java](https://github.com/actions/setup-java/releases/tag/v4.7.1)，采用官方 tag 对应 commit，复制后不依赖 tag 解析。

## 独立应用 PR 入口

上面的 `check.yml/reusable-check.yml` 仍是 CI 工具自测参考。新增
`application-pr.yml/reusable-application-pr.yml` 是需要生成后安装的应用 PR 模板；
`project_config.py` 现在支持明确 `scm.provider: github` 的无发布凭据原生 Android PR profile。
受信工具 pin、workflow pin、fork 信任校验和本地验证见[GitHub 接入](../../docs/GitHub接入.md)。
不能直接复制尚未替换占位符的模板，也不能把本地回归当作远端 Actions 通过。
