# SkillDev 模式设计文档

> 版本：v1.0 

---

## 1. 定位与目标

SkillDev 是 JiuWenSwarm 平台的一种**运行模式**，专门用于辅助开发者端到端地创建、测试、优化并打包一个 Agent Skill（`.skill` 包）。

它不是一个对话式 Agent，而是一条**确定性工程流水线**：接受用户需求描述，依次经过规划、代码生成、格式校验、测试、评测、改进、打包、描述优化等阶段，最终输出可以直接安装到 JiuWenSwarm 的 Skill 产物。

**三个入口模式**（系统自动识别，无需前端传入标志位）：

| 模式 | 触发条件 | 场景 |
|---|---|---|
| `create` | 仅有 `query` | 从零创建新 Skill |
| `create_with_resources` | `query` + `resources` | 携带参考资料（文档/代码）创建 |
| `modify` | `query` + `existing_skill` | 修改/升级已有 Skill |

---

## 2. 整体架构

### 2.1 在 JiuWenSwarm 中的位置

```
前端（对话框 + 弹窗 + Todo列表 + 产物列表）
    ↕ WebSocket / HTTP（E2A 协议，AgentResponseChunk 流）
Gateway 层（路由层保证同一 task_id 的请求到同一实例）
    ↓
JiuWenSwarm.process_message_stream()
    ├── 普通 chat 请求  → ReActAgent
    ├── skills.* 请求   → SkillManager
    └── skilldev.* 请求 → SkillDevService   ← 本文档的范围
```

SkillDev 与主 ReActAgent **完全隔离**，不共享对话上下文、内存、会话状态，仅复用：
- 模型配置（`model_name` + `model_client_config`）
- MCP 工具工厂函数（`mcp_tools_factory`）
- 文件系统访问配置（`sysop_config`）

### 2.2 模块划分

```
jiuwenswarm/agentserver/skilldev/
├── schema.py          # 数据模型层：枚举、状态、事件、挂起点配置、评测数据结构
├── pipeline.py        # 编排层：确定性状态机（运行 & 恢复逻辑）
├── service.py         # 服务层：无状态请求处理器，Method 路由
├── context.py         # 上下文层：阶段运行环境（emit + create_stage_agent）
├── deps.py            # 依赖注入：最小外部依赖集合
├── store.py           # 基础设施：状态持久化（checkpoint）
├── workspace.py       # 基础设施：任务工作区管理
└── stages/            # 阶段处理器层
    ├── base.py              # StageHandler 抽象基类 + StageResult
    ├── init_stage.py        # INIT：资源预处理
    ├── plan_stage.py        # PLAN：需求分析与规划
    ├── generate_stage.py    # GENERATE：SKILL.md 生成
    ├── validate_stage.py    # VALIDATE：格式校验
    ├── test_design_stage.py # TEST_DESIGN：测试用例设计
    ├── test_run_stage.py    # TEST_RUN：测试执行
    ├── evaluate_stage.py    # EVALUATE：评分 + 聚合 + 分析
    ├── improve_stage.py     # IMPROVE：根据反馈改进
    ├── package_stage.py     # PACKAGE：打包 .skill
    └── desc_optimize_stage.py # DESC_OPTIMIZE：描述优化循环
```

**分层依赖关系**（只允许上层依赖下层）：

```
service.py
    → pipeline.py
        → stages/*.py
            → context.py
                → deps.py
                    → store.py
                    → workspace.py
    → schema.py（所有层均可依赖）
```

---

## 3. Pipeline 状态机

### 3.1 完整阶段流程

```
INIT → PLAN → PLAN_CONFIRM* → GENERATE → VALIDATE
    → TEST_DESIGN → TEST_RUN → EVALUATE → REVIEW*
    → IMPROVE → (循环回 TEST_RUN)
    → PACKAGE → DESC_OPTIMIZE_CONFIRM* → DESC_OPTIMIZE → COMPLETED

标注 * 的为挂起点（Suspension Point）：Pipeline 在此暂停，等待前端用户确认
```

