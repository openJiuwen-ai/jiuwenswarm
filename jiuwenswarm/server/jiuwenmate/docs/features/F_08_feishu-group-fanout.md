# 飞书接入与群内分发设计方案

## 1. 元信息

| 项 | 值 |
| --- | --- |
| 建档日期 | 2026-10-14 |
| 范围 | JiuwenSwarm 新版 JiuwenMate 的飞书绑定解析、入站消息标准化、个人版与新版路由隔离，以及按接收 Bot 分发到新版同事；不改 agent-core 的执行逻辑 |
| 方案成熟度 | 按飞书应用配置中的 `persistent_agent_id` 建立一对一绑定，并映射为运行时 `agent_id`；通过 Gateway→AgentServer E2A 请求交接并由 AgentServer 持有 JM-07 生命周期 |
| 计划条目 | [JM-08 飞书接入与群内分发](../designs/jiuwenmate-development-plan.md) |
| 负责人 | 闫祉君（与开发计划一致） |
| 状态 | 待验收（提交 `496b04d9`；宿主组件通过，联合验收待完成。真实飞书应用 / 群联调与 PA-07 群执行仍待） |
| 设计依据 | [JiuwenMate 服务端架构设计](../designs/jiuwenmate-architecture.md) 第 5.3、9.1 节及附录 A；[JM-02 同事定义与配置发布](F_02_colleague-management.md)、[JM-03 会话定位与请求受理](F_03_session-request.md)、[JM-07 同事管理器、Session 排队器与执行驱动](F_07_execution-driver.md)；模板：[特性设计方案模板](../templates/feature-design-template.md) |
| 验证摘要 | 2026-10-15：既有 JM-08 生产接线回归 142 项通过；补充 JM-09 所需的 Feishu Request 路由快照及 outbox / Gateway ACK 后，JM-08/JM-09 联合聚焦测试最新为 105 项通过。2026-10-19：`app_id` 与 `persistent_agent_id` 一对一绑定、配置保存及同事绑定状态相关的 3 项定向测试通过；前端相关测试 32 项通过，生产构建通过。真实 SQLite、E2A/AgentServer、精确 app 路由及个人版相关用例有覆盖；本地 agent-core 导入链的 `SyntaxWarning` 需在联合测试命令中忽略。未做真实飞书应用 / 群联调；PA-07 群执行不属于本次验收 |
| Refs | 无独立 issue；需求来源为开发计划 JM-08 |

## 2. 背景、目标与范围

### 2.1 背景与问题

JiuwenSwarm 已有飞书连接、事件解析和通用 `MessageHandler`。新版 AI 同事需要使用这些平台能力，但消息必须进入独立的 JiuwenMate 请求链：由实际接收事件的飞书应用 `app_id` 找到配置中的 `persistent_agent_id`，并映射为运行时绑定 `agent_id`，保留群聊上下文后交给 JM-03 持久受理。不能因为同一条群消息被多个 Bot 收到，就在全局按消息 ID 只保留一份；也不能让新版消息误进个人版的 AgentServer 路由。

当前飞书入口在 `FeishuChannel._on_message` 中会跳过 `sender_type == "bot"` 的消息；这是个人版既有行为。新版还需要把其他 Agent 发出的消息交给 core 的群参与判断，因此接入时必须为新版绑定通道建立有边界的处理路径，而不是全局取消个人版的过滤。

### 2.2 目标与关键场景

| 场景 / 触发条件 | 目标行为 / 可观察结果 |
| --- | --- |
| 新版已绑定的飞书应用收到群消息 | 以接收通道实例的 `app_id` 查绑定，定位唯一 namespace / Agent；群、发送者、正文、@、回复和附件引用按 core 契约传入 |
| 同一飞书事件被平台重投 | 生成稳定的 `source_ref`；JM-03 按 `(namespace_id, agent_id, source_ref)` 幂等受理，同一 Agent 不产生第二条 Request |
| 多个 Bot 收到同一群消息 | 每个 Bot 只把自己收到的事件交给自己绑定的 Agent；不同 Agent 各有独立 Request / Session，不用全局事件去重互相吞消息 |
| 另一位 Agent 在群内发言 | 新版入口保留 `sender_kind="agent"` 并交给 core 判断；只有能可靠识别为当前接收 Bot 自己的回声时才抑制，避免自循环 |
| 个人版飞书应用收到消息 | 沿用原有解析、Bot 消息过滤和 `MessageHandler` 路由；不进入 JiuwenMate 请求链 |
| 新版 Agent 的飞书私聊 | 转成独立的 `direct_message` Request，不与群 Session 或个人版私聊混用 |

### 2.3 本次范围与非目标

- JM-08 整体目标包含：
  - 新版飞书绑定复用所有者现有应用配置；每个应用项以 `app_id`、应用凭据和 `persistent_agent_id` 表达绑定，其中 `persistent_agent_id` 就是 JM-02 同事的 `agent_id`。不另建绑定表或持久化绑定 ID；运行时按 `app_id` 解析到该同事。
  - 飞书事件标准化、来源引用生成、群 / 私聊会话定位及新版请求受理。
  - 在飞书入口和公共 `MessageHandler` 中加入明确的新旧路由分界，保证个人版路径不变。
  - 群消息按实际收到事件的 Bot 逐 Agent 处理；不由 Gateway 根据 @ 列表反查或广播给其他 Bot。
- 本次不包含：
  - core 的群参与判断、窗口拼装或工具执行；新版只将消息交给 core 判断。PA-07 就绪前不开放群执行。
  - 结果生成后的飞书发送、@ 协作和送达状态（JM-09）；群公共记录、搜索和附件资源服务（JM-11）。
  - 个人版消息语义、个人工作空间、历史数据或既有 Bot 路由的迁移。
  - 把飞书附件直接写入个人版工作空间，或把平台文件 ID 当作 workspace 授权。

