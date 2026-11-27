# SDK Subagent 机制详解：SubAgentConfig 创建与使用规范

## 1. 概述

OpenJiuwen SDK 提供了一套**声明式子代理（Subagent）机制**，允许主 Agent 通过 `task_tool` 工具将复杂任务委派给专用的子 Agent 执行。与 `fork_agent`/`spawn_subagent`（工具级子代理）不同，SDK Subagent 机制是**框架级的**，由 `SubagentRail` 管理生命周期。

### 两种子代理机制对比

| 维度 | SDK Subagent（本文） | fork_agent / spawn_subagent |
|------|---------------------|----------------------------|
| 定义位置 | `create_deep_agent(subagents=...)` | 作为 `@tool` 装饰的函数 |
| 调用方式 | LLM 调用 `task_tool(subagent_type, task_description)` | LLM 调用 `fork_agent()` / `spawn_subagent()` |
| 管理层 | `SubagentRail` 自动注册 `task_tool` | `ForkAgentExecutor` 全局实例 |
| 配置方式 | `SubAgentConfig` 声明式配置 | 运行时动态继承父 Agent 工具 |
| 子代理实例 | 由 `DeepAgent.create_subagent()` 按需创建 | 由 `ForkAgentExecutor` 创建 |
| 工具继承 | 独立配置，不继承父工具 | 从父 Agent 继承（排除部分工具） |
| 上下文 | 完全隔离 | spawn 隔离，fork 继承 |

---

## 2. 核心数据结构：SubAgentConfig

```python
from dataclasses import dataclass, field
from typing import Any, List, Optional

@dataclass
class SubAgentConfig:
    """Configuration for a DeepAgent sub-agent."""

    agent_card: AgentCard           # 子代理身份卡片（name + description）
    system_prompt: str              # 系统提示词
    tools: List[Tool | ToolCard]    # 子代理可用工具列表
    mcps: List[McpServerConfig]     # MCP 服务器配置
    model: Optional[Model] = None   # 模型（None 则复用父 Agent 的模型）
    rails: Optional[List[AgentRail]] = None  # Rails 列表
    skills: Optional[List[str]] = None       # Skill 定义
    backend: Optional[Any] = None            # 后端协议实例
    workspace: Optional[Workspace] = None    # 工作空间（None 则基于父创建子目录）
    sys_operation: Optional[SysOperation] = None
    language: Optional[str] = None           # 语言（None 则跟随父）
    prompt_mode: Optional[str] = None
    enable_task_loop: bool = False           # 是否启用外层任务循环
    max_iterations: Optional[int] = None     # 最大迭代次数（None 则跟随父）
    factory_name: Optional[str] = None       # 工厂函数名（用于特殊子代理类型）
    factory_kwargs: dict[str, Any] = field(default_factory=dict)  # 工厂额外参数
    enable_plan_mode: bool = False           # 是否启用计划模式
```

### 关键字段说明

| 字段 | 作用 | 默认行为 |
|------|------|----------|
| `agent_card` | **必填**，`name` 字段是 LLM 调用 `task_tool` 时的 `subagent_type` 值 | - |
| `system_prompt` | **必填**，子代理的系统提示词 | - |
| `model` | 子代理使用的模型 | `None` → 复用父 Agent 的模型 |
| `tools` | 子代理的工具列表 | 独立配置，不自动继承父工具 |
| `workspace` | 子代理的工作空间 | `None` → 基于父 workspace 创建 `{parent_ws}/{session_id}` 子目录 |
| `max_iterations` | 最大 ReAct 迭代次数 | `None` → 跟随父 Agent 的 `max_iterations` |
| `factory_name` | 特殊子代理类型的工厂函数 | `None` → 使用通用 `create_deep_agent` |
| `factory_kwargs` | 传给工厂函数的额外参数 | `{}` |

### factory_name 支持的值

| factory_name | 工厂函数 | 说明 |
|-------------|----------|------|
| `None` | `create_deep_agent()` | 通用 DeepAgent 子代理 |
| `"code_agent"` | `create_code_agent()` | 代码执行专用子代理 |
| `"research_agent"` | `create_research_agent()` | 研究/搜索专用子代理 |
| `"browser_agent"` | `create_browser_agent()` | 浏览器自动化子代理 |

