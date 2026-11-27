# JiuWenClaw → 专用 Skill 生成 Agent 改造方案

> 版本：v1.0  
> 日期：2026-05-13  
> 状态：设计稿

---

## 1. 改造目标

将 JiuWenClaw 从**通用对话 Agent**改造为**专用 Skill 生成 Agent**。改造后的系统：

- 核心职能：接收用户需求 → 澄清 → 生成 Skill → 测试 → 评测 → 改进 → 打包
- 所有请求都围绕 Skill 生命周期展开
- 去除与 Skill 生成无关的功能模块（通用对话、团队协作、办公助手等）
- 保留必要的基础设施（配置系统、Session 管理、文件操作等）

---

## 2. 现状分析

### 2.1 当前架构全景

**三层入口架构：**

```
WebSocket 客户端
  │
  ▼
AgentWebSocketServer (agent_ws_server.py)
  ├── 短路处理（不进 JiuWenClaw）：
  │   ├── initialize          → AgentManager.initialize（ACP 能力协商）
  │   ├── session.*           → SessionManager
  │   ├── permissions.*       → PermissionsConfigRPC（15+ 权限管理 API）
  │   ├── history.get         → SessionHistory
  │   ├── command.*           → CommandHandler
  │   ├── browser.*           → BrowserHandler
  │   ├── config.cache_clear  → 清缓存
  │   ├── agent.reload_config → 热更新
  │   ├── extensions.*        → ExtensionRegistry
  │   └── 文件传输事件        → FileTransferManager
  │
  ▼ 其余 method
AgentManager (agent_manager.py)
  ├── 按 channel_id / mode / session_id 路由到 JiuWenClaw 实例
  │
  ▼
JiuWenClaw (interface.py)
  ├── skilldev.*  → SkillDevService（独立状态机，绕过主 Agent）
  ├── skills.*    → SkillManager（技能市场/安装/卸载）
  ├── tools.*     → ToolManager（MCP 工具管理）
  ├── chat.cancel → interrupt 处理
  ├── chat.answer → 权限确认处理
  └── 其他 chat   → JiuWenClawDeepAdapter（主 DeepAgent）
```

**主 Agent 适配层内部结构：**

```
JiuWenClawDeepAdapter (interface_deep.py, ~4464 行)
├── 生命周期: create_instance → _update_runtime_config(每请求) → Runner.run_agent
├── 冷启动 Rails (14+):
│   Telemetry, FileSystem, SkillUse, RuntimePrompt, ResponsePrompt,
│   TaskExecution, StreamEvent, TaskPlanning, Security, Heartbeat,
│   Avatar, SubagentRail, Permission, DisabledTools
│   + 按模式动态: ContextEngineering, Memory, SkillEvolution, SkillProtocol...
├── SubAgents: code_agent, research_agent, browser_agent(禁用)
├── 工具 (tools/ 目录 123 个 .py 文件):
│   基础: 文件操作(6), shell, code, web_search/fetch(3)
│   多模态: vision, audio, video
│   生态: 小艺手机, Petal, 付费搜索, DeepResearch
│   子代理: fork_agent, spawn_subagent
│   会话: MultiSession, Cron, SendFile, ACP, Todo, Memory, SkillToolkit
└── Prompt 链: 身份基座(prompt_builder) + Rails 动态注入(runtime/response/skill/security...)
```

**SkillDev 服务（独立路径）：**

```
SkillDevService (skilldev/service.py)
├── 状态机 Pipeline (INIT→CLARIFY→GENERATE→VALIDATE→TEST→EVALUATE→IMPROVE→PACKAGE)
├── 10 个 StageHandler（各自创建隔离的 Agent）
├── StateStore (状态持久化) + WorkspaceProvider (工作区管理)
└── 完全独立于主 DeepAgent（仅共享 get_config() 的模型配置）
```

### 2.2 SkillDev 与主 Agent 的关系

**当前：完全独立的两条路径**

