# 会话历史 Ops 与追加事件规约

| 项 | 值 |
| --- | --- |
| 编号 | S_06 / session-history-ops |
| 最近修订日期 | 2026-10-28 |
| 关联特性 | [F_16](../features/F_16_session-history-ops.md) |

本规约维护已实现的输入、输出与不变量。能力说明、完整用户包、调用链和验收场景见 [F_16](../features/F_16_session-history-ops.md)；本次按编写指南补齐接口说明，不改变实现行为。

## 1. 查询

`await ops.session.messages(limit=50, cursor=None) -> dict`。limit 为 1～100 的整数，不接受 bool；cursor 为非空字符串或 None。只读取当前绑定会话的主历史，返回 `messages`、`next_cursor`、`has_more`、`snapshot_id`。记录按新到旧排列，采用现有历史恢复可见性规则，去除内部提交标记；没有历史返回空列表。

| 返回字段 | 类型与含义 |
| --- | --- |
| messages | list[dict]；主会话可见历史，按新到旧排列。保留平台记录的 id、role、content、request_id、channel_id、timestamp 及已有可选字段，移除内部 `_session_append` 标记，不转换为 Claude SessionMessage |
| next_cursor | str 或 None；继续同一快照的游标，不透明，调用方原样传回 |
| has_more | bool；同一快照中是否还有可继续读取的记录 |
| snapshot_id | str 或 None；历史文件快照标识；没有历史文件时为 None |

首次调用排空已经接受的写入后固定快照；同一 cursor 重试不变，后追加不会进入旧快照。游标属于其它会话、格式非法或文件已替换 / 截断时明确报错。它读取已持久化历史，不读取 Agent 正在生成但未提交的片段。

该方法在线程中直接调用 `read_session_messages` / 原历史分页服务，不触发查询事件，不产生业务消息。适用于历史展示或追加后读回；要包含后来追加的记录，使用 cursor=None 开始新查询。

## 2. 追加

`await ops.session.append(message) -> dict`。message 只能包含：

| 字段 | 契约 |
| --- | --- |
| id | 必填，1～128 位 ASCII 字母、数字、点、下划线、冒号或横线；同一业务动作重试须使用相同 id |
| role | 必填，仅 user / assistant |
| content | 必填非空字符串，UTF-8 不超过 1 MiB |

目标只来自 `ops.session_context.session_id`，包归属只来自 Core 绑定的 PackageMetadata。方法不接受会话路径、其它目标会话、时间、任意历史元数据或工具事件。扩展记录标注 transcript_only，不更新用户输入轮次、会话标题或外部投递路由。

返回字段为 `status`（persisted / duplicate / rejected）、`session_id`、`message_id`、`message`、`reason`。persisted / duplicate 携带实际历史记录；rejected 不写入，message 为 None。duplicate 表示相同原请求先前已完整写入；相同幂等身份但原请求不同报 `APPEND_ID_CONFLICT`。同一 id 在不同会话或不同包中分别计。

| 返回字段 | 类型与含义 |
| --- | --- |
| status | str；persisted 表示实际提交完成，duplicate 表示同一原请求已存在，rejected 表示扩展自身追加被合法拒绝 |
| session_id | str；平台绑定的目标会话 |
| message_id | str；本次方法参数中的原 id |
| message | dict 或 None；persisted / duplicate 为实际公开历史记录的独立副本，rejected 为 None |
| reason | str 或 None；合法拒绝的原因，成功时为 None |

回执的 message_id 是调用方传入的 id；实际记录的 id 为按包和原 id 生成的 `ext:<hash>`，避免不同包的界面记录身份相撞。原 id 保留在 extension_message_id，包归属保留在 extension_package_id。平台 Handler 没有包身份时使用保留的 `<host>` 作用域，用户包身份仍由 Core 发布记录绑定。

方法参数没有默认消息或自动 id。它把参数构造成 transcript_only 记录，捕获可信会话、包和生命周期，再触发新 session.append 链；输入映射为 `SessionAppendInput(message_id=id, role, content, source="extension")`。事件默认输出经 `to_dict()` 深拷贝；合法 `SessionAppendRejection` 由派发入口转成上述 rejected 回执。适用于审计说明等产品历史留档，不能替代发送用户输入。

## 3. 事件

事件类型常量为 `SESSION_APPEND_EVENT = "session.append"`。触发源为 append Ops、主会话 `run_history_io(append_history_record, **kwargs)` 和既有 history.append_record RPC；均在构造记录、过滤临时记录和生命周期预检之后、实际提交之前触发。一次处理一条通过前置过滤的记录尝试，重试也进入事件链，提交层再去重，不按回合固定触发。