---

## 3. 使用方式

### 3.1 步骤一：创建 SubAgentConfig 列表

```python
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.schema.config import SubAgentConfig

subagents = [
    SubAgentConfig(
        agent_card=AgentCard(
            name="research_agent",
            description="专用研究子代理，负责搜索和信息收集。",
        ),
        system_prompt="你是一个专业研究员...",
        tools=[search_tool, fetch_tool],
        model=model,  # 可选，None 则复用父模型
        max_iterations=15,
        factory_name="research_agent",  # 使用研究代理工厂
    ),
    SubAgentConfig(
        agent_card=AgentCard(
            name="code_agent",
            description="代码执行子代理，负责运行和调试代码。",
        ),
        system_prompt="你是一个代码专家...",
        tools=[bash_tool, code_tool],
        model=model,
        max_iterations=15,
        factory_name="code_agent",
    ),
]
```

### 3.2 步骤二：传入 create_deep_agent

```python
from openjiuwen.harness.factory import create_deep_agent

agent = create_deep_agent(
    model=model,
    card=AgentCard(name="main_agent", id="main"),
    system_prompt="你是主代理...",
    tools=main_tools,
    subagents=subagents,       # ← 传入子代理配置列表
    rails=rails_list,
    max_iterations=15,
    workspace=workspace,
)
```

### 3.3 自动发生的事情

`create_deep_agent` 内部会根据 `subagents` 参数自动：

1. **注册 SubagentRail**（或 SessionRail，取决于 `enable_async_subagent`）
2. SubagentRail 在 `init()` 时：
   - 收集所有子代理的 `agent_card.name` 和 `agent_card.description`
   - 创建 `task_tool` 工具并注册到父 Agent 的 `ability_manager`
   - `task_tool` 的描述中包含可用子代理列表

### 3.4 LLM 侧调用

LLM 看到的工具定义（由 SubagentRail 自动生成）：

```
task_tool:
  subagent_type: "research_agent" | "code_agent"  # 子代理类型名（= agent_card.name）
  task_description: "搜索关于XX的最新资料..."     # 任务描述
```

### 3.5 运行时流程

```
LLM 调用 task_tool(subagent_type="research_agent", task_description="...")
    │
    ▼
TaskTool.invoke()
    │
    ├─ parent_agent.create_subagent("research_agent", sub_session_id)
    │       │
    │       ├─ _find_subagent_spec("research_agent")  → 查找匹配的 SubAgentConfig
    │       ├─ 解析 factory_name → "research_agent"
    │       ├─ 调用 create_research_agent(**create_kwargs)
    │       └─ 返回 DeepAgent 实例
    │
    ├─ subagent.invoke({"query": task_description, "conversation_id": sub_session_id})
    │
    └─ 返回 ToolOutput(success=True, data={"output": result})
```

---

## 4. interface_deep.py 示范分析

`JiuWenClawDeepAdapter._build_configured_subagents()` 展示了生产环境的子代理配置模式：

```python
def _build_configured_subagents(self, model, config, config_base) -> list | None:
    subagents = []

    # 1. 根据配置有条件地添加 code_agent
    code_agent_cfg = subagents_cfg.get("code_agent")
    if self._is_subagent_enabled(code_agent_cfg):
        subagents.append(
            build_code_agent_config(
                model,
                workspace=workspace,
                language=resolved_language,
                rails=code_agent_rails,          # 可传入额外 rails
                max_iterations=_parse_int(...),
            )
        )

    # 2. 根据配置有条件地添加 research_agent
    research_agent_cfg = subagents_cfg.get("research_agent")
    if self._is_subagent_enabled(research_agent_cfg):
        subagents.append(
            build_research_agent_config(
                model,
                workspace=workspace,
                language=resolved_language,
                max_iterations=_parse_int(...),
                tools=build_jiuwen_harness_named_web_tools(...),  # 自定义工具
            )
        )

    # 3. 根据运行时条件添加 browser_agent
    if browser_enabled:
        subagents.append(
            build_browser_agent_config(
                model,
                workspace=workspace,
                language=resolved_language,
                max_iterations=_parse_int(...),
            )
        )

    return subagents or None
```

