# GitHub 应用 PR 接入

GitHub Actions 现在有独立的应用 PR 检查入口：固定版本的 CI 工具验证事件与双仓接口，再运行应用 manifest 测试和已有原生 Android Gradle `lintDebug/assembleDebug` 适配。GitHub profile 不经过 Jenkins/Gitee webhook，不授予签名、上传、通知或 Controller 自动晋级能力。

## 受信配置与范围

在 **受信 CI 仓库** `projects/<id>.json` 添加 profile，沿用[项目接入](项目接入.md)的 `platforms`、`brands`、`contract` 数据，修改以下字段：

```json
{
  "scm": {"provider": "github", "allowForks": false},
  "credentials": {},
  "providers": {},
  "jobs": {},
  "repositories": {
    "application": {
      "identity": "YOUR_ORG/YOUR_APP",
      "ssh": "git@github.com:YOUR_ORG/YOUR_APP.git",
      "https": "https://github.com/YOUR_ORG/YOUR_APP.git",
      "defaultBranch": "main",
      "trustRef": "refs/heads/main"
    },
    "automation": {
      "identity": "YOUR_ORG/YOUR_CI",
      "ssh": "git@github.com:YOUR_ORG/YOUR_CI.git",
      "https": "https://github.com/YOUR_ORG/YOUR_CI.git",
      "defaultBranch": "main",
      "trustRef": "refs/heads/main"
    }
  }
}
```

这只是字段片段，完整 profile 仍需要 `version: 1`、固定 `id`、原生 Android `adapter: gradle`/`engines: [native]`、模块与构建类型、品牌列表和应用检查契约。当前 GitHub 入口仅支持原生 Android；iOS、Harmony、CMP/Kuikly 必须另行实现与验收，不能更换域名后宣称支持。`runtime/backup/signingBackup/portal` 和非空 Jenkins/供应商字段一律拒绝。Gitee 的现有 profile 不需要修改。

应用必须提供项目契约要求的文件、manifest unittest 和可实际执行的 `gradlew`，使用自己的 Gradle wrapper 固定版本；runner 必须有项目所需的 SDK。此入口不创建签名凭据，不读取应用 `CI_PROJECT_CONFIG`，也不允许候选应用选择工具根、受信配置路径或动态构建命令。CLI 运行候选代码时只传 SDK/JDK 等运行路径及固定项目数据，过滤 GitHub、Jenkins、签名和供应商凭据环境变量。

本入口使用无额外 secret 的 `contents: read` token，工具 checkout 需要 GitHub 可访问的 CI 仓库。跨仓私有工具的读取 token 未实现，不能向此 PR 入口加生产 token 解决；是否公开 CI 仓库需另行授权。

私有项目也可以把已审工具和应用放在同一个 GitHub 仓库：两个 repository binding 使用同一身份，工具仍以 literal `TOOL_SHA` checkout 到 `trusted-ci`，应用 head/base 分别 checkout 到另两个目录。受信工具必须位于该 Git checkout 根目录，不能用应用内的任意子目录代替独立工具根；候选改动不影响固定工具提交。此布局沿用三个实际 HEAD、origin、洁净状态与目录隔离检查，无需扩大 token 权限或传递 secret。它不证明跨私有仓接入可用。

## 固定工具 SHA 与 workflow SHA

两个 SHA 的治理由维护者人工审查和固定，**没有实现自动晋级**。不可把 branch、tag、候选应用输入或 repository variable 当作可信版本：

1. 审查工具代码与完整 profile，保存为受信 CI 的提交，获得完整 `TOOL_SHA`；该提交包含 `jenkins/github_pr_check.py`。`trustRef` 只描述项目默认分支，不能替代已审 commit。
2. 使用该提交的工具生成 reusable 文件；其 checkout 的仓库和 `TOOL_SHA` 是文件内 literal，不是 caller input。把生成的 `reusable-application-pr.yml` 安装到 CI 仓库 `.github/workflows/`，评审提交后得到另一个完整 `WORKFLOW_SHA`。文件内容不能包含自己的 Git SHA，因此这两个提交不能要求自引用相等。
3. 重新生成 caller，以实际 `WORKFLOW_SHA` 固定 `uses`，把 `application-pr.yml` 放到应用 `.github/workflows/`。维护者审查该 caller，并按组织治理保护 workflow 文件与必需检查；更新两种 pin 同样经过审查。

生成命令（路径与 SHA 必须先替换为已审事实）：

```bash
python3 jenkins/github_pr_check.py render \
  --project YOUR_PROJECT_ID \
  --ci-sha FULL_40_CHARACTER_TOOL_SHA \
  --workflow-sha FULL_40_CHARACTER_WORKFLOW_SHA \
  --output /tmp/github-pr-workflows
```

