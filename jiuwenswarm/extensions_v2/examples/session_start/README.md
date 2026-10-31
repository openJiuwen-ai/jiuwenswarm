# 激活初始化示例 / Activation start example

## 中文

此包在 Worker 中首次遇到受支持的原生 chat.send 输入时，通过 session.start 初始化自己的 workspace 字典；随后已有 context.usage.updated Handler 使用该状态并输出 ready/count 日志。用户只提供 manifest、module 和 USER Handler，平台负责选择激活实例、收集可信上下文、触发事件及提供默认 CORE。需要包含 handler_ids 限定派发能力的配套 agent-core，不能用旧 Core 版本验证此特性。

在当前目录打包，保证两个文件位于 ZIP 根目录：

```powershell
Compress-Archive -LiteralPath .\manifest.yaml, .\extension.py -DestinationPath .\session-start.zip
```

将 ZIP 放在服务端可读的绝对路径后，使用已有 Web 命令：

```text
/extension install "D:\extensions\session-start.zip"
/extension query example.session-start
```

安装完成只发布 Handler。正常发送原生 Agent 消息后，在模型输入开始前出现 `session start example:`，其中 count=1、generation 为当前包激活代次，cwd/root/model 来自真实原生请求的运行时绑定。回合产生有效用量后，`session start example usage:` 应显示 ready=True count=1。

```text
启动：平台注册 session.start / Validator / CORE，已有 ops.build 登记共享 Ops
运行：权限准入 → 模型和目录绑定 → Manager 选择未通知的激活实例
  → 使用其公开发布 ID 定向触发 Worker EventBus
  → 当前包 on_start 查询 cwd/root/model，准备自己的内存状态
  → next_handler → 默认 CORE 返回真实接收回执
  → 初始化门返回 → 发送原 Agent 输入
  → 后续用量事件中使用已准备状态
```

普通下一轮、切换到同一 Worker 的另一个会话或界面重连不重复初始化。初始化上下文属于触发它的首个输入，不表示每个后续会话都有独立 workspace 缓存；需要每会话状态时，用户应另按 session_id 管理自己的资料。新包只初始化新包；成功重载后，新模块实例在下一次受支持输入前重新准备状态，count 仍从 1 开始。

```text
/extension reload example.session-start
/extension uninstall example.session-start
```

事件输入全为只读观察字段，返回值不改变运行目录或真实目标。用户应保持 Event 并返回 next 的实际回执；没有 consumed/rejected 初始化协议。USER 异常按 Core 隔离，不保证用户资源已成功准备。平台只尝试通知一次，普通输入重试、框架失败或取消都不自动重放已开始通知；需要新尝试应重新激活包。已创建的外部副作用不由平台回滚。

本例使用空 matcher。matcher 只决定这一次通知是否运行，不会把不匹配的初始化留到以后某轮再触发。本项覆盖原生 chat.send 的 unary/stream 输入；控制/历史/重连、Team、Auto-Harness 和外部 Harness 未新增接线。无新 Ops、额外 Agent 输入或前端渲染。

完整字段与边界见 [S_12](../../docs/specs/S_12_session-start.md)，设计、调用链和实际验证见 [F_22](../../docs/features/F_22_session-start.md)。

## English

Package manifest.yaml and extension.py at the ZIP root and install through `/extension install`.
This requires the paired agent-core support for the optional trigger handler_ids allowlist.
On the first supported native chat.send input, the platform waits for session.start after trusted
model/workspace binding and before sending Agent input. The USER Handler prepares activation-owned
memory using shared cwd/root/model Ops, then continues the default CORE receipt. A later usage
notification logs ready=True and count=1.

The owner is one package activation in one Worker, not each conversation, request or connection.
Further inputs and reconnects do not rerun it. New packages do not rerun old packages' empty matchers.
A successful reload publishes a new instance, initialized on the next supported input; uninstall
removes the package's handlers. The first input supplies observation facts, not a workspace cache
for every later session. Use session_id explicitly for user-owned per-session state.

All input fields are observations. Keep the Event and return next_handler's real receipt; there is
no consumed/rejected protocol and no change of the actual cwd. Core isolates USER failures. An accepted
receipt is not proof that every user's external resource initialized successfully. A started attempt
is not automatically replayed after failure/cancellation or normal input retries; reactivate to retry.
Platform code does not roll back user side effects. Matchers filter the one attempt, not defer it.

Only native chat.send unary/stream paths are wired. No new Team, Auto-Harness, external-Harness,
control, history, reconnect or frontend route is added, and no new Ops or Agent input is created.
See F_22/S_12 for the complete contract and evidence scope.
