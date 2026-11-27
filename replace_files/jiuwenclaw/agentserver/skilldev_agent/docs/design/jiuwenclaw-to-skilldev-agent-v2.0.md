# JiuWenClaw → 专用 Skill 生成 Agent 改造方案（v2 - 仅加法）

> 版本：v2.0  
> 日期：2026-05-13  
> 状态：设计稿  
> 约束：**只做加法和修改，不删除任何现有代码/模块**

---

## 1. 改造原则

### 1.1 核心约束

| 约束 | 说明 |
|------|------|
| **只做加法** | 不删除任何现有文件、模块、路由、代码行 |
| **只做修改** | 对现有文件的修改仅限于：新增分支、新增导入、新增方法 |
| **不做精简** | 不简化入口、不清理冗余模块、不移除无关路由 |
| **并存运行** | 新功能通过新模块实现，与现有系统并存，可通过配置切换 |

### 1.2 改造目标

在不破坏现有功能的前提下，**新增**一套专用 Skill 生成 Agent 的能力：

- 新增 `SkillDevDeepAdapter`：专注于 Skill 开发的轻量 DeepAgent 适配层
- 新增 `skilldev.chat` 统一 API：合并 start/respond 为单一入口
- 通过环境变量 / 配置切换：`JIUWENCLAW_AGENT_SDK=skilldev` 激活新适配层

---

## 2. 新适配层（SkillDevDeepAdapter）可行性分析

### 2.1 当前架构中适配层的角色

```
interface.py (JiuWenClaw 门面)
  │
  ├── skilldev.* ──→ SkillDevService（独立路径，绕过 adapter）
  │                    └── Pipeline → StageHandler → create_stage_agent() → 隔离 Agent
  │
  ├── skills.*   ──→ SkillManager（直接处理，不经 adapter）
  ├── tools.*    ──→ ToolManager（直接处理，不经 adapter）
  ├── chat.cancel──→ adapter.process_interrupt()
  ├── chat.answer──→ adapter.handle_user_answer()
  │
  └── 其他 chat  ──→ adapter.process_message_impl() / process_message_stream_impl()
                      └── JiuWenClawDeepAdapter → 主 DeepAgent（通用对话）
```

**关键发现：** SkillDev Pipeline 完全独立于 adapter——Pipeline 通过 `context.create_stage_agent()` 自建隔离 Agent，不使用主 DeepAgent 的任何工具、Rails 或 SubAgent。adapter 仅服务于非 skilldev 的通用对话路径。

### 2.2 可行性结论

**可行。** 原因如下：

| 维度 | 分析 |
|------|------|
| **协议兼容** | `SkillDevDeepAdapter` 实现 `AgentAdapter` Protocol（8 个方法），interface.py 通过 Protocol 调用，不关心具体实现 |
| **工厂路由** | `agent_adapters.py` 的 `create_adapter()` 基于 `sdk_name` 分支，新增 `elif sdk_name == "skilldev":` 即可 |
| **零侵入** | interface.py 不需任何修改——`_ensure_adapter()` 返回的任何 `AgentAdapter` 实现都能正常工作 |
| **Pipeline 无影响** | SkillDev Pipeline 路径完全绕过 adapter，换 adapter 不影响 Skill 生成核心流程 |
| **并存切换** | `JIUWENCLAW_AGENT_SDK=harness`（默认）使用原适配层；`=skilldev` 使用新适配层 |

### 2.3 必要性分析

**适度必要。** 详细分析：

| 场景 | 没有新适配层 | 有新适配层 | 判定 |
|------|------------|-----------|------|
| `skilldev.*` Pipeline 流程 | ✅ 不受影响 | ✅ 不受影响 | 无差别 |
| 用户在 Skill 开发中问通用问题 | 走 JiuWenClawDeepAdapter（通用 prompt，20+ 工具） | 走 SkillDevDeepAdapter（Skill prompt，~11 工具） | **新适配层更聚焦** |
| `chat.cancel` 中断处理 | ✅ 正常工作 | ✅ 需实现相同协议 | 无差别 |
| 系统启动 | 加载完整 DeepAgent（14+ Rails） | 加载精简 Agent（5 Rails） | **新适配层更快** |
| 未来扩展（Skill Agent 自由对话模式） | 需在通用 Agent 上做 Skill 适配 | 天然支持 | **新适配层更适合** |