步骤 2 尚未取得 `WORKFLOW_SHA` 时，可先用一个格式合法的临时完整 SHA 生成 reusable；caller 文件不能安装，必须在步骤 3 换成实际已审提交重新生成。生成器只验证格式，不证明 commit 已存在、已审批或已发布；这些事实必须由维护者独立核验。模板中的 `__...__` 占位符不可直接安装。

## PR 事件、fork 与执行隔离

Workflow 仅允许 `pull_request`，`contents: read`、显式零 secret，使用 `ubuntu-24.04` GitHub-hosted runner。Action 完整 SHA 与已有参考入口相同；checkout `persist-credentials: false`。禁止 `pull_request_target`、self-hosted、`secrets: inherit`、environment、签名/发布步骤和动态 `uses`。

受信脚本先验证平台事件名、`github.com` server、caller repo name/ID、PR number/URL、base 仓库身份/URL/ID、默认目标分支、完整 head/base SHA、fork repo name/ID/flag。只有 profile 明确 `allowForks: true` 才接收 fork；同仓和 fork 均在无 secret 托管 runner 运行候选代码。事件文件必须在 `RUNNER_TEMP` 内且是有界单链接普通文件，拒绝符号链接和路径越界。

校验后分别 checkout `trusted-ci`、`candidate-app`、`base-app`。应用 head 从 **base 仓库** 的 `refs/pull/<number>/head` 获取，不执行事件中的 URL、head branch 或任意 ref；base 固定事件 SHA。CLI 也拒绝 self-hosted 平台身份，再比较实际三仓 HEAD、origin、洁净状态和目录隔离，拒绝 SHA 伪造、混用 Gitee、应用根覆盖工具根和 PR 更新竞态。目标 base 对象通过本地已验证 checkout 提供应用契约基线，防止候选删掉接口后退回兼容模式。

目录隔离用于防止 checkout、配置和导入路径混淆；它不是 OS 沙箱，同一 job 的候选进程仍可能写入同一用户的文件。执行候选代码的 job 必须保持无生产 secret、无持久化 runner，并且不得把其工作目录或产物晋级为受信 CI。

这里测试 PR head，**不测试 GitHub 的合并模拟 commit**；`GITHUB_SHA` 默认是 merge SHA，不能误当成 head。任务成功/失败由 GitHub Actions 自身的 Checks 显示，不请求额外 `checks: write` 或第三方 Gitee 状态 API。CLI 只在检查成功时返回 0，失败返回非零。必需检查配置及真实 UI 回写仍须在授权的 GitHub 仓库验证。

## 本地验收与未执行边界

```bash
python3 -m unittest -b tests.jenkins.test_github_pr_check tests.jenkins.test_github_workflow_examples
python3 examples/github-actions/validate.py
# 已有 Android SDK/Gradle 的机器另执行真实 Debug 构建：
python3 examples/github-actions/validate.py --build
```

本地 fixture 生成独立 tools/head/base Git 仓库、GitHub profile 与事件，实际调用新增 CLI。普通回归用应用 Gradle 边界替身验证固定 lint/assemble 参数，不把它当真实 APK；`--build` 使用现有 SDK/Gradle 并运行真实 manifest/lint/assemble。全部 fixture 在临时目录，测试结束即清除，不改真实应用、不部署 Jenkins、不上传、不通知。YAML 用 Ruby/Psych 解析，校验权限、runner、Action 和双 SHA literal，不能代替官方远端执行器校验。

官方依据已核对：

- [PR 事件与 fork 权限](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request)：head SHA 和默认 merge SHA 的区别，以及 fork secrets/token 行为。
- [复用 workflow](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)：跨仓固定 commit 和 `uses` 不支持表达式。
- [workflow 语法与权限](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)：最小 `contents: read` 权限与未声明权限归零。
- [Checks](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/about-status-checks)：Actions job 结果对应 Checks。
- [官方 runner 事件文件写入](https://github.com/actions/runner/blob/main/src/Runner.Worker/ExecutionContext.cs)：非容器 runner 将事件写入临时目录的 `_github_workflow/event.json`；当前入口不使用 job container。
- [平台变量](https://docs.github.com/en/actions/reference/variables-reference)：事件文件、repo ID、ref 与 runner 临时目录。

远端未授权公开/推送/运行时，只能报告本地 fixture、静态 YAML 和真实本机构建证据；不能声称 GitHub PR Checks 回写、branch protection 或真实远端应用接入已通过。

