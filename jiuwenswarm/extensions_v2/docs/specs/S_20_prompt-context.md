# `prompt.context` 用户消息上下文规约

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | S_20 / prompt-context |
| 关联模块 | `jiuwenswarm/extensions_v2/`；`server/runtime/agent_adapter/` |
| 最近一次修订日期 | 2026-11-05 |
| 关联 feature | [F_34_prompt-context.md](../features/F_34_prompt-context.md) |
| 设计依据 | `D:/wiki/extension/2026-11-03-jiuwenswarm-prompt-context-event-design.md`、[内置事件开发指南](../designs/builtin-event-development-guide.md) |

本规约定义 JiuwenSwarm 在适用 UserTurn 提交前触发的 `prompt.context` 事件、输入输出模型、
宿主投影和失败边界。它不定义 openjiuwen system section、attachment 或 ContextEngine 的契约。

## 1. 范围、术语与适用条件

### 1.1 责任范围

- **本规约管：** `prompt.context` 的事件载荷、EventBus 登记、AgentServer 触发时机、UserTurn 投影和兼容边界。
- **本规约不管：** 扩展包管理、文件读取服务、模型调用、历史窗口、压缩、system prompt、attachment
  和跨进程事件传输。

### 1.2 术语与适用条件

- **context block：** 当前 UserTurn 的命名文本上下文，不等同于用户原始 `content`。
- **prepared context：** 事件链返回并绑定到当前 frozen `UserTurn` 的结果集合。
- **user-message carrier：** JiuwenSwarm envelope 中承载未知 blocks 和 `instructionFiles` 的
  `prompt_context` 字段，仍属于当前用户消息。
- 本期 AgentServer 接入范围是：unary/stream、非空 `session_id`、`ReqMethod.CHAT_SEND`、
  `external_user_authored` 来源的 session 用户请求，包括 team/team mode/group-chat。补充输入、control、retry、
  `auto_harness`、A2UI、`InteractiveInput`、内部/跨 session 请求均旁路；
  这些路径后续如需接入，必须单独定义触发边界和验收。

### 1.3 复用与适配边界

| 既有能力 / 权威规约 | 复用的契约 | 本模块的适配责任 |
| --- | --- | --- |
| openjiuwen `ExtensionEventBus` | Event、Handler 链、validator 和 USER → BUILTIN → CORE 执行顺序 | 定义 JiuwenSwarm 事件类型和 CORE，并使用已装配的同一 Bus |
| `UserTurn.render()` | 同步 envelope 生成和既有字段顺序 | 在 render 前绑定结果，把已知和未知 block 投影到 user message |
| `match_pattern_subset` | 现有扩展事件 matcher | 以完整输入集合选择 Handler，不按单 block 重复触发 |

## 2. 数据结构与事实归属

### 2.1 对象与事实归属

| 对象 | 含义与作用域 | 事实权威 / 持有者 | 修改入口 |
| --- | --- | --- | --- |
| `PromptContextBlock` | 当前请求内一个有序命名文本 block | AgentServer 构造的事件输入或 Handler 返回结果 | Handler 返回新的完整集合 |
| `PromptContextInput` | 一次事件链的完整输入 | AgentServer 当前 UserTurn | `prepare_prompt_context()` 构造 |
| `PromptContextResult` | 一次事件链的完整输出 | EventBus 最终返回值 | CORE 或 USER/BUILTIN Handler 返回 |
| `InstructionFile` | 与 context 关联的协议文件对象 | 当前事件结果；不写 session | Handler 返回；当前 host carrier 消费 |
| prepared contexts | 当前 frozen UserTurn 的派生状态 | `UserTurn.prepared_contexts` / `prepared_instruction_files` | `UserTurn.with_prompt_contexts()` 替换生成 |

### 2.2 关键字段

| 字段 | 类型 / 默认值 | 语义 | 约束 |
| --- | --- | --- | --- |
| `blocks` | `list[PromptContextBlock]`，必填 | 完整、有序的 context 集合 | 必须为 list；名称唯一；可为空 |
| `blocks[].name` | 非空 `str` | 稳定 block 名称 | 去空白后不可为空；同一结果不得重复 |
| `blocks[].text` | `str` | block 文本 | 可为空字符串；不使用 `None` 表示删除 |
| `instructionFiles` | `list[InstructionFile] | None`，默认 `None` | 可选指令文件集合 | `None`、空数组和非空数组语义不同 |
| `path` | 非空 `str` | 指令文件路径标识 | 不为空；本事件不负责读取或验证路径存在 |
| `kind` | `managed/user/project/local/memory` | 指令文件来源类别 | 只能使用五个协议值 |
| `content` | `str` | 指令文件内容 | 由结果提供；不写入 session |
| `parent` | `str | None`，默认 `None` | 父级路径或来源关联 | 可空字符串，但不改变路径权限语义 |
| `isUnverified` | `bool`，默认 `False` | 文件来源是否未经验证 | 必须是 bool，不能用整数替代 |