```
用户请求 → interface.py
  ├─ skilldev.* → SkillDevService → Pipeline → StageHandler → 隔离 Agent
  └─ 其他      → DeepAdapter → 主 DeepAgent → ReAct 循环
```

- SkillDev 完全绕过主 DeepAgent，有自己独立的 Agent 创建（`create_stage_agent`）和流式输出机制
- 两者唯一的共享是 `get_config()` 获取模型配置
- SkillDev 不使用主 Agent 的 Rails、SubAgents、工具集

### 2.3 各模块职责与保留判定

| 模块 | 当前职责 | 改造后 | 理由 |
|------|---------|--------|------|
| `interface.py` | 统一门面 + 多路由分发 | **大幅简化** | 只保留 Skill 相关路由 |
| `interface_deep.py` | 通用 DeepAgent 适配 | **重写为 SkillDev 适配** | 去除通用功能，聚焦 Skill |
| `skilldev/service.py` | SkillDev 请求处理 | **保留，升级** | 核心业务逻辑入口 |
| `skilldev/pipeline.py` | 状态机编排 | **保留** | 成熟的流程编排 |
| `skilldev/context.py` | 阶段执行上下文 | **保留** | Agent 创建/工具注册 |
| `skilldev/stages/*.py` | 10 个阶段处理器 | **保留** | 核心业务逻辑 |
| `skilldev/schema.py` | 数据模型 | **保留** | 状态/事件/挂起点 |
| `skilldev/deps.py` | 外部依赖注入 | **保留，可能扩展** | 模型配置/状态存储 |
| `skill_manager.py` | 技能市场管理 | **精简** | 只保留导入/导出 |
| `tool_manager.py` | MCP 工具管理 | **移除** | Skill 生成不需要 |
| `tools/subagent_*` | 子代理工具 | **移除** | 不需要通用子代理 |
| `deep_agent/prompt_builder.py` | 通用 prompt 构建 | **替换** | 改为 Skill 专用 prompt |
| `deep_agent/rails/` | 各种 Rail | **精简** | 只保留必要的 |
| `session_manager.py` | 会话队列管理 | **保留** | 并发控制仍然需要 |
| `session_history.py` | 会话历史记录 | **保留** | 审计/调试需要 |
| `memory/` | 本地/云端记忆 | **移除** | Skill 生成是无状态任务 |
| `team/` | 团队协作模式 | **移除** | 不适用 |
| `extensions/` | 扩展插件系统 | **移除或精简** | 暂不需要 |

---

## 3. 改造策略

### 3.1 总体策略：渐进式改造

不做一次性重写，而是分层、分批改造，每批可独立验证：

```
第 1 层：入口简化（interface.py）
第 2 层：DeepAgent 适配层专用化（interface_deep.py）
第 3 层：统一 API 协议（合并 start/respond → chat）
第 4 层：清理冗余模块
```

### 3.2 核心架构决策

**决策 1：保留 SkillDev Pipeline 作为核心引擎**

当前 Pipeline（状态机）模式成熟可靠，改造初期不替换为 Agent 驱动编排。改造重点是**精简外围**，让 Pipeline 成为系统唯一的主干。

> 未来可考虑将 Pipeline 升级为 Agent 编排（参见 `skilldev-agent-refactor-design.md` 的混合架构方案），但这是独立迭代。

**决策 2：简化主 DeepAgent 为 Skill Orchestrator**

将 `JiuWenClawDeepAdapter` 从通用 Agent 适配器改为 Skill 专用的 Orchestrator：
- 系统 Prompt 聚焦 Skill 开发
- 工具集只保留 Skill 相关（文件操作、代码执行、搜索）
- Rails 精简到最小必要集

**决策 3：SkillDev 保持独立路径，但简化入口**

SkillDev 仍然绕过主 Agent（Pipeline 模式的确定性优于 Agent 编排的灵活性），但入口从 `skilldev.start` + `skilldev.respond` 合并为 `skilldev.chat`。

