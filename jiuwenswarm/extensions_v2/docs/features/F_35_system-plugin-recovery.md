# 系统插件还原

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 / 编号 | feature / F_35 |
| 最近修订 | 2026-11-25 |
| 关联计划 | [EXTV2-35](../designs/extensions-v2-development-plan.md#extv2-35-系统插件还原) |
| 状态 | 已实现 |

## 1. 目标

当系统插件使主服务无法正常响应时，用户仍可把已安装插件还原到最近记录中的某个状态。还原保留安装目录，
写回受管 `manifest.json` 的 `enable` 字段，并同步共享安装账本的 `enabled` / `active` / `status`；独立副本与
组合包成员都在快照和还原范围内。确认还原后，父启动器会自动重启 AgentServer 和 Gateway 加载新状态。重启
AgentServer 前会回收其独立的 Runtime Worker，避免该 Worker 保留还原前已加载的 Handler 或卡死执行状态。

## 2. 记录与还原规则

每次程序启动都会在启动加载前追加一条 `startup` 记录。该记录覆盖当时所有已安装的 AgentServer 和 Gateway
系统插件，并扫描到组合包成员级别：独立副本按 `<package_id>/<version>` 记录，组合包成员按 `plugins/<member>`
记录并附带 `bundle_id` / `bundle_version`，组合包根目录不再作为伪包。启动加载完成后会刷新这条记录；若启动时
目录尚未安装首批插件，后续仅安装操作会继续刷新该启动点，首次启用/禁用/卸载后冻结。成功安装、卸载、启用、
禁用和还原后分别新增一条记录，所有启动之间共最多保存最近 50 条。

还原某条记录时，当前仍在受管目录内的独立副本和组合包成员按记录中的包 ID 和版本写回 `manifest.enable`，
并同步共享安装账本（`system_plugin_install_state.json`）的 `enabled` / `active` / `status`：

- 记录中没有、但当前安装目录仍存在的独立副本和成员会被禁用，账本写为 `disabled`；
- 记录中为启用的组合包成员写为 `enabled=true, active=false, status=pending`，由下一次启动的容器恢复加载，
  避免“账本说 active 但运行时未加载”的假状态；独立副本写为 `active=true, status=active`；
- 组合包容器按成员状态重算：全部禁用为 `disabled`，否则 `active=true`，有待加载成员为 `pending`；
- 同一 `package_id@version` 的独立副本、组合包 A 成员、组合包 B 成员分别按 `(bundle_id, bundle_version, package_id, version)` 匹配；独立副本按 `(package_id, version)` 匹配，互不覆盖；
- 台账中 `status=uninstalling` 的墓碑行不被还原改写，卸载收尾仍由各宿主完成；旧记录若只包含组合包根条目，仅在其为禁用时应用到该组合包的全部成员，根条目的启用值不用于启用成员。

已经卸载且目录不存在的包不会重新下载或恢复。还原成功会产生新的 `restore` 记录，因此该状态也可以作为后续的还原点。

加载侧同样受 `manifest.enable` 门禁：AgentServer 启动加载按该字段决定是否激活；Gateway 的容器同步在
`enable=false` 且账本不是 standby/pending 时跳过并回写 `disabled`，standby/pending 副本仍可在接管时加载。

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
仅重启 AgentServer（及其 Runtime Worker）和 Gateway，不重启桌面窗口或恢复进程。所有启动器都先停止并
等待旧 Gateway 和 AgentServer Front 实际退出，再通过 develop 既有的 Worker 所有权与关停协议确认所属 Worker 退出，
确认失败时不启动新后端。普通重启继续使用 develop 的处理方式，不递归终止不属于此次还原的进程。

本地恢复入口要求唯一的回环 Host 与服务实际端口一致；浏览器携带 Origin 时必须与该入口同源。
独立页的写操作仍校验随机令牌，并要求 `application/json`，不接受跨源或 `null` Origin。
这些检查只用于新增恢复入口，不改变企业 HTTP/SSE 的既有鉴权与租户路由。

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

2026-11-19 develop 迁移保留上述还原能力及原有普通重启流程。迁移依赖的 Gateway e95 版本尚缺部分 V2 生产装配与接口，
按用户决定留待 Gateway 合入后更新依赖并联调；这里不把本地生命周期测试作为完整跨仓验收。
原 beta3 的历史验证记录如下，不能直接视为迁移版本的当前验证结果。

定向测试覆盖：还原预览与实际还原均会禁用还原点之后安装的插件、预览忽略已删除的安装目录、新启动追加记录、记录数量限制为 50 条，以及 AgentServer
和 Gateway 的安装、卸载、启用、禁用回归，以及父启动器只重启后端对。前端执行类型检查与生产构建，并验证中英文资源。未启动业务服务或
占用端口；独立页面与四种启动方式未进行人工运行验收。
