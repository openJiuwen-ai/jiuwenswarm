# 会话轮次与用量示例 / Session turns and usage

## 中文

本包是用户侧代码，订阅已有 `context.usage.updated`，调用
`await ops.session.turns()` 和 `await ops.session.usage(breakdown=True)`，
写宿主日志后继续原 CORE 回执。用户无需修改平台业务方法、创建 EventBus 或自行收集上下文。

在本目录将 manifest.yaml 与 extension.py 放在 ZIP 根目录：

```powershell
Compress-Archive -LiteralPath .\manifest.yaml, .\extension.py -DestinationPath .\session-usage-info.zip
```

将 ZIP 放在服务能读取的位置，使用已有命令：

```text
/extension install "D:\extensions\session-usage-info.zip"
/extension query example.session-usage-info
```

正常发送消息，有效原生回合通知用量时日志出现 `session usage:`。turns 是已落盘的主会话用户输入去重数，
不是模型调用或工具次数。usage.context 是最后一次观察到的模型输入窗口占用，usage.turn 是本轮 provider 消耗，
两者不能相加或当成累计账单。context_window.occupancy_rate 是 0～1 比例，measurement 包含估算/实测来源，
`breakdown=True` 才带 context.parts 分项；旧数据可能只有 turn。None 表示缺失，0 表示真实零值。

任意其它已绑定有效 SessionContext 的 Handler 也能调用这两个共享 Ops：turns 和无宿主快照时的 usage
从 session_id 对应的已提交历史读取，用户不必自行调用 notify。会话存在但无输入时 turns=0，未知会话为 None。
宿主用量通知提供本次快照，避免异步通知时读到上一轮尚未更新的 summary；事件载荷字段不会覆盖 Ops 的资料。
查询不重新测量、不调用模型、不触发 session.measure，也不依赖 PR04 的 messages/append。

读取在线程中等待既有历史写入边界，最久 10 秒，扫描历史 O(N)。损坏、I/O 或超时抛
SessionStatisticsError，用户可捕获后自行降级；示例不提供前端 UI。历史截断后轮次按保留分支计算，
压缩摘要和扩展追加不额外增加轮次。卸载只移除用户 Handler，不修改平台事实。

```text
/extension reload example.session-usage-info
/extension uninstall example.session-usage-info
```

## English

Package manifest.yaml and extension.py at the ZIP root, install through the existing `/extension install`
command, and send a normal message. The USER Handler reads committed primary user-input turns and a
separate context-window/per-turn-usage snapshot, logs them, and continues the original CORE receipt.
Other session-bound Handlers can use the same shared Ops; no manual notify call or platform Adapter change
is required. Context occupancy is an observation, not cumulative billing; retain measurement provenance.
None means unavailable, while zero remains zero. Category parts require breakdown=True. Queries do not
call a model, measure again, trigger a new event, or depend on message/append Ops. History reads run in a
thread, scan O(N) records, and fail explicitly on corruption, I/O errors, or a 10-second queue timeout.
