---
id: F_20261123_1526_bundled-agent-memory
title: Work 与 Code 主 Agent 迁移到内置 Agent Memory 插件
type: F
author: y00841611
author_email: yangweimin5@huawei.com
modules: [jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness/common/memory, jiuwenswarm/runtime]
files: [jiuwenswarm/agents/harness/common/memory/plugin.py, jiuwenswarm/runtime/session_provisioner.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, tests/unit_tests/agentserver/memory/test_bundled_memory_plugin.py]
designs: []
design_note: 本文记录插件迁移的实现与决策；运行契约见仓内 S_04。
---

# 内置 Agent Memory 插件

## 实现与决策

Work/Code 主 Agent 将自动长期记忆从宿主直接装配改为会话可选的内置 `agent-memory-plugin`，避免新旧入口重复写入。插件随 Python 包发行、启动时首次安装；会话保存默认选用和显式关闭。插件负责配置预检、复用场景工厂、提供宿主身份和关闭时等待写入；召回与写入仍由原 Rail 和 Provider 负责。初始迁移只覆盖单人 Work/Code；Team 接入见本文后续记录，独立 MCP 连接器不受影响。当前装配与运行契约见 [S_04：Work 与 Code 文档记忆接入](../specs/jiuwenswarm/server/runtime/agent_adapter/S_04_coding-document-memory.md)。

PyPI 原锁定的 JiuwenMemory 0.2.1 没有 Code 场景参数和工具，改为固定 openJiuwen 官方源码 `6e99750`（该提交的包元数据版本为 0.2.0）。该选择与上游是否另发 PyPI 包无关。启动安装沿用现有同名包保护，已安装插件不在启动时被覆盖。

新建 Work/Code 会话默认选中记忆插件；创建响应返回已保存的装备，Web 界面同步显示开关。“新建任务”在首条消息前是草稿，插件列表会返回已安装记忆插件的默认启用状态；草稿中的手动关闭在创建实际会话时带过去。插件清单声明 SVG 图标；已安装的内置包若没有图标，目录列表和详情从发行资源读取图标，不覆盖旧包。

记忆插件的测试连接和保存配置作用于插件本身。WebSocket 跳过会话存活校验，Runtime 直接分发给插件配置处理，不走对话初始化，也不落盘会话元数据。HTTP 保存仍须通过连接测试。

## 验证证据

- Windows，Python 3.12.13，Core `0b1aa21`、Agent Memory `6e99750`；使用隔离虚拟环境与数据目录。`test_bundled_memory_plugin.py` 覆盖 Work/Code 各自的 HTTP 和 SDK。真实插件运行时、Rail 与 Provider，确定性对话模型替身；HTTP 使用回环测试服务，SDK 使用实际内核、SQLite 影子索引和临时 Markdown 存储。
- HTTP 验证实际模型输入有召回、每个普通回合恰好一次 `/v1/add`。SDK 验证落盘、新实例从既有存储召回。两种方式均验证对应工具卸载、显式关闭后新回合不召回或写入、旧开关允许时仍不走旧入口、配置无效时报告错误且不回退。
- 会话创建、TTSE、流式接口和发行检查均纳入回归：221 通过、6 失败。4 个本地媒体权限和 2 个 PowerShell 执行策略失败在同依赖的未修改 `1110e14fe` 上复现。`uv lock --offline --check` 通过；wheel 已构建，确认包含清单、配置、Rail 和固定依赖。

- 默认选用和图标增量：Python 会话和记忆插件测试 41 项通过，Web 会话创建测试 6 项通过；隔离目录验证新安装和旧安装的列表及详情均返回 SVG。

- 草稿开关增量：重建并重启本机调试服务，用 Edge 验证 Work 和 Code 单 Agent 在发消息前显示开启，手动关闭后重开扩展面板仍关闭，另开任务恢复默认开启。后端回归 172 通过；6 个 AgentGroup 路径断言在 HEAD 版包管理器上同样失败。

- 配置页空会话修复：15 项定向测试通过。回归通过 `AgentRuntime.invoke` 模拟 Web 请求，验证测试和保存均返回成功，临时数据目录里未创建对应的会话目录。HTTP 测试失败后仍保留原配置。

- SDK 抽取故障修复：历史用户配置把抽取模型指向 `echo`，回写触发后因 JSON 或候选校验失败而未落盘。已将用户插件的 SDK 抽取改为复用 Swarm 默认真实模型；插件装配时拒绝将模型抽取器绑定到 `echo`。13 项定向回归通过；另用本机真实模型对进程内 SDK 和真实回环 HTTP 服务分别验证，共 2 项通过。两条合成对话分别写入 `USER.md`、日期 Daily Markdown，并在 SQLite `memory_unit` 留下对应记录。联调使用独立临时库和确定性对话回复。

