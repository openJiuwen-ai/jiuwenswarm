# Session Detach 用户示例 / User example

将 manifest.yaml 和 extension.py 放在 ZIP 根目录，通过现有 extensions.v2.install 安装。无需修改平台业务方法或构造 SessionContext。包订阅 attach 初始化自己的资源，并在 detach 按 attachment_id 清理自己的旧资源，原样 next/返回。示例只记录日志，不自动渲染、不写历史、不结束会话。

Package manifest.yaml and extension.py at the ZIP root, then install through extensions.v2.install. The package registers USER handlers only. It owns resources by attachment_id and cleans up the old identity without touching another view or a newer association. It does not create SessionContext, render UI, write history or end Sessions.

现有创建/切换链自动产生关联变化通知。主动离开接入者可通过现有请求传输发送 session.detach，params 必须是该次 attach 的 session_id、view_id（surface_id）和 attachment_id；channel 沿用原请求。身份过期不能移除新关联，重复请求不重复通知。页面关闭的自动上报不在本示例内；不能用 WebSocket 断开推断视图关闭。

Committed create/switch transitions notify automatically. An explicit leave uses the session.detach Server request with the exact session_id, view_id (surface_id) and attachment_id obtained from attach. The routed channel remains unchanged. Stale identities cannot remove a newer association. Repeated removal does not notify twice. Browser close reporting is a client responsibility; a transport disconnect alone does not detach.

完整字段与行为见 [F_26](../../docs/features/F_26_session-detach.md)、[S_15](../../docs/specs/S_15_session-detach.md)。失败/超时不恢复已解除关联；旧清理应按 attachment_id 幂等，不按视图名删除新资源。通知只提供旧会话上下文，目录/模型缺失时保留原只读 Ops 缺省语义。

See F_26 and S_15 for all fields and error semantics. Notification failure does not restore a released binding. Cleanup must be idempotent by attachment_id. Missing directory/model facts retain the existing read-only Ops defaults.
