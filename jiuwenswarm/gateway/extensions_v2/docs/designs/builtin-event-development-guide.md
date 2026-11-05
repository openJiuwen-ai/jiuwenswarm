# Gateway 内置事件与 Ops 开发模板

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | 开发模板 |
| 适用对象 | 在 Gateway 侧新增事件类型、CORE Handler、BUILTIN Handler 或 Ops 能力的开发者 |
| 所属模块 | `jiuwenswarm/gateway/extensions_v2/` |
| 关联文档 | [详细架构](extensions-v2-architecture.md)、AgentServer [内置事件开发模板](../../../../extensions_v2/docs/designs/builtin-event-development-guide.md) |
| 最近一次修订日期 | 2026-11-04 |

本文按顺序说明一次完整的接入：定义输入输出 → 写 CORE Handler → 注册事件类型 → 按需加 BUILTIN → 开放 Ops → 接触发点 → 补测试与文档。示例中的代码是示意，落地时以实际实现为准。

## 1. 判断事件是否需要新增

先确认三件事，任一为否就不要新增事件：

1. 该处理点是否发生在 Gateway 进程内，且 AgentServer 尚未或不应参与？
2. 是否需要扩展包参与，而不是由 Gateway 内部写死？
3. 是否可以用已有的 `gateway.request` 或 Ops 方法表达？

例如"登录成功后刷新界面"不需要新事件，它是包 Handler 内的两次调用；"连接关闭时清理外部会话"如果需要包参与，才考虑新增事件。

## 2. 命名与位置

| 内容 | 约定 |
| --- | --- |
| 事件名 | `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`，现有：`gateway.request`、`ui.render`、`ui.store`、`ui.click` |
| 事件常量 | 放在 `models.py`，命名 `EVENT_*` |
| 输入输出 | 放在 `models.py`，`@dataclass(frozen=True, slots=True)` |
| 事件注册 | `event_types/<域>.py`，一个函数 `register_<域>_events(bus, ...)` |
| CORE Handler | `core_handlers/<域>_core.py`，一个工厂 `build_<域>_core_handler(...)` |
| BUILTIN Handler | `builtin/<规则>.py`，一个工厂 `build_<规则>_handler(...)` |
| Ops | `handler_ops/<域>.py`，一个 `build_<域>_ops_build_handler(bus, ...)` |
| 汇总入口 | 只在 `bootstrap.py` 引用，`runtime.py` 不直接拼装 |

## 3. 定义输入输出

```python
@dataclass(frozen=True, slots=True)
class NoteInput:
    """One inbound note from a browser connection."""

    text: str
    connection_id: str = ""
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class NoteOutput:
    """Handler chain answer."""

    handled: bool = False
    status: str = "ignored"
    code: str | None = None
```

要求：

- 冻结数据类，字段全部有默认值或有意义地必填；
- 只使用可序列化类型（str / int / bool / float / None / Mapping / list）；
- 不使用 `Any` 以外的业务对象，避免包与宿主类耦合；
- 错误用 `code` + `message` 表达，不抛异常给调用方。

## 4. 编写 CORE Handler

CORE Handler 是该事件的默认动作，必须做到"没有扩展时行为与接入前一致"。

```python
def build_note_core_handler() -> EventHandler:
    async def note_core(event: Event, ops, next_handler) -> NoteOutput:
        del ops, next_handler
        payload = event.event_input
        if not isinstance(payload, NoteInput):
            raise TypeError("note.received requires NoteInput")
        if not payload.text.strip():
            return NoteOutput(handled=True, status="rejected", code="EMPTY_TEXT")
        return NoteOutput(handled=True, status="accepted")
    return note_core
```

规则：

- 参数类型不符直接 `raise TypeError`，这是编程错误而非业务错误；
- 业务错误返回带 `code` 的结果；
- 不调用 `next_handler`：CORE 是链尾；
- 不访问包上下文，`ops` 在 CORE 层通常为 `None`。

## 5. 注册事件类型

```python
def register_note_events(bus: ExtensionEventBus, core_handler: EventHandler) -> None:
    bus.register_event_type(EVENT_NOTE_RECEIVED, match_pattern_subset, core_handler)
```