**结论：** 新适配层对 Pipeline 核心流程无影响（Pipeline 自建 Agent），但对"辅助对话"路径有明显价值——Skill 专用 prompt + 精简工具集能让 Agent 更聚焦于 Skill 开发领域。此外，它建立了一个干净的架构基础，为未来 Pipeline → Agent 编排的演进做准备。

**推荐：采用新适配层方案。** 它是纯加法变更、风险极低、且提供了明确的架构价值。

---

## 3. 详细改造方案

### 3.1 变更总览

```
变更类型分布：

[新增文件]
  agentserver/skilldev_agent/__init__.py       ← 新适配层包
  agentserver/skilldev_agent/adapter.py        ← SkillDevDeepAdapter 实现
  agentserver/skilldev_agent/prompts.py        ← Skill 专用系统 Prompt
  agentserver/skilldev_agent/rails.py          ← 可选：自定义 Rail

[修改文件]（仅新增代码，不删除）
  agentserver/agent_adapters.py                ← +10 行：新增 skilldev 工厂分支
  agentserver/skilldev/service.py              ← +50 行：新增 _handle_chat 统一入口
  schema/message.py                            ← +1 行：新增 SKILLDEV_CHAT 枚举

[不变文件]
  agentserver/interface.py                     ← 零修改
  agentserver/deep_agent/interface_deep.py     ← 零修改
  agentserver/skilldev/pipeline.py             ← 零修改
  agentserver/skilldev/context.py              ← 零修改
  agentserver/skilldev/stages/*.py             ← 零修改
  agentserver/skilldev/schema.py               ← 零修改
  所有现有 tools、rails、memory、team 模块     ← 零修改
```

### 3.2 第一部分：新建 SkillDevDeepAdapter

#### 3.2.1 文件：`agentserver/skilldev_agent/__init__.py`

```python
"""SkillDev 专用 DeepAgent 适配层.

通过 JIUWENCLAW_AGENT_SDK=skilldev 环境变量激活。
与现有 JiuWenClawDeepAdapter 并存，不修改、不替代原适配层。
"""
from jiuwenclaw.agentserver.skilldev_agent.adapter import SkillDevDeepAdapter

__all__ = ["SkillDevDeepAdapter"]
```

#### 3.2.2 文件：`agentserver/skilldev_agent/adapter.py`

**实现目标：** 一个精简版 DeepAgent 适配层，实现 `AgentAdapter` Protocol 的所有 8 个方法。

**核心设计：**

```python
class SkillDevDeepAdapter:
    """专用 Skill 生成 Agent 的 DeepAgent 适配层.
    
    与 JiuWenClawDeepAdapter（~4464 行）的关键区别：
    - Rails: 仅 5 个（vs 14+）
    - Tools: 仅 11 个（vs 20+）
    - SubAgents: 无（vs 3 个）
    - Prompt: Skill 开发专用（vs 通用对话）
    - 启动速度更快（加载更少组件）
    """
```

**需实现的 AgentAdapter Protocol 方法：**

| 方法 | 实现策略 |
|------|---------|
| `create_instance(config, mode)` | 创建 DeepAgent，注册精简 Rails + Tools |
| `reload_agent_config(config_base, env_overrides)` | 重建 Agent 实例 |
| `process_message_impl(request, inputs)` | 调用 Runner.run_agent |
| `process_message_stream_impl(request, inputs)` | 调用 Runner.run_agent_streaming |
| `process_interrupt(request)` | 委托 DeepAgent cancel/pause/resume |
| `handle_user_answer(request)` | 构建 InteractiveInput 传回 Agent |
| `handle_heartbeat(request)` | 返回 None（SkillDev 无心跳需求） |
| `is_working(session_tasks, session_queues)` | 检查 Agent 是否运行中 |

**Rails 配置：**

| Rail | 保留原因 |
|------|---------|
| `ContextEngineeringRail` | 长对话上下文管理 |
| `FileSystemRail` | Skill 文件操作前置（workspace 限制） |
| `SecurityRail` | 安全防护 |
| `HeartbeatRail` | 健康检查 |
| `JiuClawStreamEventRail` | 流式事件转换（复用现有实现） |

