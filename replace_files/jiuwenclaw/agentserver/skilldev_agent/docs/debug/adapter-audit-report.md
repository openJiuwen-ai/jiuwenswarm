# SkillDevDeepAdapter 审计与修复报告

> 对比基准：`jiuwenclaw/agentserver/deep_agent/interface_deep.py` (`JiuWenClawDeepAdapter`)
> 审计目标：`jiuwenclaw/agentserver/skilldev_agent/adapter.py` (`SkillDevDeepAdapter`)
> 更新时间：2026-05-14

---

## 总体结论

本轮审计共发现 7 个问题：

- 问题 1 至问题 6 已完成修复。
- 问题 7 经验证为误报风险，`ModelClientConfig` 允许 extra 字段，无需修复。
- 已执行编译与 linter 检查，`adapter.py` 和 `tools.py` 当前无语法错误和 linter 报错。

---

## 问题 1：工具注册 identity check 冲突

**严重程度：致命**

**当前状态：已修复**

### 原因

`create_deep_agent` 内部的 `_register_tool_instances` 会检查同 ID 工具实例是否为同一对象：

```python
if existing_tool is not tool:
    raise ValueError(...)
```

原实现每次 `create_instance` 都通过 `build_skilldev_tools` 创建新的 Tool 实例。全局 `Runner.resource_mgr` 中已存在同 ID 旧实例时，新旧对象 identity 不一致，导致异常。

### 修复结果

`adapter.py` 新增 `_register_tools`：

- 先将 Tool 实例注册到 `Runner.resource_mgr`
- 若已存在同 ID 旧实例，则先移除再注册新实例
- 返回 `ToolCard` 列表
- `create_deep_agent` 接收 `ToolCard`，不再触发 `_register_tool_instances` 的实例 identity 校验

相关文件：

- `jiuwenclaw/agentserver/skilldev_agent/adapter.py`

---

## 问题 2：SysOperation 未统一注册

**严重程度：高**

**当前状态：已修复**

### 原因

原 `tools.py` 内部自行创建 `SysOperation` 并传给文件、Shell、Code 工具，但这个实例没有注册到 `Runner.resource_mgr`。

同时，`create_deep_agent` 在未收到 `sys_operation` 参数时会自动创建另一套 `SysOperation`。这会导致：

- 工具使用一套 SysOperation
- Agent/Rail 使用另一套 SysOperation
- 两者的 `work_dir` 和访问限制可能不一致

### 修复结果

`tools.py` 已改为不再创建 SysOperation，而是接收外部传入的统一实例：

```python
def build_skilldev_tools(
    *,
    sys_operation: SysOperation,
    ...
) -> tuple[list[Tool], TodoToolkit]:
    tools: list[Tool] = [
        tool_cls(sys_operation, language=language)
        for tool_cls in HARNESS_TOOL_CLASSES.values()
    ]
```

`adapter.py` 新增 `_create_or_update_sys_operation`：

- 使用 `skilldev_agent_{agent_id or 'default'}` 作为 SysOperation ID
- 若已存在旧 SysOperation，先从 `Runner.resource_mgr` 移除
- 使用当前 `self._workspace_dir` 注册新的 `SysOperationCard`
- 从 `Runner.resource_mgr` 取回统一实例

`create_instance` 中同时将该实例传给：

- `build_skilldev_tools(sys_operation=sys_operation, ...)`
- `create_deep_agent(sys_operation=sys_operation, ...)`

相关文件：

- `jiuwenclaw/agentserver/skilldev_agent/tools.py`
- `jiuwenclaw/agentserver/skilldev_agent/adapter.py`

---

## 问题 3：流式处理前未调用 reset_abort()

**严重程度：高**

**当前状态：已修复**

### 原因

取消任务时会调用 `_stream_event_rail.abort()`，设置 abort 标志。如果下一次流式请求开始前没有清理该标志，新请求可能在 checkpoint 被立即取消。

### 修复结果

`process_message_stream_impl` 中已在进入 `ask_user_question_request_scope` 前调用：

```python
if self._stream_event_rail is not None:
    self._stream_event_rail.reset_abort()
```