Python 适配层可提供 `instruction_files` 只读别名；事件对外字段保持 `instructionFiles`。

## 3. 不变量

1. **PC-01 完整集合：** 每次适用的 UserTurn 只触发一次 `prompt.context`，输入和输出都表示完整
   blocks 集合；宿主不为单个 block 重新触发事件。
2. **PC-02 恒等 CORE：** CORE 只把合法 `PromptContextInput` 复制为 `PromptContextResult`，不添加文本、
   不访问业务服务、不写历史且不调用 `next_handler()`。
3. **PC-03 最终结果优先：** 返回结果删除或替换 `currentDate` / `interactionContext` 时，UserTurn
   按结果投影，不在事件外恢复原值。
4. **PC-04 字段存在性：** `instructionFiles=None` 表示未提供；空数组表示明确清空；非空数组表示替换。
   宿主不得把三者合并为同一种“无文件”。
5. **PC-05 用户消息边界：** 事件不得修改用户原始 content、source、type、language、channel、权限、session
   身份、sender、skills、files 或其他路由事实。
6. **PC-06 完整投影：** 未知 block 和 instruction files 必须进入 `prompt_context` carrier，不能只消费已知
   block 后静默丢弃。
7. **PC-07 失败原子性：** 事件框架失败或输出不合法时，丢弃整份新结果，使用原始输入集合渲染；不得混合部分结果。
8. **PC-08 取消传播：** 请求取消时不提交半成品 prepared contexts，`CancelledError` 不转换为普通 fail-open。
9. **PC-09 Bus 唯一性：** 触发方只使用 AgentServer 已装配的 Bus，不在业务调用中创建备用 Bus、Registry 或 Ops。

## 4. 接口契约

### 4.1 共通前置与失败规则

`bootstrap.register_event_types()` 调用 `register_prompt_context_events(bus)` 完成登记。事件登记只发生在
已有 Extensions V2 装配流程中；本事件不增加 `ops.build` 能力。

事件触发由 AgentServer 异步输入构造完成。扩展装配失败或没有可用 Bus 时跳过事件阶段并沿原
`UserTurn.render()` 路径继续。事件结果非法时记录类型和非内容摘要后 fail-open；日志不得记录 block 文本、
文件内容、凭据或完整用户请求。

### 4.2 `prompt.context`

- **入口与形态：** `Event("prompt.context", PromptContextInput)`，由
  `await ExtensionEventBus.trigger(event, SessionContext(...))` 异步触发。
- **调用方与前置条件：** AgentServer `JiuWenSwarm._build_inputs_async()`；请求必须同时满足 unary/stream、
  非空 `session_id`、`ReqMethod.CHAT_SEND`、`turn.origin_kind == "external_user_authored"`；team/team mode/group-chat
  请求也在范围内，满足后才使用
  进程级已装配 Bus。
- **集中范围 gate：** 所有生效和旁路条件集中在
  `jiuwenswarm.server.runtime.agent_adapter.prompt_context.is_prompt_context_applicable()`；后续调整范围时只修改
  该 gate 及其回归矩阵，不在各业务入口重复判断。
- **输入与校验：** `valid_prompt_context_input()` 校验对象类型、list 字段、block 名称唯一性和
  instruction file 字段。
- **成功结果：** `PromptContextResult`；返回值只代表本次事件链的结果，不代表 session 持久化或文件加载完成。
- **状态与副作用：** 只修改当前请求内 prepared UserTurn，不修改 session、历史或全局缓存。
- **重复、并发与取消：** 同一 UserTurn 的 `with_text()` / `render()` 不重复触发；不同请求各自触发并使用
  独立 SessionContext；取消沿请求任务传播。

| 错误 / 边界条件 | 判定位置与时机 | 对外结果 | 状态与副作用 |
| --- | --- | --- | --- |
| 输入不是 `PromptContextInput` 或字段非法 | EventBus input validator | 事件拒绝或触发失败 | 不绑定 prepared contexts |
| Handler 普通异常被 EventBus 隔离 | EventBus Handler 调度 | 继续链路时由 CORE 返回等价结果 | 不产生部分宿主状态 |
| `bus.trigger()` 框架级失败 | `prepare_prompt_context()` | fail-open，使用原始集合继续 render | 记录非内容摘要，不写全局状态 |
| 输出不是合法 `PromptContextResult` | host output validator | fail-open，使用原始集合继续 render | 丢弃整份非法输出 |
| 请求取消 | 上层任务 / EventBus | `CancelledError` 向上抛出 | 不渲染半完成结果 |

### 4.3 Handler 形态

普通 Handler 使用当前 openjiuwen Events 签名：

```python
async def handler(event, ops, next_handler) -> PromptContextResult:
    result = await next_handler(event)
    return result
```

Handler 可以在 next 前构造新的 `Event` 和完整 `PromptContextInput`，也可以在 next 后返回新的完整
`PromptContextResult`。Handler 不应直接修改 `UserTurn`、`inputs["query"]` 或 openjiuwen ModelContext。