**工具集（复用现有 context.py 的 HARNESS_TOOL_CLASSES）：**

```python
SKILLDEV_ADAPTER_TOOLS = {
    "file_read": ReadFileTool,
    "file_write": WriteFileTool,
    "file_edit": EditFileTool,
    "file_glob": GlobTool,
    "file_grep": GrepTool,
    "file_listdir": ListDirTool,
    "shell": BashTool,
    "code_execute": CodeTool,
    "web_search_free": JiuwenHarnessFreeSearchTool,
    "web_search_paid": WebPaidSearchTool,
    "web_fetch": JiuwenHarnessFetchWebpageTool,
}
```

> 注：这与 `skilldev/context.py` 中的 `HARNESS_TOOL_CLASSES` 完全一致，可直接导入复用。

**与 interface.py 的交互协议：**

```
interface.py 调用链（不变）：
  
  _ensure_adapter()
    → agent_adapters.create_adapter(sdk_name="skilldev")  # 仅此处变化
    → 返回 SkillDevDeepAdapter 实例
  
  create_instance()
    → adapter.create_instance()  ← SkillDevDeepAdapter 实现
    → 后续 MCP 工具加载、extension tool 注册 ← interface.py 自身逻辑

  process_message_stream() 中：
    skilldev.* → SkillDevService（不变，不经 adapter）
    其他 chat  → adapter.process_message_stream_impl()  ← SkillDevDeepAdapter 实现
```

**需注意的兼容点：**

| 兼容点 | 说明 | 处理策略 |
|--------|------|---------|
| `create_instance` 后的 MCP 加载 | interface.py 调用 `get_instance()` 拿到 Agent 后注册 MCP 工具 | SkillDevDeepAdapter 需实现 `get_instance()` 返回内部 DeepAgent |
| `set_skill_manager` | interface.py 调用 `adapter.set_skill_manager()` | 实现此方法（或 `hasattr` 检查已覆盖） |
| `_register_extension_tools` | 需要 `get_instance()` 返回有 `ability_manager` 的 Agent | SkillDevDeepAdapter 的 DeepAgent 天然支持 |
| `_build_inputs` | 构建 `query`、`conversation_id` 等 | 不依赖 adapter，interface.py 自行构建 |
| `_build_interactive_input_from_answers` | 构建权限确认的 InteractiveInput | SkillDevDeepAdapter 需支持 InteractiveInput 输入 |
| `cat_cafe_mcp` 注册 | `run_agent_task` 中注册请求级 MCP | SkillDevDeepAdapter 可忽略（无 ToolManager 依赖）或兼容处理 |

#### 3.2.3 文件：`agentserver/skilldev_agent/prompts.py`

```python
SKILLDEV_SYSTEM_PROMPT = """你是一个专业的 Skill 开发 Agent。

## 核心职责
帮助用户创建高质量的 Agent Skill（技能包）。

## 能力范围
- 分析用户需求，澄清不明确的点
- 设计 Skill 的结构和内容（SKILL.md + scripts/ + references/）
- 生成、验证、测试、评测、改进 Skill
- 优化 Skill 的触发描述和质量

## 工作方式
Skill 创建有专门的 Pipeline 流程处理（用户通过 skilldev.* 接口触发）。
你负责辅助对话：回答用户关于 Skill 开发的问题、提供建议、帮助调试。

## 工作区
工作区路径：{workspace}
所有文件操作限制在工作区内。
"""
```

### 3.3 第二部分：修改 agent_adapters.py（+10 行）

```python
# 在 create_adapter() 函数中新增一个分支：

async def create_adapter(sdk, workspace_dir, agent_id, service_id):
    sdk_name = sdk or resolve_sdk_choice()

    if sdk_name == "harness":
        # ... 现有代码不变 ...
        pass

    # ──── 新增：skilldev 适配层 ────
    if sdk_name == "skilldev":
        import asyncio

        def import_and_create():
            from jiuwenclaw.agentserver.skilldev_agent.adapter import SkillDevDeepAdapter
            return SkillDevDeepAdapter(
                workspace_dir=workspace_dir,
                agent_id=agent_id,
                service_id=service_id,
            )

        return await asyncio.get_event_loop().run_in_executor(None, import_and_create)
    # ──── 新增结束 ────

    if sdk_name == "pi":
        # ... 现有代码不变 ...
        pass
```