2026-10-14 产品规则已确认：接收 Bot 被飞书加入群后，飞书向该 Bot 投递经校验的群事件，即允许绑定 Agent 接收该群消息；不另做群白名单、审批或工作空间权限。一个飞书应用 `app_id` 只绑定一个 JM-02 同事；`persistent_agent_id` 保存的就是该同事现有的 `agent_id`，两者是一对一关系，不再引入额外身份字段或绑定表。配置解析后映射为运行时 `agent_id`。JM 数据位于该所有者的数据目录下的 `jiuwenmate/agents.sqlite3`，当前单所有者运行时使用 `namespace_id="local"`。Gateway 经 E2A WebSocket 转发标准化事件；AgentServer 持有 JM-07 调度器和持久化服务。`AccessSnapshot` 仅标识飞书发送者，`startup_scope` 与 `source_scope` 为空，不授予工作空间访问权。

## 3. 设计约束、假设与待决问题

### 3.1 已确认约束与接口事实

- JM-02 提供新版同事身份及当前发布配置；JM-03 提供 `ConversationRef`、稳定 Session 和 `accept_request` 的持久幂等受理。JM-08 的计划硬前置是 JM-02、JM-03。
- 最新 JM-07 已提供 `AgentManager.scheduler.accept(session, **accept_kwargs)`，返回 `(RequestState, is_new)`；其内部调用 JM-03 的 `ExecutionRepository.accept_request` 并驱动排队。真实 AgentServer 启停接线仍在 F_07 中标为推迟到 S1，不能把替身测试说成运行时已接通。
- core 的 `RequestInput` 支持 `group_message`、`direct_message`；群来源 `GroupMessage` 可承载 `sender_kind`、`mention_ids`、`reply_to`、`attachment_refs`。私聊来源使用 `DirectMessageSource`。字段映射以 agent-core 当前公开契约为准，不能在 Swarm 再造一套群参与判断。
- 飞书通道实例可从自身配置提供 `app_id`；当前 `MessageHandler.resolve_app_id` 会优先读取 `Message.app_id`，再回退到 `bot_id`。新版路由应使用可信的接收通道实例身份，不使用消息正文或 @ 列表推断接收 Bot。
- `GroupChatRepository.resolve` 只建立 `(namespace_id, platform, platform_chat_id)` 到 `group_chat_id` 的稳定映射，不检查 Bot 是否在群；群映射本身不是接入许可。产品规则已确认：绑定到该 Agent 的飞书 Bot 被加入群，即允许该 Agent 处理该群消息。对当前入站事件，以飞书向该绑定 Bot 投递该群事件作为其在群的事实；不另建群白名单或审批流程。
- 飞书通道现有内存去重可减少单进程重复处理，但不能替代 JM-03 的持久幂等；重启后仍由 `source_ref` 与数据库唯一约束防止重复 Request。

### 3.2 群准入规则与生产接线缺口

| 边界 / 问题 | 已确认规则 / 当前事实 | 责任与影响 |
| --- | --- | --- |
| 群准入（已确认） | 接收 Bot 被加入群即允许绑定 Agent 接收该群消息；以飞书向该 Bot 投递经校验的群事件作为事实。不新增群白名单、审批或额外群权限判断。 | JM-08 按此规则处理；群映射只用于稳定定位 Session |
| 生产群—Agent 绑定来源 | 所有者本地 `.jiuwenswarm/config/config.yaml` 的 `channels.feishu.apps[]` / `channels.feishu_enterprise.<app_key>` 中，每个应用项用 `app_id` 和 `persistent_agent_id` 组成一对一绑定；后者就是 JM-02 同事现有的 `agent_id`。运行时按收到事件的 `app_id` 解析，并映射为内部 `agent_id`。`namespace_id` 当前固定为 `local`。 | 复用现有应用配置，不建额外绑定表、持久化 binding_id 或凭据副本；缺失 `persistent_agent_id` 表示个人版通道；重复应用或同事绑定、配置漂移或无效 Agent 失败关闭 |
| JM-07 生产生命周期与请求上下文 | AgentServer 的 `AgentWebSocketServer` 在启动时仅对含有效 Feishu `app_id → persistent_agent_id` 绑定的所有者启动 JM-07；无绑定时不创建 JM 数据库或调度器。收到绑定事件时按需补启，关闭时回收 `AgentManager`。运行时内部仍使用 `agent_id`。 | `AccessSnapshot` 以 Feishu 发送者为 principal，`startup_scope` / `source_scope` 为空；不实现文件或工作空间授权 |
| Gateway 到 AgentServer 的请求交接 | 只有 Feishu 通道配置了非空 `persistent_agent_id` 时才注入 JM-08 回调；Gateway 用内部 E2A `jiuwenmate.feishu.inbound` 传递标准化事件和预期运行时 `agent_id`。AgentServer 按本地 `app_id` 配置复核后调用 JM-03/07。 | 未绑定个人版通道不走 E2A；绑定配置不一致、受理失败或响应无效时拒绝事件，不回落个人版 |
| 飞书附件引用 | JM-08 只标准化并传递附件引用，不下载或读取文件，也不定义工作空间权限。 | 附件内容读取不属于本特性；本次不新增工作空间权限逻辑 |
| 自身 Bot 回声与失败重投 | 自身回声只在平台提供可验证发送方应用身份时按接收 `app_id` 精确判断；SDK 重投语义需真机验证。JM-08 不承诺独立持久 Inbox。 | 影响防循环与可靠接收验收，不影响已确认的群准入规则 |

## 4. 总体方案与设计取舍

### 4.1 方案概览

先按接收飞书应用身份判断是否属于新版绑定，再决定走 JiuwenMate 还是原有个人版路径。新版事件在进入个人版的 Bot 过滤和 `_user_messages` 队列前被识别；只有匹配唯一有效绑定的事件进入新路由。群聊中，绑定 Bot 被加入该群即视为允许该 Agent 接入；由飞书投递给该 Bot 的已校验群事件证明当前接入事实，不再检查独立群白名单。随后经 JM-03 的群映射定位 Session；私聊使用飞书私聊 `ConversationRef`。再从 JM-02 读取当前配置快照，构造 core `RequestInput`，经 JM-07 的 Scheduler 调用 JM-03 幂等保存并排队。

