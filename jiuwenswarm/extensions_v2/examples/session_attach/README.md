# session.attach 用户示例

`session.attach` 告知插件“这个具体客户端已经加入该会话”。USER 可按渲染面筛选、使用绑定新会话的 Ops 查询资料，为本次关联建立缓存或订阅，然后 next；CORE 确认关联和目标仍然有效，回显 clientId。插件自己的前端能力决定怎样展示，事件本身不自动 ui.render。

示例包由扩展管理入口安装，manifest 的 module 指向 extension.py。它只注册 USER Handler；事件、默认 CORE、SessionContext 和触发入口由 Worker 平台提供，不需要用户改业务方法或创建 EventBus。

输入示意：

```python
from jiuwenswarm.extensions_v2.event_types.session_attach import SessionAttachInput

SessionAttachInput(surface="desktop", clientId="web:view-main", viewport=None)
```

默认输出示意：

```python
from jiuwenswarm.extensions_v2.event_types.session_attach import SessionAttachOutput

SessionAttachOutput(clientId="web:view-main")
```

例如“会话项目资料”插件：窗口 V 进入 A 后，on_attach 通过 ops.session.root() 读取 A 的可得项目根。插件可以按上下文中的 attachment_id 保存本次关联资料；随后切换 B，由旧 detach 清理 A 的关联资源，再由 B 的 attach 准备新资料。默认 CORE 不替插件准备前端，也不再次切换会话。现成示例包用日志验证 root 和 clientId。

具体字段、填写内容、触发粒度、完整调用链与旧字段迁移见 [F_23](../../docs/features/F_23_session-attach.md)，公共契约见 [S_11](../../docs/specs/S_11_session-attach.md)。示例不介入 ui.render，日志与实际结果参数用于观察。