---

## 4. 详细改造方案

### 4.1 第零层：WebSocket 入口与 AgentManager 精简

**目标：** 简化 `agent_ws_server.py` 和 `agent_manager.py`，移除不需要的短路处理。

#### 4.1.0.1 agent_ws_server.py 精简

| 现有功能 | 改造操作 | 理由 |
|---------|---------|------|
| `initialize`（ACP 能力协商） | **简化** | 保留基本初始化，去除 ACP 专用逻辑 |
| `session.*`（创建/删除/列表） | **保留** | 基础设施 |
| `permissions.*`（15+ API） | **精简** | 只保留基本文件权限 |
| `history.get` | **保留** | 审计需要 |
| `command.*` | **移除** | Skill 生成不需要命令执行 |
| `browser.*` | **移除** | 不需要浏览器控制 |
| `extensions.*` | **移除** | 不需要扩展插件 |
| 文件传输事件 | **精简** | 只保留 Skill 产物下载相关 |

#### 4.1.0.2 agent_manager.py 精简

- 移除 `code` 模式的 `switch_mode` 逻辑
- 简化 channel_id 路由（不需要区分 ACP、web、feishu 等）
- 保留 JiuWenClaw 实例管理的基本功能

#### 4.1.0.3 涉及文件

| 文件 | 改动量 | 改动描述 |
|------|--------|---------|
| `agent_ws_server.py` | 中 | 移除 browser/command/extensions 短路分支 |
| `agent_manager.py` | 中 | 简化 channel/mode 路由 |

### 4.1.1 第一层：interface.py 入口简化

**目标：** 去除与 Skill 生成无关的路由，简化 JiuWenClaw 门面。

#### 4.1.1 移除的路由

```python
# 移除：通用工具管理
_TOOL_ROUTES  # tools.add 等

# 精简：技能市场（只保留导入/导出相关）
_SKILL_ROUTES  # 保留 skills.install, skills.import_local；移除 marketplace 相关
```

#### 4.1.2 精简的路由逻辑

```python
# 改造后的 process_message_stream
async def process_message_stream(self, request):
    # 主路径：SkillDev 流式请求
    if request.req_method in _SKILLDEV_METHODS:
        async for chunk in self._get_skilldev_service().handle(request):
            yield chunk
        return
    
    # 次路径：需要主 Agent 的通用对话（保留但简化）
    # 例如用户在 skill 开发过程中需要问一些通用问题
    adapter = await self._ensure_adapter()
    ...
```

#### 4.1.3 移除的功能

| 功能 | 涉及代码 | 操作 |
|------|---------|------|
| Team 模式 | `is_team_mode` 分支、`team/` 模块 | 删除 |
| Cloud Memory | `memory_mode == "cloud"` 分支 | 删除 |
| Cat Cafe MCP | `cat_cafe_mcp` 注册 | 删除 |
| build_user_prompt | JSON 包装用户消息 | 简化 |
| Extension Tools | `_register_extension_tools` | 删除 |
| Tool Manager | `_get_tool_manager`, `_TOOL_ROUTES` | 删除 |
| 宿主项目 MCP | `find_host_project_mcp_json` | 删除 |

#### 4.1.4 保留的功能

| 功能 | 理由 |
|------|------|
| SessionManager | 并发控制、任务队列 |
| session_history | 审计追踪 |
| SkillDevService 初始化 | 核心业务 |
| create_instance / reload_agent_config | 生命周期管理 |
| chat.cancel / process_interrupt | 任务取消 |

#### 4.1.5 涉及文件

| 文件 | 改动量 | 改动描述 |
|------|--------|---------|
| `interface.py` | **大** | 删除 ~300 行路由和辅助逻辑 |
| `schema/message.py` | 小 | 清理无用的 ReqMethod 枚举值 |

---

### 4.2 第二层：DeepAgent 适配层专用化

