# Session history audit / 会话历史审计

这是一个**用户扩展包示例**，不是产品内置包。Worker 已提供 append 事件、共享 Ops 和默认持久化 CORE；用户只安装本目录中的 manifest.yaml 和 extension.py。

## 中文：功能与准备

会话正常执行并发布已有 context.usage.updated 后，本包 on_usage 追加一条结构化审计说明，再查询历史确认。on_append 只匹配 `door=note`、`origin.kind=extension`，以保留块位置的方式给文本加 `[audited]`，或拒绝以 `[block]` 开头的自己的 note。

1. 使用包含 PR04 的 Server 与既有 agent-core，按项目原方式启动。
2. 将本目录的 manifest.yaml、extension.py 打成 ZIP，安装到 extensions.v2 管理入口。
3. 正常发送一条消息，等待真实用量通知，查看宿主日志 `session history audit:` 与历史查询。
4. 卸载包后用户 Handler 不再调用，已保存历史保留。

该示例选择用量事件作入口，Ops 本身不依赖用量事件。其他平台事件只要提供有效 SessionContext，其 USER Handler 同样可以调用；用户无需自行拼平台会话上下文。

## 输入、返回与字段填写

```python
receipt = await ops.session.append({
    "id": "audit-order-42",  # 同一动作重试保持 id 与全部原参数不变
    "message": {
        "type": "assistant",
        "content": [{"type": "text", "text": "This turn was audited."}],
    },
})
messages = await ops.session.messages()  # 从旧到新，最多最近 4096 项
recent = messages[-5:]  # 只在返回数组上切片，不再传 limit/cursor
```

- id：调用者填写的稳定重试键，不是会话 ID；1～128 个 ASCII 字母、数字或 `._:-`。
- message.type：本例 assistant；也可 user/system 文本。不能伪造工具结果。
- message.content：文本块数组；每个 text 字段填写实际正文。role 可省略，默认同 type；is_meta 由平台固定 True。
- 不填写 door/origin/uuid、会话、时间或路径；这些由平台固定。
- receipt.status：persisted / duplicate / rejected。成功的 receipt.message 是结构化消息，receipt.uuid 是实际行身份，receipt.history_record 是产品历史行；它与 messages() 的消息投影不是同一结构，不能直接比较两个对象。
- receipt.message_id 回显用户 id；产品记录原 id 可用于分组，不能代替行 uuid。rejected 时 message/uuid/history_record 为 None，reason 给出原因。

事件输入为 message/door/origin/uuid；嵌套匹配用 `origin.kind` 点路径。只改已有文本块 text，不改工具关联、非文本块、数量或顺序。next_handler 继续当前链；append Ops 创建新链；messages Ops 另起 session.messages 查询链并返回 role/text/toolUses 数组。不要在追加 Hook 内递归调用 append。

成功仅表示产品历史落盘，不发送新 Agent 输入、不自动 ui.render、不保证改写活跃模型上下文。原始工具 result/私有元数据仍由平台保留，不宣称彻底脱敏所有存储。

追加事件/方法的完整字段、默认值、拒绝和迁移见 [F_16](../../docs/features/F_16_session-append.md)；messages 的无参数调用、消息数组字段、查询事件、错误和独立用户包见 [F_18](../../docs/features/F_18_session-messages.md)。本示例仍展示读写联合使用，不要求查询方先安装或使用追加功能。旧 `{id, role, content:str}` 方法参数仍支持；旧事件顶层 content/source 与旧回执 message 的产品记录形态已迁移。

## English

Install this directory as an extension ZIP through the existing management interface. It subscribes to context.usage.updated, appends a structured transcript-only audit note, and reads it back. Its session.append policy matches `door=note` plus `origin.kind=extension` and edits only existing text blocks. No user-side EventBus or platform Adapter is required.

Use a stable id and `{message: {type: "assistant", content: [{type: "text", text: "..."}]}}`. Retry the entire original request unchanged. The host supplies session/package identity, origin, door and row uuid. The receipt contains status, session_id, message_id, structured message, uuid, history_record and reason. Compare history_record with the product history page; message.content is an array, not a legacy string. Rejection has no stored message/uuid/history_record.

The Ops are shared with any platform event carrying a valid SessionContext. Appending does not run an Agent, inject a live model message, or render UI. Uninstall removes the user policy, not stored history. See [F_16](../../docs/features/F_16_session-append.md) for the append event/method, allowed edits and migration. See [F_18](../../docs/features/F_18_session-messages.md) for the read-only messages event, chronological message-array fields and a standalone reader package. Querying history does not require appending a note or registering an append handler.


2026-10-31：messages() 的项不是原产品历史行；使用 row["text"]，不读取 content、extension_message_id 或页游标。要核对刚追加的行身份，使用 append 的真实提交回执；本例仅在最近返回项中核对保存正文，不把正文相同当作全局行身份。