### 关键设计模式

1. **配置驱动**：子代理的启用/禁用由配置控制（`react.subagents.code_agent.enabled`）
2. **工厂模式**：每种子代理类型有对应的 `build_xxx_agent_config()` 函数返回 `SubAgentConfig`
3. **延迟实例化**：`SubAgentConfig` 只是配置声明，实际的 `DeepAgent` 实例在 `task_tool` 被调用时才创建
4. **工具隔离**：每个子代理有独立的工具集，不自动继承父 Agent 的工具
5. **模型复用**：大多数子代理传入相同的 `model` 实例，但可以使用不同模型

---

## 5. 子代理工作空间管理

当 `SubAgentConfig.workspace = None` 时，`DeepAgent.create_subagent()` 自动创建子级工作空间：

```python
workspace = Workspace(
    root_path=parent_workspace.root_path + f"/{sub_session_id}",
    language=parent_config.language,
)
```

子代理的 session_id 格式：
- 通用子代理：`{parent_session_id}_sub_{subagent_type}_{uuid8}`
- 浏览器子代理：`{parent_session_id}_sub_browser_agent`（固定，复用 session）

---

## 6. 已有的子代理类型参考

### 6.1 code_agent

- **工厂名**：`"code_agent"`
- **描述**（中文）：代码执行子代理
- **默认工具**：文件系统工具 + 代码执行工具
- **默认 Rails**：`FileSystemRail`（可追加其他 rails）
- **系统提示词**：聚焦代码编写、调试、测试

### 6.2 research_agent

- **工厂名**：`"research_agent"`
- **描述**（中文）：研究搜索子代理
- **默认工具**：网络搜索 + 网页抓取工具
- **系统提示词**：聚焦信息搜索、资料整理、分析

### 6.3 browser_agent

- **工厂名**：`"browser_agent"`
- **描述**（中文）：专用浏览器子代理，直接使用 Playwright MCP 工具执行网页任务
- **默认工具**：Playwright MCP 工具 + 浏览器运行时工具
- **默认 Rails**：`BrowserRuntimeRail`
- **系统提示词**：聚焦浏览器自动化任务
- **特殊参数**：`factory_kwargs={"settings": RuntimeSettings(...)}`

### 6.4 general-purpose（通用子代理）

- **触发方式**：`create_deep_agent(add_general_purpose_agent=True)`
- **描述**（中文）：通用研究助手，负责复杂问题研究、搜索文件内容、执行多步骤任务
- **特殊行为**：自动注入，继承父 Agent 的 system_prompt 和 tools
- **Rails**：继承父 Rails（排除 SubagentRail 和 SessionRail）

---

## 7. 创建自定义子代理的最佳实践

### 7.1 使用 SubAgentConfig（推荐）

适用于使用标准 DeepAgent 或已有工厂的场景：

```python
SubAgentConfig(
    agent_card=AgentCard(
        name="my_custom_agent",         # LLM 调用时的 subagent_type
        description="我的自定义子代理",  # 展示给 LLM 的描述
    ),
    system_prompt="你是一个专门处理XX的助手...",
    tools=[tool1, tool2],               # 子代理独有的工具
    model=model,                        # None 则复用父模型
    max_iterations=15,
    language="cn",
    # factory_name=None,                # 使用通用 create_deep_agent
)
```

### 7.2 使用 DeepAgent 实例

适用于需要完全控制子代理配置的场景：

```python
custom_agent = create_deep_agent(
    model=model,
    card=AgentCard(name="custom_agent", description="..."),
    system_prompt="...",
    tools=[...],
    rails=[...],
    max_iterations=20,
    workspace=Workspace(root_path="/custom/path", language="cn"),
)

# 直接传入 DeepAgent 实例
subagents = [custom_agent]
```

注意：传入 DeepAgent 实例时，`create_subagent()` 会直接返回该实例，不做任何修改。

### 7.3 自定义工厂函数

如果需要特殊的创建逻辑，可以设置 `factory_name`：

```python
SubAgentConfig(
    agent_card=AgentCard(name="browser_agent", description="..."),
    system_prompt="...",
    factory_name="browser_agent",       # 匹配已注册的工厂
    factory_kwargs={"settings": ...},   # 传给工厂的额外参数
)
```