同时在 `resolve_sdk_choice()` 中将 `"skilldev"` 加入 `valid_sdks`：

```python
valid_sdks = {"harness", "pi", "skilldev"}  # 新增 "skilldev"
```

### 3.4 第三部分：新增 skilldev.chat 统一 API

#### 3.4.1 schema/message.py 新增枚举（+1 行）

```python
class ReqMethod(str, Enum):
    # ... 现有枚举值不变 ...
    SKILLDEV_CHAT = "skilldev.chat"  # 新增
```

#### 3.4.2 service.py 新增 _handle_chat（+50 行）

在 `SkillDevService` 中新增方法，不修改现有方法：

```python
class SkillDevService:
    
    # 现有 _METHOD_DISPATCH 不变，新增 skilldev.chat 路由：
    _METHOD_DISPATCH = {
        # ... 现有映射不变 ...
        ReqMethod.SKILLDEV_CHAT: "_handle_chat",  # 新增
    }
    
    async def _handle_chat(self, request: AgentRequest):
        """统一聊天入口：根据 task_id 和当前状态自动分流.
        
        - task_id 不存在 → 等同 skilldev.start（创建新任务）
        - task_id 存在 + 任务在挂起状态 → 等同 skilldev.respond（恢复执行）
        - task_id 存在 + 任务在运行中 → 追加用户消息（未来扩展）
        """
        params = request.params or {}
        task_id = params.get("task_id")
        
        if not task_id or not self._deps.state_store.exists(task_id):
            # 新任务：委托到 _handle_start
            async for chunk in self._handle_start(request):
                yield chunk
        else:
            # 已有任务：委托到 _handle_respond
            async for chunk in self._handle_respond(request):
                yield chunk
```

> 注：`_handle_start` 和 `_handle_respond` 是现有方法，不修改。`_handle_chat` 只是在上面加了一层自动分流。

#### 3.4.3 interface.py 无需修改

因为 `_SKILLDEV_METHODS` 使用动态匹配：

```python
_SKILLDEV_METHODS: frozenset[ReqMethod] = frozenset(
    m for m in ReqMethod if m.value.startswith("skilldev.")
)
```

新增 `SKILLDEV_CHAT = "skilldev.chat"` 后，它会自动被 `_SKILLDEV_METHODS` 捕获，路由到 `SkillDevService.handle()`。**零修改。**

---

## 4. 涉及文件全表

### 4.1 新增文件

| 文件路径 | 说明 | 预估行数 |
|---------|------|---------|
| `agentserver/skilldev_agent/__init__.py` | 新适配层包入口 | ~10 |
| `agentserver/skilldev_agent/adapter.py` | SkillDevDeepAdapter 实现 | ~400-500 |
| `agentserver/skilldev_agent/prompts.py` | Skill 专用系统 Prompt | ~50 |

### 4.2 修改文件

| 文件路径 | 修改内容 | 行数变化 | 是否删除代码 |
|---------|---------|---------|-------------|
| `agentserver/agent_adapters.py` | 新增 `skilldev` 工厂分支 + valid_sdks | +12 行 | **否** |
| `agentserver/skilldev/service.py` | 新增 `_handle_chat` + 路由映射 | +50 行 | **否** |
| `schema/message.py` | 新增 `SKILLDEV_CHAT` 枚举 | +1 行 | **否** |

### 4.3 不变文件（明确列出确保零侵入）

| 文件/目录 | 说明 |
|-----------|------|
| `agentserver/interface.py` | 门面层零修改（`_SKILLDEV_METHODS` 自动捕获新枚举） |
| `agentserver/deep_agent/interface_deep.py` | 原适配层零修改 |
| `agentserver/skilldev/pipeline.py` | Pipeline 核心逻辑不变 |
| `agentserver/skilldev/context.py` | 阶段 Agent 创建不变 |
| `agentserver/skilldev/stages/*.py` | 所有 StageHandler 不变 |
| `agentserver/skilldev/schema.py` | 数据模型不变 |
| `agentserver/skilldev/deps.py` | 依赖注入不变 |
| `agentserver/agent_ws_server.py` | WS 入口不变 |
| `agentserver/agent_manager.py` | 实例管理不变 |
| `agentserver/team/` | 团队模式不变 |
| `agentserver/memory/` | 记忆系统不变 |
| `agentserver/tools/` | 所有工具不变 |
| `agentserver/deep_agent/rails/` | 所有 Rail 不变 |
| `config.py` | 配置系统不变 |