| 阶段 | 类型 | 职责 |
|---|---|---|
| `INIT` | 执行 | 资源解压、已有 Skill 加载、状态初始化 |
| `PLAN` | 执行 | ReActAgent 分析需求，输出结构化开发计划 |
| `PLAN_CONFIRM` | **挂起点** | 等待用户审阅并确认（或修改）plan |
| `GENERATE` | 执行 | ReActAgent 按 plan 生成 SKILL.md |
| `VALIDATE` | 执行 | 静态校验 SKILL.md 格式（frontmatter 合法性、命名规范） |
| `TEST_DESIGN` | 执行 | ReActAgent 设计测试用例集（EvalSet） |
| `TEST_RUN` | 执行 | 执行测试用例，采集 GradingResult + RunTiming |
| `EVALUATE` | 执行 | Grader 评分 → 聚合 Benchmark → Analyst 生成分析报告 |
| `REVIEW` | **挂起点** | 等待用户决定：继续改进 or 通过打包 |
| `IMPROVE` | 执行 | ReActAgent 根据 feedback_history 优化 SKILL.md |
| `PACKAGE` | 执行 | 打包为 `.skill` 压缩包 |
| `DESC_OPTIMIZE_CONFIRM` | **挂起点** | 询问用户是否需要描述优化 |
| `DESC_OPTIMIZE` | 执行 | 描述优化循环（train/test 分组，迭代拟合） |
| `COMPLETED` | 终态 | 流程结束 |
| `ERROR` | 终态 | 不可恢复错误 |

### 3.2 Pipeline 生命周期

Pipeline **不长驻内存**。每次请求的处理流程：

```
收到请求
  → StateStore 加载状态（或创建新状态）
  → new SkillDevPipeline(state, deps)
  → pipeline.run() 或 pipeline.resume()
  → 执行到挂起点或终态
  → StateStore 保存状态（checkpoint）
  → Pipeline 对象释放
```

这意味着即使服务重启，任务也能从上次 checkpoint 恢复继续执行。

### 3.3 run() 的内部逻辑

```python
while stage not in (COMPLETED, ERROR):
    if stage in SUSPENSION_POINTS:       # 命中挂起点
        emit TODOS_UPDATE                # 更新左侧 Todo 列表
        emit CONFIRM_REQUEST             # 驱动前端弹出确认框
        checkpoint()
        break                            # 暂停，等待下次 resume()

    handler = STAGE_HANDLERS[stage]      # 查找处理器
    emit STAGE_CHANGED                   # 通知前端阶段变更
    emit TODOS_UPDATE                    # 同步 Todo 状态
    result = await handler.execute(ctx)  # 执行阶段逻辑
    state.stage = result.next_stage      # 跳转下一阶段
    checkpoint()
```

### 3.4 resume() 的内部逻辑

```python
def resume(data: dict):
    suspension = SUSPENSION_POINTS[state.stage]  # 当前必须是挂起点
    suspension.on_resume(state, data)             # 更新状态（写入用户的 plan/反馈）
    next_stage = suspension.next_stage            # 计算下一阶段
    if callable(next_stage):
        next_stage = next_stage(data)             # REVIEW 的下一阶段由用户 action 决定
    state.stage = next_stage
    yield from run()                              # 继续执行
```

---

## 4. 挂起点（Suspension Points）机制

挂起点是 Pipeline 的**结构化暂停**：Pipeline 到达该阶段时不执行任何 Agent 逻辑，而是向前端推送确认请求，然后等待用户响应。

### 4.1 SuspensionConfig 结构

```python
@dataclass
class SuspensionConfig:
    confirm_type: str           # 标识确认类型（前端用于选择弹框样式）
    title: str                  # 弹框标题
    message: str                # 弹框描述文字
    actions: list[dict]         # 按钮列表：[{"id": "confirm", "label": "确认", "style": "primary"}]
    extract_data: Callable      # (state) → dict，从 state 提取要展示的数据
    on_resume: Callable         # (state, data) → None，根据用户响应更新 state
    next_stage: Stage | Callable # 下一阶段（REVIEW 的下一阶段取决于用户选择）
```

### 4.2 三个挂起点配置

**PLAN_CONFIRM（计划确认）**
- 推送事件：`CONFIRM_REQUEST { confirm_type: "plan_confirm", data: { plan: {...} } }`
- 用户操作："确认" → 写入 `state.plan`，跳转 GENERATE；"修改" → 前端在对话框中提出修改意见，通过 `skilldev.respond` 带入新的 plan 重新提交
- 状态变更：`state.plan = data["plan"]`，`state.plan_confirmed_at = 时间戳`