这与 `JiuWenClawDeepAdapter` 的处理方式保持一致。

相关文件：

- `jiuwenclaw/agentserver/skilldev_agent/adapter.py`

---

## 问题 4：workspace 路径累积

**严重程度：中**

**当前状态：已修复**

### 原因

原 `_handle_chat_locked` 使用当前 `_workspace_dir` 的 parent 计算 task workspace：

```python
task_workspace = Path(self._workspace_dir).parent / "skilldev" / task_id
```

但 `_workspace_dir` 会在请求中被更新为 task workspace。多次请求后，路径可能变成：

```text
.../skilldev/skilldev/task-002
```

### 修复结果

`__init__` 中新增 `_base_workspace_dir`，保存初始 workspace：

```python
self._base_workspace_dir = self._workspace_dir
```

`_handle_chat_locked` 改为基于 base workspace 计算：

```python
task_workspace = Path(self._base_workspace_dir) / "skilldev" / task_id
```

后续无论 `_workspace_dir` 如何切换，task workspace 都稳定落在同一层级下。

相关文件：

- `jiuwenclaw/agentserver/skilldev_agent/adapter.py`

---

## 问题 5：process_interrupt 缺少 supplement 意图处理

**严重程度：低**

**当前状态：已修复**

### 原因

原实现只显式处理 `pause` 和 `resume`，其它 intent 全部进入 cancel 分支。`supplement` 的语义应为“停止当前执行但保留 TODO”，不应等同于 cancel。

### 修复结果

`process_interrupt` 已新增 `supplement` 分支：

- 调用 `_stream_event_rail.abort()`
- 调用 `self._instance.abort()`
- 取消当前 session 的 `ask_user_question`
- 返回 message：`任务已切换`
- 不清理 TODO

相关文件：

- `jiuwenclaw/agentserver/skilldev_agent/adapter.py`

---

## 问题 6：cancel 后未清理 TODO

**严重程度：低**

**当前状态：已修复**

### 原因

原 cancel 分支只停止执行，没有将未完成 TODO 标记为 cancelled，也没有向前端返回更新后的 todo 列表。

### 修复结果

`adapter.py` 新增 `_cancel_pending_todos`：

- 使用 SkillDev 当前接入的 `TodoToolkit`
- 将 `waiting` 和 `running` 状态任务标记为 `cancelled`
- 保存回 `todo.md`
- 返回前端可用的 todo payload

`process_interrupt` 的 cancel 分支已调用：

```python
updated_todos = await self._cancel_pending_todos(str(request.session_id))
```

并在响应 payload 中附加：

```python
payload["todos"] = updated_todos
```

注意：这里使用的是 `jiuwenclaw.agentserver.tools.todo_toolkits.TodoToolkit` 的 markdown 存储格式，而非 `openjiuwen.harness.tools.todo.TodoModifyTool` 的 JSON 格式。这与 SkillDev Agent 当前注册的 TODO 工具保持一致。

相关文件：

- `jiuwenclaw/agentserver/skilldev_agent/adapter.py`
- `jiuwenclaw/agentserver/tools/todo_toolkits.py`

---

## 问题 7：claw_config 透传兼容性

**严重程度：低**

**当前状态：已验证，无需修复**

### 原始风险

`_create_model` 中向 `ModelClientConfig` 传入 `claw_config`：

```python
mcc["claw_config"] = config
ModelClientConfig(**mcc)
```

如果 `ModelClientConfig` 不接受额外字段，会抛出 `TypeError`。

### 验证结果

`ModelClientConfig` 是 Pydantic `BaseModel`，并配置了：

```python
model_config = {"extra": "allow"}
```

因此 `claw_config` 可作为 extra 字段被接受，不需要额外处理。

---

## 修复状态汇总

| 优先级 | 问题 | 状态 |
|--------|------|------|
| **P0** | 问题 1：工具注册 identity check | 已修复 |
| **P0** | 问题 2：SysOperation 未统一注册 | 已修复 |
| **P0** | 问题 3：缺少 reset_abort() | 已修复 |
| **P1** | 问题 4：workspace 路径累积 | 已修复 |
| **P2** | 问题 5：缺少 supplement 意图 | 已修复 |
| **P2** | 问题 6：cancel 后未清理 TODO | 已修复 |
| **P2** | 问题 7：claw_config 兼容性 | 已验证，无需修复 |

