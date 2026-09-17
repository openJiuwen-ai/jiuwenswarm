# JiuwenSwarm TUI 使用 SwarmFlow 指南

> 本文档面向 **JiuwenSwarm TUI 用户**，介绍如何在终端界面中使用 SwarmFlow（Swarm 工作流）功能，包括配置启用、触发运行、实时监控、交互查看等完整流程。
>
> **声明**：文中 TUI 界面示例（代码块、状态图标、横幅文本等）仅为示意，可能与实际显示存在偏差，请以终端实际呈现为准。

---

## 概述

**SwarmFlow** 用 **Python 工作流脚本** 描述确定性的多 Agent 编排：阶段（Phase）、模型调用、并行/流水线、Human-in-the-loop 等。Team 模式下，Leader 通过 **`SwarmflowTool`**（`openjiuwen/agent_teams/workflow/tool_swarmflow.py`）在后台执行脚本；TUI 订阅运行事件并展示 Phase / Node 树。

常用入口：**`/swarmflow`** 开关、**`/swarmflows`** 查看运行树、**`h`** 回复 `human` / `human_session` 节点。

### 脚本从哪来

| 方式 | 说明 |
|------|------|
| Leader 即时生成 | Leader 调用 `swarmflow()` 工具；工具描述（`openjiuwen/agent_teams/tools/locales/descs/cn/workflow/swarmflow.md`）引导模型编写 `scripts/workflow.py` 并执行 |
| Swarm Skill 沉淀 | 用内置 **`swarmskill-creator`** Skill 生成带工作流脚本的 Swarm Skill，可安装复用或发布到 Skills Hub |
| 离线生成的脚本 | 在 Team 会话外预先编写 `scripts/workflow.py`（或团队工作空间内如 `swarmflow/<name>.py`），会话中让 Leader 以 **`swarmflow(script_path=...)`** 或内联 **`script`** 执行；改文件后可用同一路径重跑 |

合法脚本需顶层 **`META={...}`** 与 **`async def run(args)`**；算子通过 **`from swarmflow import ...`** 引入（运行时映射到 `openjiuwen/agent_teams/workflow/engine/facade.py`）。

多数场景不必离线编写脚本；会开关、看进度、回复 HITL 即可。要精确控制编排时，可走 **离线生成** 或 **Skill 沉淀**；下方算子表供编写参考。

### 脚本算子一览

脚本通过 `from swarmflow import ...` 引入下列算子；签名以 **`openjiuwen/agent_teams/workflow/engine/facade.py`** 为准。

**编排类**

| 算子 | 定义 | 说明 | 使用场景 |
|------|------|------|----------|
| `agent` | `async def agent(prompt, *, label=None, phase=None, schema=None, options=None)` | 拉起一次性 worker subagent；`schema` 约束结构化输出；`options` 见下表 | 单步 AI：检索、分析、生成片段；要 JSON/模型实例时传 `schema` |
| `parallel` | `async def parallel(thunks)` | Fork-join：lazy thunk 批量并行，**全部等齐**后返回；失败项为 `None`，调用不抛错 | 必须汇总**全量**结果再往下走：并行搜索后去重合并、发现数为 0 则跳过后续阶段 |
| `pipeline` | `async def pipeline(items, *stages)` | 无 barrier 流水线；每项独立穿过各 stage，单项失败不影响其余 | **默认首选**：多 item 各自多阶段（如多 URL 抓取→分析→摘要），item 之间不等齐 |
| `map_parallel` / `pmap` | `async def map_parallel(items, fn)`；`pmap` 为别名 | 对每项调用 `async fn(item)`，自动绑定 item，避免闭包陷阱 | 列表 fan-out 的简写；替代手写 `parallel([lambda x=x: ...])` |
| `phase` | `def phase(title)` | 标记当前 Phase，发出阶段事件 | 划分「调研 / 分析 / 撰写」等大步骤；与 `META.phases` 标题对齐 |
| `log` | `def log(message)` | 输出一条进度日志 | 旁白、里程碑说明（如「开始合并结果」） |
| `compact` | `def compact(xs)` | 过滤假值（`None`、`''`、`0`、`[]` 等） | `parallel` / `pipeline` 后去掉失败或空结果 |
| `flatten_filter` | `def flatten_filter(xs)` | 拍平一层并过滤假值 | 嵌套列表汇总后再下游处理 |

**有状态类**

