# 编写自定义 DeepAgentRail

本文是 [Harness.md § 3.2 Rail Lifecycle：生命周期扩展机制](./Harness.md#32-rail-lifecycle生命周期扩展机制) 的实践补充。Harness.md 说明了 Rail 的用途；本页说明如何编写、注册、热挂载和测试一个 Rail。

以下每条 API 结论都对照本仓库 `uv.lock` 固定的 `openjiuwen` 版本（`0.1.18`）核实过。引用只标注定义所在的模块与符号，不写行号，以免随依赖升级失效。源码未能确认的地方标记为「未验证」。

面向生成式 Agent 包的中文骨架说明已存在于 `jiuwenswarm/resources/agent/workspace/skills/agent-creator/references/rail-spec.md`；本页是它背后更完整的参考。

## 1. 基类

两个基类需要区分：

- `openjiuwen.core.single_agent.rail.base.AgentRail` —— 所有 Agent 共享的标准 hook。
- `openjiuwen.harness.rails.base.DeepAgentRail` —— 继承 `AgentRail`，为 DeepAgent 的外层 task loop 额外增加两个 hook。

编写 Harness / DeepAgent 场景的 Rail，请继承 `DeepAgentRail`：

```python
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext


class MyRail(DeepAgentRail):
    priority = 60  # 数值越大越先执行；基类默认值 50

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        ...
```

`DeepAgentRail.__init__` 会设置 `self.sys_operation = None` 和 `self.workspace = None`，并提供 `set_workspace()` / `set_sys_operation()` 两个 setter（`openjiuwen/harness/rails/base.py`）。如果重写 `__init__`，务必调用 `super().__init__()`。

## 2. 生命周期 hook 及其触发顺序

一次 DeepAgent 调用中，hook 按以下顺序触发（依据 `openjiuwen/core/single_agent/rail/base.py` 的 `AgentCallbackEvent` 与 `@rail` 装饰器，以及 `openjiuwen/harness/rails/base.py` 的 `DEEP_EVENT_METHOD_MAP`）：

1. `init(agent)` —— 普通同步方法，不是通过回调框架触发的事件；Rail 注册时调用一次，早于任何回调（`AgentRail.init`，由 `init_rail()` 调用）。
2. `before_invoke` → …… → `after_invoke` —— 包裹整个 `agent.invoke()` 调用（`BEFORE_INVOKE` / `AFTER_INVOKE`）。
3. 外层 task loop 的每次迭代（仅 DeepAgent，来自 `DeepAgentRail`）：`before_task_iteration` → …… → `after_task_iteration`。
4. 内层 ReAct 每一轮，依次为：
   - `on_user_message` —— 每批被消费的输入在被拼接成一条 `UserMessage` 之前触发一次；Rail 可以就地修改 `ctx.inputs.parts`（`UserMessageInputs`）。
   - `before_steering_drain` —— 仅在有 steering 消息排队时触发；Rail 通过 `ctx.inputs.limit` 限制本次模型调用消费的数量（`SteeringDrainInputs`）。
   - `before_model_call` → （LLM 调用）→ `after_model_call`；失败则触发 `on_model_exception`。
   - 模型请求的每个工具调用：`before_tool_call` → （工具执行）→ `after_tool_call`；失败则触发 `on_tool_exception`。
   - `after_react_iteration` —— 一整轮 ReAct（LLM + 全部工具调用 + `ToolMessage` 写入）成功完成后触发；任何中断路径都**不会**触发（`AgentCallbackEvent` docstring）。
5. `uninit(agent)` —— 普通同步方法，Rail 被卸载时调用（`AgentRail.uninit`，由 `openjiuwen/core/single_agent/base.py` 的 `BaseAgent.unregister_rail` 调用）。

Harness.md 的表格（§ 3.2）以更概括的方式覆盖了同样的五组配对：`before_invoke`/`after_invoke`、`before_task_iteration`/`after_task_iteration`、`before_model_call`/`after_model_call`、`before_tool_call`/`after_tool_call`、`on_model_exception`/`on_tool_exception`。本页补充了该表格未列出的三个 hook：`on_user_message`、`before_steering_drain`、`after_react_iteration`。

只需重写用得到的 hook。`get_callbacks()` 会检查子类，只注册与基类空实现不同的方法（`AgentRail._is_base_method`；`DeepAgentRail._is_deep_base` 对额外的两个 hook 做同样判断）。

## 3. 回调上下文（`AgentCallbackContext`）

每个 hook 都会收到同一个 `AgentCallbackContext` dataclass 实例（`openjiuwen/core/single_agent/rail/base.py`）。已核实的字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `agent` | `BaseAgent` | Agent 实例 |
| `event` | `Optional[AgentCallbackEvent]` | 由 `fire()` 在 hook 执行前设置 |
| `inputs` | `EventInputs` | 按事件而定的类型，见 § 4 |
| `config` | `Any` | 运行时配置 |
| `session` | `Optional[Session]` | 当前会话 |
| `context` | `Optional[ModelContext]` | 当前模型上下文 |
| `extra` | `Dict[str, Any]` | 跨 Rail 的临时空间，**在一次 `invoke()` 的所有事件之间保持**；从 `before_tool_call` 向 `after_tool_call` 传数据通常走这里 |
| `exception` | `Optional[Exception]` | 在 `on_model_exception` / `on_tool_exception` 时设置 |
| `retry_attempt` | `int` | 当前失败尝试的序号 |
| `retry_history` | `List[RetryRecord]` | 本次 invoke 的每一次失败尝试，含 `attempt_index`、`exception_type`、`exception_message`、`timestamp` |
| `context_usage_report` | `Optional[Any]` | 请求级 token 报告，由前后两个 model-call hook 共享 |
| `context_usage_request_id` | `Optional[str]` | 请求级 context usage 事件 id |
| `context_usage_sequence` | `Optional[int]` | 请求级执行序号 |
| `context_usage_attribution` | `Dict[str, Any]` | 请求级归属与调用元数据 |
| `invoke_start_time` | `float` | 单调时钟起始时间 |

另有几个值得知道的方法：

- `ctx.bind_steering_queue(queue)`、`ctx.push_steering(msg)`、`ctx.drain_steering(limit=None)`、`ctx.has_pending_steering()`、`ctx.steering_queue` —— steering 消息管线。队列非空时 `drain_steering` 至少取走一条，即使 `limit` 更小，保证消费一定有进展。
- `ctx.request_model_continue()` / `ctx.consume_model_continue_request()` —— 一次性请求：在模型返回文本而非真正工具调用时，额外再跑一轮 ReAct 模型迭代。它不会绕过 Agent 的迭代预算。
- `ctx.lifecycle(before, after)` —— 异步上下文管理器，进入时触发 `before`，在 `finally` 中触发 `after`，并在前后保存/恢复 `ctx.inputs`。属于内部管线，Rail 一般不直接调用。

`ctx.inputs` 的属性**并非各事件通用** —— 具体类型取决于 `ctx.event`，见 `EventInputs` 联合类型。请先判断事件，或用 `getattr(ctx.inputs, "tool_name", "")` 这样的防御式读法。

## 4. 各事件的输入类型

已核实的 dataclass（`openjiuwen/core/single_agent/rail/base.py`）：

- `InvokeInputs`（before/after invoke）：`query`、`conversation_id`、`result`（调用后填充）、`run_kind`、`run_context`、`parent_session_id`，以及血缘字段 `invocation_id`、`parent_invocation_id`、`delegation_id`、`agent_path`、`depth`。辅助方法：`is_heartbeat()`、`is_cron()`、`is_lightweight_context()`。
- `ModelCallInputs`（before/after model call）：`messages`、`tools`、`model_context`、`response`（调用后填充）、`context_usage_report`、`context_usage_request_id`、`context_usage_sequence`、`context_usage_attribution`、`react_iteration`（内层循环轮次，从 1 开始）。
- `ToolCallInputs`（before/after tool call）：`tool_call`、`tool_name`、`tool_args`、`tool_result`（调用后填充）、`tool_msg`（调用后填充）、`react_iteration`。
- `TaskIterationInputs`（before/after task iteration）：`iteration`（从 1 开始）、`loop_event`、`conversation_id`、`result`、`query`（Rail 可在 `before_task_iteration` 改写它，从而改变内层 Agent 看到的 query）、`is_follow_up`、`run_kind`、`run_context`。
- `UserMessageInputs`（`on_user_message`）：`parts: list[str]`（可变，就地编辑）、`source: str`，取值为 `"query"`、`"steering"`、`"resume"`。
- `SteeringDrainInputs`（`before_steering_drain`）：`pending: int`、`limit: int | None`。

## 5. `priority`

`AgentRail.priority: int = 50` 是类属性，**数值越大越先执行**。一个数字同时决定两件事：

- **init 顺序**：Rail 按 priority 顺序初始化，所以一个 Rail 若需要在自己 init 时就能用到另一个 Rail 注册的工具或 prompt section，它的 priority 必须比后者**更低**。例如 `SysOperationRail.priority = 100`，很早就注册好文件系统/Shell 工具（`openjiuwen/harness/rails/sys_operation_rail.py`）。
- **回调顺序**：`AgentCallbackManager.register_rail()` 以 `rail.callback_priority(event)` 注册每个 hook 方法（`openjiuwen/core/single_agent/agent_callback_manager.py`），因此在同一条 hook 链里（比如所有 Rail 的 `before_tool_call`），priority 高的先执行。

priority 相同的 Rail 保持添加顺序（`AgentRail` docstring）。

`callback_priority(event)` 默认返回 `self.priority`。当某个 Rail 需要把单个 hook 排在与其 init 位置不同的地方时再重写它 —— 典型场景是：想**读取**同一 hook 上其他回调产生的结果，但又不想把自己的 init 也排到它们之后。

`SysOperationRail` 是高 priority 的典型：它只在 init 阶段工作，在 `init()` 里注册文件系统/Shell 工具，不参与任何 hook，声明 `priority = 100`，于是所有需要这些工具的 Rail 都用更低的 priority（见其类 docstring）。本仓库的 `CircuitBreakerRail` 声明 `priority = 95`，高于默认值 50，因此能在低 priority 的 Rail 处理之前先看到工具结果（`jiuwenswarm/agents/harness/common/rails/execution_guard/circuit_breaker_rail.py`）。

## 6. `request_retry()` 与 `request_force_finish()`

两者都在 `AgentCallbackContext` 上。

### `request_retry(delay_seconds: float = 0.0)`

- **预期调用位置**：`on_model_exception` / `on_tool_exception` hook 内部（方法 docstring）。
- **机制**：设置内部的 `RetryRequest`。包裹底层 model call / tool call 的 `@rail(...)` 装饰器会在 `except` 块中检查它：存在则 sleep `delay_seconds`（负值会被钳到 `0.0`）后回到循环重试被包裹的调用。每次失败尝试都会让 `ctx.retry_attempt` 递增，并向 `ctx.retry_history` 追加一条 `RetryRecord`。
- **生效条件**：只在 `@rail` 装饰调用的 `on_exception` 阶段请求才有效。重试循环开头的 `ctx.consume_retry_request()` 会丢弃上一次尝试遗留的请求，所以每一次失败都要重新调用 `request_retry()` 才会继续重试。
- **与 `after_tool_call` 的关系**：一旦排队了重试，外层 `finally` 会跳过本次失败尝试的 `AFTER_TOOL_CALL`，避免 `on_tool_exception` 写入的中间态 `tool_result` 被当作最终结果消费。`after_model_call` 没有这个跳过 —— 每次尝试（包括即将重试的失败尝试）都会触发，因为做取消检测的回调依赖它每次都触发（装饰器内代码注释）。

### `request_force_finish(result: Dict[str, Any])`

- **可在任意 hook 中调用** —— docstring 举的例子是 `before_model_call` 和 `after_tool_call`。
- **机制**：设置 `ForceFinishRequest`。`ctx.has_force_finish_request`（property）与 `ctx.consume_force_finish()` 用于读取和清除。
- **生效条件**：`@rail` 装饰器在触发 `before` 事件之后、调用被包裹的方法体之前检查 `ctx.has_force_finish_request` —— 一旦存在，**方法体被整体跳过**，直接返回 force-finish 的结果。因此在 `before_*` hook 中调用它，会抢先取消本该发生的模型调用或工具调用。装饰器同样承认从 `on_exception` hook 发出的 force-finish：返回该结果而不再抛出异常。而在 `after_*` hook 中调用时，信号会被记录，但被包裹的调用已经执行完毕 —— 按 docstring 所述，Agent 循环会「在每个 railed 操作之后」检查该信号，这正是 Rail 在检查完模型响应或工具结果之后再强制结束的方式。本仓库的 `CircuitBreakerRail.after_tool_call` 就是一个可运行的例子。

## 7. 添加 prompt section（`system_prompt_builder`）

`SystemPromptBuilder` 和 `PromptSection` 定义在 `openjiuwen.core.single_agent.prompts.builder`，并从 `openjiuwen.harness.prompts` 再导出；两条导入路径指向同一个类。

`PromptSection(name, content: dict[str, str], priority: int = 100, category=None, carrier="system_message")` —— `content` 以语言码为 key（`"cn"`、`"en"`），`render(language)` 在缺少所请求语言时先回落到 `DEFAULT_LANGUAGE = "cn"`，再回落到第一个可用语言。

`SystemPromptBuilder.build()` 按有效 priority **升序**排序（`get_section_sort_key`），用 `"\n\n"` 拼接渲染结果并跳过空白 section。注意方向与 `AgentRail.priority` 相反：`PromptSection.priority` 数值**越小**越靠前。本仓库把 section 的优先级集中在一处 —— `jiuwenswarm/agents/harness/common/prompt/priority_registry.py` 的 `SystemPromptPriority` —— 而不是把魔数散落在各个 Rail 里。

Agent 通过 `agent.system_prompt_builder` 暴露当前 builder，且该实例与内层 ReAct Agent 共享，所以 Rail 在 `BEFORE_MODEL_CALL` 之前对它的修改会体现在这次 LLM 调用中。典型写法，参考本仓库的 `RuntimePromptRail`（`jiuwenswarm/agents/harness/common/rails/runtime_prompt_rail.py`）以及 `openjiuwen` 的 `TaskPlanningRail` / `SysOperationRail`：

```python
def init(self, agent) -> None:
    self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

async def before_model_call(self, ctx) -> None:
    if self.system_prompt_builder is None:
        return
    self.system_prompt_builder.add_section(
        PromptSection(
            name="my_rail_section",
            content={"en": "...", "cn": "..."},
            priority=80,
        )
    )

def uninit(self, agent) -> None:
    if self.system_prompt_builder:
        self.system_prompt_builder.remove_section("my_rail_section")
```

`add_section` 会覆盖同名 section；`remove_section` 对不存在的名字是空操作。务必在 `uninit()` 中移除自己的 section，避免热卸载后留下残留内容。

## 8. 热挂载：`register_rail` / `unregister_rail`

`BaseAgent.register_rail(rail)` 与 `BaseAgent.unregister_rail(rail)`（`openjiuwen/core/single_agent/base.py`）是异步方法，Agent 存在之后的任何时刻都可以调用，不限于构造期：

```python
await agent.register_rail(MyRail())
...
await agent.unregister_rail(my_rail_instance)
```

`register_rail`：

1. 调用 `init_rail(rail, agent)`，同步执行并计时 `rail.init(agent)`。
2. 调用 `AgentCallbackManager.register_rail(rail, agent)`，把 `rail.get_callbacks()` 返回的每个被重写的 hook 以 `rail.callback_priority(event)` 注册。

`unregister_rail`：

1. 调用 `AgentCallbackManager.unregister_rail(rail, agent)`，注销该 Rail 的每个回调方法。
2. 同步调用 `rail.uninit(agent)`。

因为 `init` / `uninit` 是普通同步方法而非通过回调框架触发的事件，无论其他 Rail 在做什么，它们都在每次 `register_rail` / `unregister_rail` 时恰好执行一次。这使它们成为 Rail 注册与注销自有工具、prompt section 的正确位置。

`DeepAgent` 在 `AgentMode` 切换时也会用同一套公开 API 内部热替换 Rail（`openjiuwen/harness/deep_agent.py`）。

一个 JiuwenSwarm 特有的注意点：通过 `RailManager` 加载的用户扩展 Rail，对主 Agent 按扩展名缓存实例，team 成员则通过 `RailManager.create_fresh_rail_instance()` 拿到各自的独立实例（`jiuwenswarm/agents/harness/common/plugins/rail_manager.py`）。编写 Rail 时要让「每个成员各有一份实例状态」这件事成立，不要假设 Rail 对象是进程级单例。

## 9. 通过内置 Rail Provider / 注册表注册

除了直接实例化并调用 `agent.register_rail(...)`，Harness 还支持**声明式**注册路径，使 Rail 可以按名字从配置中引用（`RailSpec(type=..., params={...})`），见 `openjiuwen/harness/schema/deep_agent_spec.py`：

```python
from openjiuwen.harness.schema.deep_agent_spec import register_rail_provider


def build_my_rail(params: dict, context) -> "MyRail":
    # context 是 BuildContext，携带 language、workspace 等
    return MyRail(**params)


register_rail_provider("my_rail", build_my_rail)
```

- `_RAIL_PROVIDER_REGISTRY` 是模块级 `dict[str, Callable[..., Any]]`。`register_rail_provider(name, factory)` 按名覆盖：同名注册两次，后者替换前者。
- `RailSpec.build(*, language, workspace=None, context=None)` 用 `self.type` 查注册表并调用 `factory(dict(self.params), context)`；工厂可以返回单个 Rail、Rail 列表或 `None`。未传 `context` 时会合成一个最小的 `BuildContext(language=language, workspace=workspace)`。未知的 rail type 只记一条 warning 并返回 `None`，其余 Rail 照常构建。
- 查表之前，`RailSpec.build` 会先调用 `openjiuwen.harness.manifest.ensure_builtin_elements_registered()`。Harness 自带的内置 Rail 正是这样进入同一张注册表的：它们通过 `harness_element(kind=ElementKind.RAIL, ...)` 目录装饰器声明一次，`register_from_catalog()` 再把类式构造器用 `class_rail_adapter` 包装（构造函数接受 `language` 时自动注入），逐个调用 `register_rail_provider`（`openjiuwen/harness/manifest/registration.py`、`openjiuwen/harness/manifest/introspect.py`）。自定义 Rail **不必**走这套目录机制，直接调用 `register_rail_provider` 就足以让它能被 `RailSpec` 按名引用。

如果集成方式是在 Python 里直接构造 Agent，用 `await agent.register_rail(MyRail(...))`（§ 8）更简单，完全不需要注册表。只有当 Rail 需要从序列化/声明式的 Agent 配置里被选中时，才需要走注册表这条路。

## 10. 测试 Rail

大多数 Rail 行为不需要模型就能测。直接构造 `AgentCallbackContext(agent=<桩对象>)`，用手工构造的输入 dataclass（`ModelCallInputs`、`ToolCallInputs`…… 见 § 4）调用 hook 方法即可。`tests/unit_tests/agentserver/` 下现有的 Rail 测试就是这个形态，既不需要真实 `DeepAgent`，也不需要打桩。

确实需要跑完整循环时，`Model.invoke` 是最窄的打桩边界。其签名为 `async def invoke(self, messages, *, tools=None, temperature=None, top_p=None, max_tokens=None, stop=None, model=None, output_parser=None, timeout=None, **kwargs) -> AssistantMessage`（`openjiuwen/core/foundation/llm/model.py`），因此只替换这一个方法，就能让 `before_model_call` / `after_model_call` 在真实 `DeepAgent` 上运行而不发起网络调用：

```python
from unittest.mock import patch

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.foundation.llm.schema.message import AssistantMessage


async def test_my_rail_force_finishes_on_violation():
    agent = build_test_deep_agent()  # 各自测试框架里的 Agent 构造方式
    rail = MyAuditRail()
    await agent.register_rail(rail)

    fake_response = AssistantMessage(content='{"claims": []}', tool_calls=None)
    with patch.object(Model, "invoke", return_value=fake_response):
        result = await agent.invoke("do something")

    assert result["result_type"] == "answer"
```

这里依赖的 `Model.invoke` 签名已核实，但 `tests/unit_tests/` 下目前没有测试以这种方式给它打桩，所以这一模式本身标记为**未验证为既有项目约定**。

## 11. 完整最小示例

一个对工具参数做哈希留痕、遇到策略违规就强制结束的审计 Rail：

```python
"""AuditRail：为审计留痕对工具参数做哈希，命中拒绝名单时强制结束 Agent 循环。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail


def _hash_args(tool_args: Any) -> str:
    payload = json.dumps(tool_args, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditRail(DeepAgentRail):
    """对每次工具调用的参数做哈希；命中拒绝名单则强制结束。"""

    priority = 65  # 晚于 SysOperationRail（100）初始化，其工具此时已注册

    def __init__(self, *, denied_tools: set[str] | None = None) -> None:
        super().__init__()
        self.denied_tools = denied_tools or set()
        self.audit_log: list[dict[str, str]] = []

    def init(self, agent: Any) -> None:
        # 本 Rail 不注册工具和 prompt section。
        pass

    def uninit(self, agent: Any) -> None:
        self.audit_log.clear()

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "")
        tool_args = getattr(ctx.inputs, "tool_args", None)
        self.audit_log.append(
            {"tool_name": tool_name, "args_sha256": _hash_args(tool_args)}
        )

        if tool_name in self.denied_tools:
            # 在 before hook 中请求，@rail 装饰器会整体跳过这次工具调用（见 § 6）。
            ctx.request_force_finish(
                {
                    "output": f"AuditRail blocked call to denied tool '{tool_name}'.",
                    "result_type": "answer",
                    "reason_code": "policy_violation",
                }
            )

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        # 偶发失败重试一次，再失败就放弃。
        if ctx.retry_attempt == 0:
            ctx.request_retry(delay_seconds=0.5)
            return
        ctx.request_force_finish(
            {
                "output": "AuditRail stopped the run after one retry failed.",
                "result_type": "answer",
                "reason_code": "recovery_exhausted",
            }
        )
```

注册与热挂载：

```python
audit_rail = AuditRail(denied_tools={"delete_all_data"})
await agent.register_rail(audit_rail)
...
await agent.unregister_rail(audit_rail)
```

## 核对来源

对照本仓库 `uv.lock` 固定的 `openjiuwen` 版本（`0.1.18`）阅读：

- `openjiuwen/core/single_agent/rail/base.py` —— `AgentCallbackEvent`、`AgentCallbackContext`、`AgentRail`、`@rail` 装饰器，以及各事件输入 dataclass。
- `openjiuwen/harness/rails/base.py` —— `DeepAgentRail`、`DEEP_EVENT_METHOD_MAP`。
- `openjiuwen/core/single_agent/base.py` —— `register_rail`、`unregister_rail`。
- `openjiuwen/core/single_agent/agent_callback_manager.py` —— `register_rail` / `unregister_rail` 与 priority 接线。
- `openjiuwen/core/single_agent/prompts/builder.py` —— `PromptSection`、`SystemPromptBuilder`。
- `openjiuwen/harness/rails/sys_operation_rail.py`、`openjiuwen/harness/rails/task_planning_rail.py` —— `init` / `uninit` 与 prompt section 的具体例子。
- `openjiuwen/harness/schema/deep_agent_spec.py` —— `RailSpec`、`register_rail_provider`、`_RAIL_PROVIDER_REGISTRY`。
- `openjiuwen/harness/manifest/registration.py`、`openjiuwen/harness/manifest/introspect.py` —— `class_rail_adapter`、`register_from_catalog`。
- `openjiuwen/core/foundation/llm/model.py` —— `Model.invoke` 签名。

本仓库内：

- `jiuwenswarm/agents/harness/common/rails/runtime_prompt_rail.py` —— 带 `init` / `uninit` 清理的 prompt section Rail。
- `jiuwenswarm/agents/harness/common/rails/execution_guard/circuit_breaker_rail.py` —— 在 `after_tool_call` 中 `request_force_finish`。
- `jiuwenswarm/agents/harness/common/prompt/priority_registry.py` —— `SystemPromptPriority`。
- `jiuwenswarm/agents/harness/common/plugins/rail_manager.py` —— 扩展 Rail 的加载与每成员实例。