---

## 5. 分批实施计划

### 批次 0：新建 SkillDevDeepAdapter（核心）

**范围：** 创建新适配层，可通过环境变量切换

**详细步骤：**

1. 创建 `agentserver/skilldev_agent/` 目录和 `__init__.py`
2. 创建 `prompts.py`：定义 Skill 专用系统 Prompt
3. 创建 `adapter.py`：实现 `SkillDevDeepAdapter`
   - 构造函数：接收 `workspace_dir`、`agent_id`、`service_id`
   - `create_instance()`：
     - 使用 `create_deep_agent()` 创建 DeepAgent
     - 注册 5 个 Rails（复用现有 Rail 类）
     - 注册 11 个 Tools（复用 `skilldev/context.py` 的 `HARNESS_TOOL_CLASSES`）
     - 使用 Skill 专用系统 Prompt
   - `process_message_impl()`：调用 `Runner.run_agent()`
   - `process_message_stream_impl()`：调用 `Runner.run_agent_streaming()`
   - `process_interrupt()`：基本实现（cancel 当前 task）
   - `handle_user_answer()`：构建 InteractiveInput（复用 interface.py 逻辑）
   - `handle_heartbeat()`：返回 None
   - `is_working()`：检查 Agent 状态
   - `get_instance()`：返回 DeepAgent 实例（兼容 interface.py 的 MCP/extension 注册）
   - `set_skill_manager()`：接收 SkillManager 引用
4. 修改 `agent_adapters.py`：
   - `resolve_sdk_choice()` 中 `valid_sdks` 加入 `"skilldev"`
   - `create_adapter()` 中新增 `elif sdk_name == "skilldev":` 分支

**验证方法：**
```bash
# 使用新适配层启动
export JIUWENCLAW_AGENT_SDK=skilldev
python -m jiuwenclaw.app_agentserver

# 验证 1：系统正常启动，无报错
# 验证 2：发送通用 chat 请求，Agent 使用 Skill 专用 prompt 回复
# 验证 3：发送 skilldev.start 请求，Pipeline 正常执行（不受 adapter 影响）

# 切回默认适配层验证无影响
export JIUWENCLAW_AGENT_SDK=harness
# 验证 4：所有现有功能正常
```

**预计工作量：** 1-2 天

**核心参考文件（实现时需要阅读）：**
- `deep_agent/interface_deep.py`：参考 `create_instance` 的 DeepAgent 构建模式
- `skilldev/context.py`：复用 `HARNESS_TOOL_CLASSES` 和 Agent 构建逻辑
- `agent_adapters.py`：理解工厂路由

### 批次 1：新增 skilldev.chat 统一 API

**范围：** 添加统一入口，保留旧 API 不变

**详细步骤：**

1. `schema/message.py`：新增 `SKILLDEV_CHAT = "skilldev.chat"` 枚举值
2. `skilldev/service.py`：
   - 在 `_METHOD_DISPATCH` 字典中新增 `ReqMethod.SKILLDEV_CHAT: "_handle_chat"` 映射
   - 新增 `_handle_chat()` 方法（内部自动分流到 `_handle_start` 或 `_handle_respond`）
3. 无需修改 interface.py（`_SKILLDEV_METHODS` 动态匹配自动覆盖）

**验证方法：**
```bash
# 验证 1：通过 skilldev.chat 发起新任务
ws.send({"method": "skilldev.chat", "params": {"message": "创建搜索 arXiv 的 Skill"}})
# 期望：等同 skilldev.start

# 验证 2：通过 skilldev.chat 回复澄清问题
ws.send({"method": "skilldev.chat", "params": {"task_id": "sd_xxx", "user_input": {...}}})
# 期望：等同 skilldev.respond

# 验证 3：旧 API 仍然正常
ws.send({"method": "skilldev.start", ...})  # 不受影响
ws.send({"method": "skilldev.respond", ...})  # 不受影响
```