| 算子 | 定义 | 说明 | 使用场景 |
|------|------|------|----------|
| `agent_session` | `def agent_session(*, label=None, phase=None, instructions=None, options=None) -> AgentSession` | 多轮 Agent；创建时与 `send(..., options=...)` 均支持 `options` | 同一 worker 多轮协作：迭代润色、分步 coding、上下文跨轮保持 |
| `human_session` | `def human_session(*, label=None, phase=None, instructions=None, options=None) -> HumanSession` | 多轮人工；等待不占并发槽 | 与真人多轮澄清需求、逐条补充材料 |
| `human` | `async def human(prompt, *, schema=None, label=None, phase=None, options=None)` | 单次人工提问，答完即关 | 一次性审批、确认、选型（HITL 单轮） |

**机制类**

| 算子 | 定义 | 说明 | 使用场景 |
|------|------|------|----------|
| `workflow` | `async def workflow(name_or_path, args=None)` | 内联运行另一份脚本（**最多嵌套一层**）；共享并发、budget | 复用已有 `workflow.py` 作子步骤；大流程拆模块组合 |
| `budget` | `budget.total` / `budget.spent()` / `budget.remaining()` | 读取 token 预算与已耗 | 动态 fan-out（`while remaining() > N`）；撞顶前主动收束 |

**`options` 参数袋**（定义见 `openjiuwen/agent_teams/workflow/engine/primitives.py` 中 `_ENGINE_OPTIONS`）

`agent()`、`human()` 及 `agent_session()` / `human_session()` 的 `send()` 均支持 `options={...}`。显式 kwargs（如 `label` / `phase` / `schema`）优先于 `options` 同名键；合法键为引擎白名单与 backend `KNOWN_OPTIONS` 的并集，**未知键 fail-fast**。

| 键 | 适用于 | 说明 |
|----|--------|------|
| `label` | `agent` / session | 进度事件中的显示标签（也可写为显式 kwargs） |
| `phase` | `agent` / session | 归入某 Phase 进度组；在 `parallel` / `pipeline` 内部建议显式传入 |
| `schema` | `agent` / `human` / `send` | 结构化输出：Pydantic 模型 / JSON Schema dict / 省略则返回文本 |
| `model` | `agent` / `agent_session` | 覆盖本次 worker 的模型名；**默认省略**，继承团队 teammate 模型 |
| `timeout` | `agent` / `human` | 超时（秒）：`agent` 为单次 backend 调用；`human` 为等待真人回复 |
| `isolation` | `agent` | 仅支持 `'worktree'`：在独立 git worktree 中跑 worker（并行改文件防冲突，开销较大） |
| `agent_type` | `agent` | 用具名专家 subagent 替代默认 worker（与 `schema` 可组合） |

示例：

```python
await agent(
    "分析竞品",
    label="analyst",
    phase="research",
    options={"model": "strong-model", "timeout": 120},
)

s = agent_session(label="writer", phase="draft", options={"model": "fast-model"})
await s.send("写第一段", options={"timeout": 60})
```

`agent_session()` / `human_session()` 创建时的 `options` 会作为会话默认值；每轮 `send(..., options=...)` 可覆盖或追加（同键以 `send` 为准）。

### 核心概念

| 概念 | 说明 |
|------|------|
| **Workflow（工作流）** | 一次 SwarmFlow **Run** 实例，由脚本驱动 |
| **Phase（阶段）** | 脚本中 `phase()` 声明的执行阶段 |
| **Node（节点）** | Phase 内执行单元：`agent` / `agent_session` / `human` / `human_session` |
| **Run ID** | 每次运行的唯一编号，用于标识与区分运行实例 |
| **Team budget** | 团队共享 token 上限与已用量（配置 `swarmflow_budget` 后在查看器可见） |

### 工作流生命周期

从 TUI 视角，一次 Run 大致如下（底层由引擎推进 Phase / Node，TUI 实时刷新状态）：

```
/swarmflow on → 发任务 → Phase 推进 → [Node: waiting_for_human → h 回复] → completed / failed
```

| 步骤 | 你做什么 | 界面表现 |
|------|----------|----------|
| 1. 开启 | `/swarmflow on`，`/swarmflow` 确认 | `swarmflow: on · mode: team · budget: ...` |
| 2. 启动 | Team 模式下发任务，Leader 调 `swarmflow()` | **运行横幅**（workflow 名、耗时） |
| 3. 跟进 | **`/swarmflows`** 打开运行树 | Phase / Node 状态、日志、Team budget |
| 4. HITL | `human` / `human_session` 触发 | 节点 **`waiting_for_human`**，主界面 **`h`** → `chat.swarmflow_reply` |
| 5. 结束 | 等待终态或关 session | Workflow **`completed`** / **`failed`** / **`stopped`** |

