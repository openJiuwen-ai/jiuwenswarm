# SwarmFlow `facade` API 完全指南

> 状态：stable。面向 jiuwenswarm 内所有 skill / workflow 的 LLM 调起 + 人审 + 编排。
> 来源：`openjiuwen.agent_teams.workflow.engine.facade`（同时 alias 成 `swarmflow`）。

## 0. 一句话概览

`facade` 是 SwarmFlow 引擎对外暴露的**单一稳定 API 面**——所有想调 LLM sub-agent、
让人审一个检查点、跑多个分支、追踪 phase、用预算的代码，**只能**通过这层导出的原语
去调。它背后挂一个当前生效的 `Provider`（默认是 SwarmFlow runtime），provider 实现
engine backend 的真正调度。

- 别名：`openjiuwen.agent_teams.workflow.engine.facade` **==** `swarmflow`
  （后者是 `sys.modules` 注册的别名，源码里没这个包）。`from swarmflow import agent`
  跟 `from openjiuwen.agent_teams.workflow.engine.facade import agent` 等价。
- 所有原语都是**顶层 awaitable / callable**，没有 class 需要你 new；多数会转去
  `current_provider()` 拿到真正干活的对象。

## 1. 导入方式

```python
# 推荐（明确路径）
from openjiuwen.agent_teams.workflow.engine.facade import (
    agent, human, phase, log,
    parallel, pipeline, map_parallel, pmap,
    agent_session, human_session, workflow,
    budget, compact, flatten_filter,
)

# 等价（短别名；多用于 workflow 顶层）
from swarmflow import agent, human, phase, log, parallel, pipeline, map_parallel, budget
```

> `swarmflow` 名字只在 `facade` 模块 import 时被注册到 `sys.modules`；
> **先 import 过一次 `facade` 之后**，swarmflow 别名才可用。

## 2. 全部原语（按用途分组）

### 2.1 调 LLM sub-agent

| 原语 | 签名 | 用途 |
|---|---|---|
| `agent(prompt, *, label, phase, schema, options)` | `await` | **单次**调一个 LLM sub-agent（无状态）。planning skill 全部 sub-agent 走这条。 |
| `agent_session(*, label, phase, instructions, options)` | `sync` | 打开一个**有状态、多轮**的 agent 会话（`AgentSession`），自己驱动 turns。 |
| `human_session(*, label, phase, instructions, options)` | `sync` | 同上但给真人参与（`HumanSession`）。 |

#### 2.1.1 `agent()` schema 三种形态（按返回值类型分）

`agent()` 通过 `schema` 关键字控制返回值类型——这是写 LLM 调起代码最常碰到的旋钮：

```python
from openjiuwen.agent_teams.workflow.engine.facade import agent

# 1) schema=None（默认）—— 拿原始字符串
text: str | None = await agent("列出 3 个创新点")

# 2) schema=<JSON Schema dict> —— 拿 dict（json_repair 兜底）
data: dict | None = await agent(
    "返回 JSON: {hypotheses: [...] }",
    schema={
        "type": "object",
        "properties": {"hypotheses": {"type": "array", "items": {"type": "string"}}},
        "required": ["hypotheses"],
    },
)

# 3) schema=<Pydantic model> —— 拿强类型 model
from pydantic import BaseModel
class H(BaseModel):
    id: str
    text: str

h: H | None = await agent("返回 H1: ...", schema=H)
```

**3 个常见坑：**

1. `schema=None` 时**不会**自动尝试 JSON 解析；返回的是 `str`。要 dict 就传 dict
   schema 或自己 `json.loads(...)`（planning-skill 走这条，因为 prompt 复杂、信任
   LLM 直出但留一道解析失败兜底）。
2. `schema=<dict>` 的 dict **必须是 JSON Schema 格式**（`type/properties/required`），
   不是 Python `dict[str, Any]` 类型注解。
3. `schema=<Pydantic model>` 时如果 LLM 输出跟 model 不一致，框架尝试 json_repair；
   实在修不回来会返回 `None` 而不是抛错（这是设计选择——prompt 失败不该炸流程）。

#### 2.1.2 `label` / `phase` / `options` 三个修饰参数

- `label: str` —— 这个 agent 的**标识**（写 trace / 日志 / TUI 树用）。planning 传
  agent 名 `"method-designer"`。
- `phase: str` —— 关联到 `phase(title)` 标记的当前阶段（observability）。
  **必须**跟 `phase("方法设计")` 的 title 完全一致，否则 trace 会脱钩。
- `options: dict` —— 给后端 / engine 透传旋钮的"扩展包"，白名单字段包括
  `model / timeout / isolation / agent_type / temperature / max_tokens ...`。
  未知字段会抛错（防拼写错）。新加旋钮**不需要改 `agent()` 签名**。