```mermaid
flowchart LR
    E[飞书事件<br/>接收通道 app_id] --> B{新版有效绑定?}
    B -->|否| L[原个人版入口<br/>既有 Bot 过滤与路由不变]
    B -->|是| N[保留原始发送方类型<br/>标准化正文、@、回复、附件引用]
    N --> G{接收 Bot 已绑定且群事件通过飞书校验?}
    G -->|否| X[拒绝新版受理<br/>记录原因，不回落个人版]
    G -->|是| C[解析 ConversationRef<br/>JM-03 群映射 / 私聊键]
    C --> R[JM-03 定位 Session<br/>JM-02 读取当前配置快照]
    R --> S[JM-07 Scheduler.accept]
    S --> D[JM-03 持久幂等受理<br/>namespace + agent + source_ref]
    D -->|新 Request| Q[排队 / 执行<br/>PA-07 判断群消息参与]
    D -->|已存在| I[返回既有 Request<br/>不新建同 Agent 请求]
```

此处的“群内分发”是平台分别把消息交给在群内的 Bot，Swarm 再按每个接收 Bot 的绑定路由到对应 Agent；不是收到一次事件后由服务端遍历所有 Agent 广播。多个 Bot 收到相同平台消息时，共享相同平台来源身份，但因 JM-03 的去重键含 `agent_id`，不同 Agent 的请求互不覆盖。

### 4.2 关键决策

#### D-01 按接收 `app_id` 路由，不按 @ 列表选 Agent

- 依据：@ 列表表示消息提及对象；开发计划和附录 A.3 明确要求按接收通道实例及其 `app_id` 查绑定。否则 A @ B 时可能绕过 B 的实际入站链路，或让未收到事件的 Bot 被错误调用。
- 结果：事件仍只交给当前接收应用绑定的 Agent，不按 @ 列表跨应用广播。若该接收 Bot 自身被明确 @，JM-08 将 Feishu Bot `open_id` 对应为绑定的内部 `agent_id`，只加入本次 Request 的消息来源；PA-07 因而直接进入 `respond` 执行。若 @ 的是其他 Bot，不会因此让当前未被 @ 的 Agent 强制执行。
- Bot `open_id` 不是用户配置的绑定 ID：Gateway 用已有 `app_id` / `app_secret` 查询飞书 Bot 信息获得，并缓存到该应用配置；JM-08 在标准化入站事件之前确保已解析，用于精确判断“当前接收 Bot 是否被 @”。
- ExecutionDriver 执行前刷新触发消息时，保留已受理 Request 的 `mention_ids`，仅更新正文、状态和附件；不再从公共归档重建提及对象，避免丢失已经核实的当前 Agent 标记。该副本不回写公共群归档。

#### D-02 新旧路由在飞书入口处明确分流

- 依据：当前飞书 `FeishuChannel._on_message` 在公共回调前丢弃 `sender_type="bot"`；通用 `MessageHandler.handle_message` 随后进入个人版身份解析和 AgentServer 队列。只在后半段新增路由无法恢复已被过滤的 Agent 消息。
- 结果：新版绑定事件在个人版过滤 / 入队前进入 JM-08；未绑定事件仍完整使用旧流程。匹配新版绑定但校验失败时不得回落个人版，避免同一事件跨模式重复执行或越权。

#### D-03 `source_ref` 使用平台消息身份，并按 Agent 幂等

- 建议：对当前 `im.message.receive_v1` 事件使用稳定消息 ID 生成 `feishu:message:<message_id>`；不拼接接收 `app_id`，以便同一消息被多个 Bot 收到时仍有一致来源标识。若事件样本表明重投语义需要使用平台 event ID，应先与 JM-03 的 `RequestInput.source.message_id` 对齐后再调整。
- 依据：JM-03 的唯一键为 `(namespace_id, agent_id, source_ref)`。不做全局 `(platform, message_id)` Request 去重；全局公开群记录的归档去重归 JM-11。

#### D-04 保留 Agent 来源类型，只有可确认的自身回声才过滤

- 依据：计划 JM-08-V-03 要求人类与 Agent 消息都进入 core 判断；当前个人版“丢弃全部 Bot 消息”的规则不能直接沿用到新版。对其他 Agent 的消息，使用 `GroupMessage.sender_kind="agent"` 交由 PA-07 判断。
- 结果：若事件中能可靠核对发送者 app 身份与接收绑定相同，则过滤自身回声；无法确认时按待决问题处理，不扩大过滤为所有 Bot 消息。

#### D-05 只标准化和传递附件引用

- 依据：core `RequestInput` 保存 `attachment_refs`；JM-08 的范围是接入和分发，不包含文件读取、工作空间访问或权限实现。个人版 FeishuFileService 的落盘路径不在本特性复用范围内。
- 结果：JM-08 负责保留稳定附件引用和来源关联；附件内容如何读取由后续对应特性决定，不作为 JM-08 的生产启动前置权限工作。

#### D-06 Bot 加入群即允许该 Agent 接入

- 依据：产品决策采用旧版的群接入边界，不增加单独群白名单或审批步骤。
- 结果：只接受来自已校验 Feishu 事件、且接收 Bot 的 `app_id` 唯一绑定到目标 Agent 的消息。飞书向该 Bot 投递群事件即作为 Bot 已加入该群的事实；不另查群白名单或做个人级群成员授权。仅创建 `group_chat_id` 或 Session 不代表曾收到群事件。

### 4.3 替代方案与排除理由

| 候选方案 | 排除理由 |
| --- | --- |
| 根据消息中的 @ 列表把同一事件广播给所有被提及 Agent | 接收 Bot 身份与被提及对象不是一回事；可能绕过真实平台收件权限，也会破坏 A @ B 后由 B 入站处理的计划语义 |
| 对 `(platform, message_id)` 做全局 Request 去重 | 多个绑定 Bot 收到同一事件时会吞掉其他 Agent 的独立请求；全局去重只适用于 JM-11 的公共记录，不适用于 Request |
| 复用个人版 `_user_messages` / AgentServer 请求队列 | 会把新版 Request、Session 和个人版对话混在一起，无法由 JM-03 / JM-07 持久受理与调度 |
| 全局取消飞书 Bot 消息过滤 | 会改变个人版行为并引入回环；新版只对有效绑定的事件采用差异化处理 |

## 5. 详细设计

### 5.1 模块责任与边界

