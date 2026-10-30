# 群公共记录规约

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | S_01 / group-public-records |
| 关联模块 | `jiuwenswarm.server.jiuwenmate` 群归档、访问关系与 `ports.group_chat` |
| 最近一次修订日期 | 2026-10-30 |
| 关联 feature | [F_11](../features/F_11_group-public-records.md)、[F_21](../features/F_21_cross-group-search.md)、[F_21_03](../features/F_21_03_group-sharing-approval.md) |

本规约维护 JM-11 已实现的宿主契约；验证证据、未完成的生产验证及交付状态见 F_11 和开发计划。

## 1. 范围、术语与适用条件

本模块负责 JiuwenMate 飞书与钉钉群消息的公共归档、Agent—群关系、当前群读取和频道记录展示，以及 PA-23 跨群搜索的宿主实现。钉钉目前只接入公共记录展示，未完成跨群查询所需的实时成员核验。agent-core 负责 PA-07 参与判断、PA-23 工具和窗口背景合并；搜索 DTO 与公共 `GroupChatPort` 签名以 agent-core 的 `specs/S_09_group-chat-and-knowledge.md` 为准。

仅绑定 JiuwenMate Agent 的飞书或钉钉渠道接入此链。个人版和集群模式不订阅新增生命周期事件，不使用本模块端口。钉钉记录只代表机器人回调实际送达并被保存的消息；当前回调不构成完整群历史来源，读取状态必须标记为不完整，且不提供历史回拉承诺。JM-21 跨群子范围已接入宿主执行链并通过本地 SQLite、真实 core 与模型替身验证；真实平台成员核验及联调尚未完成。

## 2. 数据结构与事实归属

存储根目录复用宿主配置，默认 `.jiuwenswarm/jiuwenmate/`。

| 事实 | 权威位置与修改入口 |
| --- | --- |
| 群身份、群名 | `agents.sqlite3` 中 `group_chats`、`group_details`；`GroupChatRepository` |
| Agent 访问关系 | 同库 `agent_group_access`；可信入站事实和成员事件更新 |
| 公共消息与撤回 | `group_messages/<group_id>/messages.sqlite3`；`ConversationArchive` |
| 附件引用 | `GroupMessage.attachment_refs`；保存平台来源和已确认投递文件名，不下载文件 |
| 已发送消息的归档进度 | `agents.sqlite3` 中 `group_delivery_archives`；投递服务与归档回调 |
| 跨群搜索游标 | `agents.sqlite3` 中 `group_search_cursors`；`GroupSearchCursorRepository`，保存 token 摘要和查询／授权／归档分页元数据，不保存正文或文件路径 |

关系键是 namespace、Agent、内部群 ID、app ID，保存 `status`、`visible_from`、`verified_at`、`source_ref`、`revision`。`status` 为 `active/left/unknown`。首次可信入站建立保守下界，不代表实际入群时间；可信重新入群更新下界，不自动恢复先前范围。

内部群 ID 在同一数据根目录全局唯一，取安全 ASCII 标识。目录不含 namespace 层，但群库 `archive_info` 保留 namespace、内部群与平台群身份和 schema 版本，打开时必须一致。群库消息 ID 唯一，首次写入分配固定递增 `ingest_seq`；正文 DTO、平台修订时间和来源引用一并保存，UTC 微秒时间用于可见范围过滤。

## 3. 不变量

1. **I-1 公共去重：** 同群同消息 ID 多 Bot 并发归档只保存一条；各 Agent 的 Request 独立受理。
2. **I-2 归档前置：** 公共消息提交成功后才唤醒 Request；自身 Bot 回声归档但不创建自身 Request。
3. **I-3 即时授权：** 每次当前群读取前后均检查 Session 归属、当前访问关系和实际 Bot 成员状态；未知、退群、解绑、冲突均拒绝。提问人是否属于目标群不构成此 Agent 授权条件。
4. **I-4 不复活正文：** 撤回先于原消息也建立永久 tombstone；重投不能恢复正文与附件引用。编辑保留扫描序号和原始发送身份、时间。
5. **I-5 不重发：** 发送成功后归档失败只补归档，不因该失败重新发送或重跑 Agent。
6. **I-6 不伪装空库：** 缺失、损坏或身份不符的归档拒绝读取，不创建空库并返回 `empty`。
7. **I-7 跨群先授权：** 每次跨群搜索均从可信 Execution 取得 namespace 与 Agent，核验该 Agent 当前每个候选 Bot 群成员状态后，才解析群名并打开被选中的群库；请求者当前所在群不授予目标群访问权。
8. **I-8 游标不是授权：** 每次续页都重新核验当前 Agent—群授权，并核对游标绑定的查询、访问关系及归档正文修订；失效时不得返回旧正文或完整空结果。

