# SkillDev 模式设计文档

> 版本：v1.2（与当前实现对齐，代码优先）

---

## 1. 定位与目标

SkillDev 是 JiuWenClaw 的工程化 Skill 生产模式，目标是把「用户需求」转换为可交付的 `.skill` 产物，并在过程中提供可恢复、可审阅、可迭代的流水线能力。

它不是普通聊天对话，而是由后端状态机驱动的确定性流程，前端主要负责渲染事件与收集用户确认。

---

## 2. 系统结构

### 2.1 在系统中的位置

```text
前端（WebSocket req/res/event）
   ↓
JiuWenClaw.process_message_stream()
   ↓
SkillDevService.handle()
   ↓
SkillDevPipeline.run()/resume()
   ↓
StageHandlers + StateStore + WorkspaceProvider
```

`skilldev.*` 请求直接进入 `SkillDevService`，不经过主对话 Agent。

### 2.1.1 WebSocket 协议约定（前后端联调重点）

- 上行请求帧：`{ "type": "req", "id": "...", "method": "skilldev.xxx", "params": {...} }`
- 下行响应帧：
  - 一次性接口：`type = "res"`（与 `req.id` 对齐）
  - 流式接口：`type = "event"`（靠 `payload.task_id` 关联任务）
- `skilldev.start`、`skilldev.respond` 采用流式事件；其它 `skilldev.*` 多为单次响应
- 同一任务恢复/续跑建议传入固定 `task_id`（或固定 `session_id` 被映射为同一 `task_id`）

### 2.2 模块划分

```text
jiuwenclaw/agentserver/skilldev/
├── schema.py            # 阶段/事件/状态/挂起点配置/Todo分组
├── pipeline.py          # 状态机编排（run/resume）
├── service.py           # method 路由与 API 处理
├── context.py           # 阶段执行上下文（emit + stage agent）
├── deps.py              # 依赖注入定义（含 session_history）
├── store.py             # state.json 持久化
├── workspace.py         # task 工作区管理
└── stages/
    ├── base.py              # StageHandler 抽象基类 + StageResult
    ├── init_stage.py        # INIT：工作区初始化与上传资源落盘
    ├── clarify_stage.py     # CLARIFY：生成澄清问题
    ├── generate_stage.py    # GENERATE：SKILL.md 生成
    ├── validate_stage.py    # VALIDATE：格式校验
    ├── test_design_stage.py # TEST_DESIGN：测试用例设计
    ├── test_run_stage.py    # TEST_RUN：测试执行
    ├── evaluate_stage.py    # EVALUATE：评分 + 聚合 + 分析
    ├── improve_stage.py     # IMPROVE：根据反馈改进
    ├── desc_optimize_stage.py # DESC_OPTIMIZE：描述优化循环
    └── package_stage.py     # PACKAGE：打包 .skill
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

## 3. 状态机与阶段流程

### 3.1 当前生效主流程

```text
INIT
  -> CLARIFY
  -> QUESTION_CLARIFY*              (挂起：澄清问答)
  -> GENERATE
  -> VALIDATE
  -> SKIP_TESTS_CONFIRM*            (挂起：是否跳过测试)
  -> TEST_DESIGN
  -> TEST_RUN
  -> EVALUATE
  -> REVIEW*                        (挂起：评测审阅)
  -> IMPROVE                        (可选循环回 TEST_RUN)
  -> DESC_OPTIMIZE_CONFIRM*         (挂起：是否描述优化)
  -> DESC_OPTIMIZE                  (可选)
  -> PACKAGE
  -> COMPLETED