| 组件 | 责任 | 不负责 |
| --- | --- | --- |
| Feishu 入站适配（现有 `feishu_connect.py`） | 在旧 Bot 过滤、内存去重和个人版文件下载前构造事件并调用可选 JM-08 回调；仅 `UNCLAIMED` 继续旧流程 | 不改变未绑定个人版 Bot 消息过滤；不下载新版附件；不做 core 参与判断 |
| 新版绑定查询 / 连接配置 | 生产解析器从所有者本地配置读取飞书应用 `app_id` 和 `persistent_agent_id`，将后者映射为 `FeishuBinding.agent_id`；ChannelManager 仅为运行时 `agent_id` 非空的通道接入 JM 回调 | 不根据 @ 列表推断目标；不新增凭据存储，不把凭据写入事件或日志 |
| `server/jiuwenmate/feishu_group_fanout.py` | 负责生产配置解析、绑定校验、字段标准化、Conversation / Session 定位、`source_ref` 生成和 JM-07 Scheduler 受理 | 不实现工作空间权限、不执行 Agent 本体、不发送结果 |
| 公共 `message_handler.py` 与 `channel_manager.py` | 绑定通道通过 E2A 将事件交给 AgentServer；未绑定通道不发 E2A，维持个人版路径 | 不把新版事件再次放入个人版 `_user_messages` |
| JM-02 / JM-03 / JM-07 | 提供当前同事配置、群 / 私聊 Session、Request 持久幂等受理和调度；AgentServer 管理运行时生命周期 | 不承担飞书 SDK 解析或个人版路由 |

绑定配置直接扩展现有所有者本地 `config/config.yaml`：`channels.feishu.apps[]` 的应用项和 `channels.feishu_enterprise.<app_key>` 均填写 `persistent_agent_id`。配置路径由 `get_config_file()` 决定；正常已初始化安装为 `~/.jiuwenswarm/config/config.yaml`，使用 `JIUWENSWARM_DATA_DIR` 时位于该目录的 `config/config.yaml`。解析器把 `persistent_agent_id` 映射为运行时 `agent_id`；该值必须对应 `~/.jiuwenswarm/jiuwenmate/agents.sqlite3`（或对应数据目录中的同名数据库）内已发布的同事。新版配置不再读取旧键 `agent_id`，不做兼容回退。应用配置仍是飞书凭据的唯一来源，不新增凭据存储。

### 5.2 入站识别与互斥路由

1. 从当前 `FeishuChannel` 实例取得 `platform="feishu"` 与实际配置 `app_id`，不能从发送者字段、消息正文或 @ 列表反推接收应用。
2. 查询新版绑定：
   - 无有效新版绑定：返回“未接管”，沿用个人版现有路由。
   - 恰有一个有效绑定：由 JiuwenMate 路由独占处理该事件。
   - 多个有效绑定、失效同事或绑定信息冲突：记录可诊断错误并拒绝新版受理；不猜测 Agent，也不回落个人版。
3. 对 `sender_type="bot"` 的消息，仅新版有效绑定路径进入来源身份判断；个人版仍按当前行为过滤。自身回声须由可信发送方 app 身份精确判断。
4. 对群聊，必须确认事件来自已校验的 Feishu 接收通道，且接收 `app_id` 唯一绑定到目标 Agent；Feishu 已向该 Bot 投递此群事件即视为 Bot 在群、Agent 获准接入。不额外查群白名单或开发工作空间权限。

绑定解析按 `platform + app_id` 查询；`app_id` 必须恰好命中一个已启用配置项且该项有非空 `persistent_agent_id`。解析结果将该值作为运行时 `agent_id`。0 个命中时未绑定并沿用个人版处理；多个命中、无效 Agent 或配置读取失败时拒绝新版受理且不回落个人版。当前单所有者本地部署以 `namespace_id="local"` 标识该数据空间；后续 JM-10 引入多用户 / 多工作空间身份时，再由 JM-10 将此局部映射替换为调用者实际 namespace，不改变 JM-08 的按 `app_id` 路由语义。

### 5.3 飞书事件到 core 请求的字段映射

| 输入事实 | JM-08 标准化 / core 字段 | 规则 |
| --- | --- | --- |
| 接收通道配置 | 绑定查询键 `platform + app_id`；解析结果为 `namespace_id="local"` 和由配置 `persistent_agent_id` 映射出的运行时 `agent_id` | `app_id` 来自当前 FeishuChannel；@ 不参与绑定查询；配置项需唯一、启用且绑定目标有效 |
| 平台消息 ID | `source_ref="feishu:message:<message_id>"`；`source.message_id` | 重投不变；不含 `app_id`，使同一事件跨 Bot 的身份一致 |
| 会话类型与 `chat_id` | 群：`GroupChatRepository.resolve` 后 `ConversationRef.group(group_chat_id)`；私聊：`ConversationRef.im_direct("feishu", chat_id)` | 群与私聊各自隔离；不同 Agent 在同一群有不同 Session |
| 发送者 | 群：`GroupMessage.sender_id`、`sender_name`、`sender_kind`；私聊：`DirectMessageSource` | Feishu 用户映射为 `human`；其他 Agent Bot 映射为 `agent`；事件未提供显示名时以真实 sender ID 作非授权用途的名称标签；未知类型不得臆造身份 |
| 正文 | `RequestInput.text` 与群来源 `GroupMessage.text` | 保留消息正文；附件消息无正文时仅顶层 Request 使用 `[附件消息]` 满足非空文本契约，来源正文仍为 `None`；不把调试 / 传输字段拼入正文 |
| @ 列表 | 共享归档保留 Feishu 原始 `open_id`；Request 的 `GroupMessage.mention_ids` 在当前接收 Bot 被 @ 时追加绑定的内部 `agent_id` | Bot 是否被 @ 由当前应用的 `bot_open_id` 与原始 @ 列表精确匹配；只改本次 Request 副本，不改公共群归档；PA-07 以内部 `agent_id` 确定进入 respond |
| 回复关系 | `GroupMessage.reply_to`；顶层 `reply_target` | 两者分别保留：前者表示来源消息回复关系，后者用于结果回到哪个平台会话；需与 JM-09 使用同一语义 |
| 附件 | `RequestInput.attachment_refs` 与 `GroupMessage.attachment_refs` | 当前只构造稳定的 `feishu:attachment:<message_id>:<file_key>` 引用，不下载文件；不传个人版绝对路径；文件读取和权限不属于 JM-08 |
| 接收时间 | `received_at` / 来源消息 `received_at` | 使用可信接收时间，统一时区；不要用处理排队后的当前时间覆盖平台消息时间 |
| JM-03 / JM-07 请求上下文 | `AccessSnapshot(principal_ref="feishu:<sender_kind>:<sender_id>", startup_scope=frozenset(), source_scope=frozenset(), policy_revision="jm08.feishu.bot-membership.v1")` | 仅固定真实发送者身份，不授予工作空间权限；群接入依据仍是 Feishu 已校验事件投递给已绑定 Bot |