## 4. 接口契约

### 4.1 写入与消息状态

`ConversationArchive.append` 使用同步 SQLite 写事务提交。相同内容重复写入幂等；正文不同且无更新的平台修订依据时返回冲突。更新修订不得改变消息原始时间与发送者。`recall` 保存 tombstone 并去掉正文、附件；重复撤回不反复改变内容修订号。

归档与 Request 受理不构成跨库事务。归档成功后受理失败，依赖事件重投补受理；无法证明采集连续时须记录缺口，不能据此宣称拥有平台全量历史。

受理前重读触发消息；已撤回或不可读时不创建 Request。已受理但尚未执行的群任务，由 ExecutionDriver 在进入 core 前经宿主端口重新核验触发消息的授权与当前正文；撤回／不可核实则按执行失败结束，不把旧输入交给模型，编辑后采用当前正文与附件引用。该检查不承诺撤销已经开始的模型调用或擦除既有窗口内容。

### 4.2 当前群读取

`HostGroupChatPort.recent_messages(execution, cursor=None, limit=20)` 异步返回 `GroupMessagesResult`。端口绑定 namespace 与 Agent，群只能从该执行的 Session 解析，模型不能传入其他群。limit 为 1～500 的整数，不接受布尔值。

无 cursor 时取授权下界之后最近 limit 条，并按入库顺序返回。有消息 ID cursor 时从其后向前推进扫描，支持早于 Session 已存游标的触发消息。首轮固定上界，后续页不夹入新增消息。分页 token 通过群库密钥签名，绑定 namespace、群、Agent、可见下界、位置、上界和内容修订；不可跨 Agent 复用。编辑、撤回造成内容变化会令旧 token 失效；无内容变化的刷新及重复撤回不令其失效。

`scanned_through` 只能是实际扫描到的消息 ID，不是分页 token。未知、过期、越界游标或已知采集缺口返回 `incomplete`，不得报告完整空结果。缺失／损坏库抛出宿主错误，由 core 按读取失败处理。

生产读取会查询平台当前消息状态：撤回不返回正文，无法核实的消息返回 `status=unavailable`、空正文与空附件引用；成功读取采用最新正文。刷新后再次核对授权，并重读已选消息的当前归档状态，防止期间撤回返回旧正文。`queued_request` 每次从当前 Agent 的 Session 排队事实派生，不持久化为全体 Agent 共用标记。

### 4.3 内部平台核验协议

AgentServer 经已连接 Gateway 发送 `jiuwenmate.group.inspect` 推送，携带 `query_id`、`app_id`、`chat_id`，以及可选 `message_ids`。Gateway 只选择唯一对应 app 的 JiuwenMate 飞书渠道；消息 ID 最多 500 个，每个长度 1～512。

Gateway 使用当前渠道凭据查询，并以内部 E2A 方法 `jiuwenmate.group.response` 回传：无 `message_ids` 时实时核验 Bot 成员资格，返回 `active/left/unknown`，active 可附群名；有消息 ID 时只读取指定消息，返回 `status=ok` 与逐条消息状态，`ok` 不表示成员授权成功。消息刷新必须位于宿主读取前后两次实时授权核验之间。AgentServer 只接收与待查询 query ID、app、chat 及查询类型一致的响应；消息结果不得用于放行成员授权。群名仅成功获取后缓存 300 秒，每渠道最多保留 256 群，重启清空；成员资格不缓存，过期名称会在下次核验成功时刷新。Gateway 超时 8 秒，服务端等待 10 秒；断连、异常、格式不明返回 `unknown`，不是授权成功。此方法不开放给 Web 通用转发。

