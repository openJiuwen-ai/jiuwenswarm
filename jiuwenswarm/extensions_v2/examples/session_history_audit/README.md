# 会话历史审计 / Session history audit

## 中文

此包在真实回合的已有 `context.usage.updated` 中调用 `ops.session.append()` 写审计说明，
再用 `ops.session.messages(limit=5)` 读回。其 `session.append` USER Handler 给扩展正文加
`[audited]` 前缀，遇到 `[block]` 则显式拒绝。用户无需创建 EventBus、注册平台 Ops / CORE 或修改 Adapter。

```powershell
Compress-Archive -LiteralPath .\manifest.yaml, .\extension.py -DestinationPath .\session-history-audit.zip
```

将 ZIP 放到服务端可读取的位置，在已有 Web 命令入口执行：

```text
/extension install "D:\extensions\session-history-audit.zip"
/extension query example.session-history-audit
```

正常发送一条消息并产生有效用量后，宿主日志出现 `session history audit:`，
`visible=True` 表示从新历史快照读到了审计说明。记录也可通过现有历史恢复入口读取；本示例不主动渲染界面。

append 的 message 只含 id、role（user / assistant）、content。id 为必填稳定幂等身份，
本例由 request_id 的哈希生成；同一会话、包和 id 的同一原请求重试返回 duplicate，换内容则报冲突。
CORE 的成功回执在文件写入完成后返回，不只是接受排队。消息只进入产品历史，
不会创建 Agent 回合、触发模型或把说明注入模型上下文，也不改变会话标题和外部投递路由。

messages 返回 messages / next_cursor / has_more / snapshot_id，按新到旧分页。
继续旧 cursor 固定在旧快照，不包含后来追加；要读新消息请用 cursor=None 重新开始。
查询只需要宿主绑定的有效会话 ID，不需要用量事件或 cwd。

追加前 Hook 也覆盖已有 history.append_record RPC 和主会话的可等待历史写入；
本例匹配 source=extension，保持这些平台记录的正文原样。
显式拒绝要在 next 前返回 SessionAppendRejection；抛异常按 Core 规则隔离。
非法改写同样可能被隔离并使用原有效输入继续，不能用异常或非法输入代替显式拒绝。
在追加 Hook 内递归 append 报错。取消进入文件提交后会排空已接受写入，再传播取消；
结果未知时用相同 id 重试。异常的 code 见 S_06。

```text
/extension reload example.session-history-audit
/extension uninstall example.session-history-audit
```

## English

Install this ZIP through the existing `/extension install` command, then send a normal message.
The USER handler on `context.usage.updated` appends an audit note, awaits a real persistence
receipt and reads it back with `ops.session.messages()`. Its `session.append` handler rewrites
extension notes and explicitly rejects a body beginning with `[block]`.

Messages contain only a stable id, role and content. Retries of the same original request are
deduplicated within the bound session and package; changing content under the same identity
is a conflict. The write only affects the product transcript: it does not start an Agent,
call a model, inject model context, change a session title or alter delivery routing.

Pages are newest-first and tied to a stable cursor snapshot. Start a new query after appending
to see new records. Other host-bound session events can reuse both Ops. Existing awaitable
main-history writes and history.append_record RPC also cross the append hook; this package
only matches extension-origin notes. Explicit rejection must occur before next_handler.
Exceptions or invalid rewrites can be isolated by Core while the original valid input continues;
use SessionAppendRejection to explicitly refuse a write.
Recursive append is refused. Cancellation drains an accepted file write before propagating;
retry an uncertain outcome with the same id. No custom frontend is required for the log example.
