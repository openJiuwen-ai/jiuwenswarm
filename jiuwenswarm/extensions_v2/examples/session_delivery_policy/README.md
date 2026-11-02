# Session Delivery Policy 用户扩展示例

该包只注册 `session.send` 与 `session.receive` 的 USER Handler，观察原有持久跨会话发送工具/业务方法，不需要用户修改平台 Adapter 或手工构造 SessionContext。示例不调用 ui.render。

## 安装与触发

按既有 `extensions.v2.install` 管理入口安装本目录或其平铺 ZIP。准备同一用户的两个受支持持久会话 A/B，使用原有 `session_send_message` 工具向 B 发送文本，或由平台调用原 `SessionMessageService.send_message()`；不新增 Ops、前端操作或第二个发送工具。

完整链路：

```text
源 A 的原工具/业务方法
  → 平台校验参数、源/目标归属与模式
  → 构造 A 的可信 SessionContext
  → session.send USER：脱敏或拒绝 → next → send CORE
  → 构造 B 的可信 SessionContext
  → session.receive USER：消费或放行 → next → receive CORE
  → 原 SQLite 队列、幂等键、通知和异步执行
  → 实际队列回执沿 send 链返回原调用者
```

## 三个可观察案例

| 原调用 message | USER 处理 | 应观察到什么 |
| --- | --- | --- |
| `请联系 13800138000` | send 把演示号码替换为 `[phone]` | accepted=True；原消息库及后续执行输入保存 `请联系 [phone]` |
| `[manual] 请创建发布任务` | send 返回 SessionSendDenied | accepted=False，code=SESSION_SEND_DENIED，deny/error 为原因；没有消息行、receive 或目标执行 |
| `[ignore] 构建任务已由插件处理` | receive 返回 SessionReceiveConsumed | accepted=False，code=SESSION_RECEIVE_CONSUMED，consumed/error 为原因；没有消息行或目标执行 |

查询原持久消息服务返回的 message_id/status 可观察普通投递；accepted=True 只代表已保存并排队，不是模型已经完成。故意抛异常或返回假的成功会被 Core 隔离，明确的拒绝/消费必须使用上表专用返回对象并在 next 前返回。

send Handler 的 ops 绑定 A，receive Handler 的 ops 绑定 B。用户可调用已有只读查询；event_types 只维护定义、校验和注册，真实 trigger 在平台 session_message_events，CORE 负责衔接原队列。两条业务链没有新增专用 BUILTIN。

当前生产入口只覆盖原持久跨会话服务，不以此示例宣称普通 Web query、cron、relay 或独立 Runtime 发送入口已接入。卸载示例只撤销用户规则，保留平台 CORE 和已经排队的数据。

具体字段、填写方式、结果含义及验证见 [F_25 session.send](../../docs/features/F_25_session-send.md)、[F_26 session.receive](../../docs/features/F_26_session-receive.md)；公共约束见 [S_13](../../docs/specs/S_13_session-send.md)、[S_14](../../docs/specs/S_14_session-receive.md)。Claude 公开定义中 send 仍为 reserved，本地发送字段及差异在 F_25 明确记录。