成员事实为 `active/left/unknown`。退群立即落库；相同时间戳冲突不得把已撤权关系恢复为 active。历史普通消息不能重新激活已退群关系。重新入群须有更新可信成员事实。

新 Bot 的可信入站到达时，宿主核对同 Agent、同群的旧 app 当前配置；只有确定已不再绑定该 Agent 的旧关系才标记为带 `binding:` 来源的 unknown。当前绑定重新收到消息时，只允许这种“因解绑退休”的关系恢复并重置可见下界，不允许普通消息恢复物理退群关系。配置同时保留多个 active app 时仍拒绝歧义，不按最后一条消息擅自选择。关系选择优先唯一 active，不被已退休关系的更新时间遮蔽。

### 4.4 单群查询与附件引用

`ConversationSearch.search_group` 是宿主内部底层接口，不是面向模型的权限入口；调用方必须先确定授权群及 `visible_from`。关键词为 Unicode `casefold` 字面子串，起止时间均包含，发送者精确匹配，按消息时间倒序并以消息 ID 稳定排序。跨群授权范围、全局合并和分页契约见 §4.5。

读取仅保留与该消息来源匹配的 `feishu:attachment:<message_id>:` 引用。撤回、不可核实状态不返回附件入口。`attachments/` 目录存在不表示文件已经下载，群库和关系库也不是 Agent 普通文件工具的可读工作空间。

### 4.5 PA-23 跨群搜索

`HostGroupChatPort.search_messages(execution, query)` 实现底层授权搜索；`ExecutionDriver` 将其包装为 `GuardedGroupChatPort` 后注入 `web_direct`、`im_direct` 或 `group` Session；Scheduled Execution 不注入。底层读取主体是可信 Execution 的 namespace 和 Agent。提问者是否属于来源群不参与底层读取授权，但向当前群之外的接收方分享须独立满足 §4.6 所有者审批。

入口只接受 agent-core 导出的 `GroupMessageQuery`，结果使用 `GroupMessageSearchResult`、`GroupMessageSearchHit`。`query` 支持单个关键词，`keywords` 支持一次最多 16 个关键词；二者按 OR 语义匹配、重复命中去重，使一次批量查询只生成一个查询快照，审批仍按实际命中的来源群拆分。群 ID 与群名过滤各自在集合内取并集，同时提供二者时取交集；都未提供时搜索该 Agent 本次核验通过的全部群。指定群名按 `group_details` 当前已存名称精确匹配，不做模糊或历史别名匹配，也不以 group_id 伪造缺失名称。授权集合内名称重名时，结果只提示可授权的稳定 group_id；无法命中、目标不在授权范围或游标不属于当前请求时，返回不泄露目标存在性的 `unavailable`。

每次调用首先枚举该 Agent 的 active 访问关系，并逐一核实唯一 app 绑定和 Bot 当前群成员状态；状态为 `left` 的关系不进入搜索集合，当前候选状态未知、关系歧义或核验异常安全失败。核验和目标解析完成前不得打开任何群归档。指定 group_id 对应未解决的 `unknown` 关系时返回 `unavailable`；无群过滤的全范围搜索若存在未解决关系也返回 `unavailable`，不能把未知授权集合报告为完整 `empty`。来源为 `binding:` 的 `unknown` 是已确认退休的旧 Bot 绑定，不视为当前候选授权。群名过滤只在经核验的 active 集合内解析，不读取未授权或状态未知群的名称。各群查询限制在访问关系的 `visible_from` 与首次快照 `upper_ingest_seq` 内，关键词按 Unicode `casefold` 字面匹配；全局顺序为消息接收时间倒序、group_id 升序、message_id 升序。

需要续页时，宿主发放 15 分钟有效的不透明随机游标。`group_search_cursors` 只保存游标 SHA-256 摘要、namespace、Agent、规范化查询摘要及授权／归档快照、上界和最后排序键，不保存正文或物理路径。续页必须使用相同查询并重新核验全部目标授权；授权修订、可见下界或群名变化，归档内容修订／撤回，游标失效或过期均拒绝旧页。首轮上界之后新增的消息不进入该游标结果。