**REVIEW（评测审阅）**
- 推送事件：`CONFIRM_REQUEST { confirm_type: "review", data: { benchmark, report, iteration } }`
- 用户操作："通过，进入打包" → 跳转 PACKAGE；"继续改进" → `feedback_history` 追加记录，跳转 IMPROVE
- 状态变更：`state.feedback_history.append({ iteration, feedback })`

**DESC_OPTIMIZE_CONFIRM（描述优化确认）**
- 推送事件：`CONFIRM_REQUEST { confirm_type: "desc_optimize_confirm", data: { current_description } }`
- 用户操作："优化" → 跳转 DESC_OPTIMIZE；"跳过" → 跳转 COMPLETED
- 状态变更：无（纯路由决策）

---

## 5. 事件系统

后端通过 WebSocket 流式推送 `AgentResponseChunk`，前端根据 `event_type` 直接映射 UI 动作。

### 5.1 事件分类

| 事件类型 | 触发时机 | 前端响应 |
|---|---|---|
| `skilldev.stage_changed` | 每次阶段切换 | 内部标识，可用于调试 |
| `skilldev.progress` | 阶段内进度说明 | 对话流中显示文字提示 |
| `skilldev.agent_thinking` | Agent 推理 token 流 | 对话流中实时显示思考过程 |
| `skilldev.test_progress` | 测试执行中 | 对话流中显示测试进度 |
| `skilldev.todos_update` | 每次阶段切换 & 挂起点 | **更新右侧 Todo 列表** |
| `skilldev.confirm_request` | 命中挂起点 | **弹出确认框** |
| `skilldev.artifact_ready` | 生成文件/打包完成 | **更新右侧产物/附件列表** |
| `skilldev.eval_ready` | EVALUATE 完成 | 对话流中展示评测详情 |
| `skilldev.validate_result` | VALIDATE 完成 | 对话流中展示校验报告 |
| `skilldev.desc_opt_ready` | DESC_OPTIMIZE 完成 | 对话流中展示 before/after |
| `skilldev.error` | 不可恢复错误 | 显示错误，停止流程 |

### 5.2 关键事件 Payload 结构

**`skilldev.confirm_request`**（驱动前端弹窗的核心事件）：
```json
{
  "event_type": "skilldev.confirm_request",
  "task_id": "sd_xxx",
  "confirm_type": "plan_confirm",
  "title": "请审阅开发计划",
  "message": "以下是生成的开发计划，请确认或修改",
  "actions": [
    {"id": "confirm", "label": "确认", "style": "primary"},
    {"id": "modify",  "label": "修改", "style": "secondary"}
  ],
  "data": {
    "plan": { "skill_name": "...", "description": "...", ... }
  }
}
```

**`skilldev.todos_update`**（驱动前端 Todo 列表）：
```json
{
  "event_type": "skilldev.todos_update",
  "task_id": "sd_xxx",
  "todos": [
    {"id": "plan",         "label": "需求分析与规划", "status": "completed"},
    {"id": "generate",     "label": "技能生成与校验", "status": "in_progress"},
    {"id": "test",         "label": "测试与评测",     "status": "pending"},
    {"id": "improve",      "label": "优化改进",       "status": "pending"},
    {"id": "package",      "label": "打包",           "status": "pending"},
    {"id": "desc_optimize","label": "描述优化",       "status": "pending"}
  ]
}
```

**`skilldev.artifact_ready`**（驱动前端产物列表）：
```json
{
  "event_type": "skilldev.artifact_ready",
  "task_id": "sd_xxx",
  "artifact": {
    "id": "skill_package",
    "name": "my_skill.skill",
    "type": "skill_package",
    "size_bytes": 12345,
    "browsable": true,
    "downloadable": true
  }
}
```

### 5.3 后端驱动原则

**Todo 列表的计算完全由后端控制**，前端只做渲染。`compute_todos()` 根据 `current_stage` 和 `mode` 动态计算每个分组的状态（`completed` / `in_progress` / `pending`）：

```python
_STAGE_GROUPS = [
    _StageGroup(id="plan",         stages={INIT, PLAN, PLAN_CONFIRM}),
    _StageGroup(id="generate",     stages={GENERATE, VALIDATE}),
    _StageGroup(id="test",         stages={TEST_DESIGN, TEST_RUN, EVALUATE, REVIEW}),
    _StageGroup(id="improve",      stages={IMPROVE}),
    _StageGroup(id="package",      stages={PACKAGE}),
    _StageGroup(id="desc_optimize",stages={DESC_OPTIMIZE_CONFIRM, DESC_OPTIMIZE}),
]
```

