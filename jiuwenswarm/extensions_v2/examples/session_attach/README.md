# Session attach / 界面与会话关联示例

这是用户扩展包。它在现有创建/切换流程提交界面与主会话的关联后，接收 session.attach，读取平台绑定的项目根并记录关联与默认 CORE 回执，不创建聊天或自动渲染 UI。

## 使用方法

1. 使用包含 PR08 的 Server 与当前既有 agent-core；不要求先合入 PR04/05/06/10。
2. 将 manifest.yaml、extension.py 打在 ZIP 根目录，通过原 `/extension install "D:\extensions\session-attach.zip"` 或 extensions.v2.install 管理入口安装。
3. 在现有 Web 创建主会话或切换到已有主会话。Web 原调用已带稳定 view_id，本 PR 不增加前端操作。日志出现 `session attach example: ... surface=... attachment=... accepted=True`。
4. 同一视图同一目标重放或稳定 view_id 重连不重复通知；另一个 view_id 进入同一会话会独立通知。切换目标再切回会产生新的 attachment_id。

只需用户模块中的 register/on_attach，不提供平台 CORE、Adapter、Builder、EventBus 或 SessionContext。event.event_input 的全部字段由平台填写，surface_id 是具体 view_id，不能写成会话 ID、A2UI 卡片 ID 或 ui.ready 的槽位。用户按需要匹配 channel_id/mode，原样返回 `await next_handler(event)` 的真实回执。

项目根未知时 root 为 None；cwd 未确认时也为 None。model 若调用则遵循既有保存信息查询语义，不创建或激活模型。查询不要求用量事件，不能把保存选择当作当前运行激活的证明。

默认 CORE 只确认当时仍有效的关联。用户需要自己的展示时应另接已有渲染链路；accepted 不代表所有展示均成功。通知等待预算默认 2 秒，超时或异常不回滚已提交的创建/切换；已经发生的外部副作用需用户自己按关联 ID 幂等处理。

安装不补发旧关联，卸载只撤销 USER 订阅，原业务继续。断开 Gateway、ui.ready、输入意图和单纯隐藏/空闲不是 attach；没有物理在线检测、自动 detach 或 Team/子 Agent 通知。本地关联表不跨 Worker 重启保存，旧接口省略 view_id 的降级值不能区分多个窗口。

字段、完整调用链和验收范围见 [F_25](../../docs/features/F_25_session-attach.md)、[S_14](../../docs/specs/S_14_session-attach.md)。

## English

Install manifest.yaml and extension.py as an extension ZIP. The existing Web session.create/session.switch flow already sends a stable view_id. After the Runtime commits a valid main-Session view association, this USER handler reads the bound project root, continues the notification chain and logs its actual CORE receipt.

The host provides immutable input fields and SessionContext. No custom Adapter, EventBus, Builder, new Ops or frontend request is required. surface_id identifies a concrete view, rather than a Session, A2UI card or extension slot. A repeated association is deduplicated; another view or switching away and back creates a distinct notification.

There is no chat execution or automatic UI rendering. Acknowledgement confirms the current association, not every user's external initialization. Missing workspace facts remain unknown; saved model information does not prove activation. Notifications have a two-second wait budget and do not roll back committed business state on failure. Make external effects idempotent, and see F_25/S_14 for reconnect, stale-target, cancellation and evidence boundaries.
