# JiuwenSwarm TUI 使用 SwarmFlow 指南

> 本文档面向 **JiuwenSwarm TUI 用户**，介绍如何在终端界面中使用 SwarmFlow（Swarm 工作流）功能，包括配置启用、触发运行、实时监控、交互查看等完整流程。

---

## 概述

**SwarmFlow** 是 JiuwenSwarm 在 Team 模式下提供的工作流编排与执行能力。当多个 Agent 组成团队协作完成复杂任务时，SwarmFlow 将整个任务分解为有序的 **阶段（Phase）**，每个阶段内由若干 **Agent** 并行或串行执行子任务。SwarmFlow 提供了从启动、进度追踪到结果查看的全生命周期管理。

### 核心概念

| 概念 | 说明 |
|------|------|
| **Workflow（工作流）** | 一次完整的 SwarmFlow 运行实例，包含多个阶段和 Agent |
| **Phase（阶段）** | 工作流中的一个执行阶段，如"调研"、"分析"、"撰写" |
| **Agent（智能体）** | 阶段内执行具体子任务的团队成员 |
| **Run ID** | 每次工作流运行的唯一标识，由 `SwarmflowTool` 在启动时生成 |
| **状态** | 工作流/阶段/Agent 各自拥有独立状态：`running` / `completed` / `failed` / `stopped` / `planned` / `pending` |

### 工作流生命周期

```
workflow_started → phase → agent_started → agent_completed/failed → ... → workflow_completed/failed
```

1. **启动**：Leader Agent 分析用户需求，通过 `SwarmflowTool` 启动工作流，生成 `run_id`
2. **阶段推进**：工作流按 Phase 顺序推进，每个 Phase 内的 Agent 并行执行
3. **状态聚合**：`WorkflowMonitorHandler` 实时聚合进度事件，推送增量更新
4. **完成/失败**：所有阶段执行完毕后工作流进入终态

---

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

SwarmFlow 默认在 Team 模式配置中启用。配置文件位于 `~/.jiuwenswarm/config/config.yaml`。

### 配置项

在 `config.yaml` 的 `modes.team` 段中，`enable_swarmflow` 字段控制是否启用 SwarmFlow：

```yaml
modes:
  team:
    jiuwen_team:
      team_name: jiuwen_team
      lifecycle: persistent
      teammate_mode: build_mode
      spawn_mode: inprocess
      enable_swarmflow: true    # 设为 true 启用 SwarmFlow， 默认为True

      leader:
        member_name: team-leader
        display_name: 团队领导
        persona: "天才项目管理专家，擅长任务分解和团队协调"

      agents:
        leader: $agent_leader

      workspace:
        enabled: true

      transport:
        type: inprocess

      storage:
        type: sqlite
```

### 关键配置说明

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `enable_swarmflow` | 是否启用 SwarmFlow 工作流 | `true` |
| `team_name` | 团队名称 | `jiuwen_team` |
| `lifecycle` | 团队生命周期：`persistent`（持久）/ `temporary`（临时） | `persistent` |
| `teammate_mode` | 成员构建模式：`build_mode` | `build_mode` |
| `spawn_mode` | 进程模式：`inprocess`（单机）/ 分布式见 [分布式Team](分布式Team.md) | `inprocess` |

> **注意**：修改配置后需重启 JiuwenSwarm 后端服务（`jiuwenswarm-start`）使配置生效。

---

## 使用 SwarmFlow

### 步骤一：切换到 Team 模式

在 TUI 中输入以下命令切换到 Team 模式：

```
/mode team
```

切换成功后，TUI 界面会显示当前模式为 `team`，Leader Agent 将负责协调团队任务。

### 步骤二：发起任务

在 Team 模式下直接输入任务描述即可。SwarmFlow 会在 Leader 分析任务后自动启动：

```
在swarmflow模式下，调研新能源汽车行业，生成一份分析报告
```

Leader Agent 会：
1. 分析需求，将任务分解为多个阶段（如：调研 → 分析 → 撰写 → 审校）
2. 通过 `SwarmflowTool` 启动工作流，生成 `run_id`
3. 为每个阶段分配团队成员执行子任务

### 步骤三：监控工作流进度