---

## 6. 外部 API 接口

前端通过以下 7 个 Method 与 SkillDev 交互，所有请求统一走 `JiuWenSwarm.process_message_stream()`，由 `_SKILLDEV_METHODS` 前缀匹配自动路由到 `SkillDevService`。

### 6.1 接口总览

| Method | 类型 | 说明 |
|---|---|---|
| `skilldev.start` | 流式 | 发起新任务（或升级已有 Skill） |
| `skilldev.respond` | 流式 | 统一确认入口，后端按当前阶段自动路由 |
| `skilldev.status` | 一次性 | 查询单任务状态 / 列出所有任务 |
| `skilldev.download` | 一次性 | 下载打包产物（Base64） |
| `skilldev.cancel` | 一次性 | 取消任务 |
| `skilldev.file.list` | 一次性 | 获取工作区文件树（产物浏览） |
| `skilldev.file.read` | 一次性 | 读取工作区文件内容 |

### 6.2 接口详情

#### `skilldev.start` — 发起新任务

**请求参数（params）**：
```json
{
  "query": "帮我创建一个能搜索和下载 arXiv 论文的 Skill",
  "tools": ["web_search", "file_write"],
  "resources": ["/path/to/api_docs.pdf"],
  "existing_skill": null
}
```
- `existing_skill` 不为 null 时，系统判定为 `modify` 模式

**响应事件流**：
```
→ {event_type: "skilldev.started",       task_id: "sd_xxx"}          # 立即返回 task_id
→ {event_type: "skilldev.stage_changed", stage: "init"}
→ {event_type: "skilldev.todos_update",  todos: [...]}
→ {event_type: "skilldev.stage_changed", stage: "plan"}
→ {event_type: "skilldev.agent_thinking", delta: "...", status: "thinking"}
→ ... (plan 阶段 Agent 推理流)
→ {event_type: "skilldev.stage_changed",  stage: "plan_confirm"}
→ {event_type: "skilldev.todos_update",   todos: [...]}
→ {event_type: "skilldev.confirm_request", confirm_type: "plan_confirm", data: {plan: {...}}}
→ {event_type: "skilldev.suspended",      stage: "plan_confirm"}      # 流结束，等待用户
```

#### `skilldev.respond` — 统一确认入口

**请求参数（params）**：
```json
{
  "task_id": "sd_xxx",
  "action": "confirm",
  "plan": { ... }
}
```
- `action` 字段的合法值由 `CONFIRM_REQUEST` 事件的 `actions` 列表定义
- `plan` / `feedback` 等附加字段由具体挂起点的 `on_resume` 消费

**REVIEW 阶段的响应示例（用户选择继续改进）**：
```json
{
  "task_id": "sd_xxx",
  "action": "improve",
  "feedback": "测试用例 2 的边界条件处理有问题，请修复"
}
```

**响应事件流**（与 `start` 类似，从恢复点继续）：
```
→ ...各阶段事件...
→ {event_type: "skilldev.completed" | "skilldev.suspended", stage: "..."}
```

#### `skilldev.status` — 查询状态

**请求参数**：
- 查单个任务：`{ "task_id": "sd_xxx" }`
- 列所有任务：`{}`（不传 task_id）

**响应（单任务）**：
```json
{
  "ok": true,
  "task_id": "sd_xxx",
  "stage": "review",
  "mode": "create",
  "iteration": 1,
  "plan": { ... },
  "eval_results": { ... },
  "created_at": "2025-01-01T00:00:00Z",
  "updated_at": "2025-01-01T01:00:00Z"
}
```

#### `skilldev.download` — 下载产物

**请求参数**：`{ "task_id": "sd_xxx" }`

**响应**：
```json
{
  "ok": true,
  "filename": "arxiv_searcher.skill",
  "content_base64": "UEsDB...",
  "size_bytes": 12345
}
```

#### `skilldev.file.list` — 获取文件树

**请求参数**：`{ "task_id": "sd_xxx" }`

**响应**：
```json
{
  "ok": true,
  "tree": [
    {"path": "SKILL.md", "type": "file", "size": 2048},
    {"path": "tools/",   "type": "dir",  "children": [
      {"path": "tools/search.py", "type": "file", "size": 512}
    ]}
  ]
}
```

