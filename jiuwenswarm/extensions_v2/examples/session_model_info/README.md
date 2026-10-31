# 会话主模型名称示例

这是用户扩展包，不是产品内置包。订阅既有 context.usage.updated，在自己的 USER Handler 调用公开 Ops；Ops 现在各自派发同名查询 Event，查询 USER→BUILTIN→CORE 返回后由代理解包。用户不创建 Bus、Builder 或 CORE，不改平台 Adapter。

```python
model_name = await ops.session.model()  # str 或 None，不再是模型详情字典
```

默认优先已应用运行快照中的 model_name；缺失时只读保存资料可得的名称。只有稳定选择 ID 而无名称时为 None，不把分组/路由 ID 冒充物理模型名。不创建或切换模型。

在本目录打包，让 ZIP 根直接包含 manifest.yaml 和 extension.py：

```powershell
Compress-Archive -LiteralPath .\manifest.yaml, .\extension.py -DestinationPath .\session_model_info.zip
```

```text
/extension install "D:\extensions\session_model_info.zip"
/extension query example.session-model-info
```

一次有效原生用量通知后可以在宿主日志感知结果；其它已绑定可信会话的事件同样能调用，不必须先执行 notify。本例保持原通知 next；不调用 ui.render。逐字段含义、默认值、缺失语义和迁移见 [特性文档](../../docs/features/F_14_session-model-ops.md)，统一七项返回参数示例见 [session_query_audit](../session_query_audit/)。

## English

model() returns a model-name string or None, not the old detail object. The same-name query event is independently hookable. Saved IDs are not resolved into invented model names. The package only supplies USER handlers. The host collects trusted session context, builds Ops and serves the default CORE. Query events are call interception points, not change notifications. The example logs results and continues the original event without rendering UI. See the linked feature document for field meanings and migration.