#### 2.1.3 planning-skill 用法

```python
# scripts/_subagent.py
from openjiuwen.agent_teams.workflow.engine.facade import agent

async def call_agent_async(name: str, payload: dict, *, phase: str | None = None) -> dict:
    persona = extract_persona(agent_md_path(name))
    prompt = build_agent_prompt(persona, payload)
    text = await agent(prompt, label=name, phase=phase, schema=None)
    return json.loads(text)
```

> 对比旧 `run_subagent` 直接调 `JiuwenSwarmChatClient`——`facade.agent()` 内置
> json_repair、走框架注入的 LLMConfig（`jiuwenswarm/resources/.env` 配 1 份就够）、
> 自动接 phase trace、不会在 async 上下文里跟 event loop 打架。

---

### 2.2 人审 / 检查点

| 原语 | 签名 | 用途 |
|---|---|---|
| `human(prompt, *, schema, label, phase, options)` | `await` | **单次**让人回答一个 prompt。planning 4 个检查点走这条。 |
| `human_session(...)` | `sync` | **多轮**人审会话，自己驱动 turns。 |

`schema` 旋钮跟 `agent()` 完全相同（`None → str / dict → dict / pydantic → model`），
但多一个特殊的"action + feedback" pattern 在规划模块常用：

```python
from openjiuwen.agent_teams.workflow.engine.facade import human
from pydantic import BaseModel

class MethodReviewDecision(BaseModel):
    action: Literal["approve", "modify", "abort"] = "approve"
    feedback: str = ""

decision = await human(
    "[检查点 1] adequacy_score=8.2, passed=True ... 是否通过？",
    schema=MethodReviewDecision,
    label="checkpoint-method-design",
    phase="方法设计",
)
# decision.action ∈ {"approve", "modify", "abort"}，mod 时用 decision.feedback 重写
```

- TUI 模式下 `human()` 会渲染一个交互式表单（带 `h` 键、YAML 预览、`a/m/x` 快捷键）。
- CLI 批量跑时（`--no-human-review`）`human()` 直接返回 schema 的**默认值**——
  planning 利用这点走 AUTO-APPROVE 路径。
- `decision is None`：TUI 拒答 / schema 校验失败 / TTY 关闭——按 abort 处理是惯例。

---

### 2.3 编排（fork-join / 流式 / map）

| 原语 | 签名 | 用途 |
|---|---|---|
| `parallel(thunks)` | `await list` | **barrier**——所有 thunk 一起启动，**全部跑完**才返回。空 items 也接受。 |
| `pipeline(items, *stages)` | `await list` | **streaming**——每 item 走完所有 stage 才换下一个 item，stage 间**无** barrier。 |
| `map_parallel(items, fn)` | `await list` | `items` 上并行 `fn(item)`，**footgun-free**（items 改 fn 不收副作用）。 |
| `pmap` | alias | = `map_parallel`，短名字。 |

#### 2.3.1 `parallel` vs `pipeline` 选哪个？

- **barrier 需要**（如「所有子任务都跑完才能进入下一步」）→ `parallel(thunks)`
- **stage 间不共享数据** + **item 数量大** + **想尽早开始下一 item** → `pipeline(items, stage1, stage2, ...)`

> `parallel(thunks)` 的入参是**懒 thunk**列表（`[lambda: coro1, lambda: coro2]`），
> 不是 coro 列表。这样可以**控制并发启动时机**——`map_parallel` 内部就是把 fn 包成 thunk。

```python
# 例子：3 个 sub-agent 并行
results = await parallel([
    lambda: call_agent_async("method-designer",  payload_a, phase="方法设计"),
    lambda: call_agent_async("method-critic",    payload_b, phase="方法设计"),
    lambda: call_agent_async("experiment-planner", payload_c, phase="实验规划"),
])
```

---

### 2.4 观测 / trace

| 原语 | 签名 | 用途 |
|---|---|---|
| `phase(title)` | `sync` | 标记当前阶段（写到 journal / TUI 树）。`META.phases` 里 title 必须**字面一致**。 |
| `log(message)` | `sync` | 打一条进度行（任意类型，会 `repr`）。 |
| `budget.spent() / .remaining() / .total` | sync | 当前 workflow token 消耗。只读。 |

```python
phase("方法设计")
log("iterate_method_design 开始")
...  # 调 agent / human
log(f"已用 tokens ≈ {budget.spent()}")
```

---

### 2.5 嵌套 workflow

| 原语 | 签名 | 用途 |
|---|---|---|
| `workflow(name_or_path, args)` | `await` | 在当前 workflow 里**同步阻塞**跑另一个 workflow（最多 1 层嵌套）。 |

```python
# 在主 workflow 里嵌套跑子 workflow
sub_result = await workflow("skills/data-prep/flow.py", args={"input_dir": "..."})
```