工作流启动后，TUI 主界面会自动显示运行中的工作流状态横幅：

```
◐ 1 workflow running
  新能源汽车行业调研 · 2m 15s
```

横幅中包含：
- 动态旋转指示器（`◐◓◑◒`）
- 运行中的工作流数量
- 工作流名称与已运行时长

### 步骤四：查看工作流详情

使用 `/swarmflows` 命令打开 SwarmFlow 交互式查看器：

```
/swarmflows
```

也可使用别名：

```
/swarmworkflows
```

---

## SwarmFlow 交互式查看器

`/swarmflows` 打开后进入全屏交互视图，提供三层导航：**工作流列表 → 阶段详情 → Agent 详情**。

### 第一层：工作流列表

显示当前会话中所有工作流的概览：

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
| 状态图标 | `●` running / `○` pending / `◎` planned / `✓` completed / `✗` failed / `■` stopped |
| 工作流名称 | 由 `SwarmflowTool` 启动时设定 |
| Agent 进度 | `已完成/总计` 格式 |

**操作**：

| 按键 | 功能 |
|------|------|
| `↑` / `↓` | 在工作流列表间移动焦点 |
| `Enter` | 进入选中工作流的阶段详情 |
| `r` | 刷新工作流列表 |
| `Esc` | 关闭查看器，返回对话 |

### 第二层：阶段详情

选中某个工作流后进入阶段详情视图，左侧显示阶段列表，右侧显示当前阶段的 Agent 列表：

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
  ○ 撰写       0/2               ✓ completed 信息搜集员   · glm-5

press l to see full logs
up/down select phase · Enter show agents · Tab/Right agents · Esc back
```

**操作**：

| 按键 | 功能 |
|------|------|
| `↑` / `↓` | 在阶段列表间移动焦点 |
| `Enter` | 将焦点切换到 Agent 列表 |
| `Tab` / `→` | 在 **阶段（Phases）** 和 **Agent** 列表间切换焦点 |
| `←` | 返回工作流列表 |
| `l` | 查看工作流完整日志（进入文件查看器） |
| `r` | 刷新 |
| `Esc` | 返回工作流列表 |

### 第三层：Agent 详情

在 Agent 列表中选中某个 Agent 后按 `Enter` 进入详情：

```
数据研究员
新能源汽车行业调研 · 调研
● running · glm-5
duration 45s

Prompt
  调研新能源汽车行业近三年的市场数据，包括销量、...

Outcome
  （Agent 完成后显示执行结果）

press p to see full prompt - o outcome - e error
Esc/← back
```

**操作**：

| 按键 | 功能 |
|------|------|
| `p` | 查看完整 Prompt（进入文件查看器） |
| `o` | 查看完整 Outcome（进入文件查看器） |
| `e` | 查看完整 Error 信息（进入文件查看器，仅失败时可用） |
| `←` / `Esc` | 返回阶段详情 |

### 文件查看器

查看日志、Prompt、Outcome 或 Error 时，会进入全屏文件查看器：

| 按键 | 功能 |
|------|------|
| `↑` / `↓` | 上下滚动 |
| `PgUp` / `PgDn` | 上下翻页 |
| `Home` / `g` | 跳到开头 |
| `End` / `Shift+g` | 跳到末尾 |
| `Esc` / `Ctrl+C` | 退出查看器 |

---

## 工作流状态说明

### 工作流状态

| 状态 | 说明 |
|------|------|
| `running` | 工作流正在执行中 |
| `pending` | 工作流已创建，等待执行 |
| `planned` | 工作流已规划，尚未启动 |
| `completed` | 工作流所有阶段执行完毕 |
| `failed` | 工作流执行过程中出错 |
| `stopped` | 工作流被用户中断或停止 |

### 阶段状态

| 状态 | 说明 |
|------|------|
| `running` | 阶段正在执行 |
| `planned` | 阶段已规划，尚未开始 |
| `completed` | 阶段内所有 Agent 执行完毕 |
| `failed` | 阶段执行出错 |

### Agent 状态

| 状态 | 说明 |
|------|------|
| `running` | Agent 正在执行子任务 |
| `completed` | Agent 已完成子任务 |
| `failed` | Agent 执行出错 |

---