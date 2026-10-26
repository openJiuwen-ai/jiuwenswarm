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