**目标：** 将 `JiuWenClawDeepAdapter`（4464 行）精简为 Skill 专用适配层。

#### 4.2.1 Rails 精简

| Rail | 现状 | 改造后 | 理由 |
|------|------|--------|------|
| `ContextEngineeringRail` | 保留 | **保留** | 长对话上下文管理仍然需要 |
| `FileSystemRail` | 保留 | **保留** | Skill 文件操作核心能力 |
| `SecurityRail` | 保留 | **保留** | 安全防护 |
| `HeartbeatRail` | 保留 | **保留** | 健康检查 |
| `StreamEventRail` | 保留 | **保留** | 流式事件转换 |
| `TaskPlanningRail` | plan 模式 | **移除** | Skill 生成有固定流程 |
| `SubagentRail` | 子代理委派 | **移除** | 不需要通用子代理 |
| `SkillUseRail` | 技能使用 | **移除** | 自身就是 Skill 工厂 |
| `SkillEvolutionRail` | 技能自演进 | **移除** | 与 Skill 生成无关 |
| `MemoryRail` | 本地记忆 | **移除** | 无状态任务不需要 |
| `CodingMemoryRail` | 编码记忆 | **移除** | 同上 |
| `AvatarRail` | 头像/形象 | **移除** | 不需要 |
| `PermissionRail` | 权限审批 | **可选保留** | 如需文件操作审批 |
| `DisabledToolsRail` | 工具禁用 | **移除** | 简化后不需要 |
| `LspRail` | LSP 集成 | **移除** | 不需要 |

#### 4.2.2 工具集精简

**保留的工具（Skill 生成必需）：**

| 工具 | 来源 | 用途 |
|------|------|------|
| `ReadFileTool` | openjiuwen.harness | 读取工作区文件 |
| `WriteFileTool` | openjiuwen.harness | 写入 Skill 文件 |
| `EditFileTool` | openjiuwen.harness | 编辑文件 |
| `GlobTool` | openjiuwen.harness | 文件搜索 |
| `GrepTool` | openjiuwen.harness | 内容搜索 |
| `ListDirTool` | openjiuwen.harness | 目录浏览 |
| `BashTool` | openjiuwen.harness | Shell 命令执行 |
| `CodeTool` | openjiuwen.harness | Python 代码执行 |
| `WebPaidSearchTool` | openjiuwen.harness | 网络搜索 |
| `FreeSearchTool` | jiuwenclaw 自定义 | 免费搜索 |
| `FetchWebpageTool` | jiuwenclaw 自定义 | 网页内容获取 |

**移除的工具：**

| 工具 | 理由 |
|------|------|
| `create_vision_tools` | 图像识别与 Skill 生成无关 |
| `create_audio_tools` | 音频处理与 Skill 生成无关 |
| `fork_agent` | 通用子代理，不需要 |
| `spawn_subagent` | 通用子代理，不需要 |
| `send_file_to_user` | 前端文件推送不需要 |
| office_claw_* 系列 | 办公助手功能，全部移除 |

#### 4.2.3 SubAgent 配置清除

```python
# 移除：通用 SubAgent
# build_code_agent_config()
# build_research_agent_config()
# build_browser_agent_config()

# 注：SkillDev 的阶段 Agent 由 SkillDevContext.create_stage_agent() 管理，
# 不需要通过 DeepAgentConfig.subagents 注册
```

#### 4.2.4 系统 Prompt 替换

**现有：** 通用对话 Agent prompt（`build_identity_prompt`）
**改造为：** Skill 开发专用 prompt