**预计工作量：** 0.5 天

### 批次 2：集成验证与文档

**范围：** 端到端测试 + 文档更新

**详细步骤：**

1. 新适配层 + 旧适配层的对比测试
   - 同一 Skill 生成请求，分别用 `harness` 和 `skilldev` 适配层执行
   - 确认 Pipeline 行为完全一致
   - 确认通用对话路径在新适配层下使用 Skill 专用 prompt
2. `skilldev.chat` API 全流程测试
   - 完整流程：`chat(新建)` → 澄清 → `chat(回复)` → 生成 → ... → 打包
   - 混合使用：部分用 `skilldev.chat`，部分用 `skilldev.start/respond`
3. 兼容性回归
   - 确认所有现有 `skilldev.*` API 正常
   - 确认 `skills.*`、`tools.*`、通用 `chat` 等路径正常
   - 确认 Team 模式、Memory 等不受影响

**预计工作量：** 0.5-1 天

---

## 6. SkillDevDeepAdapter 实现参考

### 6.1 构建模式参考

新适配层的 `create_instance` 构建 DeepAgent 的模式，可参考两处现有实现：

**参考 1：`interface_deep.py` 的主 Agent 构建（完整版）**

```python
# JiuWenClawDeepAdapter.create_instance() 核心流程：
agent = create_deep_agent(
    agent_card=AgentCard(name="jiuwenclaw", description="..."),
    system_prompt=build_identity_prompt(),
    model=Model(request_config=..., client_config=...),
    workspace=Workspace(workspace_dir),
    tools=[...],           # 20+ 工具
    rails=[...],           # 14+ Rails
    subagents=[...],       # 3 个 SubAgent
    enable_task_loop=True,
    max_iterations=200,
)
```

**参考 2：`skilldev/context.py` 的 Stage Agent 构建（精简版）**

```python
# SkillDevContext.create_stage_agent() 核心流程：
agent = create_deep_agent(
    agent_card=AgentCard(name=f"skilldev_{stage}", description=stage_desc),
    system_prompt=stage_prompt,
    model=Model(request_config=..., client_config=...),
    workspace=Workspace(workspace_dir),
    tools=[ToolClass() for ToolClass in stage_tools],  # 按阶段白名单
    rails=[JiuClawStreamEventRail(...)],
    subagents=None,
    enable_task_loop=False,
    max_iterations=50,
)
```

新适配层的构建模式介于两者之间：比主 Agent 精简，比 Stage Agent 完整。

### 6.2 类骨架

```python
class SkillDevDeepAdapter:
    """专用 Skill 生成 Agent 的 DeepAgent 适配层."""

    def __init__(self, workspace_dir=None, agent_id=None, service_id=None):
        self._workspace_dir = workspace_dir or str(get_agent_workspace_dir())
        self._agent_id = agent_id
        self._service_id = service_id
        self._agent = None           # DeepAgent 实例
        self._skill_manager = None   # SkillManager 引用

    def get_instance(self):
        """返回 DeepAgent 实例（兼容 interface.py MCP/extension 注册）."""
        return self._agent

    def set_skill_manager(self, manager):
        self._skill_manager = manager

    async def create_instance(self, config=None, *, mode="claw"):
        """构建 Skill 专用 DeepAgent."""
        from openjiuwen.harness.factory import create_deep_agent
        # ... 构建精简 Agent ...

    async def process_message_impl(self, request, inputs):
        """非流式处理."""
        from openjiuwen.core.runner import Runner
        result = await Runner.run_agent(self._agent, inputs)
        # ... 包装为 AgentResponse ...

    async def process_message_stream_impl(self, request, inputs):
        """流式处理."""
        from openjiuwen.core.runner import Runner
        async for event in Runner.run_agent_streaming(self._agent, inputs):
            yield self._convert_to_chunk(event, request)

    async def process_interrupt(self, request):
        """中断处理."""
        # ... 基本 cancel 实现 ...

    async def handle_user_answer(self, request):
        """用户回答处理."""
        # ... InteractiveInput 构建 ...

    async def handle_heartbeat(self, request):
        """心跳处理：返回 None（让 interface.py 跳过）."""
        return None

    def is_working(self, session_tasks, session_queues):
        """检查 Agent 是否运行中."""
        return bool(session_tasks)
```