```

`*` 为挂起点（Suspension Point），需要 `skilldev.respond` 恢复。

### 3.2 阶段职责总览

| 阶段 | 类型 | 职责 |
|---|---|---|
| `INIT` | 执行 | 初始化工作区、落盘输入资源、读取初始上下文 |
| `CLARIFY` | 执行 | 生成结构化澄清问题 |
| `QUESTION_CLARIFY` | 挂起 | 等待用户回答澄清问题 |
| `GENERATE` | 执行 | 生成/更新 `skill/SKILL.md` 与相关文件 |
| `VALIDATE` | 执行 | 校验 `SKILL.md` frontmatter 与规范 |
| `SKIP_TESTS_CONFIRM` | 挂起 | 询问用户运行完整测试或跳过测试 |
| `TEST_DESIGN` | 执行 | 设计评测用例 |
| `TEST_RUN` | 执行 | 执行评测/测试 |
| `EVALUATE` | 执行 | 聚合评分与报告 |
| `REVIEW` | 挂起 | 用户审阅评测并选择通过或继续改进 |
| `IMPROVE` | 执行 | 按反馈改进后回测 |
| `DESC_OPTIMIZE_CONFIRM` | 挂起 | 确认是否做 description 优化 |
| `DESC_OPTIMIZE` | 执行 | 运行描述优化循环 |
| `PACKAGE` | 执行 | 打包 `.skill` |
| `COMPLETED`/`ERROR` | 终态 | 结束或错误退出 |
| `ERROR` | 终态 | 不可恢复错误或取消 |

### 3.3 run/resume 生命周期

每次请求不复用旧 Pipeline 实例，统一是：

1. 从 `StateStore` 读取状态（或初始化）
2. 创建 `SkillDevPipeline`
3. `run()` 执行到挂起点或终态
4. 边界 checkpoint 到 `state.json`
5. 释放实例

`resume(data)` 仅允许从当前挂起点恢复，按 `SUSPENSION_POINTS[current_stage]` 的 `on_resume + next_stage` 继续执行。

### 3.4 `run()` 关键伪代码

```python
while state.stage not in (COMPLETED, ERROR):
    if cancel_event.is_set():
        state.stage = ERROR
        state.error = "任务已取消"
        checkpoint()
        break

    if state.stage in SUSPENSION_POINTS:
        emit(TODOS_UPDATE, compute_todos(...))
        emit(CONFIRM_REQUEST, suspension_payload(...))
        checkpoint()
        break

    emit(STAGE_CHANGED, {"stage": state.stage.value, "iteration": state.iteration})
    emit(TODOS_UPDATE, compute_todos(...))
    result = await handler.execute(ctx)
    state.stage = result.next_stage
    checkpoint()
