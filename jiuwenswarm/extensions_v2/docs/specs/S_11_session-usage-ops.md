# 会话轮次与用量查询规约

| 项 | 值 |
| --- | --- |
| 编号 | S_11 / session-usage-ops |
| 最近修订 | 2026-10-28 |
| 关联 feature | [F_21](../features/F_21_session-usage-ops.md) |

## 1. 范围与条件

PR05 两个无业务写入的 BUILTIN Ops，由现有 `ops.build` 的 session BUILTIN 登记。用户在宿主已绑定有效 SessionContext 的 Handler 调用，查询目标只取 `ops.session_context.session_id`，不接受会话 ID 或路径参数。`ops.session_context` 是 Core 的只读属性，不是本项新增的异步方法。包身份不改变统计范围。

不依赖 PR04 或用量通知触发，不新增查询事件和查询 CORE。功能、注册示例、完整链路和版本化验证见 [F_21](../features/F_21_session-usage-ops.md)。本次按编写指南补齐接口说明，不改变实现行为。

## 2. 接口

### 2.1 await ops.session.turns() -> int | None

统计当前主会话已提交用户输入记录的不同 request_id 数；旧记录无 request_id 时按 id，二者均无时逐条计数。不统计 assistant/tool/system、子 Agent、跨会话消息、扩展追加、压缩摘要、已有心跳标记、cron/heartbeat 渠道和带任意非空 source 的记录；当前平台将 source 用作系统来源，不给普通用户历史行设置该顶层字段，无来源标记的旧 user/human 记录按历史用户记录解释。存在但无输入为 0，未知或已删除会话为 None。读取反映当前保留的历史分支，非生命周期累计计数，也不是 AgentLoop turn_id 或 turn.start 的序号。

方法没有参数，直接在线程中读取平台已提交主历史，不触发事件，不解包事件输出。适用于显示用户输入次数或审计统计；模型重试、工具调用和流片段不会各算一次用户输入。精确排除判据包括 role 非 user/human、subagent_id、extension_message_id、跨会话 message_origin、feedback_only、is_compact_summary、mode=subagent、cron/heartbeat 渠道、非空 source 和 metadata.automation.kind=heartbeat。

### 2.2 await ops.session.usage(breakdown: bool = False) -> dict | None

breakdown 默认 False，只接受 bool，字符串/数字不作替代。True 保留已存在的 context.parts，False 移除该字段；不重新计算类别、不调用模型或新事件，也不做 `{value}` 解包。适用于读取已观测窗口与最近回合消耗，不能当作实时重新测量或累计账单。

| 顶层字段 | 类型与含义 |
| --- | --- |
| schema_version | str，固定 session-usage.v1 |
| session_id | str，当前可信目标会话 |
| source | str，host 表示本次绑定宿主快照，history 表示本次读取已提交历史 |
| context | dict 或 None，已观测主模型窗口的公开投影 |
| turn | dict 或 None，最近可用 provider 回合汇总的公开投影 |

context 与 turn 可以独立缺失、时点和 request_id 不同；二者都缺失时整个方法返回 None，不返回全空对象。

| context 字段 | 含义 |
| --- | --- |
| request_id / phase / timestamp / sequence | 对应模型观测的请求、阶段、源时间和序列，不改写为查询时刻 |
| context_window.limit_tokens / input_tokens | 窗口容量与输入数值，可为 None；保留 Core 的模型语义 |
| context_window.occupancy_rate | 比例值，如 0.2 表示 20%，不是数值 20；不额外承诺取值校验到 1 |
| context_window.tokens_source | 输入总量来源，例如 provider_usage；未知沿用 Core 的 not_reported |
| context_window.local_estimated_input_tokens / reconciliation_delta_tokens | 本地估算与对账差值；后者可为负或 None，不能据此累加计费 |
| context_window.context_snapshot_id | Core 窗口关联标识，可为 None |
| measurement.category_source / total_source / tokenizer | 类别/总量的测量来源与 tokenizer 名称，保留来源语义 |
| measurement.estimated / authoritative_total | 类别/测量估算标记与总量是否为权威来源；分项估算和 provider 总量可同时存在 |
| measurement.measurement_version / context_snapshot_id | Core 测量版本和关联快照标识 |
| parts | 仅 breakdown=True 时保留的类别字典，字段见下表；不承诺每类都存在 |

| parts 每项字段 | 含义 |
| --- | --- |
| category / tokens | 类别和计数 |
| percentage_of_window / percentage_of_input | 窗口/输入比例，未知可为 None |
| estimated / tokenizer | 本项是否估算与所用 tokenizer |
| carrier / source / measurement_source | 承载位置和来源说明 |
| fallback_reason / fallback_tokenizer_model | 回退原因与模型信息，可为 None |
| order / stable_id | Core 保留的排序与稳定标识，可为 None |

| turn 字段 | 含义 |
| --- | --- |
| request_id / timestamp | 源汇总的关联请求与时间字段，旧历史可缺失，不补成查询时点 |
| source | 固定 provider_usage |
| input_tokens / output_tokens / total_tokens / cache_tokens | int、float 或 None；保留汇总值，不重算算式；缺失不补 0，携带的 0 保持 0 |

通知与原生 summary 的 cache_tokens 字段按同一存在规则：input_tokens>0 才携带，未携带时投影为 None。窗口、measurement 和 parts 的默认字段沿用固定 Core 结构化模型，例如 local_estimated_input_tokens 和 parts.tokens 默认 0；不能仅凭这些零值推断 provider 已实测，应结合来源与 estimated。

### 2.3 数据选择、复制与一致性