> 嵌套超过 1 层会抛错（runtime 限制）。

---

### 2.6 纯列表 helper（不依赖 provider）

| 原语 | 签名 | 用途 |
|---|---|---|
| `compact(xs)` | `list` | `xs.filter(Boolean)`：去 `None / '' / 0 / [] / False`。 |
| `flatten_filter(xs)` | `list` | `xs.flat().filter(Boolean)`：展 1 层 + 去 falsy，`sub` 为 `None` 也容忍。 |

```python
compact([1, None, 0, "a", ""])        # [1, "a"]
flatten_filter([[1, 2], None, [3], []]) # [1, 2, 3]
```

---

### 2.7 Provider 切换 / 测试用

| 原语 | 签名 | 用途 |
|---|---|---|
| `use_provider(p)` | `sync` | 临时换 provider（如测试塞 mock）。 |
| `reset_provider()` | `sync` | 重置回默认。 |
| `current_provider()` | `sync` | 拿当前 provider 实例（一般不需要直接调，框架自己用）。 |

正常 skill 代码不需要碰这些——只有写 framework 自身或单测时用。

---

### 2.8 Harness（直接 run 一个 workflow）

| 原语 | 用途 |
|---|---|
| `run_workflow(path, args=...)` | **同步阻塞**跑一个 workflow 文件（CLI 入口用）。planning `scripts/main.py` 调这条。 |
| `load_workflow_source(path)` | 读 workflow 源码（lint / 静态分析用）。 |
| `Runtime / Journal / AgentBackend / MockBackend / AgentResult` | 框架内部类，单测会用到。 |
| `WorkflowError / MetaError / LintError / SchemaError` | 异常类型分类。 |

```python
# scripts/main.py
from openjiuwen.agent_teams.workflow.engine.runner import run_workflow
result = asyncio.run(run_workflow("scripts/planning_flow.py", args={...}))
```

### 2.9 🚨 必读：`run_workflow()` 不传 `backend=` 时默认走 `MockBackend`

**这是 2026-08-27 planning skill 真打 LLM 才暴露的坑**——文档首版没写，踩了一天才发现。

**问题**：`openjiuwen/agent_teams/workflow/engine/runner.py:149` 写死
`backend=backend or MockBackend()`。`run_workflow()` **不传 `backend` 时**，
引擎默认塞一个 `MockBackend`——返回 `[mock:{label}] generated text {n}` 这种
**假字符串**，**完全不读 `.env` 里的 API_KEY**。

**症状**：`facade.agent()` 返的 `text` 看起来像 LLM 输出（带 persona 风格），但
实际上是 mock 文本。你的 `.env` 配得再对，planning skill 一样走 mock——这就是
为什么纯结构性回归测试（不调 LLM）看不出来，**只有真打 LLM 才暴露**。

**修复**：写一个 `AgentBackend` 子类转发到 jiuwenswarm 的真 LLM 客户端，传给
`run_workflow` 的 `backend=` 参数。planning skill 的实现见
[`scripts/_llm_backend.py`](../scripts/_llm_backend.py)：

```python
# scripts/_llm_backend.py
from openjiuwen.agent_teams.workflow.engine.backends import (
    AgentBackend, AgentResult,
)

class JiuwenBackend(AgentBackend):
    """把 facade.agent() 转发到 JiuwenSwarmChatClient（自动读 .env 凭证）。"""

    def __init__(self) -> None:
        super().__init__()
        # 延迟 import：避免 .env 缺失时 import 阶段就炸
        from jiuwenswarm.symphony.llm import LLMConfig, create_llm_client
        self._client = create_llm_client(LLMConfig.from_default_model())

    async def run(
        self, prompt: str, opts: dict, schema_json: dict | None,
    ) -> AgentResult:
        label = opts.get("label") or "agent"
        text = await self._client.complete_json_async(
            system_prompt="",  # persona 已在 prompt 头部
            user_content=prompt,
            error_context=f"JiuwenBackend[{label}]",
        )
        # 粗算 token（与 MockBackend._result 公式一致），保证 ledger 不卡 0
        # 真实 token 走 jiuwenswarm 内部 _TOKEN_USAGE_TRACKER
        tokens = (len(prompt) + len(text)) // 4
        self.budget.add(tokens)
        if schema_json is not None:
            try:
                structured = json.loads(text)
            except (ValueError, TypeError):
                structured = None
            if structured is not None:
                return AgentResult(structured=structured, tokens=tokens)
        return AgentResult(text=text, tokens=tokens)
```

```python
# scripts/main.py —— 调起时显式传 backend
from scripts._llm_backend import JiuwenBackend
try:
    backend = JiuwenBackend()           # .env 缺失时清晰报错
except Exception as e:
    print(f"[main] JiuwenBackend 初始化失败: {e}", file=sys.stderr)
    backend = None                      # 让 run_workflow 退到 MockBackend
result = asyncio.run(
    run_workflow(_PLANNING_FLOW, args=workflow_args, backend=backend),
)
```

