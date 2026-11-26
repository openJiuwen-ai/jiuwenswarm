---
author: Wang Tao
author_email: zzyyww0808@gmail.com
designs: []
design_note: 本文定义记忆插件的装配、回写与 Team 成员管理契约。
id: S_04_coding-document-memory
title: Coding 文档记忆接入契约
type: S
modules: [jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness/common/memory]
files: [jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/agents/harness/common/memory/jiuwen_rail_builder.py, jiuwenswarm/server/runtime/agent_adapter/interface.py, jiuwenswarm/agents/harness/common/memory/plugin.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py]
---

# Coding 文档记忆接入

本文同时覆盖共用同一内置记忆服务的 Work 主 Agent。

最近一次修订日期：2026-11-26。

## 单人 Work / Code 插件

- `agent-memory-plugin` 是 Work/Code 主 Agent 的唯一自动长期记忆入口。运行时启动时安装发行资源；会话创建时按 `memory.plugin.default_enabled` 决定默认选择，旧会话沿用 `session_equipment.plugin_names`。显式空列表表示关闭。创建响应携带已保存的 `session_equipment`（存在时），Web 会话据此同步插件开关。创建前的 Work/Code 单 Agent 草稿读取已安装插件列表中的 `default_enabled`，手动关闭优先于默认值。
- 插件契约声明主 Agent 的 Work/Code profile、`memory_context` 服务和 `automatic_memory` 能力。主 Agent 旧 Rail、云记忆钩子、AutoMemory 抽取和 Dreaming 启动路径不再接管，也不启用第二套回写。Team 保留原入口；会话历史、项目说明 `ProjectMemoryRail` 及用户启用的 MCP 连接器独立。
- `rails/memory_rail.py` 按 profile 调用 `build_jiuwen_memory_rail`，只使用 WorkSwarm 内置 HTTP 服务。包配置、`memory.builtin.jiuwen` 和 `plugins.agent-memory-plugin` 仍按顺序合并模型和路由参数；客户端模式、URL 和密钥不再由配置决定。
- 新会话默认开启，不受旧记忆开关影响。监听器不可用时，Work/Code 本轮跳过记忆装配且继续对话，保留已选插件；目标续接和有并行回合时沿用当前挂载状态，下一次允许切换装备时再根据服务状态调整，不回退到旧记忆入口。
- 内置监听器最多并行处理 32 个连接，连接 I/O 超时为 5 秒。停止时断开监听端口和现有连接，不再准入宿主成员操作，宿主最多等待 5 秒；如有 HTTP 请求或宿主成员操作仍在执行，后台继续清理，在处理结束前不释放或重新打开同一记忆库。
- 个人版在 AgentServer 启动时拉起监听器，在会话 Runtime 关闭后停止。仅监听回环地址，使用进程内生成并传递的密钥；插件详情只显示状态和本地地址。企业版不启用此单一身份的服务。
- 装配复用现有 Rail 和 Provider；Work 使用 `JiuwenMemoryRail`，Code 使用 `JiuwenMemoryCodingRail` 和 Code 专用工具。单次挂载只有一个写入者。有效回合结束排队回写，卸载收束已有写入并移除工具、回调和临时上下文。超时不把任务假定为已结束。加载失败对该请求报告原因，不回退旧路线。
- HTTP 使用服务侧的身份映射、模型和存储，复用 `agent/memory/managed-http`，不自动迁移或删除 SDK、外部服务的旧记忆；卸载插件不删除存量数据。
- `modes.code.memory.enabled` 单独控制 `ProjectMemoryRail`；Code 工具不能覆盖宿主绑定的项目键。

`memory.builtin.jiuwen.portal.enabled` 仅控制管理页和链接签发，不决定插件是否挂载。管理页的 Provider 选择、默认 SDK 配置和连接生命周期见 [S_03_memory-portal](../S_03_memory-portal.md)。

## 身份和存储

单人 Work/Code 以 memory.builtin.jiuwen.agent_id 绑定记忆，未配置或空值时使用 main_agent。配置身份改变会重建记忆 Rail，仅运行名称变化不会。session_id 如实传入，默认空字符串。公共 builder 的 agent_name 参数保留调用兼容。Team 按下文的稳定成员归属绑定。

单人 Work/Code 共用内置 HTTP 记忆库；不同运行名称的自定义助手使用相同配置身份时共享画像。普通聊天的新建、刷新和成员替换不改变配置身份。HTTP 请求由服务端映射身份和 Scope。

旧 SDK 调用方独立使用自己的 Markdown 根，缺省由 `get_agent_workspace_dir()` 解析，显式根目录优先；内置 HTTP 插件不复用该目录。

既有数据保留写入时的 Scope，不自动迁移或跨 Agent 兜底搜索。默认 main_agent 与原默认写入身份对齐；其他维度不变时可继续访问原默认身份数据。已有配置若显式设置其他 agent_id，则仍使用该值；需访问旧 main_agent 数据时应调整为 main_agent。配置统一作用于三种模式，不合并多个旧身份。

Work/Code 项目键统一使用 `resolve_coding_memory_project_name`：规范绝对路径的目录名 + 12 位摘要，同名不同目录隔离；未指定项目为 default。Code 工具不能覆盖该键；Work 自动回调及工具默认值取当前所选项目，未选项目归入 default，显式项目坐标仍可访问既有记忆。相同文档根或服务、相同允许访问的身份下，两种模式选择同一项目路径即可共享项目记忆；不同路径保持隔离。共享画像为 USER.md，项目内容在 memory/{项目键}/；详见 agent-memory 的 S11。

## 发布依赖