群消息按 `RequestInput(kind="group_message", source=GroupMessage(...))` 构造；私聊按 `RequestInput(kind="direct_message", source=DirectMessageSource(...))` 构造。`request_input` 顶层与来源中的重复字段须按 core schema 同步填写，并由契约测试校验。定时任务不属于 JM-08。

### 5.6 JM-09 依赖的入站路由快照

Feishu 入站绑定校验成功后，JM-08 在调用 JM-07 受理 Request 时同时写入宿主侧 `RequestRouteSnapshot`，内容来自本次已校验事件，不从当前配置或消息正文反推：

| 字段 | 写入来源 | 用途 / 约束 |
| --- | --- | --- |
| `channel="feishu"` | JM-08 固定值 | 标记此 Request 的结果渠道 |
| `app_id` | `FeishuInboundEvent.receiving_app_id` | 固定本次接收 Bot；不保存 app secret |
| `target_id` | `FeishuInboundEvent.chat_id` | 飞书目标会话；不与消息 ID 混用 |
| `source_message_id` | `FeishuInboundEvent.message_id` | JM-09 可将文本结果回复到本次来源消息 |
| `source_reply_to` | `FeishuInboundEvent.reply_to` | 保留入站消息原有的回复关系；不替代目标会话或来源消息 |

快照随 Request 受理记录写入 JM SQLite，Request 重放使用首次受理时保存的快照。历史记录缺少快照时按不可安全路由处理，不查当前绑定“补猜”Bot。`RequestRouteSnapshot` 是 JiuwenSwarm 宿主数据，不扩展 agent-core 的 `RequestInput` 公共字段。

**责任边界：** JM-08 负责在绑定已验证、事件已标准化时保存以上入站事实；不负责产生 final、不负责发送结果。JM-09 负责从 final 输出建立 outbox、使用快照触发 Gateway 的专用发送并保存平台回执。`jiuwenmate.delivery.ack` 属于 JM-09 出站回执契约；其实现和状态语义以 [F_09](F_09_web-and-feishu-result-delivery.md) 为准，不改变 JM-08 入站接管规则。

### 5.4 受理、幂等与并发

推荐调用顺序：

1. 解析有效绑定并读取 JM-02 当前发布快照；同事不存在、已删除或无当前配置时拒绝受理。
2. 对群消息核验 Feishu 事件与接收 Bot 绑定；Bot 收到该群事件即符合已确认的群接入规则。随后标准化群 / 私聊来源和 `RequestInput`，按上述契约构造仅含发送者身份、无访问 scope 的 `AccessSnapshot`；该快照不决定群准入，也不授予工作空间访问权。
3. 群聊通过 `GroupChatRepository.resolve(namespace_id, "feishu", chat_id, now=...)` 取得稳定 `group_chat_id`；随后由 JM-03 `ExecutionRepository.resolve_session` 按 namespace、Agent 和 `ConversationRef` 定位 Session。私聊直接使用飞书私聊 `ConversationRef`。
4. 调用 JM-07 `AgentManager.scheduler.accept(session, snapshot=..., source_ref=..., access=..., request_input=..., received_at=...)`。该入口内部调用 JM-03 `accept_request`：数据库唯一约束是 Request 幂等的权威事实，`is_new=False` 表示来源已受理。
5. 未被飞书明确 @ 时，先按 [JM-23](F_23_group-reply-mode.md) 决定是否受理：被动模式只保留归档，不创建 Request；没有具体内容的寒暄只受理一位自适应员工。通过后，新 Request 进入 JM-07 的 Session 排队与调度。是否回应由 PA-07 判断，并阅读 JM-23 附加的职责与轻量交流说明。飞书明确 @ 不经过该模式门闩。

并发与隔离不变量：

- 同一 Agent、同一平台消息无论重投或由多个接收实例重复送达，只有一个 `(namespace_id, agent_id, source_ref)` Request。
- 同一平台事件面向不同 Agent 时，各自独立受理；禁止进程级或数据库级全局消息 ID 去重吞掉其他 Agent。
- 同一 Agent 在同一群使用 JM-03 的稳定群 Session；不同 Agent 在同群仍有独立 Session。
- FeishuChannel 的内存去重只是性能优化；正确性依赖 JM-03 的持久唯一约束。

**JM-07 联调注意：** 当前 `SessionScheduler.accept` 先检查 Session 排队数量，再调用 JM-03 的幂等 `accept_request`。因此，在队列已满时重放一个此前已受理的 `source_ref`，可能先得到 `QUEUE_FULL`，而不是既有 Request。它不会创建第二条 Request，但会让已成功事件的重投继续失败。JM-08 接入验收应覆盖此时序，并由 JM-07 确认是否需将“查重 / 幂等返回”置于容量拒绝之前；本文不擅自修改 JM-07 契约或实现。

### 5.5 失败处理

| 情况 | 处理 |
| --- | --- |
| 无新版绑定 | 不接管事件，进入既有个人版流程 |
| 新版绑定冲突 / 已失效 | 不派发、不猜测、不回落个人版；记录绑定标识与原因，日志不含凭据 |
| Feishu 事件校验失败或接收 Bot 未唯一绑定 | 不定位后续执行、不新建 Request；返回明确失败并保留诊断上下文 |
| JM-07 所需 Request 上下文缺失 | 不新建 Request；按 JM-07 既有执行契约报告装配失败，不在 JM-08 临时发明权限或默认上下文 |
| 消息身份缺失，无法形成稳定 `source_ref` | 拒绝受理；不生成随机 ID 伪装幂等成功 |
| JM-02 / JM-03 / JM-07 未启动或数据库失败 | 不报告已受理；记录错误并按真实 Feishu SDK 重投行为处理，不宣称 JM-08 自带 Inbox 恢复 |
| 重复事件 | 使用 JM-03 返回既有 Request；不得新建另一个同 Agent Request。队列已满时的重复处理见 JM-07 联调注意 |
| 可确认的当前 Bot 自身回声 | 仅在发送方应用身份与接收 `app_id` 精确匹配时过滤，避免循环 |