```python
SKILLDEV_AGENT_SYSTEM_PROMPT = """你是一个专业的 Skill 开发 Agent。

你的核心职责是帮助用户创建高质量的 Agent Skill（技能包）。

## 能力范围
- 分析用户需求，澄清不明确的点
- 设计 Skill 的结构和内容
- 生成完整的 Skill 文件集（SKILL.md + scripts/ + references/）
- 验证 Skill 格式合规性
- 设计和运行测试用例
- 评估 Skill 质量并提供改进建议
- 优化 Skill 的触发描述

## 工作流程
Skill 创建遵循标准流程：需求澄清 → 生成 → 校验 → 测试 → 评测 → 改进 → 打包。
每个阶段由专门的处理器执行，你负责整体协调。

## 工作区
工作区路径：{workspace}
所有文件操作限制在工作区内。
"""
```

#### 4.2.5 新增 `SkillDevDeepAdapter` 类

**方案：创建新的精简适配层，而非修改现有 4464 行文件**

```python
# jiuwenclaw/agentserver/skilldev_agent/adapter.py（新文件）

class SkillDevDeepAdapter(AgentAdapter):
    """专用 Skill 生成 Agent 的 DeepAgent 适配层。
    
    对比 JiuWenClawDeepAdapter（4464 行），本适配层：
    - Rails: 仅 ContextEngineering + FileSystem + Security + StreamEvent (~5 个)
    - Tools: 仅文件操作 + Shell + Search (~11 个)
    - SubAgents: 无（SkillDev 各阶段由 Pipeline 管理）
    - Prompt: Skill 开发专用
    """
```

#### 4.2.6 涉及文件

| 文件 | 改动量 | 改动描述 |
|------|--------|---------|
| `skilldev_agent/adapter.py` | **新建** | ~500 行，精简版 DeepAdapter |
| `skilldev_agent/prompts.py` | **新建** | Skill 专用 prompt |
| `interface_deep.py` | 不改动 | 保留作为通用适配器的备份 |
| `agent_adapters.py` | 小 | 新增 adapter 工厂路由 |

---

### 4.3 第三层：统一 API 协议

**目标：** 将 `skilldev.start` + `skilldev.respond` 合并为统一的 `skilldev.chat`。

#### 4.3.1 新 API 设计

```json
// 首次启动
{
  "method": "skilldev.chat",
  "params": {
    "task_id": "sd_xxx",
    "message": "帮我创建一个搜索 arXiv 的 Skill",
    "files": [...],
    "skill_packages": [...],
    "tool_spec_files": [...]
  }
}

// 回答澄清问题
{
  "method": "skilldev.chat",
  "params": {
    "task_id": "sd_xxx",
    "user_input": { "answers": [...] }
  }
}

// 审阅确认
{
  "method": "skilldev.chat",
  "params": {
    "task_id": "sd_xxx",
    "user_input": { "action": "improve", "feedback": "..." }
  }
}
```

#### 4.3.2 向后兼容

- 保留 `skilldev.start` 和 `skilldev.respond` 作为 `skilldev.chat` 的别名
- `service.py` 内部统一路由到相同的处理逻辑
- 前端可逐步迁移

#### 4.3.3 涉及文件

| 文件 | 改动量 | 改动描述 |
|------|--------|---------|
| `service.py` | 中 | 新增 `_handle_chat` 统一入口 |
| `schema/message.py` | 小 | 新增 `SKILLDEV_CHAT` 枚举值 |

---

### 4.4 第四层：清理冗余模块

**目标：** 删除或标记 deprecated 不再使用的模块。

#### 4.4.1 可直接删除的模块

**目录级别删除：**

| 模块/目录 | 文件数(估) | 理由 |
|-----------|-----------|------|
| `agentserver/team/` | 9 | 团队模式不需要 |
| `agentserver/memory/` | 9 | 记忆系统不需要 |
| `agentserver/tools/subagent_executor/` | 7 | 通用子代理不需要 |
| `agentserver/tools/browser-move/` | 30+ | Playwright 运行时 + 上游补丁树，不需要 |
| `agentserver/tools/xiaoyi_phone_tools/` | 10+ | 小艺手机工具，不需要 |
| `agentserver/tools/deepresearch_plugin/` | 6 | 深度研究插件，不需要 |

**文件级别删除（`agentserver/tools/` 根目录）：**