当前支持的 factory_name：`"browser_agent"` / `"code_agent"` / `"research_agent"`。

---

## 8. 在 SkillDevDeepAdapter 中使用子代理

### 8.1 当前状态

`SkillDevDeepAdapter.create_instance()` 调用 `create_deep_agent` 时**未传入 subagents 参数**：

```python
self._instance = create_deep_agent(
    model=self._model,
    card=AgentCard(name="skilldev-agent", id="skilldev-agent", ...),
    system_prompt=SKILLDEV_AGENT_SYSTEM_PROMPT,
    tools=tool_cards,
    rails=rails,
    # subagents=???  ← 未配置
    ...
)
```

### 8.2 添加子代理的步骤

1. **创建子代理配置**：在 `jiuwenclaw/agentserver/skilldev_agent/subagents/` 目录下定义子代理配置
2. **构建 SubAgentConfig 列表**：编写 `_build_configured_subagents()` 方法
3. **传入 create_deep_agent**：在 `create_instance()` 中添加 `subagents=...`
4. **框架自动处理**：`SubagentRail` 自动注册 `task_tool`，LLM 可通过 `task_tool` 调用子代理

### 8.3 注意事项

- `subagents` 参数传入后，`create_deep_agent` 会自动添加 `SubagentRail`（同步模式）或 `SessionRail`（异步模式）
- `SubagentRail` 会创建 `task_tool` 工具并注册到 `ability_manager`
- 子代理的 `agent_card.name` 就是 LLM 在 `task_tool` 中选择的 `subagent_type`
- 子代理的 `agent_card.description` 会展示在 `task_tool` 的描述中，帮助 LLM 选择合适的子代理

---

## 9. 完整架构图

```
┌──────────────────────────────────────────────────────┐
│                   create_deep_agent()                 │
│                                                       │
│  subagents=[                                          │
│    SubAgentConfig(name="research_agent", ...),        │
│    SubAgentConfig(name="code_agent", ...),            │
│    SubAgentConfig(name="browser_agent", ...),         │
│  ]                                                    │
└──────────────┬───────────────────────────────────────┘
               │ 自动注册
               ▼
┌──────────────────────────────────────────────────────┐
│              SubagentRail.init(agent)                  │
│                                                       │
│  1. 收集子代理描述 → available_agents 字符串          │
│  2. create_task_tool(parent_agent, available_agents)   │
│  3. Runner.resource_mgr.add_tool(task_tool)           │
│  4. agent.ability_manager.add(task_tool.card)         │
└──────────────┬───────────────────────────────────────┘
               │ LLM 可见
               ▼
┌──────────────────────────────────────────────────────┐
│                  task_tool (LLM 调用)                  │
│                                                       │
│  参数：                                                │
│    subagent_type: "research_agent"                    │
│    task_description: "搜索关于XX的资料..."            │
└──────────────┬───────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────┐
│            TaskTool.invoke(inputs)                     │
│                                                       │
│  1. parent_agent.create_subagent(type, session_id)    │
│     ├─ _find_subagent_spec(type) → SubAgentConfig     │
│     ├─ 解析 factory_name → 选择工厂函数               │
│     └─ create_xxx_agent(**kwargs) → DeepAgent 实例     │
│  2. subagent.invoke({"query": task, "conversation_id": sid})  │
│  3. 返回 ToolOutput(output=result)                    │
└──────────────────────────────────────────────────────┘
```

---

## 10. 总结

| 要素 | 说明 |
|------|------|
| 核心类 | `SubAgentConfig`（声明式配置） |
| 管理 Rail | `SubagentRail`（自动注册 `task_tool`） |
| 调用工具 | `task_tool(subagent_type, task_description)` |
| 创建时机 | 延迟创建，`task_tool` 被调用时才实例化 |
| 工厂支持 | `code_agent` / `research_agent` / `browser_agent` / 通用 |
| 模型 | 可独立配置，也可复用父 Agent 模型 |
| 工具 | 独立配置，不自动继承父 Agent 工具 |
| 工作空间 | 自动创建子目录，或显式指定 |
| 接入方式 | 传入 `create_deep_agent(subagents=[...])` 即可 |