## 6. 影响面与实施设计

| 实施项 | 文件或模块 | 计划改动 | 验证产物 |
| --- | --- | --- | --- |
| I-01 | `gateway/channel_manager/im_platforms/feishu/feishu_connect.py` | 已实现原始事件字段标准化，并在旧 Bot 过滤、内存去重和附件下载前调用可选回调；未接管消息仍走个人版原逻辑 | JM-08-V-01、V-03；单测 |
| I-02 | `server/jiuwenmate/feishu_group_fanout.py` | 已实现注入绑定与请求上下文端口、JM-02 配置快照读取、群 / 私聊 Session 定位、稳定 `source_ref` 和 JM-07 Scheduler 受理；JM-09 前置路由快照在同一受理调用传入 `RequestRecord`；请求上下文沿用 JM-03 的 `AccessSnapshot`，不定义权限策略 | JM-08-V-01、V-02；真实 SQLite 与 AgentManager/Scheduler，core 执行入口为替身 |
| I-03 | `gateway/message_handler/message_handler.py`、`gateway/channel_manager/channel_manager.py` | 通道的运行时 `agent_id` 从外部配置 `persistent_agent_id` 映射；仅绑定通道接入 JM 回调；未绑定个人版通道不发 E2A，绑定请求由 Gateway E2A 交给 AgentServer | JM-08-V-03；个人版消息 / 附件回归 |
| I-04 | Feishu 配置、Gateway E2A 客户端、`ReqMethod`、`AgentWebSocketServer` | 已加入外部 `persistent_agent_id` 到运行时 `agent_id` 的映射和 `jiuwenmate.feishu.inbound`；AgentServer 负责按配置创建、启动、关闭 JM-02/03/07 运行时并返回接管状态；`AccessSnapshot` 无 workspace scope。结果发送与 ACK 是 F09/JM-09 的独立出站扩展 | JM-08-V-01～V-03；生产配置解析、E2A 转换、dispatch 与生命周期测试 |
| I-05 | `tests/unit_tests/gateway/test_persistent_group_fanout.py`、`tests/unit_tests/server/jiuwenmate/test_feishu_group_production_wiring.py` | 覆盖绑定配置形状、唯一 `app_id` 解析、事件 E2A 往返、AgentServer dispatch、条件启停、个人版隔离、JM-03/07 幂等，以及当前接收 Bot @ 身份映射和共享归档隔离；真实 SQLite、core 执行入口替身 | JM-08-V-01～V-03；专项测试已并入聚焦回归 |

**运行时接入边界：** FeishuChannel 和个人版 `MessageHandler` 在 Gateway；JM-02/03/07 数据服务与 Scheduler 在 AgentServer。Gateway 只对配置了非空 `persistent_agent_id` 的 Feishu 实例发送入站 E2A，并映射为运行时 `agent_id`；AgentServer 从所有者本地配置按接收 `app_id` 复核唯一绑定，再调用 JM-03/07。运行时按需启停，不改变未绑定个人版路径。JM-08 不新增工作空间权限。JM-09 的文本结果出站组件现已实现，但真实平台回执仍待飞书环境联调；PA-07 未就绪前不开放真实群执行。

## 7. 兼容与迁移

| 兼容范围 | 处理方式 |
| --- | --- |
| 个人版飞书接入 | 未命中新版绑定时继续原有入口，包括当前 Bot 消息过滤、身份解析、`_user_messages` 队列及 AgentServer 路由；不修改个人版消息语义 |
| 新旧事件隔离 | 命中新版绑定后由 JM-08 独占处理；失败不回落个人版，确保一条事件不同时进入两套执行链 |
| 个人版数据与文件 | 不迁移个人版历史，不复用个人版 Session、个人版文件路径或附件落盘结果 |
| 新版绑定 | 外部配置只接受 `persistent_agent_id`，旧键 `agent_id` 不再兼容，升级配置时需手动改名；运行时仍映射为 `FeishuConfig.agent_id`。连接凭据复用既有安全配置途径，不把个人版同事 / 会话数据转换为新版数据 |
| 数据迁移 | 不需要。新版 Request / Session 从新链路受理开始创建；旧数据保留原归属与原读取路径 |

## 8. 验证设计、执行记录与结论

### 8.1 验证设计与通过条件

| 验证 ID | 场景 | 验证入口 | 预期结果 | 层级 |
| --- | --- | --- | --- | --- |
| JM-08-V-01 | `app_id` 绑定与消息字段映射 | `test_persistent_group_fanout.py`；构造群 / 私聊及含 @、回复、附件的 Feishu 事件 | 路由到绑定 Agent；RequestInput 类型、发送者、时间、正文、@、回复和附件引用与原事件一致 | 单测 + JM-02 / JM-03 / JM-07 组件集成 |
| JM-08-V-02 | 重放、多 Bot 与群准入 | 以同一 `message_id` 经不同 Bot 绑定重复提交；并发重放同一 Agent 来源；未绑定 / 冲突 `app_id` 或未通过 Feishu 事件校验 | 同 Agent 只存在一条 Request；不同 Agent 各自有 Request；绑定 Bot 收到经校验的群事件即受理，不再检查额外群白名单；队列满重放行为符合 JM-07 确认后的语义 | 真实 SQLite、并发测试；绑定与事件校验用例；真机群补充验证 |
| JM-08-V-03 | 人类 / Agent 来源及个人版回归 | 以 `sender_type=user`、其他 Bot、当前 Bot 自身回声、未绑定个人版 app 发送事件 | 人类与其他 Agent 消息到新版 core 决策入口；可确认自身回声不循环；个人版旧过滤及路由不变；任何事件不同时进入两条执行链 | 单测；真实飞书测试应用和测试群 |

### 8.2 实际执行与证据