---

## 验证结果

已完成以下验证：

```powershell
python -m compileall jiuwenclaw\agentserver\skilldev_agent\adapter.py jiuwenclaw\agentserver\skilldev_agent\tools.py
```

结果：

- `adapter.py` 编译通过
- `tools.py` 编译通过
- `ReadLints` 检查无 linter errors

---

## 后续建议

建议下一步进行一次运行时 smoke test：

1. 设置 `JIUWENCLAW_AGENT_SDK=skilldev`
2. 启动 `python -m jiuwenclaw.app_agentserver`
3. 发送一次 `skilldev.chat`
4. 验证：
   - 不再出现 Tool id duplicate / identity check 报错
   - 工具文件操作落在 task workspace 下
   - cancel 后下一轮请求不会被残留 abort 影响
   - 多个不同 task_id 不会出现 `skilldev/skilldev/...` 路径嵌套
# SkillDevDeepAdapter 潜在问题分析报告

> 对比基准：`jiuwenclaw/agentserver/deep_agent/interface_deep.py` (JiuWenClawDeepAdapter)
> 审计目标：`jiuwenclaw/agentserver/skilldev_agent/adapter.py` (SkillDevDeepAdapter)
> 日期：2026-05-14

---

## 问题 1（已修复）：工具注册 identity check 冲突

**严重程度：致命 → 已修复**

### 原因

`create_deep_agent` 内部的 `_register_tool_instances` 对工具注册有严格的 identity check：
如果全局 `Runner.resource_mgr` 里已有同 ID 的工具实例，且新传入的不是同一个对象（`is not tool`），
就会抛 `ValueError`。

`adapter.py` 每次 `create_instance` 都通过 `build_skilldev_tools` 创建全新的 Tool 对象，
但之前的对象仍留在全局注册表中 → identity 不匹配 → 报错。

### 修复

新增 `_register_tools` 方法：先自行将 Tool 实例注册到 `Runner.resource_mgr`（若已存在旧实例
则先移除再重新注册），然后返回 `ToolCard` 列表。传给 `create_deep_agent` 的是 ToolCard 而非
Tool 实例，`_register_tool_instances` 收到空 `tool_instances` 不再触发 identity check。

---

## 问题 2：SysOperation 未注册到 Runner.resource_mgr

**严重程度：高 — 可能导致文件操作工具运行时失败**

### 现状

`tools.py` 中创建了 `SysOperation` 实例并传给 Harness 工具的构造函数：

```python
# tools.py
sys_operation = SysOperation(
    SysOperationCard(
        id=f"skilldev_agent_{agent_id or 'default'}",
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(work_dir=str(workspace)),
    )
)
```

但这个 `SysOperation` **从未注册到 `Runner.resource_mgr`**。

### 对比 interface_deep.py

```python
# interface_deep.py _create_sys_operation()
result = Runner.resource_mgr.add_sys_operation(sysop_card)
return Runner.resource_mgr.get_sys_operation(sysop_card.id)
```

### 叠加问题

`create_deep_agent` 内部在未收到 `sys_operation` 参数时，会自动创建并注册一个新的
`SysOperation`（`id=skilldev-agent_skilldev-agent`），其 `restrict_to_sandbox=True`
且**不含 `work_dir`**。

结果：存在两套 SysOperation ——
1. 工具构造时用的一套（未注册、带 `work_dir`）
2. `create_deep_agent` 自动创建的一套（已注册、不带 `work_dir`、`restrict_to_sandbox=True`）

Agent 在 Rail 层面使用的 SysOperation 与工具实际使用的不一致。

### 修复建议

在 `tools.py` 或 `adapter.py` 中将 `SysOperation` 注册到 `Runner.resource_mgr`，
并将其传递给 `create_deep_agent(sys_operation=...)` 参数。

---

## 问题 3：流式处理前未调用 reset_abort()