仅当目标范围完整读取且没有已知采集缺口时，才能返回 `found` 或 `empty` 并令 `query_complete=true`。有下一页时返回 `incomplete` 和 next_cursor；存在部分可读目标、已知缺口、读取中变化或无法取得下一页时返回无 cursor 的 `incomplete`；没有任何可读取目标归档时返回 `failed`。授权未知或查询目标不可用返回 `unavailable`，执行身份不匹配返回 `forbidden`。搜索结果不代表平台全量历史；钉钉不完整回调带来的缺口会保持 `incomplete`，JM-11 §6 的归档限制继续适用。

### 4.6 所有者跨群分享审批

当前群读取维持既有规则；搜索其他群并命中消息时，宿主按实际命中的来源群拆分冻结结果，在候选进入请求模型前分别发到配置所有者 Web 私聊 `sharing-approvals`。无命中的群不生成空审批卡。原渠道只接收通用等待提示，卡片预览不进入请求模型或原群。

`group_share_approvals` 和 `group_share_grants` 保存在宿主 agents.sqlite3，`GroupShareRepository` 为权威；Agent 可写表格、普通工具权限及 full_access 均不能代替分享授权。所有者由当前个人版可信 Web 身份 local 确认；namespace、配置、运行 Agent、提问者和目标来自宿主 Request，不接受模型改写。IM 运行 Agent 通过 connection_of 找回配置所有者。

一次批准覆盖原 Request、完整规范化查询（含 limit 和分页）及冻结候选，不补入新增内容。永久授权按 namespace／配置／运行 Agent／来源群／目标渠道、app、类型和稳定 ID 保存，所有者可撤销；群名不充当授权键。每次仍须通过平台访问、可见下界与来源状态检查。待审批 15 分钟过期；一次决策幂等，同一查询可绑定同一原生暂停的多个工具调用。

RPC 为 `jiuwenmate.sharing.list {agent_id}`、`.decide {agent_id, approval_id, decision: once|permanent|reject}`、`.revoke {agent_id, grant_id}`。agent_id 是配置 ID，统一返回 `{approvals,grants,conversation_id:'sharing-approvals'}`；query 为完整 GroupMessageQuery 对象，正文允许 null。每条审批记录必须只含一个实际命中消息的来源群和一个目标会话；一次搜索命中多个来源群时拆成多条独立审批，分别决策后才恢复原工具调用。重跑搜索时按原查询读取首次冻结的来源群和消息集合，不将新命中加入原审批。卡片展示来源／接收方／请求者／查询、有限正文摘要和完整可展开候选。未绑定原生现场或来源群数量不为一的记录不可批准。

Core 以 GroupMessageApprovalRequired 发出安全中断，事件携带可选 approval_ref 供宿主精确关联。业务 rail 与工具使用稳定 native owner/kind，保存前轮 carried_answers；只消费自己的确认。所有者决定后恢复原 Request／Spec，原 IM 及普通工具审批接口不得自批。拒绝、超时或来源变化均安全结束或返回不可用，不静默放行。

审批操作入口与执行归属分开：Web 运行管理器只恢复 Web 请求，IM 运行管理器只恢复其渠道请求。Web 对 IM 请求仅持久化所有者决定，由 IM 的审批恢复循环继续原 Request，不能使用缺少平台校验能力的 Web driver 代跑。IM 暂未启动时保留已决记录，等待原渠道恢复；普通工具审批、超时、启动恢复也遵守同一渠道归属。

所有者卡片的提问者标签可按原 Request 的目标群及接收 app 查询可见群成员姓名，仅修饰返回视图，不修改 principal、冻结 scope 或批准范围。查不到姓名时返回空标签，由前端显示本地化占位；不能把 open_id 当作姓名。已有卡片的后台轮询不反复切换初次加载状态，须保留展开内容与焦点。

