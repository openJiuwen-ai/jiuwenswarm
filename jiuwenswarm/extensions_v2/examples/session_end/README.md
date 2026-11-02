# Session end / 会话运行结束收尾示例

这是用户扩展包。已有 context.usage.updated 为本包建立按 session_id 管理的项目根缓存；真实 Runtime 会话运行结束时，session.end 让它保存恢复标识并清理自己的活跃缓存。结束本次运行不要求删除历史。

## 使用方法

1. 把 manifest.yaml 和 extension.py 放到 ZIP 根目录，使用已有扩展安装入口安装。
2. 正常聊天，等待已有用量通知建立用户缓存。该事件仅用于示例准备资源，不是 session.end 的触发前置。
3. 通过真实会话资源释放或正常 Worker 停机结束运行；通知在扩展卸载前执行。
4. 查看日志 session end example: session=... reason=other resume=... released=True。没有预建缓存时 released=False 合法；CORE 仍回显真实 session_id。
5. 原平台历史保持原有持久化策略；恢复同一持久 ID 后重新建立资源，新运行结束时会再收到通知。

USER 输入只有 reason、session_id、resume.id，全由平台填写。reason 对齐 clear/resume/logout/prompt_input_exit/other；当前原生关闭入口使用 other，不新增对应远程方法。返回 await next_handler(event) 得到的 SessionEndOutput(session_id)，移除旧 accepted/end_id/session_deleted 用法。ops.session_context 是平台绑定的只读入口，不是用户负责收集上下文的函数。

默认 1.5 秒预算，异常/超时后原 Runtime 继续释放。用户只释放自己拥有的连接、缓存和任务；不要删除平台目录或重入同一会话的结束/恢复。普通回复完成、取消当前生成、界面切换、attach/detach、Supervisor 暂时断连均不构成结束；明确 client.disconnect 若实际释放运行则通知。崩溃或强杀无法保证通知。

不新增 Ops、EventBus、Adapter 或前端渲染。Team/子 Agent 未接入。完整字段与平台调用链见 [F_24](../../docs/features/F_24_session-end.md) 和 [S_13](../../docs/specs/S_13_session-end.md)。

## English

This user package caches its own session state from the existing usage notification and releases it when the actual Runtime session run ends. The host notifies plugins after quiescence, before resource release and plugin teardown. Persisted history is independent of this notification and may be resumed through the original platform API.

Read the immutable reason, session_id and resume.id; return the actual SessionEndOutput(session_id) from next_handler(event). Native Runtime release and shutdown currently report other. The default notification budget is 1.5 seconds. Cleanup is best-effort and idempotent. Turn completion, ordinary view transitions and temporary Supervisor disconnects do not end the run. An explicit client disconnect that releases Runtime resources does. Hard termination cannot guarantee delivery. No extra Ops or UI integration is required.