`session.append` 输入为 `SessionAppendInput(message_id, role, content, source)`。source 区分 extension、history 和 history.append_record。USER 只能替换 content；其它字段必须与可信调用封装一致。仅 source=extension 时，返回 `SessionAppendRejection(reason)` 可在调用 next 前显式拒绝；抛异常不等同拒绝。平台主历史和 RPC 的拒绝输出无效，按 Core USER 故障隔离继续原有效输入和默认提交。默认 CORE 校验并提交，已完成的真实提交回执具有最终效力，不能在 next 返回后用拒绝伪装撤销写入。

| 输入字段 | 类型、来源与可变性 |
| --- | --- |
| message_id | str；扩展参数 id 或平台已构造记录 id，不可改写 |
| role | str；已构造记录的 user / assistant，不可改写 |
| content | str；可改写正文，原正文非空时不能改为空白。UTF-8 长度不超过 max(1 MiB, 原正文长度)，以兼容既有较大平台记录 |
| source | str；extension / history / history.append_record，不可改写，不作为包身份或目标会话的授权来源 |

正常输出是平台 CORE 产生的 `SessionAppendResult(status, session_id, message_id, message=None, reason=None)`，字段含义沿用第 2 节；事件返回 Python 对象，Ops 方法返回字典。合法短路是扩展自身追加在 next 前返回 `SessionAppendRejection(reason)`，原因去空白后为 1～512 字符。Handler 不应自行构造成功或 rejected 的 SessionAppendResult 代替真实回执/拒绝对象。

输入改写在 next 边界和 CORE 再次校验。事件在 Worker 的当前可信调用封装内触发；直接构造同名事件不能伪造目标或写入上下文。CORE 不再回调追加 Ops。Hook 内递归 append 报 `APPEND_REENTRANT`，不产生第二次写入。

平台来源的成功结果须经过实际 CORE 提交；未经 CORE 返回伪造回执属于无效 Hook 输出，按故障隔离继续默认提交。CORE 的真实提交错误仍向外传播，不把 I/O 失败改写为成功。

沿用当前 Core 的 USER 故障隔离：Handler 抛异常或传递非法改写时，可跳过该 Handler 并用它进入前的有效输入继续；这不等同于整体拒绝。用户必须通过合法的 SessionAppendRejection 显式拒绝。匹配参数可使用 source / role / message_id / content；不由事件 payload 决定写入目标。

