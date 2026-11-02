# session.end 用户示例

`session.end` 在平台真正停稳某个主会话运行后、释放该运行资源前通知 USER。插件可以保存自己按会话维护的摘要、关闭会话专用连接或任务，然后 next；CORE 核对可信结束通知并回显 sessionId，平台继续原释放。一次 query/模型回合完成不触发，结束运行也不要求删除历史。

示例包由扩展管理入口安装，manifest 的 module 指向 extension.py。它只注册 USER Handler；事件、默认 CORE、SessionContext 和触发入口由 Worker 平台提供，不需要用户改业务方法或创建 EventBus。

输入示意：

```python
from jiuwenswarm.extensions_v2.event_types.session_end import SessionEndInput, SessionResume

SessionEndInput(reason="other", sessionId="demo-session-a",
                resume=SessionResume(id="demo-session-a"))
```

默认输出示意：

```python
from jiuwenswarm.extensions_v2.event_types.session_end import SessionEndOutput

SessionEndOutput(sessionId="demo-session-a")
```

例如插件在 A 的每次用量更新后保存活跃摘要。平台真正清理 A 的运行或正常关闭 Worker 时，on_end 按 sessionId 清理 A 的活跃缓存，记录 reason 与 resume.id；B 的缓存不受影响。示例包记录真实清理日志；若需要跨进程持久保存摘要，应使用用户自己的存储能力，不能把普通内存字典当作已落盘。

具体字段、填写内容、触发粒度、完整调用链与旧字段迁移见 [F_24](../../docs/features/F_24_session-end.md)，公共契约见 [S_13](../../docs/specs/S_13_session-end.md)。示例不介入 ui.render，日志与实际结果参数用于观察。