**为什么 jiuwenswarm 自己的 LLM 客户端是走 `config.yaml` 而不是 `.env`**：
`LLMConfig.from_default_model()` 不读 `.env`，是读 `config.yaml` 的
`models.defaults[0]`——里面的 `${API_KEY}` / `${MODEL_NAME}` 等占位符靠
`resolve_env_vars()` 从 `os.environ` 替换。**所以 `.env` 必须在
`LLMConfig` 解析前 load 进 `os.environ`**（`scripts/main.py: 39-46` 那段
`load_dotenv` 不能省）。

**会话四件套（`open_session` / `send_turn` / `close_session`）不实现**：
JiuwenBackend 只覆盖 `run()`，会话方法保留基类默认 `raise NotImplementedError`。
框架明确：单次 backend 不实现会清晰报错，我们用不到 `agent_session()` 多轮。

---

## 3. 决策树：「我要调 LLM 怎么办？」

```
要调 LLM
  ├─ 一次性、无状态 ────────────►  await agent(prompt, ...)
  ├─ 多轮、有状态（同一 agent 上下文延续）►  agent_session(...).send(...)
  └─ 让真人审 / 决定 ────────────►  await human(prompt, schema=MyDecisionModel)

要并发跑 N 个 LLM sub-agent
  ├─ 结果互不依赖 ──────────►  await parallel([lambda: agent(...), ...])
  └─ 每 stage 间不共享数据 ──►  await pipeline(items, stage1_fn, stage2_fn)

要嵌一个子 workflow ──────────►  await workflow("path/to/flow.py", args=...)

要打 trace / 标阶段 ──────────►  phase("X") / log("...") / budget.spent()
```

## 4. planning-skill 已落地的 facade 模式

| 模式 | 用到原语 | 文件 |
|---|---|---|
| 单次调 LLM sub-agent | `agent(prompt, label, phase, schema=None)` + 自己 `json.loads` | [_subagent.py](../scripts/_subagent.py) |
| Pydantic 强约束人审 | `human(prompt, schema=MethodReviewDecision)` 4 处 | [planning_flow.py](../scripts/planning_flow.py) |
| 阶段标记 | `phase("方法设计")` 等 5 处 | [planning_flow.py](../scripts/planning_flow.py) |
| 进度日志 | `log("...")` 多处 | [planning_flow.py](../scripts/planning_flow.py) |
| TUI 集成 | `human()` 自动接 TUI `h` 键 | 无需代码 |
| CLI 兜底 | `--no-human-review` 让 `human()` 走 schema 默认值 | [main.py](../scripts/main.py) |

## 5. 旧 `run_subagent.py` 走了什么弯路（**踩坑留底**）

| 坑 | 表现 | facade 怎么避免 |
|---|---|---|
| 自己同步包 `asyncio.run()` | "asyncio.run() cannot be called from a running event loop" | `agent()` 本来就是 async，与 workflow event loop 天然兼容 |
| 单例 LLM 客户端 + 全局 `reset_llm_client()` | 改 .env 后忘了重置 → 拿到旧 client | `agent()` 走 provider seam，provider 内部统一管 LLMClient 生命周期 |
| prompt 拼装（persona + JSON payload）手撸 | persona 路径容易写错 | planning-skill 把"persona 抽取 + 拼 prompt"封在 `_subagent.py`，其它 stage 脚本只调 `call_agent_async(name, payload, phase=...)` |
| 抽 22 个 facade API 自己实现 | 重复造 `phase / log / parallel` 等 | facade 自带，planning_skill 只调 `phase` / `log` 即可 |

**结论**：sub-agent 拉起**永远走 facade.agent()**；skill 内部可以再封一层
helper（如 `_subagent.call_agent_async`）加 persona 抽取，但**不要**自己
import `JiuwenSwarmChatClient` 调 LLM。

## 6. 调试小抄

```python
# 1) 看 facade 全部原语
from openjiuwen.agent_teams.workflow.engine.facade import (
    agent, human, phase, log, parallel, pipeline, map_parallel, pmap,
    agent_session, human_session, workflow, budget, compact, flatten_filter,
    use_provider, current_provider, reset_provider,
)
print(__all__)  # 模块有 __all__，列全部 35 个导出

# 2) 走 mock backend 跑 workflow（不真发 LLM 请求）
from openjiuwen.agent_teams.workflow.engine.facade import use_provider
from openjiuwen.agent_teams.workflow.engine.backends import MockBackend
# ... use_provider(some_mock_provider)  # 框架内部用，单测才会写

# 3) 看当前 provider
print(current_provider())
```
