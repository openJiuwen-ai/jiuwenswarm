# 轮次与上下文用量示例

这是用户扩展包，不是产品内置包。订阅既有 context.usage.updated，在自己的 USER Handler 调用公开 Ops；Ops 现在各自派发同名查询 Event，查询 USER→BUILTIN→CORE 返回后由代理解包。用户不创建 Bus、Builder 或 CORE，不改平台 Adapter。

```python
turns = await ops.session.turns()
usage = await ops.session.usage(breakdown="summary")
context = usage["context"]
percent = context.get("percent")  # 整数百分比；缺失是未知
```

turns 是已提交原生用户轮次或 None，不统计扩展 note。usage 返回 context/rateLimits/可选 cost。无账本则省略 cost，无额度读数则 []；未知数字不填零。summary 只返回已观察分类子集，full 默认明确不支持额外计数。旧 breakdown=True/False、context_window/turn 存储对象需迁移。

在本目录打包，让 ZIP 根直接包含 manifest.yaml 和 extension.py：

```powershell
Compress-Archive -LiteralPath .\manifest.yaml, .\extension.py -DestinationPath .\session_usage_info.zip
```

```text
/extension install "D:\extensions\session_usage_info.zip"
/extension query example.session-usage-info
```

一次有效原生用量通知后可以在宿主日志感知结果；其它已绑定可信会话的事件同样能调用，不必须先执行 notify。本例保持原通知 next；不调用 ui.render。逐字段含义、默认值、缺失语义和迁移见 [usage 特性](../../docs/features/F_21_session-usage-ops.md) 与 [turns 特性](../../docs/features/F_29_session-turns-query.md)，统一七项返回参数示例见 [session_query_audit](../session_query_audit/)。

## English

turns() counts committed native user inputs. usage() returns context, rateLimits and optional cost. Unknown figures are omitted, not zeroed. summary projects existing category estimates; full counting is unavailable by default. Boolean breakdown arguments and the old storage-shaped return are no longer public. The package only supplies USER handlers. The host collects trusted session context, builds Ops and serves the default CORE. Query events are call interception points, not change notifications. The example logs results and continues the original event without rendering UI. See the linked feature document for field meanings and migration.
