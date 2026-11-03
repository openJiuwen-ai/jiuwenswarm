# session.compact / ops.session.compact 规约

| 项 | 值 |
| --- | --- |
| 编号 | S_15 |
| 最近修订 | 2026-11-03 |
| 关联特性 | [F_28_session-compact.md](../features/F_28_session-compact.md) |
| 计划 | EXTV2-26；依赖新的 Core 压缩 Hook 与 Extension Runtime 调用校验 |

## 1. 范围与职责

实现位置：[session_compaction/dispatch.py](../../session_compaction/dispatch.py) 负责事件调用状态、消息投影和分发；[session_compaction/service.py](../../session_compaction/service.py) 负责 Ops 承接与平台协调。包初始化不集中重导出服务，事件校验中的延迟导入继续保留，避免循环依赖。

当前为 Ops / Event 接口阶段。生产只注册类型、校验、matcher、CORE 与 Ops 代理；真实 ContextEngine 绑定、manual/auto/overflow 桥接、预计算调度均注释。用户可注册 Handler，但生产压缩不会触发本事件；合法的活动 Handler 调用 Ops 明确报 COMPACT_NOT_INTEGRATED。独立测试通过显式受控绑定验证事件链，不能宣称生产真实压缩已接入。不依赖 notify_context_usage，不修改前端、不进入 ui.render。子 Agent / 外部 Harness 尚未开放。

Core 的原生请求、候选、保存和恢复以 Core context_engine/S_01_compaction 为准。Server 管理公开消息投影、私有 handle 映射、目标准入、EventBus 与原 JSONL 压缩标记；业务 trigger 不放入 event_types。

## 2. 接口、字段与改写

`await ops.session.compact(*, instructions: str | None = None) -> SessionCompacted | SessionCompactSkipped`。参数只表达本次摘要重点，缺省/空白为无附加要求；目标、trigger、messages、agent_id、bus、回调与 Processor 不从用户参数接受。

| 对象/字段 | 类型与缺省 | 权威来源/作用 |
| --- | --- | --- |
| SessionCompactInput.trigger | manual/auto/plugin/precompute，必有 | 平台确认，USER 只读；用作 matcher |
| agent_id | str 或 None；首期固定 None | 保留子循环身份语义，不能填主会话 ID 或自行选择目标 |
| instructions | str 或 None，默认 None | USER 可 replace；真正进入本次摘要模型请求 |
| messages | list[SessionMessage]，必有、旧到新 | 平台 live 原生消息公开快照；USER 可 replace，不是产品历史分页 |
| SessionCompacted.messages | list[SessionMessage]，该分支必有 | 完整最终可压缩对话内容；摘要也是其中一条消息，无 summary 字符串字段 |
| tokens_before/tokens_after | 非负 int 或 None | 引擎本地消息计数，可能估算，不包含独立系统/工具声明，不是计费用量；precompute 无 after；最终出口不信任用户自填数字 |
| SessionCompactSkipped.skip | 去空白后 1～512 字符 | 跳过原因；不能同时返回 messages |

SessionMessage 的 role=user/assistant、text、toolUses 必有；toolResults 可选，handle 只由引擎在当前 compact 调用内产生。工具字段沿用现有查询结构；逐字段填写、用途和例子见 F_28 第 3 节。JSON 键 toolUses/toolResults/isError 保留 Claude 写法；顶层 agentId/tokensBefore/tokensAfter 在 Python 为 snake_case。

## 3. 不变量

- SC-01：启动时登记 event/matcher/唯一 CORE，ops.build 的 BUILTIN 只登记方法代理；业务时由共享 Builder 创建各 Handler 的独立 Ops，不重跑 ops.build。
- SC-02：公开 Ops 只使用 Runtime 发布的当前 USER/BUILTIN 包 Handler 与已绑定 SessionContext；Runtime 校验当前 Ops、嵌套层、Task 和活动期。非受管直接注册、后台复制、过期或手工构造的 Ops、无宿主状态的直接 trigger 不得取得压缩权限。
- SC-03：生产不建立真实引擎绑定，不调用原生压缩算法或正式安装；未接入是 COMPACT_NOT_INTEGRATED，不伪造成功/skip。保留的空闲独占、auto、precompute 机制仅用于独立受控测试及后续接入。
- SC-04：层级顺序 USER → 可选 BUILTIN → 唯一 CORE；本期无额外 compact BUILTIN。CORE 调用私有默认 continuation **只准备候选**。USER 前置或 post-next skip 都可阻止安装。
- SC-05：同名事件通过宿主 Hook 触发。USER 不接收真实 Session、Engine、Processor、提交函数或句柄字典，不能改写固定会话/trigger/agent_id。
- SC-06：有 handle 时恢复完整原生消息并忽略其公开字段改写；无 handle 时按公开消息构造。未知、重复、跨调用 handle 和非法工具链拒绝。Ops 返回的最终消息不提供可供后续调用复用的 handle。
- SC-07：仅在测试显式绑定/后续宿主接入时，整个事件链返回后才由 Core 安装和保存。产品历史仍保留旧记录，追加原有压缩标记；messages 历史查询不等于压缩后 live 消息。
- SC-08：EventBus / Ops / Builder 不增加身份、筛选或排除接口；plugin 正常执行完整匹配链。同会话压缩重入在第二次事件和等待自身锁前拒绝（COMPACT_BUSY 或 COMPACT_REENTRANT），不排除整个包或函数。
- SC-09：manual/plugin/precompute 使用宿主绑定的原手动算法白名单；auto 使用实际触发的原生摘要算法。预计算默认关闭，每目标一个后台任务/候选；策略代次变化后不得复用旧候选。
- SC-10：原 command.compact 按原签名/响应执行，不透传新增 instructions/skip，也不触发本事件。生产输入、任务结束与清理接入点注释；修改预计算配置不能绕过注释启用。受控绑定中的 skip/precompute 不写正式压缩标记。

## 4. 配置与失败

配置放在 `react.context_engine_config.compaction_precompute`：enabled 默认 False，ratio 默认 0.8，0＜ratio＜1。生产任务结束处的调用已注释，因此当前 enabled=True 也不调度；仅独立测试显式验证该机制。历史窗口比例和其它摘要算法配置沿用既有配置；不在用户 Handler 中收集或修改它们。

CompactionError.code 区分 NOT_INTEGRATED（当前生产未接入）、BUSY、SESSION_UNAVAILABLE、UNSUPPORTED、UNBOUND_INVOCATION、REENTRANT、INVALID_RESULT、STALE、CONTINUATION_REUSED、COMMIT_UNCERTAIN、NOTIFICATION_FAILED（实际均带 COMPACT_ 前缀）。取消传播 CancelledError；安装前清理候选，保存未知冻结目标并通过权威 Session 重载恢复。不把这些错误统一包装成 skip。

## 5. 验证与案例

输入/输出、单次注册、完整用户案例和分版本验收维护在 F_28；当前证据见其第 6.3 节，旧真实模型证据不作为本阶段主流程已启用的结论；独立示例位于 `examples/session_compact`，只返回/记录参数，不渲染界面。测试入口 `tests/unit_tests/extensions_v2/test_session_compact.py`，原命令参数验证在 `tests/unit_tests/agentserver/test_agentserver_cli_commands.py`。Core/Server 提交与真实证据关联在开发计划维护，不用规约代替进度台账。