Swarm、core 与 agent-memory 须使用包含本特性的配套版本。正式发布时更新 Swarm 对 core、JiuwenMemory 的依赖及 lock；仅合并源码不代表已发布的依赖包包含本特性。

## 场景装配扩展

`build_jiuwen_memory_rail` 是 Jiuwen 记忆专用装配入口，不负责其他功能 Rail。既有参数和默认值保持不变，追加可选 `team_binding`、`security_provider_factory` 与 `wrap_rail_class`；通过场景工厂获得 Rail 类型、Provider 参数及 Rail 参数，再执行公共身份解析和 SDK/HTTP 装配。

内置 `agent` 工厂延迟导入 Work Rail，并选择上游增强版 `SwarmJiuwenMemoryRail`，保留按实际项目路径解析与 `session_end_extract`；`code` 工厂仅导入 Coding Rail，并传递绑定的 `scene/project`。工厂每次返回独立参数，不缓存 Provider 或 Rail。未注册场景、依赖缺失或创建失败返回 None，由现有调用方执行停用策略。

扩展接入时新增场景工厂并注册；若记忆行为不同，同时在 agent-memory 注册对应场景策略。新 Rail 沿用现有 Provider 协议，调用时机仍由 Core Rail 定义。该注册机制不增加用户配置键；单人 Work/Code 的启停由会话插件管理。


共用 builder 的旧 SDK 调用方仍遵循既有媒体权限和配置规则；内置插件不再装配 SDK Provider，也不通过会话 Rail 向 HTTP 服务下发媒体配置。媒体授权范围及门禁见同目录 S_01_multimodal-tool-loading。旧 SDK 配置的 `markdown_store`、`default` 和 `params` 允许 YAML 空值并使用缺省字典；`default` 的字符串 target 简写兼容，其他错误类型报告配置错误。

## Team 扩展

Team Harness 由宿主直接从内置包加载记忆 Rail 工厂，按会话插件开关和可信团队坐标装配成员。通用插件运行时仍不接受 Team profile。

`memory.builtin.jiuwen.portal.enabled` 仅控制管理页面与新链接签发，不控制 Team Rail 的装配、预取、回写或工具调用；不存在由 Portal 新增的顶层 `memory.enabled` 总开关。页面开关及实例生命周期见 [S_03_memory-portal](../S_03_memory-portal.md)。

`attach_only` 是被动事件订阅，不调整宿主的记忆 Rail；真实输入仍按原规则移除入口的普通记忆 Rail。同一 DeepAdapter 的内置记忆配置协调必须串行覆盖判断、注册、清理和注销，避免请求与配置刷新重复释放同一对象；不同 Adapter 独立协调。回归与故障依据见 [Team 记忆 Rail 并发卸载修复](../../../../../bugfix/jiuwenswarm/server/runtime/agent_adapter/B_20261126_1052_team-memory-rail-race.md)。

team.work、team.code 在 memory.builtin.provider=jiuwen 时进入 Jiuwen 路线，Team Harness 根据会话的内置记忆插件选项装配 Team Rail，旧会话无装备时沿用插件默认值。关闭插件后不再召回或写回，也不启用旧 Team 记忆。内置监听器及宿主团队坐标须可用；缺少团队坐标时记录告警并跳过 Rail，初始化失败不回退旧记忆。team.design 和 provider 为空时保持原生路线；单人 Work/Code 的长期记忆独立由会话插件控制。

团队入口通过 SwarmBuildContext 绑定账号、项目与团队，在 Swarm 内部保留专家团标识用于派生稳定团队键与解析成员模板身份（包括 role="leader" 对应的 {专家团}:leader），但向 Memory 仅绑定和登记项目与团队维度，不向 Memory 传递或登记 group 维度。常规集群使用主 Agent 身份，专家团的稳定专家（含配置了 leader 模板的团长）使用模板身份，临时成员使用 main_agent。每位回答者各持 Rail、Provider 和缓存。SDK 按规范文档根共享 Runtime，首份配置生效，后续差异告警。入口只装配 Rail，答复者负责预取和回写。恢复时按当前配置解析记忆身份。SDK 始终提供共享文档根，每次请求解析调用身份。HTTP 通过现有可信连接调用服务端 Runtime。

团队键按归属账号和专家团派生。项目使用宿主确定的项目键，无项目时为 default。Swarm 经专用管理身份在内置服务所拥有的库上管理成员，不向 HTTP 客户端授予管理权限，不访问旧 SDK 或外部 HTTP。服务离线时跳过登记和新增成员；撤销记录持久化，并在内置服务下次开放监听前重放，成功后清除。登记失败同步到成员恢复使用的 seed，恢复成员与 Leader 一样跳过 Team 记忆 Rail。

Team 的四个工具为 team_memory_search/read/write/edit。每次搜索使用请求的总条数预算及前置成员和分享校验。自动提炼由真人成功回合或子任务验收触发，单次抽取决定条目的合适落点。敏感内容过滤沿用原开关，覆盖显式写改及自动回写。

租约构造成功才计入引用；首次绑定失败清理无人持有的实例，已有实例的借用失败不增加引用、不影响原持有者。成员退出归还租约；最后一个引用退出前等待已接受操作结束。正常关闭期间的新申请等待完成后重新查找或创建实例，等待不阻塞其他根目录；关闭失败继续隔离，不新建覆盖旧实例。进程退出清理资源。独立 Provider 未使用宿主持有者时保持原生命周期；SDK continuity 池的配置和主体冲突规则不变。

Jiuwen Team 入口不挂 ProjectMemoryRail，冷启动、模式切换和重建都遵循这一限制；切回单人 Code 后按原开关恢复。此限制不移除编码回答者上的 ProjectMemoryRail；provider 为空的原生路线不变。
