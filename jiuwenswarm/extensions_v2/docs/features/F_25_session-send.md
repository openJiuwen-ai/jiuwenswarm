# session.send 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_25 / session-send |
| 归档日期 | 2026-11-02 |
| 关联计划 | [EXTV2-23 / 跨会话发送前扩展](../designs/extensions-v2-development-plan.md#extv2-23-跨会话发送前扩展) |
| 主责规约 | [S_13](../specs/S_13_session-send.md) |

## 1. 范围、状态与依据

本文设计现有 `session_send_message` 工具所调用的 `SessionMessageService.send_message()` 在跨会话消息发出前的扩展点。实现和验证记录见第 6 节；未完成验证前不将设计写成验收通过。

扩展事件在 Worker 内运行，不改 Front/Supervisor 路由，不增加前端页面或新的发送工具，不新增 `ops.session.send()`。平台先检查源和目标的真实会话归属、支持模式及消息参数，然后由本事件的 CORE 接入目标 `session.receive`，最终继续原有持久消息队列。成功只代表保存并排队，目标模型执行仍异步发生。

参考 Claude Code Mods 2.1.277、固定提交 `80c6959d28c4c30ecd14fc1cdf172f19d9adad81` 的声明。该版本明确把 `session.send` 列为 `session.receive` 的预留对偶，**没有 SessionSendInput / SessionSendResult 或已开放 send 方法**。以下发送字段为 JiuwenSwarm 对现有平台方法的适配，不能称为与 Claude 完全兼容。

## 2. 能力说明与 Claude 对照

例如会话 A 的项目助手要向会话 B 发送“检查订单，联系电话 13800138000”。发送方 USER Handler 可在发送前把号码替换为 `[phone]`，也可返回 `SessionSendDenied(deny="需要人工发送")` 阻止本次发送。放行后，目标的 receive Handler 仍有独立的改写和消费机会；两条链不是同一个 Handler 的上下两半。

| 项 | 本次设计 | 与固定 Claude 定义的关系 |
| --- | --- | --- |
| 触发语义 | 源会话发往另一会话、进入目标持久队列之前 | 实现其预留的发送前扩展意图；Claude 未公开此事件的字段 |
| 字段命名 | text；目标为 target_session_id；投递模式为 input_mode | text 与 receive 对齐；路由、模式沿用本地平台接口 |
| 可改内容 | 仅 text | 不允许通过 Hook 改接收人、用户归属、工具调用 ID 或重试键 |
| 短路 | 返回带非空 deny 的专用对象 | 本地新增明确业务拒绝，不冒充 Claude 已实现能力 |
| 成功回执 | 原平台 accepted/message_id/status 等字典 | 仍是本地投递回执，不是目标模型的答案 |

## 3. 事件契约

### 3.1 触发前后与粒度

```text
模型调用既有 session_send_message(target_session_id, message, input_mode)
  → SessionMessagingToolkit 从宿主绑定取得源会话、请求和真实 tool_call_id
  → SessionMessageService.send_message：原参数、会话归属、模式和跳数校验
  → 平台从已校验元数据构造源会话 SessionContext；保存私有调用状态
  → await Worker bus.trigger(Event("session.send", input), source_context)
  → USER：查看/改写 text，或在 next 前返回 SessionSendDenied
  → await next_handler(原事件或带新 text 的事件)
  → 其余 Handler → session_send_core：复验绑定身份和保护字段
  → CORE 调用平台 continuation → 目标 session.receive 链
  → receive CORE → 原持久队列入队，生成真实回执并异步投递
  → 真实回执沿 receive → send → 业务方法 → 工具返回
```

本事件没有新增 BUILTIN Handler。现有共享 Ops 仍由启动时的 ops.build / Builder 登记，并为每次匹配的 USER 绑定**源会话** SessionContext；用户可以查询 cwd/model 等信息，但查询不会自动发送。CORE 使用平台保存的 continuation 完成发送，不让用户重新调用业务方法来模拟放行。

### 3.2 输入：每个字段表达什么、由谁填写

公开类型为 `jiuwenswarm.extensions_v2.event_types.session_send.SessionSendInput`。

| 字段 | 类型 / 是否必有 | 准确含义 | 应填写什么、例子与用途 | Handler 权限 |
| --- | --- | --- | --- | --- |
| target_session_id | str，必有 | 本次已通过归属校验的接收会话 ID | 平台从业务参数填写，例如 `target-1`；用户可用作 matcher，只把消息送给该会话 | 只读，不能改目标 |
| text | str，必有 | 将交给目标接收链的完整文本正文 | 平台从 message 规范化得到；改写时填写新的正文，例如 `请检查订单，电话 [phone]`；UTF-8 1～32768 字节且不能全空白 | 可用 replace 改写 |
| input_mode | str，必有 | 原发送请求的投递策略，steer 或 follow_up | 平台填写；steer 尝试补充当前任务，follow_up 排队独立执行；内部旧空字符串对外显示为 follow_up | 只读，不能通过改写改变执行策略 |

源会话 ID、user_id、request_id、tool_call_id、idempotency_key、chain_id、parent_message_id 和 hop_count 由原平台对象持有，不在可改输入中重复提供。用户不能填写 SessionContext 冒充另一个会话。

### 3.3 输出：成功与拒绝

`await next_handler(event)` 的成功结果是原平台回执字典。USER 若不调用 next，只能返回 `SessionSendDenied(deny="原因")`；伪造 accepted=True 不会执行发送。

| 字段 | 何时出现 / 类型 | 含义、如何消费和例子 |
| --- | --- | --- |
| accepted | 总有，bool | True 表示真实入队；False 表示被发送或接收规则拦截。不能把 True 当目标已完成 |
| target_session_id | 总有，str | 实际固定的目标，例如 target-1；不会取用户伪造的路由 |
| message_id | 入队成功，str | 原 SQLite 消息行 ID；后续状态查询使用它，不等同于请求 ID |
| status | 入队成功，str | 原平台当时的投递状态；通常 queued，重试可能返回该行的后续状态 |
| deduplicated | 入队成功，bool | True 为同一重试键已存在，不新增第二行 |
| input_mode | 入队成功，str | 原持久化策略；steer 或旧兼容空字符串，空字符串代表独立任务 |
| code | 拦截时，str | SESSION_SEND_DENIED 或 SESSION_RECEIVE_CONSUMED，区分在哪一侧被拦截 |
| error | 拦截时，str | 给调用者解释的非空原因；不是异常堆栈 |
| deny | 发送方拒绝时，str | 用户填写的发送拒绝原因，去首尾空白后 1～512 字符 |
| consumed | 接收方消费时，str | 目标 receive Handler 填写的消费原因；此时没有消息行、目标不会执行 |

CORE 已入队后，USER 在返回阶段改写回执或返回拒绝，不能撤销入队事实。业务入口使用保存的真实回执。未捕获异常和非法返回按既有 Core USER 故障隔离处理，不能当作可靠的业务拒绝。

### 3.4 注册、筛选、改写示例

```python
from dataclasses import replace
from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.session_send import SessionSendDenied

class Extension:
    def register(self, registrar):
        registrar.register_event_handler("session.send", self.on_send, {}, "user")

    async def on_send(self, event, ops, next_handler):
        value = event.event_input
        if value.text.startswith("manual-only:"):
            return SessionSendDenied(deny="这类请求需要人工发送")
        rewritten = replace(value, text=value.text.replace("13800138000", "[phone]"))
        return await next_handler(Event(event.event_type, rewritten))

def create_extension():
    return Extension()
```

## 4. 方法契约

### 4.1 复用既有发送方法

```python
receipt = await service.send_message(
    source, target_session_id="target-1", message="请检查订单", input_mode="steer",
)
```

这是**平台方法**；扩展用户通过上述 Handler 接入，模型继续使用既有 session_send_message 工具。source 为宿主绑定的 SessionMessageSource，不要求用户构造它，也不增加公开 Ops。

| 业务参数 | 谁填写 / 应填什么 | 与事件字段的关系 |
| --- | --- | --- |
| source | 平台/工具 Rail 填写真实源身份及稳定调用键 | 固定本次源上下文及幂等身份，不允许 USER 改写 |
| target_session_id | 原调用者选择，须为同一用户支持的已有会话 | 校验后进入事件同名字段 |
| message | 原调用者填写完整非空文本，最多 32 KiB UTF-8 | 去首尾空白后成为 text；规则改写仍受同一上限约束 |
| input_mode | 默认 steer；follow_up 或旧空字符串表示独立任务 | 对外事件统一 steer/follow_up；原存储格式保持 |

### 4.2 兼容与迁移

无扩展或装配未启用时沿用原行为。已有工具名、参数、源授权、SQLite Schema、跳数限制和成功回执不变；启用新 Handler 后新增两种明确的 accepted=False 业务结果。原队列中的消息不回放本次发送事件，重试调用则重新经过规则，最终由原幂等键去重。

## 5. 接入与兼容方案

event_types 只定义类型、校验与注册；平台 session_message_events 收集可信上下文并 trigger；event_handlers/session_send.py 的 CORE 调用平台 continuation；原 SessionMessageService 继续负责持久化、锁内目标复查、通知和后台消费。

Hook 在原队列锁之外运行，避免规则等待时阻塞整个目标。CORE 后仍在原锁内检查目标是否删除/换归属。每次调用保存真实回执，重复 next 不重复入队；取消遵循原入队/通知边界，不承诺取消就撤销已经保存的行。包卸载撤销 USER，平台 CORE 与已经排队的数据保留。

不直接在 EventBus 中信任裸 trigger 构造的 send 输入；没有真实平台私有调用状态不能获得发送回执。嵌套 Hook 重新发送受重入保护，正常 next 不触发重入。

## 6. 验收与未决问题

开发前方案：待执行实际 Registry / EventBus / USER / CORE / SQLite 验证，覆盖发送改写/拒绝、接收改写/消费、保护字段、假回执、重复 next、幂等、取消、归属和跨会话隔离。最终记录将区分单元、独立进程与真实模型，未执行项不写成通过。

明确范围：本次接入持久跨会话消息服务，不承诺拦截所有普通 Web 输入、Team 内部消息、cron 或独立 AgentRuntime.send_session_message 入口。Claude 预留 send 不具备可核对的正式 Schema，后续官方开放时需另行评估迁移。

## 7. 参考资料

- [S_13 主责规约](../specs/S_13_session-send.md)、[接收特性](F_26_session-receive.md)。
- [既有发送工具](../../../agents/harness/common/tools/session_messaging_toolkit.py)、[平台消息服务](../../../server/runtime/session/session_message_service.py)。
- [Claude 固定类型定义](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts)：CoreEventOf 的 session.receive 注释明确 session.send 为 reserved。
