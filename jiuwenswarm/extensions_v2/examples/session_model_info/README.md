# 会话主模型信息示例 / Session model information

## 中文

此包订阅已有 `context.usage.updated`，在 USER Handler 中调用
`await ops.session.model()` 并写入宿主日志。用户无需创建 EventBus、注册 Ops 或提供平台 CORE。

在本目录打包，使 ZIP 根目录直接包含 manifest.yaml 与 extension.py：

```powershell
Compress-Archive -LiteralPath .\manifest.yaml, .\extension.py -DestinationPath .\session-model-info.zip
```

将 ZIP 放到服务运行环境能读取的目录，在现有 Web 命令入口逐条执行：

```text
/extension install "D:\extensions\session-model-info.zip"
/extension query example.session-model-info
```

随后正常发送消息，产生有效原生回合用量时，日志出现 `session model:`。
运行快照包含本回合绑定的模型名称和提供方，`source="runtime"`、`activated=True`。
若其它事件只提供会话 ID，可读取保存的稳定选择或旧模型名，
`source="session"`、`activated=None` 表示激活状态未知。无模型资料返回 None。
稳定模型组 ID 不代表已选择某个物理路由成员。

查询不启动或切换模型，不调用模型，不返回凭据，不自动生成 UI。
如有界面需求，由用户另行组装渲染数据并调用 UI 能力。

```text
/extension reload example.session-model-info
/extension uninstall example.session-model-info
```

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
