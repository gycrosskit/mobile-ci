# 新 Mac 的 CI 迁移方案

当前提供锁版本检查、实例清单 plan/validate、Compose 与独立 Mac Agent plist 生成。
还没有通过新 Mac 的完整“一键恢复 Jenkins、插件、所有 Agent 并打包”验收；本轮不迁生产、不上传、不通知。

## 目标与已有边界

| 角色 | 新 Mac 上的方案 | 必须验证 |
| --- | --- | --- |
| Controller | 同版本自定义 Linux 镜像运行于 Mac Docker VM，零 Executor | 镜像 ID/OS/arch、插件实际解析、VM卷恢复、ACL/审批/Credentials |
| 原生 iOS/CI 工具节点 | 独立账号、HOME、SDK/Remoting、Secret、工作区/cache与生成 plist；现 app_pr/ci_pr 主任务仍拒绝 native 绑定 | Xcode/JDK/Fastlane/SDK执行、账号权限、节点连接和静默构建 |
| 原生 Android | 可配置 SDK 节点并预检；现 Android 打包 Job 拒绝 native 绑定 | 下一阶段完成 Mac 资源互斥/预算适配、静默打包与签名产物验收 |
| Harmony | 先保留已验收 Linux amd64 Agent | 官方 SDK、`/proc`预算、签名/产物和回执；不直接改为原生 Mac |
| Mac 承担全部角色 | 后续单独验收官方 Mac SDK，或本机 Linux amd64 VM 的实际运行能力 | 平台/架构probe、内存适配或VM预算、CPU/内存峰值、HAP/签名验证 |

Mac host 的 `darwin/arm64` 与 Controller/容器 target 的 `linux/arm64` 是两个事实。
Linux amd64 Android/OHOS 工具不能靠改 `arch` 字段成为 Linux arm64；如使用仿真，需另记运行模式、
性能与实际产物证据。现 Android 打包 Pipeline 仍依赖 Linux `flock`/内存契约，不能因
native Mac SDK 预检通过就声明 Android QA/Release 已可迁移。Xcode 原生运行，Docker VM 与它共享宿主资源，不以 Docker memory limit 代替整机预算。