### 4.4 Host preparation

```python
async def prepare_prompt_context(
    turn: UserTurn,
    request: AgentRequest,
    *,
    cwd: str | None,
) -> UserTurn:
    ...
```

该函数构造 `UserTurn.build_prompt_contexts()` 的结果，建立可信 SessionContext，触发事件，校验输出，
并通过 `turn.with_prompt_contexts(...)` 返回新的 frozen UserTurn。它不创建 Bus，不修改请求身份，也不写历史。

### 4.5 UserTurn 投影

- `currentDate` → envelope `timestamp`。
- `interactionContext` → envelope 前的 `\n<context>\n\n` 前缀。
- 其他 blocks → `envelope["prompt_context"]["blocks"]`，保持返回顺序。
- `instructionFiles` 非 `None` → `envelope["prompt_context"]["instructionFiles"]`，保持返回字段和顺序。
- 删除已知 block 不恢复原始值；如果结果为空，按空集合渲染。

## 5. 状态与生命周期

### 5.1 状态定义与合法转移

| 源状态 | 触发操作 / 事件 | 执行方 | 目标状态 | 资源处置 |
| --- | --- | --- | --- | --- |
| 未准备 | `_build_inputs_async()` | host | 等待事件 | UserTurn 仍未渲染 |
| 未准备 | Bus 不可用 | host | 原始渲染 | 不创建备用 Bus |
| 未准备 | 事件成功且结果合法 | EventBus + host | 已准备 | 结果绑定当前 UserTurn |
| 未准备 | 事件失败或结果非法 | host | 原始渲染 | 丢弃新结果 |
| 已准备 | `with_text()` / `render()` | UserTurn | 已准备 | 复用同一 prepared tuple |
| 任意未完成状态 | 请求取消 | 上层任务 | 取消 | 不保存半成品 |

### 5.2 资源与持久化生命周期

prepared contexts 只属于当前 UserTurn 的内存生命周期。事件结果不写 session，不进入历史缓存，也不跨请求
复用；UserMessage 最终仍由既有 openjiuwen admission / history 流程处理。当前没有 session-level cache、
invalidate 或后台任务。

### 5.3 跨接口运行流程

```text
request
  → _build_inputs(render_prompt=False)
  → UserTurn.build_prompt_contexts()
  → EventBus.trigger(prompt.context)
  → validate PromptContextResult
  → UserTurn.with_prompt_contexts()
  → UserTurn.render()
  → inputs["query"]
```

本期只有适用的外部用户 `CHAT_SEND`（unary/stream）入口在异步 helper 中触发事件，包括 team/team mode/group-chat；补充输入、control、retry、
skill rebuild 和其它专用入口即使经过异步 helper，也会在 preparation gate 旁路。同步
`build_inputs()` 仍用于兼容和测试构造，不作为本期事件触发入口。

## 6. 适用限制与兼容性

- `instructionFiles` 当前只保证通过 user-message carrier 传递；不保证独立文件加载、路径授权、system
  section、attachment 或 provider projection。
- 只有 `currentDate` 和 `interactionContext` 是宿主默认提供的 block；其它 block 由 Handler 返回并由 carrier 承载。
- 同步旧调用方可以继续调用 `build_inputs()`，但不会获得异步事件等待语义；生产 AgentServer 不使用该路径。
- A2UI、`InteractiveInput`、补充输入、control、retry、`auto_harness`、
  内部/跨 session 消息均不触发本事件。
- 本期 gate 放行有非空 session 的外部用户 `CHAT_SEND`（unary/stream），包括 team/team mode/group-chat；扩大范围需要新增专门规约和回归矩阵。

## 7. 关联规约与验证索引

### 7.1 关联规约

- [F_34 prompt.context 特性设计](../features/F_34_prompt-context.md)：背景、取舍、实施影响和实际验证记录。
- [Extensions V2 内置事件开发指南](../designs/builtin-event-development-guide.md)：登记、Handler、真实业务接入和验证职责。
- openjiuwen Events 公共契约：以当前环境加载的 `openjiuwen.core.events` 公共 API 为准；本次不修改 Core。

### 7.2 验证索引

- 事件模型、matcher、validator、CORE 和 Handler 链：
  `tests/unit_tests/extensions_v2/test_prompt_context.py`。
- AgentServer 触发一次、结果投影、已知 block 删除、范围旁路和 fail-open：
  `tests/unit_tests/agentserver/test_prompt_context_integration.py`。
- UserTurn 原有字段和渲染兼容：`tests/unit_tests/server/test_user_turn.py`。
- 异步入口相关回归：`tests/unit_tests/agentserver/test_agentserver_modes.py`、
  `tests/unit_tests/agentserver/test_context_usage.py`、
  `tests/unit_tests/extensions_v2/test_context_usage.py`、
  `tests/unit_tests/agentserver/test_team_helpers.py`。