用户包通过 `registrar.register_event_handler("session.append", handler, match_pattern, "user")` 注册；matcher 按所给字段的相等条件合取，空字典匹配全部。放行用 `return await next_handler(event)`；正文改写用 dataclasses.replace 产生新输入，再传 `Event(event.event_type, rewritten)`。完整示例见 [F_16 第 3.4 节](../features/F_16_session-history-ops.md#34-注册筛选和最小用户示例)。`next_handler` 继续当前链，append Ops 另起追加链，messages 直接读取，不互相替代。

## 4. 平台覆盖与不变量

- SH-01：messages / append 在现有 session Ops Build 登记；只有 append 创建业务事件和平台 CORE，agent-core 不修改。
- SH-02：接入 `history.append_record` RPC 与 `run_history_io(append_history_record, ...)` 主会话写入；同步记录、Team 和子 Agent 专用记录维持原路径。主 Agent 的正文写入改写不等同改写 Agent 的执行输入。
- SH-03：生命周期预检在 Hook 前捕获 generation，写入锁内再次校验同一代次；Hook 期间不持文件 / 队列锁。CORE 在后台线程完成持久化，不阻塞 Worker 事件循环。
- SH-04：先排空既有队列，再在文件与生命周期锁下完成查重和写入。成功回执在文件关闭之后产生，不把排队受理当作写入成功。
- SH-05：同一追加调用的重复 next 复用回执；扩展稳定 id 支持完整记录的持久去重。后台普通流记录不因 request_id 相同而被错误合并。
- SH-06：文件写入开始后的取消会排空已接受的写入，再向外传播 CancelledError；结果未知时用相同 id 重试。写失败报错；若回滚也失败，返回结果未知错误，不声称未写入或成功。
- SH-07：查询只依赖有效会话身份，不要求 cwd、项目根、模型或用量事件；追加不会发送输入、创建 Agent 回合或主动渲染 UI。
- SH-08：平台写入事件绑定可用的可信会话资料。已绑定的原生执行范围内使用该会话的 cwd、project_root 和运行时模型快照；无运行时的入口从宿主持久元数据读取可用目录，model 仍由既有保存值读取契约提供。无资料为 None，不兜底到进程目录，不把保存模型冒充已激活模型。上下文只用于观察和能力读取，目标身份仍由可信调用封装与 CORE 校验。

## 5. 错误与生命周期

没有绑定 SessionContext 报 RuntimeError；方法边界对会话身份、message、分页参数或可信派发上下文的校验失败抛 `SessionHistoryError`，通过 code 区分；USER 的非法事件改写按第 3 节隔离。主要代码包括 INVALID_SESSION、INVALID_MESSAGE、INVALID_CURSOR、SNAPSHOT_CHANGED、SESSION_NOT_FOUND、APPEND_ID_CONFLICT、APPEND_REENTRANT、APPEND_REJECTED、HISTORY_IO_FAILED、APPEND_OUTCOME_UNKNOWN。生命周期拒绝保留其原 code。错误不包含原始文件内容或底层 I/O 文本。

| 错误/结果 | 对外含义 |
| --- | --- |
| INVALID_SESSION / SESSION_NOT_FOUND | 绑定身份或路径不合法 / 扩展追加的目标会话目录不存在；查询无历史仍返回空页 |
| INVALID_MESSAGE / INVALID_MESSAGE_LIMIT | 消息键、id、角色或正文不合法 / limit 非 1～100 的非 bool 整数 |
| INVALID_CURSOR / SNAPSHOT_CHANGED | 游标非法或跨会话 / 固定快照已被替换或截断，需重新查询 |
| APPEND_ID_CONFLICT / APPEND_REENTRANT | 同一幂等身份的原请求不同 / 当前追加 Hook 内递归追加 |
| HISTORY_IO_FAILED / APPEND_OUTCOME_UNKNOWN | I/O 失败 / 回滚也失败、提交结果未知，后者不能宣称未写入 |
| status=rejected | append Ops 的正常拒绝回执，不抛 APPEND_REJECTED；后者是平台适配入口对 rejected 状态的内部错误转换，当前平台来源拒绝会先被隔离 |

分页还包括 INVALID_MESSAGE_LIMIT、HISTORY_FORMAT_UNSUPPORTED；追加调用边界包括 PACKAGE_CONTEXT_REQUIRED、APPEND_BUS_UNAVAILABLE、APPEND_CONTEXT_REQUIRED、APPEND_RESULT_INVALID。没有历史返回空页；显式旧 JSON 写入模式不提供游标查询，旧 JSON 文件的自动迁移仍复用原服务。

写入接入前及实际提交前均校验；拒绝不写历史、不更新元数据。低层元数据更新仍沿用现有尽力而为行为，文件成功不表示所有派生元数据原子提交。示例、覆盖清单、实际验证与限制见 F_16。

方法的直接调用错误若未在 USER Handler 内捕获，仍受外层 Core USER 故障隔离约束；不能把外层业务继续执行视为追加成功。取消提交中的调用需在已接受写入排空后传播 CancelledError；完整历史记录保留时可用同 id 重试确认。重载/卸载移除包的 Handler，不删除已提交记录或平台 CORE。

## 6. 与 Claude mods 的关系

本规约定义 JiuwenSwarm 自主接口，借鉴事件和 Ops 分工，不声明与 Claude 同名接口可直接互换。对照为官方 `anthropics/claude-code` 提交 `bf2323b3021c9d3db7a31e1e7d61068c3f704497` 的 [mods/types/claude-code.d.ts](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)，文件头标记 Claude Code 2.1.277。对应符号为 SessionAppendArgs、SessionAppendInput、SessionAppendMessage、SessionAppendResult，以及 session.messages 的 OpEvent / 返回值声明。

| 项 | JiuwenSwarm 当前契约 | 对照接口的差异 |
| --- | --- | --- |
| append 方法输入 | id、user/assistant role、字符串 content | 不是 Claude 的 message.type=user/system、内容块数组和可选 agentId |
| append 事件输入 | message_id、role、content、source | source 表示平台接入位置，不等同 door 与 origin 两个独立维度；不支持复制 door=tool-result 等 matcher |
| append 回执 | status、session_id、message_id、message、reason | 不是 Claude 的 message/uuid 或 deny 结构 |
| 拒绝范围 | 仅可信 extension 来源的追加可显式拒绝 | 平台历史只允许受控正文改写，不能用拒绝结果阻断默认记录；对应收窄到扩展自身追加的设计方向 |
| messages | limit/cursor 固定快照、倒序分页对象、无查询事件 | 不是 Claude 的同名可 Hook OpEvent 和 SessionMessage 数组；需要拦截查询时另行定义事件及结果契约 |
| 数据生效 | 产品历史 transcript_only | 不注入模型上下文；追加 user 记录不表示 Agent 已接收新的用户输入 |

用户包使用本规约的输入输出，不能直接粘贴 Claude Handler 或 matcher。完整消息内容块与模型输入适配需要真实运行时接线，不能仅以字段改名声称兼容。