再在 `bootstrap.register_event_types` 中增加一行。`register_event_type` 必须在任何 Handler 登记之前执行；重复注册同名事件会触发 `EVENTS_REGISTRATION_INVALID`。

## 6. 增加 BUILTIN Handler

BUILTIN 承载浏览器协议分派、输入约束等平台规则，在包之后、CORE 之前执行。登录鉴权属于 USER 请求策略。以下为长度限制的示意实现：

```python
def build_note_guard() -> EventHandler:
    async def note_guard(event: Event, ops, next_handler) -> NoteOutput:
        del ops
        payload = event.event_input
        if len(payload.text) > 4096:
            return NoteOutput(handled=True, status="rejected", code="NOTE_TOO_LONG")
        return await next_handler(event)
    return note_guard
```

在 `bootstrap.register_handlers` 中登记并返回 handler id，供 `runtime.shutdown()` 注销。注意 BUILTIN 不计入 `runtime.active`：新增 BUILTIN 不会破坏零扩展快路径。

## 7. 开放 Ops 能力

Ops 通过 `ops.build` 的 BUILTIN Handler 注册，装配期执行一次：

```python
def build_note_ops_build_handler(bus: ExtensionEventBus) -> EventHandler:
    async def build_ops(event: Event, ops, next_handler) -> None:
        del next_handler
        builder = event.event_input
        if not isinstance(builder, EventHandlerOpsBuilder):
            return None
        builder.add_namespace("note")

        async def note_send(ops_view: EventHandlerOps, payload: NoteInput) -> NoteOutput:
            return await bus.trigger(Event(EVENT_NOTE_RECEIVED, payload), ops_view.session_context)

        builder.add_method("note", "send", note_send)
        return None
    return build_ops
```

规则：

- 同一命名空间只 `add_namespace` 一次；重复注册会触发 `EVENTS_OPS_INVALID`；
- Ops 方法用调用方的 `session_context` 触发新链，不推进当前链；
- Ops 方法内不要直接发帧，交给 CORE 统一处理；
- `ops.build` 之后能力目录冻结，运行时不再增删方法。

## 8. 接入触发点

| 触发点位置 | 说明 |
| --- | --- |
| `runtime.intercept` | 每帧原生分发前；已接入 `gateway.request` |
| `BrowserBridge.build_request_dispatch_handler` | 在 `gateway.request` 的 USER 策略继续后分派 application 槽位的 `webui.dispatch`，触发 `ui.*` |
| `runtime.on_disconnect` | socket 关闭；用于清理 connection 作用域 |
| `channel` 生命周期 | 启动装配与停机，分别由 `web_connect.py` 调用 |

新增触发点要求：

1. 放在 `runtime.py`，不要在渠道代码里直接 `bus.trigger`；
2. 设置超时并 fail-open，异常不得阻断原生路径；
3. 在 `__init__.py` 暴露稳定入口，渠道只依赖该入口。

## 9. 测试

| 场景 | 必测 |
| --- | --- |
| 无扩展 | 事件链仍返回 CORE 默认结果；`runtime.active` 为 False |
| USER 短路 | 包返回结果后 CORE 未执行 |
| BUILTIN 拦截 | 规则命中时 CORE 未执行，返回约定错误码 |
| Ops 调用 | `ops.build` 后方法存在，调用能到达 CORE |
| 异常与超时 | 帧仍走原生路径 |
| 停机 | handler 已注销，再次触发不执行 |

测试文件位于 `tests/unit_tests/gateway/extensions_v2/`，命名 `test_gateway_<能力>.py`。

## 10. 文档

| 文档 | 更新时机 |
| --- | --- |
| `docs/features/F_NN_*.md` | 新增能力或行为变化 |
| `docs/specs/S_NN_*.md` | 对外契约、错误码或边界变化 |
| [详细架构](extensions-v2-architecture.md) | 模块职责、依赖或流程变化 |
| [开发计划](extensions-v2-development-plan.md) | 新增或调整条目、更新进度 |