---

## 7. 风险与缓解

### 7.1 interface.py 兼容性

**风险：** `interface.py` 的 `create_instance()` 在调用 `adapter.create_instance()` 后，还执行了 MCP 工具加载和 extension tool 注册。这些操作依赖 `adapter.get_instance()` 返回一个有 `ability_manager` 的 Agent。

**缓解：** `SkillDevDeepAdapter.get_instance()` 返回内部 DeepAgent 实例，其 `ability_manager` 是 `create_deep_agent` 自动创建的标准组件。MCP 工具和 extension tool 的注册会正常执行（虽然 Skill 场景不一定需要它们，但不会报错）。

### 7.2 _build_inputs 兼容性

**风险：** `interface.py` 的 `_build_inputs()` 构建包含 `build_user_prompt` 包装的 query。新适配层可能不需要这种 JSON 包装。

**缓解：** 这是 interface.py 层面的逻辑，不由 adapter 控制。短期内接受这个包装（Agent 的 Prompt 可以指导它解析 JSON 格式的输入）；长期可在 interface.py 中增加一个条件分支（小改动）。

### 7.3 cat_cafe_mcp 注册

**风险：** `interface.py` 的 `run_agent_task` 中会尝试注册 `cat_cafe_mcp`，调用 `self._get_tool_manager().register_request_scoped_cat_cafe_mcp()`。

**缓解：** 这个调用在 interface.py 层面发生，在 `adapter.process_message_impl()` 之前。`_get_tool_manager()` 是 JiuWenClaw 的方法，不依赖 adapter。即使 ToolManager 注册了 MCP，新适配层的 Agent 不使用它也不会出错。

### 7.4 环境变量切换成本

**风险：** 需要重启进程才能切换适配层。

**缓解：** 可以后续在 `agent_adapters.py` 中支持配置文件级别的切换（读 `config.yaml` 中的 `agent_sdk` 字段），甚至支持运行时热切换。但初期环境变量已经足够。

---

## 8. 与原方案（v1）的对比

| 维度 | v1 方案（做减法） | v2 方案（只做加法） |
|------|------------------|-------------------|
| **interface.py** | 删除 ~300 行路由 | 零修改 |
| **interface_deep.py** | 标记 deprecated | 零修改 |
| **agent_ws_server.py** | 移除 browser/command 分支 | 零修改 |
| **tools/ 目录** | 删除 50+ 文件 | 零修改 |
| **team/, memory/** | 删除 | 零修改 |
| **新增文件** | 4 个 | 3 个 |
| **修改文件** | 7 个 | 3 个 |
| **删除文件** | 60+ 个 | 0 个 |
| **风险等级** | 中-高（删除可能触发隐式依赖） | **低**（纯新增，不影响现有功能） |
| **回滚难度** | 高（删除后需恢复） | **极低**（删除新增文件即可） |
| **SkillDev Pipeline** | 不变 | 不变 |
| **切换方式** | 不可逆改造 | 环境变量随时切换 |
| **总工作量** | 3-5 天 | **2-3 天** |

---

## 9. 总结

本方案的核心思路是**只做加法**：

1. **新增 SkillDevDeepAdapter**：一个精简的 Skill 专用适配层（~500 行），与现有 4464 行的通用适配层并存
2. **新增 skilldev.chat API**：统一入口，旧 API 不变
3. **仅修改 3 个文件**：`agent_adapters.py`（+12 行）、`service.py`（+50 行）、`message.py`（+1 行）
4. **零删除**：所有现有模块、路由、工具、Rails 完全保留不动
5. **环境变量切换**：`JIUWENCLAW_AGENT_SDK=skilldev` 激活新适配层，默认仍使用原适配层

改造分 3 个批次（含集成验证），总工作量预计 2-3 天。由于是纯加法变更，回滚成本极低——删除新增文件、恢复 3 个文件的少量改动即可完全回退。
