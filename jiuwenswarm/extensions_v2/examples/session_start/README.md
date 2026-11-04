# session.start 用户示例

`session.start` 在原生 chat.send 完成权限准入、模型应用及执行目录绑定后、Agent 处理该输入前触发。USER 收到 cwd、surface、isInteractive，可以准备自己的规则、缓存或能力，然后 next；CORE 回显可信 cwd，由初始化门消费后继续原输入。后续 Handler 是否复用资源由插件自己的同一实例状态决定，事件本身不会自动把规则传给模型。

示例包由扩展管理入口安装，manifest 的 module 指向 extension.py。它只注册 USER Handler；事件、默认 CORE、SessionContext 和触发入口由 Worker 平台提供，不需要用户改业务方法或创建 EventBus。

输入示意：

```python
from jiuwenswarm.extensions_v2.event_types.session_start import SessionStartInput

SessionStartInput(cwd="D:/projects/demo/src", surface="desktop", isInteractive=True)
```

默认输出示意：

```python
from jiuwenswarm.extensions_v2.event_types.session_start import SessionStartOutput

SessionStartOutput(cwd="D:/projects/demo/src")
```

例如插件读取 rules.json 到自己的 self._rules，后续同一对象的 on_usage 使用这份规则。start CORE 不保存规则；同一实例的成员状态使两个回调能复用。示例包先保存首个工作区观察值，后续用量 Handler 记录初始化是否完成；跨会话状态必须另外按 session_id 管理，不把首个工作区当成所有会话的目录。

具体字段、填写内容、触发粒度、完整调用链与旧字段迁移见 [F_21](../../docs/features/F_21_session-start.md)，公共契约见 [S_09](../../docs/specs/S_09_session-start.md)。示例不介入 ui.render，日志与实际结果参数用于观察。