```

### 3.5 `resume(data)` 关键伪代码

```python
suspension = SUSPENSION_POINTS[state.stage]
suspension.on_resume(state, data)
next_stage = suspension.next_stage(data) if callable(...) else suspension.next_stage
state.stage = next_stage
yield from run()
```

---

## 4. 挂起点机制（SUSPENSION_POINTS）

### 4.1 统一配置结构

`schema.py` 中每个挂起点均用 `SuspensionConfig` 描述：

- `confirm_type`
- `title/message`
- `actions`
- `extract_data(state)`
- `on_resume(state, data)`
- `next_stage`（可为函数，按 action 动态分流）

### 4.2 当前 4 个挂起点

#### 1) `QUESTION_CLARIFY`
- `confirm_type`: `question_clarify`
- action: `submit`
- 输入关键字段：`answers`
- 恢复后：进入 `GENERATE`

#### 2) `SKIP_TESTS_CONFIRM`（新增）
- `confirm_type`: `skip_tests_confirm`
- action: `run_tests` / `skip_tests`
- `run_tests` -> `TEST_DESIGN`
- `skip_tests` -> `DESC_OPTIMIZE_CONFIRM`

#### 3) `REVIEW`
- `confirm_type`: `review`
- action: `accept` / `improve`
- `accept` -> `DESC_OPTIMIZE_CONFIRM`
- `improve` -> `IMPROVE`（可携带 `feedback`）

#### 4) `DESC_OPTIMIZE_CONFIRM`
- `confirm_type`: `desc_optimize_confirm`
- action: `optimize` / `skip`
- `optimize` -> `DESC_OPTIMIZE`
- `skip` -> `PACKAGE`

### 4.3 `confirm_request` 事件示例

```json
{
  "event_type": "skilldev.confirm_request",
  "task_id": "sd_xxx",
  "confirm_type": "skip_tests_confirm",
  "title": "测试流程",
  "message": "SKILL.md 已通过校验。你可以运行完整测试，或跳过测试并进入打包前优化确认。",
  "actions": [
    { "id": "run_tests", "label": "运行测试", "style": "primary" },
    { "id": "skip_tests", "label": "跳过测试", "style": "secondary" }
  ],
  "data": {
    "skill_name": "my_skill",
    "current_description": "..."
  }
}
```

---

## 5. 事件系统与前端驱动

### 5.1 设计原则

SkillDev 事件模型采用“后端描述语义、前端直接渲染”的模式，前端不做流程推理：

- 后端决定当前阶段、待办状态、确认卡内容、可下载产物
- 前端按 `event_type` 进行确定性分发，不基于文本猜测业务语义
- 所有流式事件都携带 `task_id`，用于多任务并行时路由到正确会话

### 5.2 事件类型分层

| 分层 | 事件 | 作用 |
|---|---|---|
| 流程控制 | `skilldev.started` `skilldev.stage_changed` `skilldev.suspended` `skilldev.completed` `skilldev.error` | 驱动流程状态机 UI |
| 过程输出 | `skilldev.progress` `skilldev.agent_thinking` `skilldev.agent_output` | 渲染运行日志与模型输出 |
| 工具观测 | `skilldev.tool_call` `skilldev.tool_result` | 展示工具执行轨迹 |
| 结构化 UI | `skilldev.todos_update` `skilldev.confirm_request` `skilldev.artifact_ready` | 更新右侧面板/确认卡 |
| 阶段结果 | `skilldev.validate_result` `skilldev.eval_ready` `skilldev.desc_opt_ready` `skilldev.skill_name_ready` | 展示关键产物数据 |

### 5.3 前端消费映射（建议）

| 事件 | 前端动作 |
|---|---|
| `skilldev.stage_changed` | 更新阶段时间线与顶部状态 |
| `skilldev.todos_update` | 覆盖当前 Todo 列表（以后端为准） |
| `skilldev.confirm_request` | 渲染确认卡；按钮 id 直接作为 `respond.action` |
| `skilldev.agent_thinking` | 渲染 thinking 通道（可折叠） |
| `skilldev.agent_output` | 渲染正文输出通道 |
| `skilldev.tool_call/tool_result` | 按 `tool_call_id` 配对展示 |
| `skilldev.artifact_ready` | 更新产物列表，展示下载入口 |
| `skilldev.suspended` | 设置可交互状态（允许用户确认） |
| `skilldev.completed` | 标记结束，禁用继续输入 |
| `skilldev.error` | 标记失败并显示错误信息 |

### 5.4 事件时序约束（实现约定）

- 阶段开始前，通常先收到：`stage_changed` -> `todos_update`
- 命中挂起点时，通常收到：`todos_update` -> `confirm_request` -> `suspended`
- 正常结束时，收到：`completed`（并结束流）
- 异常结束时，收到：`error`（并结束流）

说明：同一阶段内部可能穿插 `progress` / `agent_*` / `tool_*` 事件。

### 5.5 典型事件 payload

`confirm_request`（澄清）：

```json
{
  "event_type": "skilldev.confirm_request",
  "task_id": "sd_xxx",
  "confirm_type": "question_clarify",
  "title": "请回答以下问题",
  "message": "AI 需要补齐关键信息后继续生成",
  "actions": [
    { "id": "submit", "label": "提交回答", "style": "primary" }
  ],
  "data": {
    "questions": [
      {
        "id": "q1",
        "question": "问题文本",
        "options": [{ "id": "o1", "label": "选项A" }],
        "allow_custom": true
      }
    ]
  }
}
```

`todos_update`：

```json
{
  "event_type": "skilldev.todos_update",
  "task_id": "sd_xxx",
  "todos": [
    { "id": "plan", "label": "需求澄清", "status": "completed" },
    { "id": "generate", "label": "技能生成与校验", "status": "in_progress" },
    { "id": "test", "label": "测试与评测", "status": "pending" }
  ]
}
```

`tool_call` / `tool_result`：

```json
{
  "event_type": "skilldev.tool_call",
  "task_id": "sd_xxx",
  "stage": "generate",
  "tool_call_id": "file_write_1746",
  "tool_name": "file_write",
  "arguments": { "path": "skill/SKILL.md", "content": "..." }
}
```

```json
{
  "event_type": "skilldev.tool_result",
  "task_id": "sd_xxx",
  "stage": "generate",
  "tool_call_id": "file_write_1746",
  "tool_name": "file_write",
  "success": true,
  "result": "ok"
}
```

`artifact_ready`：

```json
{
  "event_type": "skilldev.artifact_ready",
  "task_id": "sd_xxx",
  "artifact": {
    "id": "skill_package",
    "name": "my_skill.skill",
    "type": "skill_package",
    "size_bytes": 12345
  }
}
```

### 5.6 与会话恢复的关系

`skilldev.restore` 返回的 `timeline_items` 本质上是事件回放数据。前端恢复时应保持与实时流一致的事件处理路径：

1. 先用 `snapshot` 做基线初始化（stage/todos/artifacts/suspended）
2. 再按 `seq` 回放 `timeline_items`，复用实时事件 handler
3. 对确认卡事件优先使用显式 `confirm_seq` 关联 resolved 结果

这样可保证“恢复后界面”与“实时运行中界面”行为一致。

---

## 6. Todo 分组（compute_todos）

当前展示分组与阶段映射为：

| todo id | label | 包含阶段 |
|---|---|---|
| `plan` | 需求澄清 | `INIT` `CLARIFY` `QUESTION_CLARIFY` |
| `generate` | 技能生成与校验 | `GENERATE` `VALIDATE` |
| `test` | 测试与评测 | `SKIP_TESTS_CONFIRM` `TEST_DESIGN` `TEST_RUN` `EVALUATE` |
| `review` | 评测审阅 | `REVIEW` |
| `improve` | 优化改进 | `IMPROVE` |
| `desc_optimize` | 描述优化 | `DESC_OPTIMIZE_CONFIRM` `DESC_OPTIMIZE` |
| `package` | 打包 | `PACKAGE` |

终态规则：

- `COMPLETED` -> 全部 `completed`
- `ERROR` -> 全部 `cancelled`

---

## 7. 外部 API（service.py 对齐）

### 7.1 接口总览

| Method | 类型 | 说明 |
|---|---|---|
| `skilldev.start` | 流式 | 新建或续跑任务，执行到挂起/终态 |
| `skilldev.respond` | 流式 | 在挂起点恢复 |
| `skilldev.status` | 一次性 | 查单任务状态或列任务 |
| `skilldev.session.list` | 一次性 | 列可恢复会话摘要 |
| `skilldev.restore` | 一次性 | 拉取会话快照与时间线 |
| `skilldev.parse_skill` | 一次性 | 任务开始前导入 `.zip/.skill` 到 `skill/` |
| `skilldev.download` | 一次性 | 获取打包产物下载信息 |
| `skilldev.cancel` | 一次性 | 发送取消信号 |
| `skilldev.file.list` | 一次性 | 浏览 `skill/` 文件树 |
| `skilldev.file.read` | 一次性 | 读取 `skill/` 相对路径文件 |

### 7.2 `skilldev.start`

关键行为：

- `task_id = params.task_id or session_id`
- 已存在且未完成任务会续跑；不存在/已完成则新建状态
- 初始化输入字段：`query`、`files`、`skill_packages`、`tool_spec_files`
- 事件流结束时返回 `skilldev.completed` 或 `skilldev.suspended`

请求示例：

```json
{
  "task_id": "sd_xxx",
  "session_id": "sess_xxx",
  "query": "帮我创建一个用于接口文档审查的 skill",
  "files": [],
  "skill_packages": [],
  "tool_spec_files": []
}
```

### 7.3 `skilldev.respond`

关键约束：

- 当前阶段必须属于 `SUSPENSION_POINTS`
- 请求中 `action` 需匹配当前挂起点 `actions`
- 支持字段：`answers`、`feedback`（按挂起点消费）

请求示例（澄清）：

```json
{
  "task_id": "sd_xxx",
  "action": "submit",
  "answers": [
    { "question_id": "q1", "answer": "..." },
    { "question_id": "q2", "answer": "..." }
  ]
}
```

请求示例（评测审阅）：

```json
{
  "task_id": "sd_xxx",
  "action": "improve",
  "feedback": "增加错误处理说明，并补充一个反例测试"
}
```

### 7.4 `skilldev.parse_skill`（你提到的新增签名语义）

当前 handler 签名为：

```python
async def _handle_parse_skill(
    self, params: dict, request_id: str, channel_id: str, session_id: str
) -> AsyncIterator[AgentResponseChunk]:
```

关键行为：

- task 解析优先级：`params.task_id` -> `params.session_id` -> 入口 `session_id`
- 仅允许在未开始（无 `state.json`）时导入
- 支持 `base64Data` 或 `url` 下载
- 解压后解析 `SKILL.md` 并推送 `skilldev.skill_name_ready`

### 7.5 `skilldev.session.list` / `skilldev.restore`

- `session.list`：返回会话摘要列表（task_id/stage/更新时间/是否挂起等）
- `restore`：返回 `{ task_id, snapshot, timeline_items, version }`

`session.list` 响应示例：

```json
{
  "ok": true,
  "sessions": [
    {
      "task_id": "sd_abc",
      "stage": "review",
      "updated_at": "2026-05-09T07:00:00Z",
      "created_at": "2026-05-09T06:50:00Z",
      "is_suspended": true
    }
  ]
}
```

`restore` 响应示例（节选）：

```json
{
  "ok": true,
  "task_id": "sd_abc",
  "snapshot": {
    "task_id": "sd_abc",
    "stage": "review",
    "is_suspended": true,
    "is_processing": false,
    "query": "..."
  },
  "timeline_items": [
    { "seq": 1, "source": "user", "event_type": "skilldev.user_start", "payload": { "query": "..." } },
    { "seq": 2, "source": "assistant", "event_type": "skilldev.started", "payload": { "task_id": "sd_abc" } }
  ],
  "version": "1"
}
```

### 7.6 其它接口关键细节

- `skilldev.status`
  - 无 `task_id`：返回 `{ ok: true, tasks: [...] }`
  - 有 `task_id`：返回 `state.to_status_dict()` 摘要
- `skilldev.download`
  - 当前返回下载信息（`filename/url/mimeType/exportId/exportedAt`），不再返回 base64 文件体
- `skilldev.cancel`
  - 若任务正在运行则设置取消信号；否则返回“任务未在运行中”
- `skilldev.file.list`
  - 仅暴露 `skill/` 子树，目录 path 以 `/` 结尾
- `skilldev.file.read`
  - 只允许读取 `skill/` 内相对路径，含路径越界保护

---

## 8. 会话保存与恢复设计（新增）

### 8.1 目标

支持页面刷新、断线重连、跨时刻回到同一任务并继续执行，且前端能恢复到“像刚刚运行到这里”的状态。

### 8.2 服务端保存点（session_history）

通过 `SkillDevDeps.session_history` 注入，`service.py` 在以下时机写入：

- `skilldev.start`：`append_user_start`
- `skilldev.respond`：`append_user_respond`
- 执行流事件：`append_agent_event`
- 请求结束：`save_state_snapshot`

并在确认响应后补写 `skilldev.confirm_resolved` 语义事件，保证恢复时可还原确认卡状态。

### 8.3 恢复接口语义

- `skilldev.session.list`：列可恢复任务
- `skilldev.restore`：返回
  - `snapshot`（状态基线）
  - `timeline_items`（事件时间线）
  - `version`（协议版本）

### 8.4 前端恢复策略（hydrate + replay）

恢复分两步：

1. `hydrateFromSkillDevRestore(snapshot)`：先重建任务基线（stage/todos/artifacts/suspended 等）
2. `replayRestoreTimeline(timeline_items)`：再回放历史消息、工具调用、确认卡状态

设计要点：

- 时间线按 `seq/timestamp` 回放，使用历史时间戳而不是当前时间
- 确认卡优先按 `confirm_seq` 关联 resolved 事件
- 仅在真实待确认挂起时保留可交互确认

---

## 9. 状态与持久化

### 9.1 状态对象

`SkillDevState` 是运行时唯一真相，核心字段包括：

- `task_id` `stage` `mode` `iteration`
- `input`（query/files/skill_packages/tool_spec_files）
- `clarification_questions` `clarification_answers`
- `plan`（兼容字段，承载技能元信息）
- `skipped_benchmark_tests`
- `evals` `eval_results` `feedback_history`
- `zip_path` `zip_size`
- `error` `created_at` `updated_at`

建议重点关注这些“流程行为字段”：

- `generate_retries` / `last_validate_error`：控制 GENERATE <-> VALIDATE 重试
- `skipped_benchmark_tests`：记录是否在 `skip_tests_confirm` 选择跳过测试
- `feedback_history`：REVIEW->IMPROVE 的迭代反馈历史
- `desc_optimize_result`：描述优化阶段的迭代产物

### 9.3 checkpoint 接口语义

- `save_state(task_id, state)`：阶段边界持久化
- `load_state(task_id)`：异步恢复（start/respond 路径）
- `load_state_sync(task_id)`：同步读取（status/轻量查询）
- `list_tasks()`：列所有有效任务

### 9.2 落盘路径

任务目录：

```text
{workspace}/skilldev/{task_id}/
  ├── state.json
  ├── skill/
  ├── resources/
  ├── evals/
  └── output/
