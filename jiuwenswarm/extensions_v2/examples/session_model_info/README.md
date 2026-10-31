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

一次有效原生用量通知后可以在宿主日志感知结果；其它已绑定可信会话的事件同样能调用，不必须先执行 notify。本例保持原通知 next；不调用 ui.render。逐字段含义、默认值、缺失语义和迁移见 [特性文档](../../docs/features/F_14_session-model.md)，统一七项返回参数示例见 [session_query_audit](../session_query_audit/)。

## English

This package registers a USER handler for the existing `context.usage.updated`
event, awaits `ops.session.model()`, logs the safe result, and continues the
original event chain. The host supplies the EventBus, Ops, and default CORE.

Place manifest.yaml and extension.py at the ZIP root, install through the
existing `/extension install` command using a path readable by the server, then
send a normal message. A native turn with nonzero usage produces a log entry.

Runtime facts have `source="runtime"` and `activated=True`. Saved selections
have `source="session"` and `activated=None`: persistence cannot establish
runtime activation. Missing facts return None. A model-group selector does not
identify the physical route used for one model call. The read does not create,
switch, or call a model, expose credentials, or render a UI.

## 字段填写速查 / Field guide（2026-10-29）

model() 无参数；返回 selection、model_name、provider、source、activated 五键。selection 有值时 type/id 必有，route_id 可缺省且仅组选择可用。runtime 表示当次运行摘要，activated=True；session 表示保存事实，activated=None，不猜测未激活。没有事实时整个返回 None。 用户不填写这些返回字段，不传其他会话或路径；修改返回副本不会改变平台事实。

每个字段的类型、默认/缺失、合法值、来源和示例，以及完整注册/调用链见 [特性文档第 4、5 节](../../docs/features/F_14_session-model-ops.md)。本轮仅文档，历史业务验证未重跑。

English: the query takes no arguments. All returned fields are host-provided facts, not caller input. Read the linked field tables for precise types, nullability, source and state combinations. Returned data does not activate a model, modify Git or render UI. This revision only clarifies documentation; historical runtime evidence retains its original date and scope.

model() returns a model-name string or None, not the old detail object. The same-name query event is independently hookable. Saved IDs are not resolved into invented model names. The package only supplies USER handlers. The host collects trusted session context, builds Ops and serves the default CORE. Query events are call interception points, not change notifications. The example logs results and continues the original event without rendering UI. See the linked feature document for field meanings and migration.