#### `skilldev.file.read` — 读取文件内容

**请求参数**：`{ "task_id": "sd_xxx", "path": "SKILL.md" }`

**响应**：
```json
{
  "ok": true,
  "path": "SKILL.md",
  "content": "---\nname: arxiv_searcher\n..."
}
```

---

## 7. 核心数据模型

### 7.1 SkillDevState — 运行时状态（唯一可信源）

```python
@dataclass
class SkillDevState:
    task_id: str
    stage: SkillDevStage        # 当前阶段
    mode: SkillDevTaskMode      # create / create_with_resources / modify
    iteration: int              # 改进轮次（从 0 开始）

    # 输入
    input: dict                 # query, tools, resources, existing_skill

    # 中间产物（按阶段逐渐填入）
    reference_texts: list[str]  # resources 解析后的文本
    existing_skill_md: str      # modify 模式时的原始 SKILL.md
    plan: dict                  # PLAN 阶段输出
    plan_confirmed_at: str      # 用户确认计划的时间
    evals: dict                 # TEST_DESIGN 输出的测试用例集
    eval_results: dict          # EVALUATE 输出的评测结果
    feedback_history: list      # 每轮 REVIEW 的用户反馈
    desc_optimize_result: dict  # DESC_OPTIMIZE 输出

    # 输出
    zip_path: str               # 打包产物路径
    zip_size: int               # 产物大小（bytes）

    # 元数据
    created_at: str
    updated_at: str
    error: str
```

**State 的生命周期**：
- `SkillDevService._handle_start()` 创建初始 State
- Pipeline 各阶段的 StageHandler 通过 `ctx.state` 读写
- `pipeline._checkpoint()` 在每个阶段边界将 State 序列化到 `state.json`
- `SkillDevService._handle_respond()` 从 `state.json` 加载并恢复

### 7.2 评测相关数据结构

