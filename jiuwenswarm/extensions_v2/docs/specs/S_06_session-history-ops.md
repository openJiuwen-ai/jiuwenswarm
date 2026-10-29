# 会话历史 Ops 与追加事件规约

| 项 | 值 |
| --- | --- |
| 编号 | S_06 / session-history-ops |
| 最近修订日期 | 2026-10-29 |
| 关联特性与字段填写手册 | [F_16](../features/F_16_session-history-ops.md) |

本规约维护稳定边界和不变量。[F_16 第 3 节](../features/F_16_session-history-ops.md#3-事件契约)逐字段说明事件输入、message/origin 嵌套字段和输出；[第 4 节](../features/F_16_session-history-ops.md#4-方法契约)说明所有 Ops 参数、默认值、填写示例和迁移。实际类型唯一实现于 event_types/session_history.py，不另维护一份完整 Schema。

## 1. 公开接口

事件 `session.append` 输入为 `SessionAppendInput(message, door, origin, uuid)`，message 为 `SessionAppendMessage(type, content, role=None, name=None, is_meta=False, tool_call_id=None, tool_calls=None)`，origin 为 `SessionAppendOrigin(kind, name, entry)`。固定字段不得改写，只有原位置 text 块的 text 可改；同样数量/顺序的媒体和未知块保持。具体类型、枚举、填写责任与例子见 F_16 第 3.2 节。

默认事件输出为 `SessionAppendResult(status, session_id, message_id, message=None, reason=None, uuid=None, history_record=None)`；方法返回其独立字典。message 是实际公开消息视图，history_record 是实际产品记录，二者不混用。合法拒绝是 `SessionAppendRejection(reason)`，仅自己的 note、next 前允许；拒绝回执 message/uuid/history_record 均为 None。

`ops.session.append(note)` 接受新格式 `{id, message}` 或兼容旧格式 `{id, role, content}`；同一原请求必须连参数格式一起保持重试。新 message 只允许 user/assistant/system 文本消息，不能制造工具调用/结果或媒体。平台固定会话和包身份、来源和行 uuid；追加 transcript_only，不启动 Agent。旧事件字段 content/source/role/message_id 已被结构化输入替代，旧回执 message 的产品记录应改读 history_record。

`ops.session.messages(limit=50, cursor=None)` 直接读主历史固定快照，limit 为 1～100 的非 bool 整数；cursor 为不透明非空字符串或 None。返回 messages/next_cursor/has_more/snapshot_id，记录按新到旧，移除内部 _session_append。新记录可含 uuid/transcript_message/native_message，旧记录不保证有这些可选字段。

## 2. 平台覆盖

主会话异步 run_history_io(append_history_record, **kwargs)、history.append_record RPC 和 append Ops 在过滤、构建、生命周期预检后，实际提交前进入 Worker 事件链。平台异步入口支持 content 为 BaseMessage，并保留原生快照；已有产品流记录照常适配。同步后台、Team/子 Agent 保持原路径；不宣称所有 Core 消息写入已经覆盖。

event_types 只定义/校验/注册，history_events 触发，history_message 适配并合并，session_append_core 实际提交。用户扩展安装模块/Handler 即可，不应修改平台 Adapter 收集身份或绕过可信入口直接构造写入事件。

## 3. 不变量

- SH-01：messages/append 由已有 ops.build BUILTIN 登记，Builder 绑定每次 Handler 的可信上下文；仅 append 另起业务事件，agent-core 无变化。
- SH-02：目标会话、包归属、代次、原记录及原输入来自平台调用封装，不从可改写字段决定。上下文入口 ops.session_context 是 Core 已有只读属性。
- SH-03：Hook 前生命周期预检，提交锁内复验同一 generation；不持文件/队列锁运行 USER，实际 I/O 在后台线程。
- SH-04：先排空既有写入，再在文件/生命周期锁内查重提交。成功回执在文件关闭之后产生，不把队列受理当作持久化成功。
- SH-05：同调用重复 next 复用真实回执；按包/会话/稳定 id 持久去重。同请求的多条普通历史记录不合并，原产品 id 分组语义保留，新增 uuid 提供行身份。
- SH-06：写入开始后取消先排空再传播 CancelledError；I/O 回滚失败报 APPEND_OUTCOME_UNKNOWN，同原 id/原参数重试确认，不声称无副作用。
- SH-07：查询和追加只要求有效会话身份；不要求 cwd/root/model 或用量事件。不会发送新输入或自动渲染 UI。
- SH-08：平台事件收集可用的会话目录和模型快照，原生实际绑定与保存信息区别沿用既有 Ops 契约；未知不兜底其他会话或进程目录，作用域退出后失效。
- SH-09：message 的类型、角色、名称、meta 标记、工具关联、内容块数量/顺序和非 text 字段固定；uuid/door/origin 固定。只改现有文本；非法 USER 输入/输出依 Core 故障隔离继续原有效链。
- SH-10：扩展 note 可在 next 前显式拒绝，平台/RPC 历史不可被 USER 拒绝。真实 CORE 回执最终有效，后置伪造/修改不能撤销或替换已存事实。
- SH-11：公开工具正文改写同步 rendered_result；原始 result 和宿主元数据保留。BaseMessage 改写只合并待存快照，不原地修改调用者对象，不保证当前活跃模型输入同步变化。

## 4. 失败与兼容

没有 SessionContext 抛 RuntimeError；SessionHistoryError.code 区分 INVALID_SESSION、SESSION_NOT_FOUND、INVALID_MESSAGE、INVALID_MESSAGE_LIMIT、INVALID_CURSOR、SNAPSHOT_CHANGED、APPEND_ID_CONFLICT、APPEND_REENTRANT、HISTORY_IO_FAILED、APPEND_OUTCOME_UNKNOWN、APPEND_BUS_UNAVAILABLE、APPEND_CONTEXT_REQUIRED、APPEND_RESULT_INVALID 等。生命周期错误保留原 code，错误不包含原始记录/文件内容。

新公开事件/回执在未合入 PR 内迁移，旧 append 方法字符串参数保留；旧历史格式不强制迁移。旧 JSON 写入模式保留既有写行为，但 cursor 查询报 HISTORY_FORMAT_UNSUPPORTED。历史查询空结果不等于错误，保存文件成功不保证所有派生元数据原子提交或 fsync 断电耐久。

完整 USER 包、两条运行链、字段表、Claude 对照、实际验证与未验证范围统一见 F_16 与示例 README。本次不引入 agentId/Team 范围，不恢复 server.request.handle，不新增消息查询事件。