| 文件 | 理由 |
|------|------|
| `subagent_tools.py` | 通用子代理 |
| `subagent_models.py` | 同上 |
| `audio_tools.py` | 多模态 |
| `video_tools.py` | 多模态 |
| `image_tools.py` | 多模态 |
| `browser_tools.py` | 浏览器控制 |
| `browser_start_client.py` | 浏览器控制 |
| `cron_tools.py` | 定时任务 |
| `deepresearch_tools.py` | 深度研究 |
| `deepresearch_task_manager.py` | 同上 |
| `memory_tools.py` | 记忆系统 |
| `multi_session_toolkits.py` | 多会话管理 |
| `petal_search_tools.py` | Petal 搜索 |
| `send_file_to_user.py` | 文件推送 |
| `acp_output_tools.py` | ACP 专用 |
| `ephemeral_stdio_mcp_tool.py` | 临时 MCP |
| `todo_toolkits.py` | Todo 管理 |
| `user_todo_tool.py` | 用户 Todo |
| `task_tools.py` | 通用任务工具 |
| `skill_step_toolkit.py` | 技能执行步骤 |
| `multimodal_config.py` | 多模态配置 |

**`agentserver/deep_agent/` 内部精简：**

| 文件 | 操作 | 理由 |
|------|------|------|
| `ask_user_question_registry.py` | 删除 | 通用 Agent 交互用 |
| `cron_runtime.py` | 删除 | 定时任务 |
| `team_helpers.py` | 删除 | 团队模式 |
| `rails/avatar_rail.py` | 删除 | 数字分身 |
| `rails/task_execution_rail.py` | 视情况保留 | pause/cancel 需要 |
| `rails/team_member_skill_toolkit_rail.py` | 删除 | 团队模式 |
| `rails/skill_compliance_rail.py` | 删除 | 技能合规 |
| `rails/skill_prompt_rail.py` | 删除 | 技能协议 |
| `rails/disabled_tools_rail.py` | 删除 | 工具禁用 |

**`agentserver/tools/` 中保留的文件：**

| 文件 | 理由 |
|------|------|
| `harness_named_web_tools.py` | free_search / fetch_webpage |
| `search_tools.py` | 搜索工具基础 |
| `web_fetch_tools.py` | 网页获取 |
| `skill_toolkits.py` | 技能安装/搜索（精简） |
| `ask_user_question_tool.py` | 可能需要保留用于交互 |
| `command_tools.py` | 基础命令工具 |
| `ssl_config.py` | SSL 配置（基础设施） |
| `mcp_toolkits.py` | MCP 工具管理（精简） |

#### 4.4.2 可标记 deprecated 的模块

| 模块 | 理由 |
|------|------|
| `interface_deep.py` | 被新的 `SkillDevDeepAdapter` 替代 |
| `deep_agent/prompt_builder.py` | 被 Skill 专用 prompt 替代 |

#### 4.4.3 需保留但精简的模块

| 模块 | 精简内容 |
|------|---------|
| `skill_manager.py` | 只保留 `handle_skills_install`, `handle_skills_import_local` |
| `config.py` (get_config) | 精简配置项 |
| `extensions/` | 如果完全不需要扩展能力，可移除 |

---

## 5. 涉及文件全表

### 5.1 新增文件

| 文件路径 | 说明 |
|---------|------|
| `agentserver/skilldev_agent/__init__.py` | 新适配层包 |
| `agentserver/skilldev_agent/adapter.py` | SkillDevDeepAdapter（~500 行） |
| `agentserver/skilldev_agent/prompts.py` | Skill 专用 prompt 模板 |
| `agentserver/skilldev_agent/config.py` | Skill Agent 专用配置 |

### 5.2 改动文件