- SDK 跨会话召回修复：现网日志中的 `Registry writer already exists` 来自旧 `continuity` 配置启用的 SQLite 独占写锁。一个 Work 会话写入后保持启用，后续会话会初始化失败并返回 `provider not initialized`。Work/Code 内置插件仅从 SDK 配置副本中去掉该段。回归使用两个独立会话写入北京、豆汁儿和 SEG 评审，在第三个会话核对实际记忆工具输出，并在召回前检查 Markdown 和 SQLite。定向回归 101 通过，4 项本地视频权限用例仍失败；用本机真实抽取模型在隔离库验证 SDK 和回环 HTTP，2 项通过。对话工具选路由测试替身驱动，未重启当前 Web 服务。

## Team 接入增量

- `team.work`、`team.code` 在 Jiuwen 记忆路线下使用同一内置插件的开关和 SDK/HTTP 配置。Team Harness 继续传入可信团队坐标；Leader 与成员各持独立 Rail，同一成员的 `code_agent` 复用该成员的 Rail。关闭后不回退到原生 Team 记忆。
- 运行时和详情读取当前发行资源中的记忆插件，保留已安装副本与用户配置。其他插件仍按现有规则装配。
- 锁定的 Core `02b2ce453` 和 Agent Memory `ab2e0df`：Team Work/SDK 与 Team Code/HTTP 的真实 Rail 装配测试通过，使用确定性模型与 Provider 替身验证召回注入、写入调用、结论收口和启停。上游 Team Provider 的 29 项测试通过，含真实 SDK 临时库的团队共享与隔离。Work/Code 的 4 项旧用例失败在相同依赖的改动前暂存版本中复现。未在用户数据目录或真实 Team Web 会话中联调。

## 2026-11-26：仅保留内置 HTTP

- 本次替代上面的 SDK／HTTP 切换、外部 HTTP 地址和手动启动配置；旧段落的测试结果只代表历史版本。个人版 AgentServer 启动时拉起已存在的回环 HTTP 监听器，关闭时在会话清理完成后停止。客户端自动取用同进程的地址和认证信息；详情不暴露密钥。企业版不启动单一身份的监听器，以免共享租户数据。
- 保留服务端模型配置、会话记忆开关和已有内置 HTTP 存储目录；不迁移 SDK 或外部服务数据。通用记忆 builder 的非插件调用不在本次变更范围。
- Windows、Python 3.13.9：66 项定向测试通过，含真实 HTTP 自动启动、认证、关闭，以及不同会话写入和召回，核对 Markdown 与 SQLite。前端插件详情套件 16 项通过；市场搜索套件的打包因未配置 SVG loader 未执行。未在用户数据目录重启 WorkSwarm，也未用真实模型验收。

## Team 用户画像回写修复

- 内置 HTTP 的画像位于 `agent/memory/managed-http/USER.md`，不写入旧 SDK 的 `agent/workspace/USER.md`。新版 Agent Memory 需要宿主绑定团队坐标、可信 HTTP 身份和带角色的对话来源；旧版保留原有调用方式。新版提示模型由回合结束后的回写处理用户画像，不调用用于工作经验的工具来记录个人偏好。
- 在隔离数据目录用实际模型运行 `team.code.normal`：服务随 AgentServer 启动，无密钥请求返回 401、携带密钥返回 200。给定对话的工作地点和饮食偏好进入 Markdown 与 SQLite；新会话未复述任何事实，模型回答「你在杭州工作，喜欢吃西湖醋鱼。」未重启用户正在使用的实例，也未向它的存储补写历史回合。

## 2026-11-26：检视修复

- 记忆监听器离线时，Work/Code 继续本轮对话而不装配记忆，保留会话选择供服务可用后重新装配。新会话保持默认开启，不继承旧记忆开关。不迁移记忆数据，不恢复 SDK 模式。
- 限制监听器并发和 I/O 等待；停机时关闭端口和连接，给宿主等待设上限，仍在运行的处理线程退出后再释放记忆库。
- 已在 Windows 定向验证真实回环 HTTP 的自动启动与认证、启动失败、停机收尾、Work/Code 离线降级与恢复、Team 和写入召回；未重启用户实例。

## 2026-11-26：复查修复

- Team 插件的登记、变更及撤销仅访问内置服务。服务离线时跳过登记和新增成员；撤销先按监听器身份写入本地日志，并在服务下次开放监听前重放，成功后清除待处理记录。旧项目记忆调用仍保留其原有连接行为。
- 宿主成员操作登记为在途操作，释放生命周期锁后再执行。停机有界等待，待 HTTP 和宿主操作都结束后才释放底层记忆库。
- 目标续接或有并行回合时，在离线降级前校验显式记忆开关变更；保留已保存的选择和当前挂载。服务恢复后在允许切换的普通回合装配，其他装备变更规则不变。
- Windows 扩展回归 62 项通过，含 Team 离线时拒绝两种旧连接、成员操作期间的停机等待、Work/Code 的目标续接和普通回合恢复。另有 3 项旧 SDK 视频导入用例因本地媒体不在允许范围内失败；未改动该权限策略，也未重启用户实例。

## 未覆盖

未通过桌面前端验收，未在用户数据目录或生产 HTTP 服务中执行回写。旧安装不会被自动覆盖，因此不迁移已安装副本上的任何自定义文件。
