# Writing a Custom DeepAgentRail

This guide is a practical companion to [Harness.md § 3.2 Rail Lifecycle](./Harness.md#32-rail-lifecycle-lifecycle-extension-mechanism). Harness.md explains *what* Rails are for; this page walks through *how* to write, register, hot-mount, and test one.

Every API detail below was checked against the `openjiuwen` revision this repository pins in `uv.lock` (version `0.1.18`). Claims cite the defining module and symbol rather than line numbers, so they stay readable as the dependency moves. Anything the source did not settle is marked **unverified**.

A shorter, Chinese-only skeleton aimed at generated agent packages already lives at `jiuwenswarm/resources/agent/workspace/skills/agent-creator/references/rail-spec.md`; this page is the fuller reference behind it.

## 1. Base classes

Two base classes matter:

- `openjiuwen.core.single_agent.rail.base.AgentRail` — the standard hooks shared by every agent.
- `openjiuwen.harness.rails.base.DeepAgentRail` — subclasses `AgentRail` and adds the two outer task-loop hooks used by DeepAgent.

For a Harness/DeepAgent rail, subclass `DeepAgentRail`:

```python
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext


class MyRail(DeepAgentRail):
    priority = 60  # higher runs first; the base class default is 50

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        ...
```

`DeepAgentRail.__init__` sets `self.sys_operation = None` and `self.workspace = None`, with `set_workspace()` / `set_sys_operation()` setters (`openjiuwen/harness/rails/base.py`). If you override `__init__`, call `super().__init__()`.

## 2. Lifecycle hooks, in firing order

A single DeepAgent invocation fires hooks in this order (`AgentCallbackEvent` and the `@rail` decorator in `openjiuwen/core/single_agent/rail/base.py`, plus `DEEP_EVENT_METHOD_MAP` in `openjiuwen/harness/rails/base.py`):

1. `init(agent)` — plain synchronous method, not an event fired through the callback framework; runs once when the rail is registered, before any callback can fire (`AgentRail.init`, invoked from `init_rail()`).
2. `before_invoke` → ... → `after_invoke` — wrap the whole `agent.invoke()` call (`BEFORE_INVOKE` / `AFTER_INVOKE`).
3. Per outer task-loop iteration (DeepAgent only, from `DeepAgentRail`): `before_task_iteration` → ... → `after_task_iteration`.
4. Per inner ReAct round, in order:
   - `on_user_message` — fires once per consumed input batch, before it is joined into the conversation as a single `UserMessage`; rails may edit `ctx.inputs.parts` in place (`UserMessageInputs`).
   - `before_steering_drain` — fires only when steering messages are pending; rails cap how many this model call absorbs via `ctx.inputs.limit` (`SteeringDrainInputs`).
   - `before_model_call` → (LLM call) → `after_model_call`, or on failure `on_model_exception`.
   - For each tool call the model requested: `before_tool_call` → (tool execution) → `after_tool_call`, or on failure `on_tool_exception`.
   - `after_react_iteration` — fires after one full ReAct round (LLM + all tool calls + `ToolMessage` writes) completes successfully; it does **not** fire on any break path (`AgentCallbackEvent` docstring).
5. `uninit(agent)` — plain synchronous method, called when the rail is unregistered (`AgentRail.uninit`, invoked from `BaseAgent.unregister_rail` in `openjiuwen/core/single_agent/base.py`).

Harness.md's table (§ 3.2) covers the same five pairs at a higher level: `before_invoke`/`after_invoke`, `before_task_iteration`/`after_task_iteration`, `before_model_call`/`after_model_call`, `before_tool_call`/`after_tool_call`, `on_model_exception`/`on_tool_exception`. This page adds the three hooks that table omits: `on_user_message`, `before_steering_drain`, and `after_react_iteration`.

Only override the hooks you need. `get_callbacks()` inspects your subclass and registers a method only when it differs from the base class's no-op (`AgentRail._is_base_method`; `DeepAgentRail._is_deep_base` does the same for the two extra hooks).

## 3. The callback context (`AgentCallbackContext`)

Every hook receives one `AgentCallbackContext` dataclass instance (`openjiuwen/core/single_agent/rail/base.py`). Verified fields:

| Field | Type | Notes |
| --- | --- | --- |
| `agent` | `BaseAgent` | the agent instance |
| `event` | `Optional[AgentCallbackEvent]` | set by `fire()` before your hook runs |
| `inputs` | `EventInputs` | typed per event — see § 4 |
| `config` | `Any` | runtime configuration |
| `session` | `Optional[Session]` | current session |
| `context` | `Optional[ModelContext]` | current model context |
| `extra` | `Dict[str, Any]` | cross-rail scratch space that **persists across all events within one `invoke()`** — the usual way to hand data from `before_tool_call` to `after_tool_call` |
| `exception` | `Optional[Exception]` | set on `on_model_exception` / `on_tool_exception` |
| `retry_attempt` | `int` | current failed-attempt index |
| `retry_history` | `List[RetryRecord]` | every failed attempt for this invoke, each with `attempt_index`, `exception_type`, `exception_message`, `timestamp` |
| `context_usage_report` | `Optional[Any]` | request-local token report shared by the before/after model-call hooks |
| `context_usage_request_id` | `Optional[str]` | request-local context-usage event id |
| `context_usage_sequence` | `Optional[int]` | request-local execution sequence |
| `context_usage_attribution` | `Dict[str, Any]` | request-local owner / invocation metadata |
| `invoke_start_time` | `float` | monotonic start time |

Additional call-only methods worth knowing:

- `ctx.bind_steering_queue(queue)`, `ctx.push_steering(msg)`, `ctx.drain_steering(limit=None)`, `ctx.has_pending_steering()`, `ctx.steering_queue` — steering-message plumbing. `drain_steering` always takes at least one message from a non-empty queue, even when `limit` is smaller, so a drain always makes progress.
- `ctx.request_model_continue()` / `ctx.consume_model_continue_request()` — a one-shot request for one more ReAct model iteration after a text response, for providers that emit recoverable protocol text instead of a real tool call. It does not bypass the agent's iteration budget.
- `ctx.lifecycle(before, after)` — async context manager that fires `before` on entry and `after` in a `finally` block, saving and restoring `ctx.inputs` around the block. Internal plumbing you are unlikely to call from a rail.

`ctx.inputs` is **not** attribute-uniform across events — its concrete type depends on `ctx.event`, per the `EventInputs` union. Check the event, or read defensively with `getattr(ctx.inputs, "tool_name", "")`.

## 4. Typed event inputs

Verified dataclasses (`openjiuwen/core/single_agent/rail/base.py`):

- `InvokeInputs` (before/after invoke): `query`, `conversation_id`, `result` (filled after), `run_kind`, `run_context`, `parent_session_id`, plus the lineage fields `invocation_id`, `parent_invocation_id`, `delegation_id`, `agent_path`, `depth`. Helper methods: `is_heartbeat()`, `is_cron()`, `is_lightweight_context()`.
- `ModelCallInputs` (before/after model call): `messages`, `tools`, `model_context`, `response` (filled after), `context_usage_report`, `context_usage_request_id`, `context_usage_sequence`, `context_usage_attribution`, `react_iteration` (1-based inner loop index).
- `ToolCallInputs` (before/after tool call): `tool_call`, `tool_name`, `tool_args`, `tool_result` (filled after), `tool_msg` (filled after), `react_iteration`.
- `TaskIterationInputs` (before/after task iteration): `iteration` (1-based), `loop_event`, `conversation_id`, `result`, `query` (rails may rewrite this in `before_task_iteration` to change what the inner agent sees), `is_follow_up`, `run_kind`, `run_context`.
- `UserMessageInputs` (`on_user_message`): `parts: list[str]` (mutable, edited in place), `source: str` — one of `"query"`, `"steering"`, `"resume"`.
- `SteeringDrainInputs` (`before_steering_drain`): `pending: int`, `limit: int | None`.

## 5. `priority`

`AgentRail.priority: int = 50` is a class attribute, and **higher runs first**. One number governs two things:

- **Init order**: rails are initialized in priority order, so a rail that needs another rail's tools or prompt sections to exist at init time must declare a *lower* priority than that rail. For example `SysOperationRail.priority = 100` registers filesystem/shell tools early (`openjiuwen/harness/rails/sys_operation_rail.py`).
- **Callback order**: `AgentCallbackManager.register_rail()` registers each hook method at `rail.callback_priority(event)` (`openjiuwen/core/single_agent/agent_callback_manager.py`), so within one hook chain — all rails' `before_tool_call`, say — higher-priority rails run first.

Rails sharing a priority keep the order they were added in (`AgentRail` docstring).

`callback_priority(event)` defaults to returning `self.priority`. Override it when a rail needs one hook placed differently from its init position — for instance a rail that must *read* what the other callbacks of a hook produced, without also initializing after them.

`SysOperationRail` is the canonical high-priority case: it is purely init-time, registers filesystem/shell tools in `init()`, takes part in no hook, and declares `priority = 100` so any rail that needs those tools at init time declares a lower priority (class docstring). In this repository, `CircuitBreakerRail` declares `priority = 95`, ahead of the default 50, so it sees tool results before lower-priority rails act on them (`jiuwenswarm/agents/harness/common/rails/execution_guard/circuit_breaker_rail.py`).

## 6. `request_retry()` and `request_force_finish()`

Both live on `AgentCallbackContext`.

### `request_retry(delay_seconds: float = 0.0)`

- **Intended call sites**: inside `on_model_exception` / `on_tool_exception` hooks (method docstring).
- **Mechanics**: sets an internal `RetryRequest`. The `@rail(...)` decorator that wraps the underlying model-call / tool-call method checks for it in its `except` block: if present, it sleeps `delay_seconds` (negative values are clamped to `0.0`) and loops back to retry the wrapped call. `ctx.retry_attempt` increments and a `RetryRecord` is appended to `ctx.retry_history` on every failed attempt.
- **When it is honoured**: only when requested during the `on_exception` phase of a `@rail`-decorated call. A stale request from a previous attempt is dropped by `ctx.consume_retry_request()` at the top of the retry loop, so `request_retry()` must be re-issued on every failed attempt you want retried.
- **Interaction with `after_tool_call`**: when a retry is queued, the wrapping `finally` block skips firing `AFTER_TOOL_CALL` for that failed attempt, so an intermediate `tool_result` written by `on_tool_exception` is not consumed as if it were final. `after_model_call` has no such skip — it fires on every attempt, including ones about to be retried, because cancellation-detecting callbacks depend on it firing every time (code comment in the decorator).

### `request_force_finish(result: Dict[str, Any])`

- **Can be called from any hook** — the docstring names `before_model_call` and `after_tool_call` as examples.
- **Mechanics**: sets a `ForceFinishRequest`. `ctx.has_force_finish_request` (property) and `ctx.consume_force_finish()` read and clear it.
- **When it is honoured**: the `@rail` decorator checks `ctx.has_force_finish_request` immediately after firing the `before` event and *before* calling the wrapped method body — if set, **the method body is skipped entirely** and the force-finish result is returned instead. Calling it from a `before_*` hook therefore pre-empts the model or tool call that was about to happen. The decorator also honours a force-finish requested from an `on_exception` hook, returning the result instead of re-raising. Called from an `after_*` hook it records the signal, but the wrapped call has already run: the agent loop is documented to check the signal "after every railed operation", which is how a rail force-finishes once it has inspected a model response or tool result. `CircuitBreakerRail.after_tool_call` in this repository is a working example.

## 7. Adding a prompt section (`system_prompt_builder`)

`SystemPromptBuilder` and `PromptSection` live in `openjiuwen.core.single_agent.prompts.builder` and are re-exported from `openjiuwen.harness.prompts`; both import paths resolve to the same class.

`PromptSection(name, content: dict[str, str], priority: int = 100, category=None, carrier="system_message")` — `content` is keyed by language code (`"cn"`, `"en"`), and `render(language)` falls back to `DEFAULT_LANGUAGE = "cn"`, then to the first available language, when the requested one is missing.

`SystemPromptBuilder.build()` sorts registered sections by effective priority **ascending** (`get_section_sort_key`) and joins their rendered text with `"\n\n"`, skipping blank sections. Note the direction is the opposite of `AgentRail.priority`: a *lower* `PromptSection.priority` renders earlier in the assembled prompt. This repository keeps its section priorities in one place — `SystemPromptPriority` in `jiuwenswarm/agents/harness/common/prompt/priority_registry.py` — rather than scattering magic numbers across rails.

The agent exposes the current builder as `agent.system_prompt_builder`, and the same builder instance is shared with the inner ReAct agent, so a rail that mutates it before `BEFORE_MODEL_CALL` is reflected in the LLM call. Typical pattern, as used by `RuntimePromptRail` and by `TaskPlanningRail` / `SysOperationRail` in `openjiuwen`:

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

`add_section` overwrites any existing section with the same `name`; `remove_section` is a no-op when the name is absent. Always remove your section in `uninit()` so hot-unmounting the rail does not leave a stale section behind.

## 8. Hot-mounting: `register_rail` / `unregister_rail`

`BaseAgent.register_rail(rail)` and `BaseAgent.unregister_rail(rail)` (`openjiuwen/core/single_agent/base.py`) are async and can be called at any point after the agent exists, not only at construction time:

```python
await agent.register_rail(MyRail())
...
await agent.unregister_rail(my_rail_instance)
```

`register_rail`:

1. Calls `init_rail(rail, agent)`, which times and runs `rail.init(agent)` synchronously.
2. Calls `AgentCallbackManager.register_rail(rail, agent)`, which registers every overridden hook returned by `rail.get_callbacks()` at `rail.callback_priority(event)`.

`unregister_rail`:

1. Calls `AgentCallbackManager.unregister_rail(rail, agent)`, which unregisters each of the rail's callback methods.
2. Calls `rail.uninit(agent)` synchronously.

Because `init` and `uninit` are plain synchronous methods rather than events fired through the callback framework, they run exactly once per `register_rail` / `unregister_rail` call regardless of what other rails are doing. That makes them the right place for a rail to register and deregister its own tools and prompt sections.

`DeepAgent` hot-swaps rails internally as part of `AgentMode` transitions (`openjiuwen/harness/deep_agent.py`) using this same public API.

One JiuwenSwarm-specific caveat: user extension rails loaded through `RailManager` are cached per extension name for the host agent, and team members get their own instance via `RailManager.create_fresh_rail_instance()` (`jiuwenswarm/agents/harness/common/plugins/rail_manager.py`). Write rails so that per-member instance state is meaningful, and do not assume a rail object is a process-wide singleton.

## 9. Registering via the builtin rail provider/registry

Beyond directly instantiating and calling `agent.register_rail(...)`, Harness supports a **declarative** registration path so a rail can be referenced by name from configuration (`RailSpec(type=..., params={...})`), in `openjiuwen/harness/schema/deep_agent_spec.py`:

```python
from openjiuwen.harness.schema.deep_agent_spec import register_rail_provider


def build_my_rail(params: dict, context) -> "MyRail":
    # context is a BuildContext carrying `language`, `workspace`, etc.
    return MyRail(**params)


register_rail_provider("my_rail", build_my_rail)
```

- `_RAIL_PROVIDER_REGISTRY` is a module-level `dict[str, Callable[..., Any]]`. `register_rail_provider(name, factory)` is a name-keyed overwrite: registering the same name twice replaces the earlier factory.
- `RailSpec.build(*, language, workspace=None, context=None)` looks `self.type` up in the registry and calls `factory(dict(self.params), context)`; the factory may return a single rail, a list of rails, or `None`. When `context` is omitted, a minimal `BuildContext(language=language, workspace=workspace)` is synthesized. An unknown rail type logs a warning and returns `None`, so the remaining rails still build.
- Before any lookup, `RailSpec.build` calls `openjiuwen.harness.manifest.ensure_builtin_elements_registered()`. That is how Harness's own built-in rails end up in the same registry: they are declared once through the `harness_element(kind=ElementKind.RAIL, ...)` catalog decorator, and `register_from_catalog()` wraps class-based builders through `class_rail_adapter` — which injects `language` when the constructor accepts it — before calling `register_rail_provider` for each (`openjiuwen/harness/manifest/registration.py`, `openjiuwen/harness/manifest/introspect.py`). A custom rail does **not** have to go through the catalog; calling `register_rail_provider` directly is enough to make it addressable from a `RailSpec`.

For integrations that construct the agent directly in Python, plain `await agent.register_rail(MyRail(...))` (§ 8) is simpler and needs no registry at all. Use the registry path when the rail must be selectable from serialized or declarative agent configuration.

## 10. Testing a rail

Most rail behaviour is testable without a model. Construct an `AgentCallbackContext(agent=<stub>)` directly and call the hook methods with hand-built input dataclasses (`ModelCallInputs`, `ToolCallInputs`, … — § 4). This is the shape the existing rail tests in `tests/unit_tests/agentserver/` use, and it needs neither a real `DeepAgent` nor any patching.

When a test does need the real loop, `Model.invoke` is the narrowest boundary to patch. Its signature is `async def invoke(self, messages, *, tools=None, temperature=None, top_p=None, max_tokens=None, stop=None, model=None, output_parser=None, timeout=None, **kwargs) -> AssistantMessage` (`openjiuwen/core/foundation/llm/model.py`), so patching that one method lets `before_model_call` / `after_model_call` run against a real `DeepAgent` without a network call:

```python
from unittest.mock import patch

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.foundation.llm.schema.message import AssistantMessage


async def test_my_rail_force_finishes_on_violation():
    agent = build_test_deep_agent()  # your test harness's agent construction
    rail = MyAuditRail()
    await agent.register_rail(rail)

    fake_response = AssistantMessage(content='{"claims": []}', tool_calls=None)
    with patch.object(Model, "invoke", return_value=fake_response):
        result = await agent.invoke("do something")

    assert result["result_type"] == "answer"
```

The `Model.invoke` signature this relies on is verified, but no test in `tests/unit_tests/` currently patches it this way, so treat the pattern itself as **unverified as an established project convention**.

## 11. Complete minimal example

An audit rail that hashes tool arguments and force-finishes on a policy violation:

```python
"""AuditRail: hashes tool arguments for an audit trail and force-finishes the
agent loop when a tool call violates a simple denylist policy."""

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
    """Hash every tool call's arguments; force-finish on a denied tool."""

    priority = 65  # initializes after SysOperationRail (100) has registered its tools

    def __init__(self, *, denied_tools: set[str] | None = None) -> None:
        super().__init__()
        self.denied_tools = denied_tools or set()
        self.audit_log: list[dict[str, str]] = []

    def init(self, agent: Any) -> None:
        # No tools or prompt sections to register for this rail.
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
            # Requested from a *before* hook, so the @rail decorator skips the
            # tool call entirely (see section 6).
            ctx.request_force_finish(
                {
                    "output": f"AuditRail blocked call to denied tool '{tool_name}'.",
                    "result_type": "answer",
                    "reason_code": "policy_violation",
                }
            )

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        # Retry once on a transient tool failure, then give up.
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

Registering and hot-mounting it:

```python
audit_rail = AuditRail(denied_tools={"delete_all_data"})
await agent.register_rail(audit_rail)
...
await agent.unregister_rail(audit_rail)
```

## Sources checked

Read against the `openjiuwen` revision pinned in `uv.lock` (`0.1.18`):

- `openjiuwen/core/single_agent/rail/base.py` — `AgentCallbackEvent`, `AgentCallbackContext`, `AgentRail`, the `@rail` decorator, and the typed event-input dataclasses.
- `openjiuwen/harness/rails/base.py` — `DeepAgentRail`, `DEEP_EVENT_METHOD_MAP`.
- `openjiuwen/core/single_agent/base.py` — `register_rail`, `unregister_rail`.
- `openjiuwen/core/single_agent/agent_callback_manager.py` — `register_rail` / `unregister_rail`, priority wiring.
- `openjiuwen/core/single_agent/prompts/builder.py` — `PromptSection`, `SystemPromptBuilder`.
- `openjiuwen/harness/rails/sys_operation_rail.py`, `openjiuwen/harness/rails/task_planning_rail.py` — concrete `init` / `uninit` / prompt-section examples.
- `openjiuwen/harness/schema/deep_agent_spec.py` — `RailSpec`, `register_rail_provider`, `_RAIL_PROVIDER_REGISTRY`.
- `openjiuwen/harness/manifest/registration.py`, `openjiuwen/harness/manifest/introspect.py` — `class_rail_adapter`, `register_from_catalog`.
- `openjiuwen/core/foundation/llm/model.py` — `Model.invoke` signature.

In this repository:

- `jiuwenswarm/agents/harness/common/rails/runtime_prompt_rail.py` — prompt-section rail with `init` / `uninit` cleanup.
- `jiuwenswarm/agents/harness/common/rails/execution_guard/circuit_breaker_rail.py` — `request_force_finish` from `after_tool_call`.
- `jiuwenswarm/agents/harness/common/prompt/priority_registry.py` — `SystemPromptPriority`.
- `jiuwenswarm/agents/harness/common/plugins/rail_manager.py` — extension-rail loading and per-member instances.