| 文件路径 | 改动描述 | 预估行数变化 |
|---------|---------|-------------|
| `agentserver/interface.py` | 删除无关路由，简化门面 | -300 行 |
| `agentserver/agent_adapters.py` | 新增 SkillDevDeepAdapter 工厂 | +20 行 |
| `schema/message.py` | 清理无用枚举，新增 SKILLDEV_CHAT | ±10 行 |
| `agentserver/skilldev/service.py` | 新增 _handle_chat 统一入口 | +50 行 |
| `config.py` | 精简配置项 | -50 行 |

### 5.3 删除/Deprecated 文件

| 文件/目录 | 操作 |
|-----------|------|
| `agentserver/team/` | 删除 |
| `agentserver/memory/` | 删除（除非有外部依赖） |
| `agentserver/tools/subagent_executor/` | 删除 |
| `agentserver/tools/subagent_tools.py` | 删除 |
| `agentserver/tools/subagent_models.py` | 删除 |
| `agentserver/deep_agent/interface_deep.py` | 标记 deprecated |

### 5.4 不变文件

| 文件/目录 | 理由 |
|-----------|------|
| `agentserver/skilldev/` 全部 | 核心业务逻辑不动 |
| `agentserver/session_manager.py` | 基础设施 |
| `agentserver/session_history.py` | 基础设施 |
| `schema/agent.py` | 请求/响应模型 |
| `utils.py` | 工具函数 |

---

## 6. 分批实施计划

### 批次 0：创建新适配层（可独立验证）

**范围：** 新建 `skilldev_agent/adapter.py`，实现精简版 DeepAdapter

**详细步骤：**
1. 创建 `agentserver/skilldev_agent/` 目录
2. 实现 `SkillDevDeepAdapter`：
   - `create_instance()` → 创建只含 Skill 相关工具和 Rails 的 DeepAgent
   - `process_message_impl()` → 简化的消息处理
   - `process_message_stream_impl()` → 简化的流式处理
3. 实现 `prompts.py` → Skill 专用系统 Prompt
4. 在 `agent_adapters.py` 注册新适配层

**验证：** 通过环境变量 `SDK_CHOICE=skilldev_deep` 选择新适配层，基本对话能力正常。

**预计工作量：** 1-2 天

### 批次 1：入口层简化

**范围：** interface.py 路由精简

**详细步骤：**
1. 创建 `interface_skilldev.py`（新的精简版门面，与 `interface.py` 并存）
2. 移除 Team 模式、Cloud Memory、Cat Cafe MCP、Extension Tools 路由
3. 精简 `_SKILL_ROUTES`（只保留 install + import_local）
4. 移除 `_TOOL_ROUTES`
5. 简化 `_build_inputs()`
6. 通过配置开关选择使用新门面还是旧门面

**验证：** SkillDev 全流程（start → clarify → generate → ... → package）在新门面下正常工作。

**预计工作量：** 1 天

### 批次 2：API 统一

**范围：** 合并 `skilldev.start` + `skilldev.respond` → `skilldev.chat`

**详细步骤：**
1. `schema/message.py` 新增 `SKILLDEV_CHAT = "skilldev.chat"`
2. `service.py` 新增 `_handle_chat()` 方法
3. `_handle_chat` 内部根据 `task_id` 是否存在 + 当前 state 自动分流到 `_handle_start` 或 `_handle_respond`
4. 更新 `_METHOD_DISPATCH`
5. 旧 API 保留为别名

**验证：** 前端使用 `skilldev.chat` 完成完整流程；旧 API `skilldev.start`/`skilldev.respond` 仍然兼容。

**预计工作量：** 0.5 天

### 批次 3：冗余模块清理

**范围：** 删除不需要的模块

**详细步骤：**
1. 删除 `team/` 目录
2. 删除 `memory/` 目录（检查依赖关系）
3. 删除 `tools/subagent_executor/`、`subagent_tools.py`、`subagent_models.py`
4. 清理 `schema/message.py` 中的无用枚举
5. 标记 `interface_deep.py` 为 deprecated

**验证：** 系统启动正常，无 import 错误，Skill 生成流程完整。