**常见分支**

| 情况 | TUI 表现 |
|------|----------|
| 嵌套 `workflow()` | 子 Phase 卡片 **`▸ {name} #{N}`** |
| 配置 `swarmflow_budget` | Team budget 行；token 用尽 → run **`failed`**，不可断点续跑 |

> Leader 聊天区的进度文字是辅助；**以 `/swarmflows` 状态为准**。配置变更后若未生效，执行 **`/new`** 新建 session。

## 前置条件

### 1. 安装并启动 JiuwenSwarm 后端

```bash
# 安装
pip install jiuwenswarm

# 初始化（首次）
jiuwenswarm-init

# 启动后端服务
jiuwenswarm-start
```

### 2. 安装并启动 TUI

```bash
# 安装 TUI
pip install jiuwenswarm-tui

# 启动 TUI（另开终端）
jiuwenswarm-tui
```

> TUI 通过 WebSocket 连接本机 Gateway 的 TUI 端点（默认 `ws://127.0.0.1:19001/tui`）。请确保后端服务已启动。

### 3. 配置模型 API

首次使用需在配置中设置模型 API。可通过 Web 前端（`http://localhost:5173`）的 **配置信息** 面板，或在 TUI 中使用 `/config` 命令完成配置。

---

## 启用 SwarmFlow

运行时配置文件：`~/.jiuwenswarm/config/config.yaml`（由 `jiuwenswarm-init` 从 **`jiuwenswarm/resources/config.yaml`** 初始化）。

SwarmFlow 仅涉及 `modes.team.jiuwen_team` 下两项（仓库模板原文如下）：