评测阶段（TEST_DESIGN → TEST_RUN → EVALUATE）使用以下结构，设计参考 [official skill-creator](https://github.com/anthropics/anthropic-quickstarts/tree/main/skill-creator)：

```
EvalSet
  └── EvalCase[]         # 每个测试用例（id, prompt, expectations[]）

GradingResult            # 单次运行的评分结果
  └── GradingExpectation[] # 每条 assertion 的 pass/fail + 证据

RunTiming                # 单次运行的耗时/token 数据

Benchmark                # 完整基准测试结果
  └── BenchmarkRun[]     # with_skill vs baseline 的对比 run 记录

DescOptimizeIteration    # 描述优化的单轮迭代结果
```

---

## 8. 基础设施

### 8.1 StateStore — 状态持久化

**职责**：在阶段边界将 `SkillDevState` 序列化为 JSON 文件（checkpoint），支持断点续传。

**存储路径**：
```
~/.jiuwenswarm/agent/workspace/skilldev/{task_id}/state.json
```

**核心接口**：
```python
await store.save_state(task_id, state)      # checkpoint（阶段结束时调用）
await store.load_state(task_id)             # 恢复（resume 时调用）
store.load_state_sync(task_id)              # 同步版（status 查询时调用）
store.list_tasks()                          # 列出所有有效 task_id
```

**扩展点**：当前为本地文件实现，多实例部署时可替换为 Redis 实现，接口不变。

### 8.2 WorkspaceProvider — 任务工作区

**职责**：为每个 task_id 维护独立、标准化的工作区目录。

**目录结构**：
```
~/.jiuwenswarm/agent/workspace/skilldev/{task_id}/
├── state.json          ← StateStore 的 checkpoint 文件
├── resources/          ← 上传的资源文件（解压后的原始内容）
├── skill/              ← 生成的 Skill 目录（Agent 的写入区）
│   ├── SKILL.md
│   └── ...（工具实现文件等）
├── evals/
│   ├── evals.json          ← 测试用例定义（EvalSet）
│   └── iteration-{N}/      ← 第 N 轮测试的结果文件
│       ├── grading.json
│       └── timing.json
└── output/
    └── {skill_name}.skill  ← 最终打包产物
```

**核心接口**：
```python
workspace = await provider.ensure_local(task_id)  # 确保目录存在，返回路径
path = provider.get_local_path(task_id)           # 仅返回路径（不创建）
await provider.sync_to_remote(task_id)            # 扩展点：同步到远程存储
```

### 8.3 SkillDevDeps — 依赖注入

`SkillDevService` 不依赖 `JiuWenSwarm` 实例，只接收最小外部依赖：

```python
@dataclass
class SkillDevDeps:
    model_name: str                     # 默认模型名
    model_client_config: dict           # 模型调用配置
    mcp_tools_factory: Callable[[], list] # MCP 工具工厂函数
    sysop_config: object | None         # 文件系统访问配置
    state_store: StateStore             # 状态持久化
    workspace_provider: WorkspaceProvider # 工作区管理
```

由 `JiuWenSwarm._get_skilldev_service()` 懒初始化并注入（首次 `skilldev.*` 请求触发）。

---

## 9. 阶段处理器开发指南

### 9.1 StageHandler 合同

每个阶段实现一个 `StageHandler` 子类：

```python
class MyStageHandler(StageHandler):
    async def execute(self, ctx: SkillDevContext) -> StageResult:
        # 1. 从 ctx.state 读取上游数据
        plan = ctx.state.plan

        # 2. 通过 ctx.emit() 向前端推送进度事件
        await ctx.emit(SkillDevEventType.PROGRESS, {"message": "开始处理..."})

        # 3. 通过 ctx.create_stage_agent() 创建隔离 Agent 执行 AI 逻辑
        agent = ctx.create_stage_agent(
            stage_name="my_stage",
            system_prompt=MY_SYSTEM_PROMPT,
            tools=["file_read", "file_write"],
        )
        result = await agent.run(prompt)

        # 4. 将结果写入 ctx.state
        ctx.state.some_field = result

        # 5. 返回下一阶段
        return StageResult(next_stage=SkillDevStage.NEXT_STAGE)
```

**关键约束**：
- StageHandler 不得持有跨请求的状态（不能有实例变量保存业务数据）
- 所有业务状态通过 `ctx.state` 读写
- Agent 通过 `ctx.create_stage_agent()` 创建，每阶段独立，不共享上下文
- 通过 `ctx.workspace` 访问任务目录（Path 对象）

### 9.2 每阶段 Agent 隔离原则

| 阶段 | 推荐工具 | System Prompt 焦点 |
|---|---|---|
| PLAN | `web_search` | 分析需求，输出结构化 plan JSON |
| GENERATE | `file_write`, `file_read` | 按 plan 生成 SKILL.md 及支撑文件 |
| TEST_DESIGN | （无文件工具） | 根据 SKILL.md 设计测试用例 |
| TEST_RUN | `file_read`, skill 调用工具 | 执行测试，记录结果 |
| EVALUATE | （无文件工具） | Grader 评分 + Analyst 分析 |
| IMPROVE | `file_read`, `file_write` | 根据 feedback 修改 SKILL.md |
| DESC_OPTIMIZE | （无文件工具） | 迭代优化描述文字 |

### 9.3 注册新阶段的步骤

1. 在 `stages/` 下创建 `{name}_stage.py`，实现 `StageHandler`
2. 在 `stages/__init__.py` 导出新 Handler
3. 在 `schema.py` 的 `SkillDevStage` 枚举中添加新阶段值
4. 在 `pipeline.py` 的 `STAGE_HANDLERS` 字典中注册
5. 如需在 Todo 列表中显示，在 `schema.py` 的 `_STAGE_GROUPS` 中配置归属分组

---

## 10. 端到端调用示例

以下是一次完整 Skill 开发流程的接口调用时序（前端视角）：

```
① 用户在对话框输入需求
  → 前端发送: skilldev.start { query, tools }
  ← 后端推送: skilldev.started { task_id }
  ← 后端推送: (多个事件流...)
  ← 后端推送: skilldev.confirm_request { confirm_type: "plan_confirm", data: { plan } }
  ← 后端推送: skilldev.suspended { stage: "plan_confirm" }

② 前端弹出计划确认框，用户查看并点击"确认"
  → 前端发送: skilldev.respond { task_id, action: "confirm", plan: {...} }
  ← 后端推送: (GENERATE / VALIDATE / TEST_DESIGN / TEST_RUN / EVALUATE 各阶段事件流)
  ← 后端推送: skilldev.confirm_request { confirm_type: "review", data: { benchmark, report } }
  ← 后端推送: skilldev.suspended { stage: "review" }

③ 前端弹出评测结果审阅框，用户点击"继续改进"
  → 前端发送: skilldev.respond { task_id, action: "improve", feedback: "..." }
  ← 后端推送: (IMPROVE / TEST_RUN / EVALUATE 迭代事件流)
  ← 后端推送: skilldev.confirm_request { confirm_type: "review", data: { benchmark, report } }
  ← 后端推送: skilldev.suspended

④ 用户对结果满意，点击"通过，进入打包"
  → 前端发送: skilldev.respond { task_id, action: "accept" }
  ← 后端推送: (PACKAGE 事件流)
  ← 后端推送: skilldev.artifact_ready { type: "skill_package", downloadable: true }
  ← 后端推送: skilldev.confirm_request { confirm_type: "desc_optimize_confirm" }
  ← 后端推送: skilldev.suspended

⑤ 用户选择"优化"描述
  → 前端发送: skilldev.respond { task_id, action: "optimize" }
  ← 后端推送: (DESC_OPTIMIZE 事件流)
  ← 后端推送: skilldev.desc_opt_ready { before: "...", after: "..." }
  ← 后端推送: skilldev.completed

⑥ 用户下载产物
  → 前端发送: skilldev.download { task_id }
  ← 后端返回: { filename, content_base64, size_bytes }

⑦（可选）用户浏览工作区文件
  → 前端发送: skilldev.file.list { task_id }
  ← 后端返回: { tree: [...] }
  → 前端发送: skilldev.file.read { task_id, path: "SKILL.md" }
  ← 后端返回: { content: "..." }
```

---

## 11. 关键设计决策与约束

### 决策一：Pipeline 不长驻内存
**Why**：避免大量并发任务的内存积压；强制所有状态经过 StateStore 持久化，使服务重启透明。
**Trade-off**：每次请求都有 `load_state` / `save_state` 的文件 I/O 开销，但对于分钟级的 AI 任务可以忽略不计。

### 决策二：单一 `skilldev.respond` 确认入口
**Why**：前端不需要知道当前处于哪个挂起点，只需将用户的决策数据（`action` + 附加字段）发给后端，后端自动根据 `task_id` 当前阶段路由。
**扩展影响**：新增挂起点时，前端代码无需修改，只需在 `SUSPENSION_POINTS` 中注册新的 `SuspensionConfig`。

### 决策三：后端驱动 UI 状态
**Why**：Todo 列表、弹框内容、产物列表等 UI 状态全部由后端事件携带，前端纯渲染，避免前后端状态同步问题。
**实现**：`compute_todos()` 是 Todo 状态的唯一计算来源；`CONFIRM_REQUEST` 事件携带弹框的完整描述（标题、描述、按钮列表、展示数据）。

### 决策四：每阶段独立 Agent
**Why**：工具隔离（PLAN 阶段不应有文件写入工具）、Prompt 隔离（每阶段有焦点明确的系统提示）、内存隔离（避免长 context 干扰）。
**当前状态**：`create_stage_agent()` 接口已定义，实际接入 `openjiuwen ReActAgent` 的代码待实现（已有占位标注）。

### 决策五：工作区路径统一
**Why**：SkillDev 的任务目录必须在 JiuWenSwarm 的统一工作区下，避免数据散落在系统各处。
**约定**：`~/.jiuwenswarm/agent/workspace/skilldev/{task_id}/`，由 `get_workspace_dir() / "skilldev"` 构造。

---

## 12. 当前待办和扩展点

| 项目 | 位置 | 说明 |
|---|---|---|
| 接入 ReActAgent | `context.py:create_stage_agent()` | 待接入 `openjiuwen` 实际 Agent 构建逻辑 |
| 工具注册逻辑 | `context.py:_register_tools()` | 按工具名白名单注册到 Agent |
| sysop_config 构造 | `interface.py:_get_skilldev_service()` | 从 `_sysop_card_id` 构造文件系统权限配置 |
| 取消逻辑 | `service.py:_handle_cancel()` | 中断正在运行的 Pipeline（协程取消） |
| 远程存储同步 | `workspace.py:sync_to_remote()` | 多实例部署时同步到 S3/OBS/NFS |
| StateStore Redis 实现 | `store.py` | 多实例部署的分布式状态存储替换 |
| 各 StageHandler 的 Agent 实现 | `stages/*.py` | 当前各阶段均有待实现注释，逻辑框架已完整 |