**预计工作量：** 0.5-1 天

### 批次 4：配置精简

**范围：** 精简配置项和启动流程

**详细步骤：**
1. 精简 `config.yaml`，移除通用 Agent 配置项（subagents, plan_mode, evolution 等）
2. 精简 `agent-data.json` 中的资源定义
3. 更新文档

**验证：** 最小配置下系统正常启动和运行。

**预计工作量：** 0.5 天

---

## 7. 风险与缓解

### 7.1 依赖耦合

**风险：** 删除模块时可能触发隐式 import 错误。

**缓解：**
- 每删一个模块前，全局搜索其 import 路径
- 分步删除，每步验证启动
- 先标记 deprecated，确认无依赖后再删除

### 7.2 SkillDev 依赖主 Agent 组件

**风险：** SkillDev 的 `create_stage_agent()` 使用 `openjiuwen.harness` 的工具类，这些工具在新适配层中可能未注册。

**分析：** SkillDev 的 Agent 是独立创建的（`create_deep_agent` → 独立 `Runner.resource_mgr.add_tool`），**不依赖主 Agent 的工具注册**。因此清理主 Agent 工具不影响 SkillDev。

### 7.3 配置兼容性

**风险：** 删除配置项后，旧 config.yaml 启动报错。

**缓解：**
- 所有删除的配置项使用 `.get(key, default)` 兜底
- 提供 migration script 或文档

### 7.4 前端兼容

**风险：** 前端依赖被删除的 API。

**缓解：**
- API 变更在批次 2 中通过别名保持向后兼容
- `skilldev.start` / `skilldev.respond` 仍可用

---

## 8. 改造后的目标架构

```
SkillDev Agent (interface_skilldev.py)
├── 请求路由
│   ├── skilldev.chat       → SkillDevService（主路径）
│   ├── skilldev.status     → 状态查询
│   ├── skilldev.download   → 产物下载
│   ├── skilldev.cancel     → 任务取消
│   ├── skilldev.file.*     → 工作区文件操作
│   ├── skills.install      → 技能安装
│   ├── chat.cancel         → 中断处理
│   └── 其他 chat           → SkillDevDeepAdapter（辅助对话）
│
├── SkillDevDeepAdapter (~500 行)
│   ├── 5 个 Rails: CE + FileSystem + Security + StreamEvent + Heartbeat
│   ├── 11 个工具: 文件操作(6) + shell + code + search(3)
│   ├── Skill 专用 Prompt
│   └── 无 SubAgent
│
├── SkillDevService
│   ├── Pipeline（INIT → ... → PACKAGE）
│   ├── 10 个 StageHandler（各自创建隔离 Agent）
│   ├── StateStore + WorkspaceProvider
│   └── 统一 chat API
│
└── 基础设施
    ├── SessionManager（并发控制）
    ├── SessionHistory（审计追踪）
    └── Config（精简配置）
```

**代码量对比：**

| 维度 | 改造前 | 改造后 | 变化 |
|------|--------|--------|------|
| interface.py | ~1077 行 | ~500 行 | -54% |
| DeepAdapter | ~4464 行 | ~500 行 | -89% |
| 模块总数 | ~30+ | ~15 | -50% |
| 工具数量 | 20+ | 11 | -45% |
| Rails 数量 | 14+ | 5 | -64% |

---

## 9. 总结

本改造方案的核心思路是**做减法**：

1. **入口简化**：从多路由门面精简为 Skill 专用入口
2. **适配层专用化**：从 4464 行通用适配器精简为 ~500 行 Skill 专用适配器
3. **API 统一**：`skilldev.chat` 一个入口覆盖所有交互
4. **模块清理**：删除 Team、Memory、SubAgent、Extension 等无关模块
5. **核心不动**：SkillDev Pipeline、StageHandler、Context 等核心业务逻辑保持不变

改造分 5 个批次，每批可独立验证，总工作量预计 3-5 天。