```yaml
modes:
  team:
    jiuwen_team:
      enable_swarmflow: false

      # Team-level token budget ceiling for swarmflow runs.
      # Unset / null = unbounded (no ceiling).  Set to a positive integer to
      # cap total tokens across ALL swarmflow runs spawned by this team.
      # swarmflow_budget: 500000
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `enable_swarmflow` | 为 `true` 时 Leader 可调用 `swarmflow()` 启动工作流；为 `false` 时即使 `/mode team` 也**不会**跑 SwarmFlow | `false` |
| `swarmflow_budget` | Leader 级 token 上限，本 Team 下所有 run **共享**；脚本内 `budget.total` / `spent()` / `remaining()` 可读。设为**正整数**启用上限；**未设置或 `null`** 表示无硬上限 | 未设置（模板中为注释） |

出厂模板默认 **关闭** SwarmFlow（`enable_swarmflow: false`），首次使用请在下列途径之一将其打开。

### 修改配置的三种途径

| 途径 | 适用项 | 做法 | 生效方式 |
|------|--------|------|----------|
| **1. 编辑 `config.yaml`** | `enable_swarmflow`、`swarmflow_budget` | 修改 `~/.jiuwenswarm/config/config.yaml` 中 `modes.team.jiuwen_team` 对应字段 | **重启后端服务**后全局生效 |
| **2. Web 前端** | 仅 `enable_swarmflow` | 进入 **更多 → 配置信息 → 其他配置 → SwarmFlow**，勾选「启用 SwarmFlow」开关（**集群模式**；前端**不暴露** `swarmflow_budget`，预算须改 `config.yaml` 或用 TUI `/swarmflow on --budget`） | **不支持热更新**：保存后需**新建会话**（新开聊天或等价操作）才生效；正在跑的 workflow 不会被中断 |
| **3. TUI `/swarmflow`** | `enable_swarmflow`；`swarmflow_budget` 可用 `on --budget` | `/swarmflow on` / `off` 开关；`/swarmflow on --budget <tokens\|none>` 改预算；`/swarmflow` 查询状态 | 写入配置后提示 **`Use /new to apply.`** — 当前 session 不热更新，执行 **`/new`** 新建 session 后生效 |

> **选用建议**：日常在 TUI 里用 **`/swarmflow on`** 最省事；长期固定策略可改 `config.yaml` 并重启；在 Web 集群模式下开启 SwarmFlow 后记得**新开会话**。

---

## TUI 命令：`/swarmflow`

`/swarmflow` 是 TUI 里启用/关闭 SwarmFlow 的**首选入口**：读写配置项 `enable_swarmflow`，并显示当前 mode。

### 子命令

| 命令 | 作用 |
|------|------|
| `/swarmflow` | 查询状态，形如 `swarmflow: on · mode: team · budget: unbounded`（设了预算则显示具体值） |
| `/swarmflow on` | 写入 `enable_swarmflow=true`；当前非 team 时会**一并切到 team**；可选 `--budget <tokens\|none>` |
| `/swarmflow off` | 写入 `enable_swarmflow=false`；**不会**自动离开 team |
| `/swarmflow invalid` | 报错并提示正确用法 `on` / `off` |

### 推荐用法（首次启用）

```
/swarmflow on
/swarmflow          # 确认：swarmflow: on · mode: team · budget: unbounded
```

然后直接输入任务，Leader 会在后台调用 **`SwarmflowTool`**（`openjiuwen/agent_teams/workflow/tool_swarmflow.py`）跑工作流脚本。

### 与 `/mode team` 的关系

| 方式 | 何时用 |
|------|--------|
| **`/swarmflow on`** | 推荐：一次完成「开 SwarmFlow + 进 team」 |
| `/mode team` | 仅切模式；若配置里 `enable_swarmflow: false`，Leader **不会**跑 SwarmFlow |

### 生效边界（session）

| 场景 | 行为 |
|------|------|
| 非 team → `/swarmflow on` | 本地切 team 并写配置；**下次** workflow run 即可看到监控 |
| 已在 team、开关 off → `/swarmflow on` | 写配置；当前 session 的 monitor 可能未补建 → 执行 **`/new`** 后立刻生效 |
| team 且已有运行中 workflow → `/swarmflow off` | **不中断**当前 run；关闭对**新 session** 生效，必要时 `/new` |
| 重复 `/swarmflow on`（已 on） | 提示已开启，不再写配置 |

关闭 SwarmFlow：`/swarmflow off`，再用 `/swarmflow` 确认 `swarmflow: off`。

---

## 使用 SwarmFlow

### 步骤一：开启 SwarmFlow

在 TUI 执行：

```
/swarmflow on
```

查询是否就绪：

```
/swarmflow
```

期望输出类似：`swarmflow: on · mode: team · budget: unbounded`（未设预算时显示 `unbounded`，设了则显示具体 token 数）。

若已在 team 但刚打开开关，且界面仍未出现 workflow 横幅，执行 **`/new`** 新建 session 后再发任务。

> 等价做法：手动 `/mode team` 且保证配置里 `enable_swarmflow: true`；不如 `/swarmflow on` 省事。

### 步骤二：发起任务

在 Team 模式下直接输入任务描述即可。SwarmFlow 会在 Leader 分析任务后自动启动：

```
在swarmflow模式下，调研新能源汽车行业，生成一份分析报告
```

Leader Agent 会：
1. 分析需求，将任务分解为多个阶段（如：调研 → 分析 → 撰写 → 审校）
2. 通过 **`SwarmflowTool`**（`openjiuwen/agent_teams/workflow/tool_swarmflow.py`）启动工作流，生成 `run_id`
3. 为每个阶段分配 worker / 会话节点执行子任务

### 步骤三：沉淀工作流为 Swarm Skill（可选）

当某个工作流编排需要反复使用时，可借助内置的 **`swarmskill-creator`** Skill 把它固化为可复用的 Swarm Skill（含 `workflow.py` 脚本），之后会话中直接让 Leader 调用该 Skill 即可，不必每次重新生成脚本。

`swarmskill-creator` 为内置技能，默认已安装；若环境中缺失，先安装：

```
/skills search swarmskill-creator
/skills install swarmskill-creator
```

随后在 Team 会话中直接召唤该 Skill，并描述要沉淀的工作流（支持 CREATE 从零创建、CONVERT 把单 Agent Skill 转为 Swarm Skill、MODIFY 修改已有 Swarm Skill 三种模式）：

```
/swarmskill-creator 帮我把「调研→分析→撰写→审校」这套工作流做成可复用的 Swarm Skill
```

创建出的 Swarm Skill 可本地复用，或发布到 Skills Hub 供其他团队安装。详见 `docs/zh/SwarmSkills.md`。

### 步骤四：监控工作流进度

工作流启动后，TUI 主界面会自动显示运行中的工作流状态横幅：

```
◐ 1 workflow running
  新能源汽车行业调研 · 2m 15s
