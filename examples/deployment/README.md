# 独立实例与逐 Agent 配置

```bash
python3 deployment_config.py validate examples/deployment/linux.json
python3 deployment_config.py plan examples/deployment/linux-with-remote-mac.json --example
python3 deployment_config.py render examples/deployment/linux-with-remote-mac.json
```

以上只读离线演示。`--example` 仅允许两个固定清单和固定第二项目 `testOnly` profile；计划明确
`testOnly=true`、`trustedProfilesVerified=false`、`applySupported=false`。镜像 ID、Remoting 摘要和版本均为
**占位示例**，不能用于真实部署。第二份示例新增远程原生 Mac iOS 节点准备配置，该 Android 示例项目仍只绑定
Android/PR Job；实际 iOS profile、工具安装和运行验收须另行完成。

## 清单边界

- `controller`：宿主 `host` 与容器/VM `target` 分开；Mac Docker VM 仍为 Linux Controller。`imageId`
  是管理员验收的完整已建镜像 ID，不能用工具链 base image 或浮动 tag 代替。Controller 为零 Executor。
- `agents[]`：每个 ID、name、labels、capabilities、服务账号/HOME（native用户名和UID逐节点独占）、工作/缓存/运行/连接材料目录、SDK 位置、
  CPU/内存预算、单 Executor、连接 `secretRef` 独立声明。原生 Mac 另绑定本地 Java/Remoting 路径与 Remoting 摘要。
  `expectedVersions` 独立锁定 Python，iOS 还必须声明 Ruby/CocoaPods；工具链版本引用唯一 `toolchains.lock.json`。
- `profiles`：固定项目 ID → 实际 Job 角色 → 管理归属 Agent ID。真实计划和渲染核对管理员受信 profile 中的
  Job 与凭据引用；不携带 secret 内容，不接受应用/MR 配置路径。`primaryProjectId` 必须显式指向已绑定项目，Controller portal/promotion只服务该主profile；
  其余受信profile进入每Agent独立root-owned只读 `/etc/mobile-ci/projects` 卷。实际 Pipeline 沿用现有标签契约；同标签的
  等价 Agent 是调度 pool，归属声明不保证单独调度某个节点。计划列出 `eligibleAgentIds`。
- PR 主节点禁止生产构建、iOS 构建和签名维护标签及共用任务。现有 iOS PR 子阶段的
  `mac-build && ios-pr-builder` 属于受信单 Executor iOS 节点共享边界；另建物理隔离 iOS PR pool 需要同时迁移
  Pipeline 标签策略。不能仅新增一个标签就宣称隔离完成。
- Linux 容器 CPU/内存限制进入 Compose；native Mac 资源为管理员容量预算，launchd 不提供等价容器限额。
  目前仅同一宿主的 Linux Compose 与远程原生 Mac；远程多 Compose 宿主分发尚未实现。
  Mac Android 现有发布 Pipeline 的 Linux 内存检查尚未迁移，因此清单允许准备节点但拒绝绑定 Android
  打包任务。Mac/arm64 Harmony 能力当前直接拒绝，官方 Mac 工具实测与 Pipeline 迁移通过前不宣称支持。
  应用/CI PR 主任务同样保留 Linux 入口合同，拒绝绑定原生 Mac；iOS PR 子阶段保持现有 Mac 合同。
  维护节点必须配置既有 Linux Harmony SDK 与独立签名目录，不接受缺少入口依赖的 maintenance-only 节点。

## 新实例渲染与消费

1. 管理员独立审核真实 profile、完整镜像 ID、工具链、服务账号、目录、SDK 和节点预算。真实 profile 安装到
   固定管理员目录；示例 `testOnly` profile 不能进入运行部署。不要以源码示例补齐生产材料。
2. 准备当前管理员所有的 `0700` 私有目录和 `0600`、单链接、无符号链接实例文件，目录链不能由其他用户写入。
   清单 `controller.stateRoot` 指向准备好的**新输出目录**。执行
   `python3 deployment_config.py plan /ABS/instance.json --private`，再显式
   `python3 deployment_config.py render /ABS/instance.json --private --output-dir /ABS/state-root`。
   渲染拒绝覆盖已有输出；没有 `apply` 命令。
3. 产物包含 `deployment.json`、摘要绑定的 `nodes.json`、`compose.json`，以及每个 native Agent 独立的
   `native.<ID>.plist` / `native.<ID>.env.json`。Linux 入口设置
   `JENKINS_DEPLOYMENT_FILE=/ABS/state-root/deployment.json` 和
   `JENKINS_COMPOSE_FILE=/ABS/state-root/compose.json` 后使用既有 `stack.sh check|status|verify`。
   `start` 是管理员另行授权的真实启动；只消费同一受信固定渲染结果并且 `--no-build`。禁止 overlays，镜像必须
   已安装，SDK 只读挂载。initializer 只认领空新卷；已有未知权限、不同摘要或内容须显式迁移，不自动覆盖。
4. 原生 Mac 在每台机器单独创建清单中的非 root 账号与私有 HOME/工作/缓存/运行/secret 目录，安装管理员
   审核的 SDK，并执行 `toolchains.py preflight` 核对实际版本、Remoting 摘要、Xcode/SDK 与服务身份。
   `deployment_config.py preflight /ABS/instance.json --private --agent <ID>` 必须在该Mac以实际服务账号执行；
   先由管理员将审核过的无 secret 清单以该服务账号所有、目录0700/文件0600安装到受信父链的私有目录，
   保存与管理员原始清单相同的摘要；不能直接让服务账号读取管理员0600文件或放宽原文件权限。
   它严格消费此节点SDK路径、Remoting SHA256和expectedVersions，不用宿主probe替代容器SDK。
   新native HOME采用root受信父链 `/private/var/lib/mobile-ci/<ID>`；现有 `/Users/Shared` 若父链可被他人写入则拒绝preflight。
   连接 secret 由 Controller 为该 name 生成，再经管理员批准的私有传输放到该节点 `paths.secret/agent.secret`。
   生成 plist 的 `ProgramArguments` 直接调用该节点 Java/Remoting，`-secret @file` 不把 secret 内容放进参数。
   精确 ScriptApproval 和 Job 门禁须由管理员/lifecycle 单独审核后才启用；部署不自动导入旧实例审批。
   管理员审核后将对应 plist 以 root:wheel、0644 安装到唯一
   `/Library/LaunchDaemons/com.mobile-ci.agent.<ID>.plist`，再显式 `launchctl bootstrap system` 消费。
   旧 `install-pr-agent-launchdaemon.sh` 是已有固定实例兼容入口，不会自动安装通用多 Agent 清单。
5. `verify` 检查实际容器镜像、账号、CPU/内存、环境、完整挂载集合、Controller 健康和 Jenkins node 配置。
   native launchd、在线连接、SDK 真实 probes、实际构建、设备和交付验收分别完成；该检查不代替这些证据。
   备份、迁移、DWS 与外网入口继续单独审核，不从新清单隐式绑定旧实例材料。