```

会话历史（若启用）：

```text
{workspace}/skilldev/{task_id}/session_history/
  ├── events.jsonl
  └── snapshot.json
```

---

## 10. 阶段开发约束

- 阶段处理器必须实现 `StageHandler.execute(ctx) -> StageResult`
- 阶段间状态只能通过 `ctx.state` 传递
- 每阶段 Agent 隔离，工具受 `STAGE_TOOL_WHITELIST` 约束
- 不允许把跨请求业务状态放到 handler 实例变量

### 10.1 阶段工具隔离（概念映射）

| 阶段 | 工具侧重点 |
|---|---|
| `CLARIFY` | 只读文件与检索 |
| `GENERATE` | 读写/编辑文件 + 必要执行能力 |
| `VALIDATE` | 只读校验 + shell 校验命令 |
| `TEST_DESIGN` | 读取技能与写入评测定义 |
| `TEST_RUN` | shell/code 执行 |
| `EVALUATE` | 只读分析 |
| `IMPROVE` | 读写/编辑文件 |
| `PACKAGE` | 打包命令与文件读取 |
| `DESC_OPTIMIZE` | 轻量读取 |

### 10.2 新增阶段的注册步骤

1. 在 `stages/` 新建 `{name}_stage.py` 并实现 `StageHandler`
2. 在 `stages/__init__.py` 导出 Handler
3. 在 `schema.py` 的 `SkillDevStage` 加入枚举值
4. 在 `pipeline.py` 的 `STAGE_HANDLERS` 注册
5. 在 `schema.py` 的 `_STAGE_GROUPS` 增加 Todo 分组映射（如需展示）
6. 若为挂起点，在 `SUSPENSION_POINTS` 配置 `SuspensionConfig`

---

## 11. 典型时序（简化）

```text
1) start
   -> skilldev.start
   <- skilldev.started
   <- ...阶段事件...
   <- skilldev.confirm_request(question_clarify)
   <- skilldev.suspended

