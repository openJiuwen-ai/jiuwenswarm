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
仅 source=extension 的追加可在 next 前返回 SessionAppendRejection 显式拒绝。
普通平台历史和 RPC 的拒绝输出无效，按 Core 故障隔离继续默认写入，受控正文改写仍可用。
抛异常按 Core 规则隔离。
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
only matches extension-origin notes. Only extension-origin appends may return an explicit rejection
before next_handler. Rejection outputs for platform history and RPC records are invalid and isolated
by Core, so the default write continues; controlled content rewrites remain available.
Exceptions or invalid rewrites can be isolated by Core while the original valid input continues;
use SessionAppendRejection to explicitly refuse a write.
Recursive append is refused. Cancellation drains an accepted file write before propagating;
retry an uncertain outcome with the same id. No custom frontend is required for the log example.

## 平台上下文 / Platform context

平台主历史事件在已绑定的原生执行范围内提供当前 cwd、project_root 和运行时模型快照；
独立 RPC 等入口从宿主持久会话资料提供可用目录，模型查询返回保存值语义，不激活模型。
资料缺失时为 None，用户无需修改 Adapter 补资料。扩展 Ops 发起的追加沿用当前 Handler 的可信上下文。
平台不会把事件正文或 RPC 中任意提供的 cwd 当作查询授权，也不会借用另一个会话的运行时资料。

Native platform-history events use the bound request's workspace and applied model snapshot.
Standalone RPC writes use available persisted session workspace facts; model reads retain saved-selection
semantics and never activate a model. Missing facts remain None. Extension Ops appends keep their current
trusted Handler context. Event bodies and arbitrary RPC paths cannot replace those host facts.

## 与 Claude mods 的差异 / Differences from Claude mods

本包使用 JiuwenSwarm 自主协议，不可直接替换 Claude 同名接口。append 接受 id/role/字符串 content，
返回自定义状态回执，仅写产品历史；source 是接入位置，不等同 Claude 的 door/origin。messages 是直接只读分页，
没有查询事件，也不返回 Claude 的 SessionMessage 数组。完整对照见 [S_06](../../docs/specs/S_06_session-history-ops.md#6-与-claude-mods-的关系)。

This package uses the JiuwenSwarm contract, not a drop-in Claude mods interface. Append takes id/role/string
content and returns a custom persistence receipt; it affects only the product transcript, not model input.
Source identifies the platform entry point rather than Claude's door/origin axes. Messages is a direct,
read-only paginated query without a query event, not a Claude SessionMessage array. See S_06 for the comparison.