```

横幅中包含：
- 动态旋转指示器（`◐◓◑◒`）
- 运行中的工作流数量
- 工作流名称与已运行时长

### 步骤五：查看工作流详情

```
/swarmflows
```

别名：`/swarmworkflows`。

### 步骤六：关闭 SwarmFlow（可选）

不再使用工作流编排时：

```
/swarmflow off
/swarmflow    # 确认 swarmflow: off
```

当前 session 里已在跑的 workflow **不会被**该命令强行停止。

---

## 人工介入（HITL）

脚本使用 `human` / `human_session` 时，节点会进入 **`waiting_for_human`**，TUI 展示待答问题。

### 主界面快捷操作

| 操作 | 作用 |
|------|------|
| 小写 **`h`** | 存在 pending 人工节点时，进入 **pending-list**（不是 `/swarmflows` 列表） |
| 回复框 | 选中 pending 项后输入 Answer，`Enter` 提交；经 `chat.swarmflow_reply` 送回引擎 |

### 在 `/swarmflows` 查看器中

- **human / human_session** 节点显示 Question；提交回复后短暂 `running`，再 `completed` 或 `failed`
- **agent_session** 为多轮 LLM 会话节点，无人工等待态
- 普通 **agent** 为单轮 worker 节点

### TUI 命令对照

| 命令 / 按键 | 用途 |
|-------------|------|
| **`/swarmflow` / `on` / `off`** | 开关 SwarmFlow、查状态 |
| **`/swarmflows`** | 全屏查看运行树 |
| **`h`** | 主界面 pending 人工回复列表 |

---

## SwarmFlow 交互式查看器

`/swarmflows` 打开全屏视图（需已 `/swarmflow on`）。导航：**工作流列表 → 阶段详情 → 节点详情**。

### 嵌套子工作流

脚本调用 `workflow()` 时，查看器为子流建 **child phase** 卡，展示名形如 `▸ intro #0`（并发同名子流用 `#N` 区分）。子流内节点挂在 child 卡下，而非混在父 author phase 里。

### Token 与 Team budget

| 展示位置 | 内容 |
|----------|------|
| 节点详情 | 单次 `agent` / 会话 turn 的 **tokens**（有 provider usage 时） |
| Run 摘要 | **Team budget**：`spent / total`（配置了 `swarmflow_budget` 时） |
| 预算耗尽 | Run 进入 **failed** 终态；不可 resume（与 pause 不同） |

### 第一层：工作流列表

```
Swarm workflows
2 running, 1 completed

  ● running  新能源汽车行业调研      3/8 agents
  ● running  竞品分析               1/5 agents
  ✓ completed 用户画像分析           6 agents

up/down select - Enter view - r refresh - Esc close
```

| 信息 | 说明 |
|------|------|
| 状态图标 | `●` running / `○` pending / `◇` planned / `✓` completed / `×` failed / `■` stopped |
| 工作流名称 | 由 **`SwarmflowTool`**（`openjiuwen/agent_teams/workflow/tool_swarmflow.py`）启动时设定 |
| Agent 进度 | `已完成/总计` 格式 |

**操作**：

| 按键 | 功能 |
|------|------|
| `↑` / `↓` | 在工作流列表间移动焦点 |
| `Enter` | 进入选中工作流的阶段详情 |
| `r` | 刷新工作流列表 |
| `Esc` | 关闭查看器，返回对话 |

### 第二层：阶段详情