读取前后核对来源集合，拒绝未列来源命中；恢复前验证冻结消息仍有效。投递前复查来源与永久授权。失效时先持久化实际安全提示，清除文件和提及；ACK、重试、公开归档和 WeLink job_json 恢复不能带回原候选或接力目标。保留投递渠道认领隔离，unknown 不盲目重发。

恢复预检或执行流失败时，先持久化本请求失败，再由 Core 的 discard_persistent_confirmation 校验身份与 Spec、结清本请求等待现场，最后消费已决审批并放行 Session；重启补齐此顺序。宿主不解码或删除整个 Core 窗口。

原生审批创建失败时，根据最后一次执行的持久化 confirmation pause 补建，启动和运行中定期重试；已消费或被新执行覆盖的旧暂停不重放。补建保留原暂停时间与到期时间；尚无原生行的有效暂停也阻止同 Session 新任务，两个状态查询使用同一 SQLite 读快照。冻结分享先过期时仍可绑定原中断以结清拒绝，不重新授予期限。

本契约保护群搜索及对应请求投递，不擦除已披露上下文，不提供同 OS 用户无限制 shell 隔离或后续任务的全量文件溯源。真实平台与浏览器验收状态见 F_21_03。

## 5. 生命周期与部分成功

已确认 Feishu / DingTalk 发送 ACK 先持久化，再以实际平台消息 ID 和发送正文归档。归档标记仅在群库提交后写入；中间失败留下可重试项，已有投递扫描在运行和重启后继续补归档。没有发送 ACK 的模型候选不进入公共群历史。

补归档按 `updated_at, delivery_id` 稳定分页；本进程保留扫描位置，到尾后重新扫描。失败项保留，但不会令下一轮总停在最早一批而阻断其他群；重启可从头扫描，进度丢失只导致幂等重读，不导致重发。

删除 Agent 删除其访问关系，但保留共享群消息库。每个群最多保留最近 5000 条消息，更早的在写入或读取时删除；部署升级前已经清理的旧消息无法恢复。Web 群记录按每页 100 条读取，使用签名游标向较早记录翻页。关闭能力不删除整个归档。每次数据库访问独立开启与释放连接，不跨调用持有 SQLite 事务。

## 6. 限制与兼容

不回拉接入前的完整历史，不下载附件，不撤销已经进入模型窗口的派生内容，不承诺跨库原子提交或平台事件无损。现有跨 namespace 群 ID 冲突须报错处理，不自动改写 Session 引用。飞书成员与消息状态、断连重投行为仍须真实联调；钉钉全量群历史和实时成员核验未实现。

本模块不替代宿主 PA-18 容量配置与可靠计数支持；正式执行需满足 core 最新装配条件。生产能力不能由端口构造成功或模型替身测试推导。

## 7. 关联与验证索引

- [F_11](../features/F_11_group-public-records.md)：设计决策、实现影响和验证证据；[开发计划 JM-11](../designs/jiuwenmate-development-plan.md#jm-11-群公共记录与搜索) 维护唯一交付状态。
- [F_21](../features/F_21_cross-group-search.md)：跨群搜索实现选择、验证与遗留；开发计划 JM-21 维护该扩展特性的整项状态。
- [F_08](../features/F_08_feishu-group-fanout.md)、[F_09](../features/F_09_web-and-feishu-result-delivery.md)：入站绑定与出站 ACK 的原有契约。
- `tests/unit_tests/server/jiuwenmate/test_group_chat_port.py`：I-1、I-3、I-4、I-6 与分页、字面查询。
- `test_feishu_group_production_wiring.py`、Gateway `test_persistent_group_fanout.py`：平台协议、生命周期与 I-2。
- `test_commit_port.py`：I-5 的真实 SQLite 归档重试。
- `tests/integration_tests/jiuwenmate/test_group_records_real_core.py`：真实 core 参与判断及执行窗口；模型为替身，不证明真实平台可用。
- `tests/unit_tests/server/jiuwenmate/test_cross_group_search.py`、`test_session_scheduler.py` 和 `tests/integration_tests/jiuwenmate/test_cross_group_search_real_core.py`：跨群授权／解析／分页、执行链注入与真实 PA-23 工具调用；不证明真实飞书成员查询或其他 IM 平台已联调。