宿主 SessionContext.metadata **包含** session_usage_info 键时，返回该绑定快照的公开副本；明确值为 None 时也直接返回 None，不回读上一轮。未包含该键才在线程中读取目标主历史，反向选择最新可用 context.usage 与 chat.usage_summary。Event payload 数字不是 Ops 的数据来源。宿主快照类型/会话身份不符报 ValueError。

宿主 context 与 turn 独立校验：单个分量校验失败置 None，保留另一个有效分量；两者都不可用返回 None。历史非法 JSON、非对象记录或进入结构化校验的数据无效时仍报错；不可识别的缺失观测保持缺失语义，不伪造数据。历史用量排除外会话、子 Agent、非零 depth、父调用、成员和 Team 标记的记录。

返回值为独立副本，不返回文本、凭据、模型连接或其他会话数据，修改返回值不回写平台。turns 与 usage 是分别读取的方法，不保证组合调用取得同一个原子快照；同一宿主快照也只表示捕获时的观测。

## 3. 不变量与失败

- SU-01：无 SessionContext 抛 RuntimeError；非法会话 ID 抛 ValueError；路径越界拒绝。
- SU-02：未知会话和缺失事实不创建目录、不激活 Agent、不调用模型、不触发新事件。
- SU-03：历史读取在线程内等待至多 10 秒的队列边界；超时、读取失败或结构损坏抛 SessionStatisticsError，不能返回伪造 0。
- SU-04：主历史与原写锁串行读取；两会话隔离，无统计缓存。取消不撤销原历史队列，但查询无业务写入。
- SU-05：通知参数只由平台提供并复制为快照；Event payload 不是 Ops 的数据来源。平台负责可信 SessionContext，用户不修改平台 Adapter。
- SU-06：宿主上下文与历史用量可来自不同测量时刻，保留 request_id/phase，不将旧窗口描述为实时值；不重复计数模型调用或流片段。

| 异常 | 触发条件与调用方处理 |
| --- | --- |
| RuntimeError | 未绑定 SessionContext，选择平台已有的会话绑定事件 |
| ValueError | 会话 ID 非法、宿主快照类型或目标不匹配，不能传其他会话的资料 |
| TypeError | breakdown 不是严格 bool |
| SessionStatisticsError | 历史读取、格式、结构化用量或队列边界失败；按异常类型处理，本异常没有 code 字段 |
| CancelledError | 查询等待中取消；原队列和已启动的线程读不会被回滚，本查询没有业务写入 |

未捕获的方法异常仍受外层 USER Handler 故障隔离规则约束，外层继续不表示查询成功。业务读数错误不能以伪造的 0 代替。无统计缓存或第二份统计持久化文件；卸载包只撤销其 Handler，不删除平台事实或查询能力。

## 4. 验证入口与限制

测试和实际执行证据由 [F_21 第 6 节](../features/F_21_session-usage-ops.md#6-验收与未决问题) 维护。历史查询扫描 O(N)，宿主查询读当前副本；本项不提供实时重新测量、Team 汇总或累计计费。删除与显式截断依现有生命周期及历史事实生效；旧 JSON/JSONL 沿原读取路径使用，不新建统计存储。

## 5. 复用通知的接口与边界

既有 `context.usage.updated` 是本项的示例入口，不是查询事件。`ContextUsageUpdate` 字段如下，定义仍在 [context_usage.py](../../event_types/context_usage.py)：

| 字段 | 已有类型、默认值与用途 |
| --- | --- |
| session_id / request_id | 必填 str，会话/请求关联 |
| input_tokens / output_tokens / total_tokens / cache_tokens | int，默认 0，回合汇总计数 |
| cache_hit_rate | str 或 None，默认 None，已有百分比文本 |
| context_window_tokens | int 或 None，默认 None，平台取得的窗口容量 |
| usage_percent | float 或 None，默认 None，已有平台百分值字段，不与 Ops occupancy_rate 比例值混用 |
| model | str 或 None，默认 None，平台模型名称 |

原生流式请求末尾、total_tokens>0 时调用 notify_context_usage；没有 Bus/运行事件循环时不发布。notify 捕获本次可信资料和独立校验的用量副本，调度异步 `_dispatch`，实际 `bus.trigger` 同时传 Event 与 SessionContext。用户不负责从 Adapter 收集资料。其他入口若已有有效会话绑定也能查询；新增尚未绑定上下文的平台入口需由该入口维护者接线。

事件 dataclass 为 frozen；默认 CORE 仅检查输入类型，返回 `ContextUsageOutput(accepted=True, request_id=...)`，accepted 必填 bool，request_id 默认空字符串。本项未新增该事件的完整数值/输出校验或拒绝协议。示例保持输入并返回 next 的回执；改写事件不覆盖 Ops 元数据，短路也不能撤销已完成的业务。USER 异常按 Core 故障隔离，通知任务异常由遥测适配层捕获记录。

matcher 按原输入的字段相等条件合取，空字典匹配全部，不在改写后重新匹配。用户通过 `registrar.register_event_handler("context.usage.updated", handler, {}, "user")` 接入；注册、包文件和完整调用链见 [F_21](../features/F_21_session-usage-ops.md)。没有新 Rail 或 Front/Supervisor 查询协议。

## 6. Claude 对照依据

对照锁定官方 [claude-code.d.ts](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)，文件头标记 2.1.277。turns 语义相近但本项明确保留历史/过滤/None 规则且无同名查询 Hook。usage 不采用其 full/summary breakdown 参数，不提供 columns、rateLimits 或 cost；只读已有统计而不进行额外计算。现有通知也不等同其 session.measure 的测量变化语义。完整比较见 [F_21 第 2 节](../features/F_21_session-usage-ops.md#2-能力说明与-claude-对照)。