选中某个工作流后进入阶段详情视图，左侧为阶段列表，右侧为当前阶段的 **Agents** 列表（含 `agent` / `agent_session` / `human` / `human_session` 四类节点，见 [第三层：节点详情](#第三层节点详情)）：

```
新能源汽车行业调研
调研新能源汽车行业并生成分析报告
● running · 3/8 agents
2m 15s

Logs
  [leader] 启动调研阶段...
  [researcher] 正在搜索行业数据...

Phases                          Agents · 调研
  ✓ 调研       2/3               ● running  数据研究员    · glm-5
  ● 分析       1/3               ● running  市场分析师    · glm-5
  ◇ 撰写       0/2               ✓ completed 信息搜集员   · glm-5

press l to see full logs
up/down select phase · Right agents · Left back · Esc back
```

**操作**：

| 按键 | 功能 |
|------|------|
| `↑` / `↓` | 在阶段列表间移动焦点 |
| `→` | 将焦点从 **阶段（Phases）** 切到 **Agents** 列表；已在 Agents 时按 `→` 进入选中节点的详情 |
| `←` | 返回上一层：Agents → Phases → 工作流列表 |
| `l` | 查看工作流完整日志（进入文件查看器） |
| `r` | 刷新 |
| `Esc` | 返回工作流列表 |

### 第三层：节点详情

第二层 **Agents** 列表中的每一项，对应脚本里的一类 **Node**（`node_type`）。选中后按 `→` 进入详情；session 类算子还可能显示 **父行 + turn 子行**（如 `turn 0`、`turn 1`）。

#### 四类节点对照

| 脚本算子 | 列表形态 | 详情页字段 | 轮次与历史 |
|----------|----------|------------|------------|
| **`agent()`** | 独立单行 | **模型**、**Prompt**、**Outcome** / **Error** | 单次调用；**无** Session History |
| **`agent_session()`** | 同 phase、同 `label` 下：**session 父行** + **turn 子行** | 每 turn：**模型**、**Prompt**、**Outcome** / **Error** | 多轮 LLM；详情按 **`s`** → **Session History**（多轮 Prompt/Outcome） |
| **`human()`** | 独立单行 | **Question**、**Answer**（等待时为 `waiting_for_human`） | 单次 HITL；**无** Session History；主界面 **`h`** 回复 |
| **`human_session()`** | 同 **`agent_session`** 的树形结构 | 每 turn：**Question**、**Answer** | 多轮 HITL；**`s`** → Session History；waiting turn 可在历史中 **`Tab`** 回复 |

> **区分要点**：`agent` / `agent_session` 走 **Prompt → Outcome**（AI worker）；`human` / `human_session` 走 **Question → Answer**（真人）。`human` 进度事件若带模型名，行内可能显示为 `human(模型名)`。

#### 示例：`agent()` 节点

```
数据研究员
新能源汽车行业调研 · 调研
● running · glm-5
duration 45s

Prompt
  调研新能源汽车行业近三年的市场数据，包括销量、...

Outcome
  （完成后显示）

press p prompt · o outcome · e error · s session history (若适用)
Esc/← back
```

#### 示例：`human()` / `human_session()` 节点

```
审批人
新能源汽车行业调研 · 审校
☺ waiting_for_human

Question
  是否批准发布该分析报告？

Answer
  （等待回复；主界面按 h，或 Session History 中 Tab 回复）

press q question · a answer · s session history (仅 human_session 多轮)
Esc/← back
```

**操作**：

| 按键 | 功能 | 适用节点 |
|------|------|----------|
| `p` | 查看完整 **Prompt**（文件查看器） | `agent` / `agent_session` |
| `q` | 查看完整 **Question**（文件查看器） | `human` / `human_session` |
| `o` | 查看完整 **Outcome** | `agent` / `agent_session` |
| `a` | 查看完整 **Answer** | `human` / `human_session` |
| `e` | 查看 **Error**（失败时） | 所有节点 |
| **`s`** | **Session History** | 仅 **`agent_session`** / **`human_session`** 多轮 |
| `Tab` | 进入回复模式（编辑器聚焦） | 仅 `waiting_for_human` 的人工节点 |
| `←` / `Esc` | 返回阶段详情 |

### 文件查看器

查看日志、Prompt、Outcome 或 Error 时，会进入全屏文件查看器：

| 按键 | 功能 |
|------|------|
| `↑` / `↓` | 上下滚动 |
| `PgUp` / `PgDn` | 上下翻页 |
| `Home` / `g` | 跳到开头 |
| `End` / `Shift+g` | 跳到末尾 |
| `Esc` | 退出查看器 |

---

## 工作流状态说明

`/swarmflows` 中的状态名与引擎事件一致：

### Workflow

| 状态 | 说明 |
|------|------|
| `planned` | 已规划，尚未启动 |
| `pending` | 已创建，等待调度 |
| `running` | 执行中 |
| `completed` | 全部 Phase 完成 |
| `failed` | 脚本报错或 token budget 超限 |
| `stopped` | 用户中断或 session 结束 |

### Phase

| 状态 | 说明 |
|------|------|
| `planned` | 尚未开始 |
| `running` | 执行中 |
| `completed` | 本 Phase 内 Node 均已终态 |
| `failed` | Phase 出错，或 run 进入终态时被 seal |
| `stopped` | run 终态或 session 销毁时被 seal |

### Node

| 状态 | 说明 |
|------|------|
| `running` | 执行中（含 `agent_session` 多轮） |
| `waiting_for_human` | 等待 HITL 回复（`human` / `human_session`） |
| `completed` | 成功完成 |
| `failed` | 执行失败 |
| `stopped` | run/phase 终态或 session 销毁时被 seal |

---
---

## 返回导航

- [返回文档首页](../README.md)
- [返回项目首页](../../README_CN.md)