| 执行日期 / 范围 | 实现版本与环境 | 命令或步骤 | 实际结果 | 未覆盖项 |
| --- | --- | --- | --- | --- |
| 2026-10-14 / 组件与入口首轮实现 | Python 3.12.12；真实 SQLite JM-02 / JM-03 / JM-07 组件，core 执行入口替身 | `uv run pytest --no-cov tests/unit_tests/gateway/test_persistent_group_fanout.py -q`；`uv run ruff check jiuwenswarm/server/jiuwenmate/feishu_group_fanout.py jiuwenswarm/gateway/channel_manager/channel_manager.py jiuwenswarm/gateway/channel_manager/im_platforms/feishu/feishu_connect.py jiuwenswarm/gateway/message_handler/message_handler.py tests/unit_tests/gateway/test_persistent_group_fanout.py` | JM-08 新增测试 25 项通过；Ruff 检查通过 | 生产绑定来源 / 启动装配、AgentManager 生命周期、真机事件 / SDK 重投、PA-07 群执行、JM-09 回复和附件读取未验证 |
| 2026-10-14 / 聚焦回归与并发重投补充 | 同上 | `uv run pytest --no-cov tests/unit_tests/gateway/test_persistent_group_fanout.py tests/unit_tests/gateway/test_message_handler_mode.py tests/unit_tests/channel/test_attachment_persist.py tests/unit_tests/server/jiuwenmate/test_request_routing.py tests/unit_tests/server/jiuwenmate/test_session_scheduler.py -q`；`uv run ruff check jiuwenswarm/server/jiuwenmate/feishu_group_fanout.py jiuwenswarm/gateway/channel_manager/channel_manager.py jiuwenswarm/gateway/channel_manager/im_platforms/feishu/feishu_connect.py jiuwenswarm/gateway/message_handler/message_handler.py tests/unit_tests/gateway/test_persistent_group_fanout.py` | 131 项通过（含 JM-08、个人版消息 / 附件、JM-03、JM-07 回归和同 Agent 并发重投）；Ruff 检查通过 | 未运行全仓测试；未做飞书真机或生产装配 |
| 2026-10-14 / 绑定配置与生产 E2A 接线 | Python 3.12.12；JM-02/03/07 默认运行时、Gateway E2A 到 AgentServer 的内部 dispatch | `uv run pytest -W 'ignore:invalid escape sequence:SyntaxWarning' --no-cov -q -o log_cli=false --tb=short tests/unit_tests/gateway/test_persistent_group_fanout.py tests/unit_tests/server/jiuwenmate/test_feishu_group_production_wiring.py tests/unit_tests/gateway/test_message_handler_mode.py tests/unit_tests/channel/test_attachment_persist.py tests/unit_tests/server/jiuwenmate/test_request_routing.py tests/unit_tests/server/jiuwenmate/test_session_scheduler.py` | 聚焦 JM-08、个人版消息 / 附件、JM-03、JM-07 回归共 142 项通过；`compileall` 通过；Ruff 检查未见新增问题（忽略既有 E402 / F841 / F821） | 未做真实飞书应用 / 群联调；PA-07 群执行、JM-09 回复及全仓测试未覆盖 |
| 2026-10-15 / JM-09 前置路由快照与文本出站接线 | Python 3.12.12；JM-08 入站快照、JM-04 outbox、JM-09 文本 Gateway/AgentServer 路径；平台 API 为 SDK 替身 | `uv run --offline pytest -W 'ignore:invalid escape sequence:SyntaxWarning' --no-cov -q -o log_cli=false --tb=short tests/unit_tests/server/jiuwenmate/test_commit_port.py tests/unit_tests/server/jiuwenmate/test_web_private_chat.py tests/unit_tests/gateway/test_persistent_group_fanout.py tests/unit_tests/server/jiuwenmate/test_feishu_group_production_wiring.py` | 初次运行 101 项通过；加入 ACK、断连和取消恢复边界测试后，最新同范围 105 项通过；覆盖来源快照、个人版入口、精确 app 路由、持久 outbox 与 ACK；Feishu API 只由 SDK 模型 / 假响应验证 | 尚未做真实飞书应用 / 群端到端投递；PA-07 群执行、PA-08 文件与副作用工具协作发送未覆盖 |
| 2026-10-20 / 明确 @ 当前 Bot 映射 | Python 3.12.12；真实 SQLite JM-02/03/07、core 执行入口替身、Feishu SDK 事件与 bot 查询为替身 | `uv run pytest -W 'ignore:invalid escape sequence:SyntaxWarning' --no-cov -q -o log_cli=false --tb=short tests/unit_tests/gateway/test_persistent_group_fanout.py tests/unit_tests/server/jiuwenmate/test_feishu_group_production_wiring.py -k 'receiver_bot_mention or persistent_route_resolves_bot_open_id or mention_of_another_bot or payload_round_trip or bound_attachment_event'`；Ruff `--ignore E402,F821,F841` 检查改动 Python 文件 | 5 项 @ 映射 / 归档隔离相关测试通过；Ruff 检查通过。两测试文件全量运行 61 项通过、1 项失败：既有用例期待未发布同事返回 `REJECTED`，当前实现返回 `HANDLED`，本次未改该无关处理语义 | 未调用真实 Feishu API 或测试租户；未验证应用启动配置持久化到实际用户目录；全量测试中上述既有断言差异仍待单独处理 |

### 8.3 验证结论

- 已验证：生产绑定按所有者本地配置读取，`app_id → persistent_agent_id → 运行时 agent_id`；旧键 `agent_id` 不再作为绑定配置读取。未绑定通道不发 E2A，绑定通道通过内部 E2A 在 AgentServer 复核配置并独占处理；群 / 私聊字段、空 workspace scope、真实 SQLite 请求幂等、AgentServer 分发和 JM-07 条件启停均有测试。个人版消息 / 附件回归与 JM-03/07 聚焦回归包含在 142 项通过结果中；2026-10-19 一对一绑定保存、配置解析及同事状态专项 3 项通过。
- 尚未证明：真实飞书测试应用 / 群的入站与文本出站、自身 Bot 身份字段在目标租户 SDK 事件中的实际形态、飞书 SDK 失败重投及幂等语义、PA-07 群执行。AgentServer 导入链使用本地 agent-core 时，`playwright_runtime/probes.py` 的无效转义告警会被 pytest 的 warning-as-error 策略提升，因此本地联合测试命令显式忽略该 `SyntaxWarning`；不属于 JM-08 代码改动。
- 结论：JM-08 的配置解析、E2A 生产入站、JM-07 生命周期接线及供 JM-09 使用的入站路由快照均有实现和自动化覆盖；仍需在真实飞书测试应用 / 群完成联合验收后再标为完成。群接入仍按 Bot 被加入群并向其投递经校验事件即允许，不新增群白名单或工作空间权限。PA-07 未就绪前不开放真实群执行。