通用源码唯一维护于 [gycrosskit/mobile-ci](https://github.com/gycrosskit/mobile-ci)，
应用仍消费管理员审核的固定 CI Commit，保留私有 Gitee 工程；无需给应用新增 YAML，
也不要求为了更新通用源码同时同步另一份私有 Gitee 实现。

## 迁移前保留的四类版本证据

1. **源码与版本清单**：固定工具 Commit、`toolchains.lock.json` 及规范化摘要；项目 profile 和生成实例摘要。
2. **Controller/Agent 镜像**：官方基础 manifest digest、实际完整自定义 image ID、OS/arch 与容器实例 ID。
   官方 `jenkins/jenkins` digest 未包含本项目 init/security/plugin 安装结果；它不能替代完整镜像。
3. **插件**：受审 `plugins.txt` 摘要、实际 `plugins-resolved.txt`、Jenkins Home 已装插件版本/二进制摘要。
   `--latest=false` 仍有依赖解析；不能只迁直依赖列表或在新主机自动升级到 latest。
4. **主机工具与运行状态**：每个 Agent 的 JDK、Xcode、Fastlane、Python/Ruby/CocoaPods 精确观察值、
   SDK artifact/运行版本、Remoting SHA256、账号/目录/节点/资源预算与静默构建证据。
   Brew/apt 和 gem 传递依赖未整体冻结，漂移由 `expectedVersions` 和 preflight 拒绝。

Controller HOME 必须保留 jobs/Folders、nodes、全局配置、ACL、`scriptApproval.xml`、Credentials 及配对的
`secrets/master.key`、`secrets/hudson.util.Secret` 等原解密材料。只复制 `credentials.xml` 无法可靠恢复原凭据。
不要把 HOME 当作项目签名备份：它含整个 Controller 的秘密，只交 Controller 管理员。
节点账号、Android SDK、Xcode、钥匙串/Profile、DWS认证、签名材料和交付回执分别登记去向；
不把个人钥匙串整盘复制到 CI 服务账号，不因“换机器”重新上传未知回执对应的产物。

## 单一入口的准备与隔离恢复顺序

1. **Plan**：在独占工具 Worktree 执行下列离线入口。替换示例 profile、假 image ID 和假版本后，
   由管理员核对真实镜像、节点、路径和资源。plan 不连接线上 Jenkins，也不启动 Docker。

   ```bash
   python3 toolchains.py check
   python3 deployment_config.py plan examples/deployment/linux-with-remote-mac.json --example
   python3 deployment_config.py validate examples/deployment/linux.json
   ```

2. **Validate/render**：审核真实私有 profile 与实例，目录0700、文件0600；输出路径必须与清单 `stateRoot` 一致。
   按[通用部署](部署.md)生成 Compose/nodes/plist，检查 native Agent 的独立账号、HOME、Secret 和 SDK。
   生成仅写部署产物；安装账号、工具、插件或 Credentials 仍是后续部署动作。

3. **Export**：在获授权且队列/构建空闲的维护窗口，复用旧实例的
   [backup.sh](../jenkins/remote/backup.sh) 和现有资源清单，保留覆盖台账、manifest 摘要、HOME、配对 secrets、
   签名/认证及原交付回执。该脚本会停止/恢复相应服务，输出敏感 `tar.gz`；
   整个 HOME 迁移包不自动外层加密。传输前由管理员按既有受审方式加密完整迁移包并核验接收人/摘要，
   解密后只在0700隔离目录供恢复入口读取。此处不新增加密引擎，也不在 plan 阶段执行备份。

4. **Restore**：在新 Mac 隔离 Docker VM 中准备原完整自定义镜像与全新具名卷，复用
   [restore.sh](../jenkins/remote/restore.sh) 的摘要/coverage/拒绝覆盖验证与 `docker run --volume` 恢复机制。
   恢复原 UID/GID 后，先使用隔离端口和 Agent，不接生产 Webhook，不运行上传/通知。
   旧恢复工具目前依赖旧实例资源布局；通用多 Agent 清单模式主动拒绝默认 backup/restore，
   必须先评审资源映射与所有材料/回执覆盖。不能把 render 的新卷名直接套进旧备份脚本。

5. **Verify**：按[运行验收](运行验收.md)逐角色执行 preflight、恢复回读和实际静默构建。
   核对 jobs/Folder ACL、精确脚本审批、插件版本及配对 secrets 的解密可用性；在获授权隔离环境重启，
   再回读相同审批/Job/node。核对原始回执与防重复交付，设置 `UPLOAD_AND_NOTIFY=false`。
   通过后才形成生产切换计划；迁移、正式切换、真实上传与通知分别授权。

原生节点使用生成的 `com.mobile-ci.agent.<id>` plist 和标准 `launchctl`，
而非历史固定 `mac-build` 的 `bootstrap-mac-agent.sh --connect-agent`。
先按管理员清单安装服务账号及工具、只读预检，再在获授权的部署步骤将对应 plist 交由 launchd 管理；
本轮只离线验证其参数/环境，不创建账号、不加载 launchd、不连接 Agent。

## 尚未通过的迁移验收

新 Mac Docker VM 的卷恢复、插件/凭据/审批持久化、真实 Agent 连接和静默应用产物仍需实际演练。
旧 `backup.sh` 直接访问 Docker volume 的宿主 `.Mountpoint`，这在 Mac Docker VM 上不是可直接访问的 Mac 路径；
Linux 源导出和基于容器的目标恢复可作为迁移方案，但迁移后 Mac 主机上的后续备份仍需适配/验证。
这也是当前不能宣称完整“一键迁移”已可用的具体边界。

115 条第三方 UNKNOWN 仍只阻断镜像再分发，未阻断通用源码维护；它不授权公开镜像/SDK，
也不证明 Mac 工具能力。没有可合法迁移的已审镜像、官方 SDK 或完整秘密/回执覆盖时保留旧实例，
停止切换方案，不用新 Home、重建凭据或重新上传来掩盖缺失。
