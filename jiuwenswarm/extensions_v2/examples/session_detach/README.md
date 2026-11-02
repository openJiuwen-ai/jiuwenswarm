# session.detach 用户示例

`session.detach` 告知插件“这个客户端已经离开旧会话”。USER 按旧关联身份取消自己的刷新、订阅或缓存，再 next；CORE 只确认旧关联确实已解除并回显 clientId。它本身不停止 Agent、不删除会话历史、不移除同一会话的其他客户端，也不负责执行第二次解除。

示例包由扩展管理入口安装，manifest 的 module 指向 extension.py。它只注册 USER Handler；事件、默认 CORE、SessionContext 和触发入口由 Worker 平台提供，不需要用户改业务方法或创建 EventBus。

输入示意：

```python
from jiuwenswarm.extensions_v2.event_types.session_detach import SessionDetachInput

SessionDetachInput(surface="desktop", clientId="web:view-main", reason="detach")
```

默认输出示意：

```python
from jiuwenswarm.extensions_v2.event_types.session_detach import SessionDetachOutput

SessionDetachOutput(clientId="web:view-main")
```

例如窗口 V 从 A 切到 B：旧 detach 绑定 A、clientId 仍为 web:V，插件按旧 attachment_id 取消 A 的刷新任务；随后新 attach 绑定 B，同一个 clientId 对应新的 attachment_id。迟到的 A 清理只能删旧键，不会误删 B 的资源。若只是关停 A 的运行，则对仍看 A 的窗口发送 reason=end，B 的窗口继续保留。

具体字段、填写内容、触发粒度、完整调用链与旧字段迁移见 [F_26](../../docs/features/F_26_session-detach.md)，公共契约见 [S_15](../../docs/specs/S_15_session-detach.md)。示例不介入 ui.render，日志与实际结果参数用于观察。