**严重程度：高 — cancel 后下一次对话可能直接被 abort**

### 现状

`process_message_stream_impl` 在开始流式处理前**没有**调用 `reset_abort()`。

### 对比 interface_deep.py

```python
# interface_deep.py process_message_stream_impl()
if self._stream_event_rail is not None:
    self._stream_event_rail.reset_abort()
```

### 影响

如果用户之前发送过 cancel（调用了 `_stream_event_rail.abort()`），abort 标志会持续保持，
导致下一次请求到达 checkpoint 时立即被中断，无法正常对话。

### 修复建议

在 `process_message_stream_impl` 的 `ask_user_question_request_scope` 之前加入：

```python
if self._stream_event_rail is not None:
    self._stream_event_rail.reset_abort()
```

---

## 问题 4：workspace 路径累积（_handle_chat_locked）

**严重程度：中 — 多次请求后工作区路径嵌套越来越深**

### 现状

```python
# adapter.py _handle_chat_locked()
task_workspace = Path(self._workspace_dir).parent / "skilldev" / task_id
```

`self._workspace_dir` 在第一次请求后被 `update_workspace` 改为上一次的 task workspace
（例如 `.../skilldev/task-001`），后续请求中 `.parent` 变成 `.../skilldev`，
再拼 `"skilldev" / task_id` 就变成了 `.../skilldev/skilldev/task-002`，路径越来越深。

### 修复建议

在 `__init__` 中保存初始 base workspace（`self._base_workspace_dir`），
`_handle_chat_locked` 中基于 base 计算 task workspace：

```python
task_workspace = Path(self._base_workspace_dir) / "skilldev" / task_id
```

---

## 问题 5：process_interrupt 缺少 supplement 意图处理

**严重程度：低 — 功能不完整但不会崩溃**

### 现状

`adapter.py` 的 `process_interrupt` 只处理了 `pause`、`resume`，其余全部走 cancel 分支。

### 对比 interface_deep.py

支持 4 种 intent：`pause`、`resume`、`supplement`、`cancel`。
`supplement`（停止当前执行但保留 TODO）的语义在我们的实现中被丢失，变成了全量 cancel。

### 修复建议

增加 `supplement` 分支，停止执行但不清理 TODO。

---

## 问题 6：cancel 后未清理 TODO

**严重程度：低 — 功能不完整**

### 现状

`adapter.py` 的 cancel 分支直接停止执行，没有将未完成 TODO 标记为 cancelled。

### 对比 interface_deep.py

```python
# cancel 分支末尾
updated_todos = await self._cancel_pending_todos(request.session_id)
```

并将更新后的 TODO 列表附在响应中返回给前端。

### 修复建议

在 cancel 分支中调用 `TodoToolkit` 的相关方法清理未完成 TODO，
并在响应 payload 中附带 `todos` 字段。

---

## 问题 7：claw_config 透传兼容性

**严重程度：低 — 取决于 ModelClientConfig 构造函数签名**

### 现状

`_create_model` 中设置 `mcc["claw_config"] = config`，随后在 `_build_model_from_entry`
中做 `ModelClientConfig(**mcc)`。如果 `ModelClientConfig` 不接受 `claw_config` 关键字参数，
这里会抛 `TypeError`。

### 修复建议

验证 `ModelClientConfig` 的构造函数是否支持 `**kwargs`。
若不支持，在 `_build_model_from_entry` 中 pop 掉非标准字段后再构造。

---

## 修复优先级汇总

| 优先级 | 问题 | 状态 |
|--------|------|------|
| **P0** | 问题 1：工具注册 identity check | ✅ 已修复 |
| **P0** | 问题 2：SysOperation 未注册 | ❌ 待修复 |
| **P0** | 问题 3：缺少 reset_abort() | ❌ 待修复 |
| **P1** | 问题 4：workspace 路径累积 | ❌ 待修复 |
| **P2** | 问题 5：缺少 supplement 意图 | ❌ 待修复 |
| **P2** | 问题 6：cancel 后未清理 TODO | ❌ 待修复 |
| **P2** | 问题 7：claw_config 兼容性 | ❌ 待验证 |