## 9. 风险与已知遗留

| 风险 / 遗留 | 影响 | 处理要求 |
| --- | --- | --- |
| `GroupChatRepository.resolve` 不是接入事实校验 | 可能把首次出现的群映射误当作 Bot 已加入群 | 先校验 Feishu 事件和唯一绑定，再建立群映射；群映射本身不得作为 Bot 在群的证据 |
| 个人版在通用回调前过滤 Bot 消息 | 新版无法收到其他 Agent 发言；全局移除过滤又会影响个人版 | 只对新版有效绑定开路径，并增加旧路由回归测试 |
| 配置中 `app_id` 重复、绑定漂移或 Agent 未发布 | 消息可能路由到错误同事或落入个人版路径 | AgentServer 再按本地配置解析；重复、漂移或无效 Agent 均拒绝，不回落个人版 |
| Scheduler 排队上限先于幂等查询 | 队列满时有效重复事件返回容量错误 | JM-07 核对并调整受理顺序，JM-08 加回归测试 |
| 当前事件回调及内存去重不构成持久 Inbox | 进程在事件处理与 JM-03 保存间失败时，是否重投取决于平台 SDK | 真机验证重投语义；在没有持久 Inbox 前不承诺无损接收 |
| 尚未完成真实飞书联合验收 | 测试应用的事件字段、自身 Bot 回声和 SDK 重投语义可能与样本不同 | 用真实测试应用和群验证接收 Bot、发送者身份及失败重投；不宣称已具备无损 Inbox |
| PA-07 尚未就绪，或 JM-09 真实飞书回执未联调 | Request 可受理但暂不能验收完整群协作闭环 | 将消息受理、执行能力和回复投递分开验收；文本出站的自动化测试不替代真实平台验收 |

## 10. 设计演进与修订

| 日期 | 变更 | 依据 |
| --- | --- | --- |
| 2026-10-14 | 初稿；明确接收 Bot 身份路由、逐 Agent 幂等、新旧入口互斥；记录 Bot 消息旧过滤、群授权与 Scheduler 容量检查等实现接缝 | 最新开发计划 JM-08、JM-07 与当前飞书 / Request / Scheduler 代码 |
| 2026-10-14 | 确认群接入许可采用旧版规则：绑定 Bot 被加入群即允许其 Agent 接收群消息；不新增群白名单，且不将群接入许可扩展为文件 / 工具授权 | 用户确认；参考旧版 Persistent Agent 的 Feishu Bot 群成员检查与入站路由 |
| 2026-10-14 | 实现首轮可注入 JM-08 分发组件和飞书个人版入口前的可选互斥分流；使用宿主提供的绑定 / 授权端口，不新增生产存储或生命周期接线；增加同 Agent 并发重投覆盖，记录 25 项 JM-08 测试和 131 项聚焦回归通过 | 本地 `jiuwenmate` 工作区实现与测试；整体验收、生产装配及真机联调仍待完成 |
| 2026-10-14 | 修正文档边界：JM-08 只按接收 Bot 的有效绑定和飞书实际投递决定群消息准入，不开发工作空间权限；明确当前代码没有生产绑定源、Gateway 到 AgentServer 的 JM 请求入口或 JM-07 生产装配 | 用户确认的特性范围及对当前启动链、Feishu 配置和 JM 代码的检查 |
| 2026-10-14 | 按确认的 `app_id → agent_id` 一对一配置完成生产解析、Gateway E2A、AgentServer 事件分发及 JM-07 条件启停；个人版未绑定通道保持原路径；142 项聚焦回归通过 | JM-08 实现与自动化验证；真实 Feishu 群联调仍待完成 |
| 2026-10-15 | 为 JM-09 在 Request 受理时持久化 Feishu `app_id`、`chat_id`、来源 `message_id` 与 `reply_to`；明确该快照属于 JM-08 的入站事实，outbox、出站 ACK 与文本发送由 F09/JM-09 负责；个人版路径不变 | JM-09 方案细化与当前 Request / E2A 代码；JM-08/JM-09 聚焦自动化验证 |
| 2026-10-19 | 飞书应用配置直接以 `app_id` + `persistent_agent_id` 表达一对一绑定；同一同事改绑时替换旧应用项，不另存绑定表或绑定 ID；解析后映射为运行时 `agent_id` | 用户确认；创建弹窗保存凭据与绑定，同事列表从配置恢复状态；重复绑定失败关闭；3 项定向测试、前端相关测试及生产构建通过，真实飞书入站仍待复测 |
| 2026-10-20 | 修复明确 @ 当前接收 Bot 的身份映射：在 JM-08 入站回调前自动解析/加载 Bot `open_id`，将命中的 Feishu `open_id` 映射为当前绑定 Agent 的内部 ID，仅追加到本次 Request 来源；公共群归档保持平台原始 @ 标识 | Feishu `app_id` / `app_secret` 查询 Bot 信息；PA-07 明确 @ 的确定性 `respond` 契约；5 项定向测试通过。两测试文件全量 61 项通过，1 项未发布同事处置预期差异未在本次修改 |
| 2026-10-21 | 群消息在受理前增加 JM-23 回复模式。被动且未被明确 @ 时不创建 Request；纯寒暄只受理一位自适应员工。第 5.4 节第 5 步改按该规则描述 | [F_23](F_23_group-reply-mode.md)；真实飞书仍待验收 |

生产装配已接入；待使用真实 Feishu 测试应用和测试群确认事件身份字段、SDK 重投行为及 JM-09 文本回执。JM-08 不新增工作空间权限或其策略，也不伪造执行上下文；PA-07 群执行、PA-08 文件投递和 PA-15 中途协作不属于 JM-08 闭环。
