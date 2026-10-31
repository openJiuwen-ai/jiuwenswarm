# Session end / 主会话结束清理示例

这是用户扩展包，不是产品默认包。它在已有 `context.usage.updated` 通知中按 session_id 保存自己的项目根缓存，在主会话永久删除前的 `session.end` 通知中移除该会话的缓存；日志中的 released 表示本包是否确实删除过缓存。

## 使用方法

1. 使用包含 PR10 的 Server 和当前既有 agent-core；不要求先合入 PR04/05/06。
2. 将本目录的 manifest.yaml、extension.py 打到 ZIP 根目录，通过已有 `/extension install "D:\extensions\session-end.zip"` 或 extensions.v2.install 管理入口安装。
3. 正常发送消息并等待有效用量通知，用户缓存随之建立；通过现有会话永久删除操作删除这个主会话。
4. 日志出现 `session end example: ... reason=session_deleted released=True`，平台随后继续原资源释放和删除。没有预建缓存时 released=False 是合法幂等清理，CORE 仍正常接收通知。

用户只注册 USER Handler，不提供 session_id/结束原因，不定义平台事件/CORE，不调用 notify 或修改 Adapter。Handler 的 event.event_input 为平台 dataclass；所有字段只读。ops.session_context 是当前只读会话入口，已有 root 查询按平台资料返回；本包不创建 Builder 或 EventBus。

`return await next_handler(event)` 继续原通知链。默认 CORE 只确认通知，accepted 不等于平台删除已经提交，也不保证每个外部资源都释放成功。不要在这个 Hook 内重入 session.delete 或删除平台目录。实际连接或外部资源由用户以幂等方式释放；清理预算默认 2 秒，超时/异常后平台继续原删除。

断开网页、归档、每轮执行完成和临时 Worker 回收不会触发此通知。它不与按包激活实例触发的 session.start 一一配对，不涉及 Team/子 Agent。卸载只撤销本包订阅，不删除产品会话和平台默认处理；真实外部资源还应由用户按其资源生命周期安排卸载/异常补偿。

完整字段、调用链、错误和验收见 [F_24](../../docs/features/F_24_session-end.md) 与 [S_13](../../docs/specs/S_13_session-end.md)。

## English

Install manifest.yaml and extension.py as a ZIP through the existing extension management interface. This user package caches the bound project root after a normal native turn reports usage, then removes only its own per-Session cache when the host emits session.end during permanent main-Session deletion.

The host supplies immutable target/reason fields, package identity and SessionContext. Call next_handler(event) to continue the notification chain. Its CORE acknowledges the notification; Runtime alone continues native release and deletion. No new user Ops, custom Adapter, EventBus or dependency on the pending PR04/05/06 is required.

Cleanup is best-effort, bounded by a two-second wait budget, and should be idempotent. An acknowledgement is not a committed delete result. Disconnect, archive, idle eviction and turn completion do not emit this event; Team/subagent lifecycles and process-exit notifications are outside scope. Uninstall removes the user subscription without changing the platform's default deletion. See F_24/S_13 for fields, failures and evidence.