2) 澄清回答
   -> skilldev.respond(action=submit, answers=...)
   <- ...generate/validate...
   <- skilldev.confirm_request(skip_tests_confirm)
   <- skilldev.suspended

3) 测试决策
   -> skilldev.respond(action=run_tests 或 skip_tests)
   <- ...test/evaluate 或 desc_opt_confirm...

4) 评测审阅与改进
   -> skilldev.respond(action=improve, feedback=...)
   <- ...improve->test循环...
   -> skilldev.respond(action=accept)
   <- skilldev.confirm_request(desc_optimize_confirm)

5) 描述优化确认与打包
   -> skilldev.respond(action=optimize 或 skip)
   <- ...package...
   <- skilldev.artifact_ready
   <- skilldev.completed
```

---

## 12. 设计决策摘要

1. **无常驻 Pipeline**：每次请求临时创建，依赖 checkpoint 保障可恢复。
2. **统一 respond 入口**：前端不分挂起点，只传 `action + data`。
3. **后端驱动 UI 状态**：Todo/确认卡/产物由事件直驱，前端不做业务推断。
4. **会话可恢复**：snapshot + timeline 双轨恢复，兼容中断与刷新。

---

## 13. 扩展点与后续工作

| 项目 | 位置 | 说明 |
|---|---|---|
| TaskMode 自动推断增强 | `service.py` / `init_stage.py` | 可基于输入资源与工作区状态自动标注模式 |
| 远程状态存储 | `store.py` | 可替换本地 JSON 为 Redis/数据库 |
| 远程工作区同步 | `workspace.py:sync_to_remote()` | 支持多实例下共享工作区 |
| 更细粒度权限 | `deps.sysop_config` / `context.py` | 进一步收紧阶段工具与 shell 白名单 |
| 前端恢复体验 | `web/src/stores/skillDevStore.ts` | 可增加回放进度、断点定位、筛选事件 |

---

## 14. 参考文档

- [SESSION_HISTORY_API.md](./SESSION_HISTORY_API.md)
- [FRONTEND_RESTORE_WORKFLOW.md](./FRONTEND_RESTORE_WORKFLOW.md)
