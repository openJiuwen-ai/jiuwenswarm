# 系统插件还原

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 / 编号 | feature / F_35 |
| 最近修订 | 2026-11-16 |
| 关联计划 | [EXTV2-35](../designs/extensions-v2-development-plan.md#extv2-35-系统插件还原) |
| 状态 | 已实现 |

## 1. 目标

当系统插件使主服务无法正常响应时，用户仍可把已安装插件还原到最近记录中的某个状态。还原保留安装目录，
只改变受管 `manifest.json` 的 `enable` 字段；确认还原后，父启动器会自动重启 AgentServer 和 Gateway 加载新状态。重启
AgentServer 前会回收其独立的 Runtime Worker，避免该 Worker 保留还原前已加载的 Handler 或卡死执行状态。

## 2. 记录与还原规则

每次程序启动都会在启动加载前追加一条 `startup` 记录。该记录覆盖当时所有已安装的 AgentServer 和 Gateway
系统插件，不为启动加载的每个包分别新增记录。成功安装、卸载、启用、禁用和还原后分别新增一条记录，所有启动
之间共最多保存最近 50 条。

还原某条记录时，当前仍在受管目录内的包按记录中的包 ID 和版本写回 `enable`。记录中没有、但当前安装目录
仍存在的系统插件会被禁用。已经卸载且目录不存在的包不会重新下载或恢复。还原成功会产生新的 `restore` 记录，
因此该状态也可以作为后续的还原点。

还原不撤销插件已经产生的网络、文件或第三方服务副作用。确认后正在处理的任务会因后端重启而中断；它不是恶意代码隔离。

## 3. 进程与页面

恢复进程是启动器管理的独立子进程，启动顺序早于 AgentServer 和 Gateway。它只监听回环地址，地址含随机
令牌，且不导入任何插件模块。这样即使主服务的事件循环被插件阻塞，独立页面仍能读取记录和写回 manifest。

常规设置页位于“实验功能”之后。点击后，主应用在当前窗口进入 `/system-plugin-recovery`，页面内容嵌入独立
恢复页；主地址不显示随机令牌。恢复页显示标题、说明和最近记录。记录主标题为“存档点 / Restore point”和时间，
副标题显示本次操作。初始时所有记录均收起；点击一条记录后，仅展开该条，再次点击已展开的记录会将其收起，打开另一条
记录时自动收起上一条。展开时恢复进程按当前受管安装目录计算实际还原结果：已删除的安装目录不会显示，记录之后安装且
目录仍存在的插件显示为禁用。每条记录左侧提供单选点以选择还原目标；未选择时页面右上角“还原 / Restore”按钮禁用，
选择后启用。还原按钮不在详情内，展示的状态与执行还原后的状态一致。写入前提示“还原将恢复此刻对应的系统插件启用状态，并重启后台，正在处理的任务将中断。”
独立页面使用系统的明暗主题偏好，不依赖 AgentServer 和 Gateway。源码与 Wheel 启动使用 Python 模块入口；冻结 EXE、DMG 使用已有子进程分发；Electron
和 Python Desktop 启动器均纳入该子进程及其生命周期管理。恢复进程只写入一次性重启请求，当前启动器消费该请求后
仅重启 AgentServer（及其 Runtime Worker）和 Gateway，不重启桌面窗口或恢复进程。普通服务退出和崩溃重启沿用
develop 的 Front 与常驻 Worker 生命周期；只有明确的一键还原走冷重启路径：先确认 Gateway 与 Front 已退出，再通过
`common/system_plugin_recovery_restart.py` 校验当前 deployment 和 data scope 下的 Worker 身份并请求退出，最后启动新后端。
该路径复用受控 Worker 退出接口，并在启动锁内确认旧 Worker、已记录后代进程、IPC 端点和 lifetime lease 均已释放。
不会枚举并终止其他部署的 Worker，也不会把端点尚未建立的启动中 Worker 当作已退出。

一键还原本来就会中断在途任务；卡死 Worker 被强制回收且已确认退出时，允许继续冷启动，但不声称其持久化已完整完成。
身份无法确认、旧进程仍存活或端点无法释放时不启动替代后端。普通应用关闭仍保留 develop 对持久化完整性的严格失败语义。
Python 和 Electron 启动器调用同一个恢复专用入口；Electron 使用旧 Agent 进程的环境定位 Worker，冻结程序显式分发该入口。

从设置页执行还原时，收到重启请求成功的确认且连接实际断开并重连后，当前窗口清理还原前的插件界面。
仍在挂载的应用级和会话级槽位按各自规则重新上报 `ui.ready`。普通断网重连沿用现有行为。
插件广场、我的扩展和详情页中，已安装 Gateway 插件的开关读取还原后受管版本的 `manifest.json`。

When a restore from Settings confirms a restart request and the connection reconnects, the current window
clears pre-restore plugin views. Mounted application and session slots report `ui.ready` under their existing
rules. Ordinary reconnects retain their existing behavior.
The marketplace, installed list and detail page read an installed Gateway plugin’s toggle from its restored managed manifest.

## 4. 职责边界

恢复账本属于 JiuwenSwarm，不写入 agent-core。agent-core 只提供通用的包安装、卸载和 `enable` 管理接口；
JiuwenSwarm 在成功结果后记录本产品的状态。Gateway 与 AgentServer 使用同一个受管安装目录和账本位置，
恢复进程根据 manifest 的 `package_type` 识别系统插件，不依赖任一扩展运行时。

## 5. 验证

定向测试覆盖：还原预览与实际还原均会禁用还原点之后安装的插件、预览忽略已删除的安装目录、新启动追加记录、记录数量限制为 50 条，以及 AgentServer
和 Gateway 的安装、卸载、启用、禁用回归，以及父启动器只重启后端对。前端执行类型检查与生产构建，并验证中英文资源。未启动业务服务或
占用端口；独立页面与四种启动方式未进行人工运行验收。

2026-11-16 合流调整：以最终合入 develop 后的代码为检查对象，区分普通生命周期与恢复冷重启，并保留 JiuwenMate 和新版视频适配器。
本轮按用户要求未运行测试、构建或静态检查工具；仅进行调用链、差异空白及三方合并文本检查。其他责任人的未决冲突与 Gateway V2 迁移不在本轮处理范围内，完整业务联调仍待集成后开展。
