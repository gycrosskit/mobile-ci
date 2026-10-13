# 通用工具迁入 GitHub

目标公共工具仓库为 `gycrosskit/mobile-ci`。应用继续由私有 Gitee 保存，沿用原工程与契约，不新增 YAML。
工具 GitHub Actions 只做本仓库源码与安全回归，生产 Jenkins 显式消费管理员审核的固定完整 CI SHA。
公开 PR 不会部署 Controller、读取应用或供应商凭据、签名、上传安装包或发通知。

## 公共检查

`.github/workflows/check.yml` 调用同一提交的 `reusable-check.yml`；
[参考例子](../examples/github-actions/README.md) 与实际 workflow 同步维护。
使用 GitHub-hosted `ubuntu-24.04`、`contents: read`、完整 Action SHA、JDK 21 和锁定摘要的 Groovy 2.4.21。
不接受 secrets、不使用 `pull_request_target` 或 self-hosted runner，不归档安装包。

`python3 jenkins/run_public_checks.py` 先做运行时预检，再复制 UTF-8 公开源码到临时树。
测试用 `tests/fixtures/legacy-project.json` 只含假仓库、假账号和零摘要，标记 `testOnly=true`。
仅在临时树模拟已审管理员配置时移除标记；公开包没有 `projects/`，生产 loader 不会回退读取 fixture。
第二项目在另一个独立临时双仓库验证，无 SDK、签名、供应商请求或真实凭据读取。
任何公共核心测试失败或工具缺失都会失败，不能用跳过安全测试得到成功。

公共套件运行通用行为、并发恢复、权限拒绝、备份/JCA、模板及示例；
[7 个私有兼容用例](../jenkins/run_public_checks.py)只保留真实品牌、已审应用公钥、实际 CA和旧仓库快照。
逐字生产快照、已审应用接收人公钥及真实 Controller/CPS 属于私有兼容与部署验收，
需在私有环境验证，不与公共 Actions 结果混用。公共 runner 的私有兼容清单按精确用例列出，
变更该清单必须解释其私有材料依赖；不能整模块排除安全行为。
私有 Jenkins 门禁继续执行完整 `python3 -m unittest discover -b -s tests -t .`，
并按 [运行验收](运行验收.md) 单独核对 CPS、审批与设备/交付。

## 源码发行与追溯

`python3 jenkins/export_public_source.py --output build/public-source.tar.gz` 生成无 Git 历史源码包。
`SOURCE-MANIFEST.json` 绑定每个发行文件的 SHA-256，`SOURCE-ORIGIN.json` 标明源 Commit 和工作树快照。
未提交内容是否属于该 Commit 必须据快照说明区分。LICENSE、NOTICE、上游声明与安全报告入口保留。
公司 profile、运维 runbook、生产操作文档和私有验收链接不会随包公开；入口改为通用文档。
镜像核查的原始实例记录不进入包，115 UNKNOWN 仍阻断镜像发行；官方 Linux CLT 26.0.0.851 原 ZIP已完整实装，冻结的 ZIP/许可摘要未变，
UID1000且network-none runtime通过；冷缓存 HAP待正式工具提交后独立验收。

仓库公开、初次上传与生产工具源切换须分别核对实际结果。本地测试与静态 YAML 检查不等于远端 Actions 通过。
安全邮箱已于 2026-10-13 17:53:46+08:00 完成一次自发自收，
只能证明该账号此次收信；一封外部邮件已于 2026-10-13 18:10:22+08:00 送达，经人工纠正垃圾分类后在收件箱；不能保证所有发件方自动归入收件箱，维护仍需检查垃圾箱。GitHub 私有漏洞报告入口尚未验收。
