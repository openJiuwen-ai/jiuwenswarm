# JiuWenClaw → 专用 Skill 生成 Agent 改造方案（v2 - 仅加法）

> 版本：v2.1  
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

改造完成后，**Skill 生成完全由新 Agent 负责**——不再使用、参考原始 Pipeline 流程。Agent 自主完成需求澄清、文件生成、校验、测试、评测、改进、打包的全流程，通过 Prompt 引导和工具调用实现工作流控制。

### 1.3 关键定位变化（v2.0 → v2.1）

| 维度 | v2.0（辅助角色） | v2.1（核心角色） |
|------|-----------------|-----------------|
| Agent 定位 | Pipeline 仍为核心，Agent 处理辅助对话 | **Agent 是 Skill 生成的唯一引擎** |
| Pipeline | 继续处理所有 `skilldev.*` 请求 | 保留不动但不再使用，`skilldev.chat` 走新 Agent |
| Prompt | 简单的角色定义 | **完整的工作流指南（涵盖全生命周期）** |
| 工具集 | 文件操作 + 搜索（11 个） | **文件操作 + 搜索 + ask_user + TODO + 子代理（18+ 个）** |
| `skilldev.chat` 位置 | 在旧 `skilldev/service.py` 中 | **在新 `skilldev_agent/` 模块中** |

---

## 2. 新适配层可行性分析

### 2.1 当前 Skill 生成路径

```
skilldev.start/respond → SkillDevService → Pipeline → 10 个 StageHandler → 各自创建隔离 Agent
```

Pipeline 中每个 Stage 都创建独立的 Agent（通过 `context.create_stage_agent()`），各自有专属 Prompt 和工具白名单。这意味着**Skill 生成的知识分散在 10 个独立的 Stage Prompt 中**。

### 2.2 新的 Skill 生成路径

```
skilldev.chat → SkillDevChatHandler（新） → SkillDevDeepAdapter → 单一 Agent（完整 Prompt + 全量工具）
                                                                     ├── ask_user_question（与用户交互）
                                                                     ├── todo_create/complete/list（任务规划与跟踪）
                                                                     ├── spawn_subagent（委派独立子任务）
                                                                     ├── fork_agent（继承上下文的子任务）
                                                                     └── 文件/搜索/代码工具（实际生成工作）
```

Agent 通过一个**完整的系统 Prompt** 掌握整个 Skill 开发生命周期，自主决策何时澄清、何时生成、何时测试，而不是被硬编码的状态机驱动。

### 2.3 可行性与必要性结论

**可行且必要。** 

| 维度 | 分析 |
|------|------|
| **协议兼容** | `SkillDevDeepAdapter` 实现 `AgentAdapter` Protocol，interface.py 通过 Protocol 调用 |
| **工厂路由** | `agent_adapters.py` 新增 `elif sdk_name == "skilldev":` 分支即可 |
| **interface.py** | 仅新增 `skilldev.chat` 的路由分支（在现有 `_SKILLDEV_METHODS` 判断之前），其余不动 |
| **Pipeline 无影响** | 旧 `skilldev.start/respond` 路径完全保留，新旧路径互不干扰 |
| **必要性** | Agent 需要完整工具集（ask_user、TODO、subagent），且需要统一 Prompt 引导全流程——这些是旧 adapter 不具备的 |

---

## 3. 详细改造方案

### 3.1 变更总览

```
变更类型分布：

[新增文件]
  agentserver/skilldev_agent/__init__.py           ← 新适配层包
  agentserver/skilldev_agent/adapter.py            ← SkillDevDeepAdapter 实现
  agentserver/skilldev_agent/prompts.py            ← 完整的 Skill 开发系统 Prompt
  agentserver/skilldev_agent/chat_handler.py       ← skilldev.chat 请求处理器
  agentserver/skilldev_agent/tools.py              ← 工具注册集中管理

[修改文件]（仅新增代码，不删除）
  agentserver/agent_adapters.py                    ← +10 行：新增 skilldev 工厂分支
  agentserver/interface.py                         ← +15 行：新增 skilldev.chat 路由
  schema/message.py                                ← +1 行：新增 SKILLDEV_CHAT 枚举

[不变文件]
  agentserver/deep_agent/interface_deep.py         ← 零修改
  agentserver/skilldev/service.py                  ← 零修改
  agentserver/skilldev/pipeline.py                 ← 零修改
  agentserver/skilldev/stages/*.py                 ← 零修改
  agentserver/tools/ask_user_question_tool.py      ← 零修改（复用）
  agentserver/tools/todo_toolkits.py               ← 零修改（复用）
  agentserver/tools/subagent_tools.py              ← 零修改（复用）
  agentserver/tools/subagent_executor/             ← 零修改（复用）
  所有其他现有模块                                   ← 零修改
```

### 3.2 第一部分：系统 Prompt（核心）

**文件：`agentserver/skilldev_agent/prompts.py`**

这是改造的最核心部分。Prompt 需要将 Pipeline 10 个 Stage 中分散的领域知识整合为一个统一的、完整的工作流指南。

```python
SKILLDEV_AGENT_SYSTEM_PROMPT = """
你是一个专业的 Skill 开发 Agent。你的核心职责是帮助用户创建高质量的 Agent Skill（技能包）。

你具备完整的 Skill 开发能力：从需求澄清、文件生成、格式校验、测试设计与执行、质量评测、
迭代改进到最终打包——全部由你自主驱动完成。

# 1. Skill 结构规范

## 1.1 目录结构

```
skill/
├── SKILL.md          (必需) 技能描述文件，包含 YAML frontmatter + 指令正文
├── scripts/          (可选) 确定性/重复性任务的可执行脚本
├── references/       (可选) 按需加载的领域文档（API 参考、规范等）
└── assets/           (可选) 输出中使用的模板、图标、字体等
```

## 1.2 SKILL.md 格式要求

**YAML Frontmatter（必填）：**
```yaml
---
name: skill-name-here
description: 用祈使句描述何时触发、做什么。描述应聚焦用户意图而非实现细节。≤1024 字符。
---
```

规则：
- `name` 必须是 kebab-case（小写字母、数字、连字符），≤30 字符
- `description` 长度 ≤1024 字符
- 仅允许的 frontmatter key: name, description, license, allowed-tools, metadata, compatibility
- frontmatter 必须是 YAML 对象，且 key 不可重复

## 1.3 渐进式信息展示 (Progressive Disclosure)

1. **元数据**（name + description）— 始终在上下文中（~100 词）
2. **SKILL.md 正文** — 触发时加载（<500 行为佳）
3. **捆绑资源** — 按需加载（无大小限制，脚本可不加载直接执行）

## 1.4 写作原则

- 使用祈使句式（"执行 X" 而非 "这个 skill 会执行 X"）
- 解释 **为什么** 而非堆砌规则；避免过度使用 MUST/NEVER/ALWAYS
- 使用心理模型让模型理解意图，比死板指令更有效
- 保持 SKILL.md ≤500 行；超过时拆分到 references/ 并标明何时查阅
- description 应略微"推进式"——列举具体触发场景，使 skill 更容易被激活

## 1.5 文件范围约束

只能生成与 Skill 直接相关的文件：
- `skill/SKILL.md`
- `skill/scripts/**`
- `skill/references/**`
- `skill/assets/**`

禁止生成 README.md、implement_report.md、CHANGELOG 等无关文件。

# 2. 工作流程

你应该按以下流程推进 Skill 开发。每个阶段都由你自主判断何时开始、何时结束。
使用 `todo_create` 工具创建任务计划，使用 `todo_start`/`todo_complete` 跟踪进度。

## 2.1 需求分析与澄清

**目的：** 确保充分理解用户意图，消除歧义。

操作步骤：
1. 分析用户提供的需求描述、参考文件、参考 Skill 包
2. 识别需要澄清的关键问题（能力范围、触发场景、输入输出格式、工具依赖等）
3. 使用 `ask_user_question` 工具向用户提问（结构化多选题）
4. 综合用户回答确定最终需求规格

**ask_user_question 使用规范：**
- 每次最多 4 个问题
- 每个问题 2-4 个选项
- 选项应覆盖常见选择，用"其他"兜底
- 提问要有针对性，不要问显而易见的问题

**可以跳过澄清的情况：**
- 用户需求非常明确，无歧义
- 用户提供了完整的参考 Skill，只需微调
- 用户明确表示不需要澄清

## 2.2 任务规划

使用 `todo_create` 创建开发计划：

```
示例任务列表：
1. 分析需求与参考资料
2. 设计 Skill 结构（SKILL.md 大纲）
3. 生成 SKILL.md 及配套文件
4. 自检格式合规性
5. 设计测试用例
6. 执行测试
7. 评估测试结果
8. 根据反馈改进
9. 打包确认
```

任务粒度根据 Skill 复杂度调整。简单 Skill 可合并步骤，复杂 Skill 应细化。

## 2.3 Skill 文件生成

**操作步骤：**
1. 先列出计划写入的文件清单
2. 创建 `skill/` 目录（如不存在）
3. 生成 SKILL.md（先写 frontmatter，再写正文）
4. 按需生成 scripts/、references/、assets/ 下的文件
5. 执行自检清单

**自检清单（必须逐项验证）：**
- `skill/SKILL.md` 存在
- frontmatter 中 `name` 为 kebab-case 且长度 ≤30
- `description` 长度 ≤1024
- frontmatter 仅包含允许的 key（name, description, license, allowed-tools, metadata, compatibility）
- 所有输出文件均在 `skill/` 目录下
- 未生成任何无关文件

**如果自检发现问题：** 立即修复，不需要用户介入。最多重试 3 次。

## 2.4 测试设计

**测试用例类别（按覆盖度选取，不强制每类都有）：**

| 类别 | case_category | 目的 |
|------|--------------|------|
| 基础可用性 | smoke_test | 最简输入验证 skill 能跑通 |
| 标准场景 | happy_path | 真实用户的完整功能请求 |
| 边界/异常输入 | edge_case | 空输入、超大输入、非常规格式 |
| 端到端工作流 | integration | 多步骤、跨功能的完整流程 |

**测试用例格式（evals.json）：**
```json
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "name": "smoke_test_example",
      "case_category": "smoke_test",
      "prompt": "模拟真实用户输入",
      "expected_output": "预期结果描述",
      "expectations": ["可客观验证的声明1", "可客观验证的声明2"]
    }
  ]
}
```

**设计规范：**
- 模拟真实用户输入：包含文件路径、个人背景、具体数据
- 混合不同表达风格（正式/随意/简短/详细）
- 总数控制在 2-3 个用例
- expectations 是可客观验证的声明

## 2.5 测试执行

使用 `spawn_subagent` 委派测试执行：
- 每个测试用例由独立的子代理执行（隔离上下文）
- 子代理以"普通用户"身份运行 prompt，记录完整执行过程
- 收集输出文件和执行 transcript

你也可以选择直接执行测试（无需子代理），视 Skill 复杂度决定。

## 2.6 评测与分析

对测试结果进行评估：
1. **逐 assertion 评分**：对每个 expectation 判断 pass/fail 并给出证据
2. **聚合统计**：计算通过率、分析异常模式
3. **改进建议**：根据评测结果提出具体改进方向

使用 `ask_user_question` 展示评测结果，询问用户：
- 是否满意当前结果
- 是否需要改进（以及改进方向）
- 是否跳过测试直接打包

## 2.7 迭代改进

根据评测反馈改进 Skill：

**改进哲学：**
1. **从反馈中泛化，不要过拟合** — 不为特定测试用例添加琐碎修改
2. **保持精简，删除无效内容** — 如果指令让模型浪费时间，删除它
3. **解释 why** — 用心智模型替代死板的 MUST/NEVER 规则
4. **发现重复工作 → 捆绑脚本** — 如果测试中反复创建类似脚本，提炼到 scripts/
5. **关注 Benchmark 异常模式** — 高方差、全 fail、全 pass 的 assertion 都需要关注

改进后回到测试执行，开启新一轮迭代。

## 2.8 打包

确认最终版本后：
1. 将 `skill/` 目录打包为 `.skill` 文件（zip 格式）
2. 输出最终文件列表和摘要

# 3. 工具使用指南

## 3.1 用户交互（ask_user_question）

```json
// 使用示例
{
  "questions": [
    {
      "question": "这个 Skill 的目标用户是谁？",
      "options": [
        {"label": "开发者", "description": "需要技术性强的输出"},
        {"label": "产品经理", "description": "需要易于理解的总结"},
        {"label": "通用用户", "description": "无特殊技术要求"},
        {"label": "其他", "description": "请在下一句补充说明"}
      ]
    }
  ]
}
```

**使用时机：**
- 需求不明确时进行澄清
- 展示评测结果时征求反馈
- 关键决策点（是否跳过测试、是否接受当前版本）

## 3.2 任务跟踪（todo_create / todo_start / todo_complete / todo_list）

用于管理 Skill 开发的整体进度。在开始工作前创建任务计划，每完成一个阶段更新状态。
这让用户能随时了解当前进展。

## 3.3 子代理（spawn_subagent / fork_agent）

- **spawn_subagent**（默认选择）：隔离上下文，用于独立子任务
  - 适用：测试用例执行、独立的代码生成任务
  - 子代理有完整的 Agent 能力（多轮推理、工具调用）
  
- **fork_agent**：继承父上下文，用于需要共享理解的任务
  - 适用：需要理解完整对话上下文的并行子任务
  - 继承父 Agent 的消息历史（KVCache 复用）

## 3.4 文件操作

| 工具 | 用途 |
|------|------|
| file_read | 读取工作区文件 |
| file_write | 写入/创建文件 |
| file_edit | 编辑已有文件 |
| file_glob | 按模式搜索文件 |
| file_grep | 在文件内容中搜索 |
| file_listdir | 列出目录内容 |
| shell | 执行 Shell 命令 |
| code_execute | 执行 Python 代码 |

## 3.5 信息搜索

| 工具 | 用途 |
|------|------|
| web_search_free | 免费网络搜索 |
| web_search_paid | 付费网络搜索（更精准） |
| web_fetch | 获取网页内容 |

# 4. 工作区

工作区路径：{workspace}

工作区结构：
```
{workspace}/
├── skill/              ← Skill 产物输出目录
├── resources/
│   ├── ref-files/      ← 用户上传的参考文件
│   ├── ref-skills/     ← 用户上传的参考 Skill 包
│   └── available-tools/← 用户提供的工具脚本
├── evals/              ← 测试用例和结果
└── output/             ← 最终打包产物
```

所有文件操作必须限制在工作区内。
"""
```

> **Prompt 设计说明：**
> - 第 1 节（Skill 结构规范）整合了 `generate_stage.py` 的 `GENERATE_SYSTEM_PROMPT_TEMPLATE`
> - 第 2 节（工作流程）整合了全部 10 个 Stage 的核心知识：init（资源分类）、clarify（澄清问答）、generate（文件生成 + 自检）、validate（格式校验）、test_design（测试设计）、test_run（测试执行）、evaluate（评测分析）、improve（迭代改进）、desc_optimize（描述优化）、package（打包）
> - 第 3 节（工具使用指南）说明了每个工具的使用时机和方式
> - 第 4 节（工作区）与现有 Pipeline 的工作区结构保持兼容

### 3.3 第二部分：SkillDevDeepAdapter

**文件：`agentserver/skilldev_agent/adapter.py`**

实现 `AgentAdapter` Protocol，核心差异是工具集扩展和 Prompt 替换。

#### 3.3.1 工具集（18+ 个）

| 分类 | 工具 | 来源 | 说明 |
|------|------|------|------|
| **文件操作** | file_read | openjiuwen.harness | 读取文件 |
| | file_write | openjiuwen.harness | 写入文件 |
| | file_edit | openjiuwen.harness | 编辑文件 |
| | file_glob | openjiuwen.harness | 文件搜索 |
| | file_grep | openjiuwen.harness | 内容搜索 |
| | file_listdir | openjiuwen.harness | 目录浏览 |
| **执行** | shell | openjiuwen.harness | Shell 命令 |
| | code_execute | openjiuwen.harness | Python 执行 |
| **搜索** | web_search_free | jiuwenclaw 自定义 | 免费搜索 |
| | web_search_paid | openjiuwen.harness | 付费搜索 |
| | web_fetch | jiuwenclaw 自定义 | 网页获取 |
| **用户交互** | ask_user_question | jiuwenclaw 自定义 | 结构化追问（**新增**） |
| **任务跟踪** | todo_create | jiuwenclaw 自定义 | 创建任务计划（**新增**） |
| | todo_start | jiuwenclaw 自定义 | 标记任务开始（**新增**） |
| | todo_complete | jiuwenclaw 自定义 | 标记任务完成（**新增**） |
| | todo_insert | jiuwenclaw 自定义 | 插入新任务（**新增**） |
| | todo_list | jiuwenclaw 自定义 | 查看任务列表（**新增**） |
| **子代理** | spawn_subagent | jiuwenclaw 自定义 | 隔离上下文子任务（**新增**） |
| | fork_agent | jiuwenclaw 自定义 | 继承上下文子任务（**新增**） |

#### 3.3.2 工具注册实现

**文件：`agentserver/skilldev_agent/tools.py`**

```python
"""SkillDev Agent 的工具集注册."""

from openjiuwen.harness.tools.code import CodeTool
from openjiuwen.harness.tools.filesystem import (
    ReadFileTool, WriteFileTool, EditFileTool,
    GlobTool, GrepTool, ListDirTool,
)
from openjiuwen.harness.tools.bash import BashTool
from openjiuwen.harness.tools.web_tools import WebPaidSearchTool

from jiuwenclaw.agentserver.tools.harness_named_web_tools import (
    JiuwenHarnessFetchWebpageTool,
    JiuwenHarnessFreeSearchTool,
)
from jiuwenclaw.agentserver.tools.ask_user_question_tool import get_ask_user_question_tool
from jiuwenclaw.agentserver.tools.todo_toolkits import TodoToolkit


HARNESS_TOOL_CLASSES = [
    ReadFileTool, WriteFileTool, EditFileTool,
    GlobTool, GrepTool, ListDirTool,
    BashTool, CodeTool,
    JiuwenHarnessFreeSearchTool, WebPaidSearchTool,
    JiuwenHarnessFetchWebpageTool,
]


def build_skilldev_tools(session_id: str = None, todo_dir=None):
    """构建 SkillDev Agent 的完整工具列表.
    
    Returns:
        list[Tool]: 包含文件操作、搜索、用户交互、TODO、子代理的完整工具集
    """
    tools = []
    
    # 1. Harness 工具（文件操作 + Shell + 搜索）
    for tool_cls in HARNESS_TOOL_CLASSES:
        tools.append(tool_cls())
    
    # 2. ask_user_question
    tools.append(get_ask_user_question_tool())
    
    # 3. TODO 工具
    todo_toolkit = TodoToolkit(session_id=session_id, todo_dir=todo_dir)
    tools.extend(todo_toolkit.get_tools())
    
    # 4. 子代理工具（fork_agent / spawn_subagent）
    #    注：需要在 adapter 层通过 ForkAgentExecutor 初始化上下文
    from jiuwenclaw.agentserver.tools.subagent_tools import fork_agent, spawn_subagent
    tools.extend([fork_agent, spawn_subagent])
    
    return tools
```

#### 3.3.3 子代理工具的初始化依赖

`fork_agent` 和 `spawn_subagent` 工具依赖 `ForkAgentExecutor` 上下文变量。在现有的 `JiuWenClawDeepAdapter` 中，这些上下文变量通过 `_init_subagent_tools()` 方法设置。新适配层需要复用此机制：

```python
# adapter.py 中需要在处理请求前设置上下文变量
from jiuwenclaw.agentserver.tools.subagent_executor import (
    ForkAgentExecutor,
    set_fork_agent_executor,
    set_subagent_parent_session,
    set_current_agent_context,
    set_effective_request_workspace_dir,
)

class SkillDevDeepAdapter:
    def _init_subagent_context(self, request):
        """在处理请求前初始化子代理上下文变量."""
        executor = ForkAgentExecutor(
            parent_deep_agent=self._agent,
            workspace_dir=self._workspace_dir,
        )
        set_fork_agent_executor(executor)
        set_effective_request_workspace_dir(self._workspace_dir)
```

#### 3.3.4 Rails 配置

| Rail | 来源 | 保留原因 |
|------|------|---------|
| `ContextEngineeringRail` | openjiuwen.harness | 长对话上下文压缩管理 |
| `FileSystemRail` | openjiuwen.harness | Workspace 安全限制 |
| `SecurityRail` | openjiuwen.harness | 安全防护 |
| `HeartbeatRail` | openjiuwen.harness | 健康检查 |
| `JiuClawStreamEventRail` | jiuwenclaw 自定义 | 流式事件转换 |
| `TaskExecutionRail` | openjiuwen.harness | pause/cancel 支持 |

#### 3.3.5 适配层类骨架

```python
class SkillDevDeepAdapter:
    """专用 Skill 生成 Agent 的 DeepAgent 适配层."""

    def __init__(self, workspace_dir=None, agent_id=None, service_id=None):
        self._workspace_dir = workspace_dir or str(get_agent_workspace_dir())
        self._agent_id = agent_id
        self._service_id = service_id
        self._agent = None
        self._skill_manager = None

    def get_instance(self):
        return self._agent

    def set_skill_manager(self, manager):
        self._skill_manager = manager

    async def create_instance(self, config=None, *, mode="claw"):
        """构建 Skill 专用 DeepAgent."""
        from skilldev_agent.prompts import SKILLDEV_AGENT_SYSTEM_PROMPT
        from skilldev_agent.tools import build_skilldev_tools

        tools = build_skilldev_tools()
        rails = self._build_rails()
        
        self._agent = create_deep_agent(
            agent_card=AgentCard(name="skilldev-agent", description="专用 Skill 生成 Agent"),
            system_prompt=SKILLDEV_AGENT_SYSTEM_PROMPT.format(workspace=self._workspace_dir),
            model=self._build_model(config),
            workspace=Workspace(self._workspace_dir),
            tools=tools,
            rails=rails,
            subagents=None,
            enable_task_loop=True,
            max_iterations=200,
        )

    async def process_message_stream_impl(self, request, inputs):
        """流式处理：Agent 自主驱动 Skill 开发全流程."""
        self._init_subagent_context(request)
        async for event in Runner.run_agent_streaming(self._agent, inputs):
            yield self._convert_to_chunk(event, request)

    # ... 其他 AgentAdapter 方法 ...
```

### 3.4 第三部分：skilldev.chat 路由

#### 3.4.1 为什么不放在 `skilldev/service.py`

| 维度 | 放在旧 service.py | 放在新 skilldev_agent/ |
|------|-------------------|----------------------|
| 职责清晰度 | 混合了 Pipeline 路径和 Agent 路径 | **Agent 路径完全独立** |
| 依赖方向 | service.py 需要反向依赖 adapter | **chat_handler 直接使用 adapter** |
| 改动量 | 修改旧代码 | **新增文件，零改动旧代码** |
| 未来演进 | Pipeline 和 Agent 耦合 | **可独立替换、升级** |

#### 3.4.2 新增：`agentserver/skilldev_agent/chat_handler.py`

```python
"""SkillDev Agent Chat Handler.

处理 skilldev.chat 请求，委托给 SkillDevDeepAdapter 执行。
与旧的 SkillDevService（Pipeline）完全独立。
"""

class SkillDevChatHandler:
    """处理 skilldev.chat 请求."""
    
    def __init__(self, adapter: SkillDevDeepAdapter, workspace_dir: str):
        self._adapter = adapter
        self._workspace_dir = workspace_dir
    
    async def handle_stream(self, request: AgentRequest):
        """流式处理 skilldev.chat 请求.
        
        核心逻辑：
        1. 解析 Skill 开发特有参数（files, skill_packages, tool_spec_files）
        2. 初始化工作区目录（如首次请求）
        3. 构建 Agent inputs（query + 预加载资源信息）
        4. 委托 adapter 执行
        """
        params = request.params or {}
        
        # 解析参数
        query = params.get("message") or params.get("query", "")
        files = params.get("files", [])
        skill_packages = params.get("skill_packages", [])
        tool_spec_files = params.get("tool_spec_files", [])
        
        # 首次请求：初始化工作区
        task_id = params.get("task_id")
        if not task_id:
            task_id = self._create_task_id()
            await self._init_workspace(task_id, files, skill_packages, tool_spec_files)
        
        # 构建 inputs
        inputs = {
            "conversation_id": request.session_id or task_id,
            "query": query,
        }
        
        # 委托 adapter 执行
        async for chunk in self._adapter.process_message_stream_impl(request, inputs):
            yield chunk
```

#### 3.4.3 修改：`agentserver/interface.py`（+15 行）

在 `process_message_stream` 方法中，在现有 `_SKILLDEV_METHODS` 判断**之前**，新增 `skilldev.chat` 的路由：

```python
async def process_message_stream(self, request: AgentRequest):
    # ──── 新增：skilldev.chat 走新 Agent 路径 ────
    if request.req_method == ReqMethod.SKILLDEV_CHAT:
        handler = self._get_skilldev_chat_handler()
        try:
            async for chunk in handler.handle_stream(request):
                yield chunk
        except Exception as exc:
            logger.error("[JiuWenClaw] skilldev.chat 处理失败: %s", exc)
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "skilldev.error", "error": str(exc)},
                is_complete=True,
            )
        return
    # ──── 新增结束 ────

    # 现有代码：其他 skilldev.* 走旧 Pipeline（不变）
    if request.req_method in _SKILLDEV_METHODS:
        service = self._get_skilldev_service()
        ...
```

同时新增懒初始化方法（仅在 `JIUWENCLAW_AGENT_SDK=skilldev` 时生效）：

```python
def _get_skilldev_chat_handler(self):
    """懒初始化 SkillDev Agent Chat Handler."""
    if not hasattr(self, '_skilldev_chat_handler') or self._skilldev_chat_handler is None:
        from jiuwenclaw.agentserver.skilldev_agent.chat_handler import SkillDevChatHandler
        # 复用门面的 adapter（此时应为 SkillDevDeepAdapter）
        adapter = self._adapter
        self._skilldev_chat_handler = SkillDevChatHandler(
            adapter=adapter,
            workspace_dir=self._workspace_dir,
        )
    return self._skilldev_chat_handler
```

#### 3.4.4 修改：`schema/message.py`（+1 行）

```python
class ReqMethod(str, Enum):
    # ... 现有枚举值不变 ...
    SKILLDEV_CHAT = "skilldev.chat"  # 新增
```

> `_SKILLDEV_METHODS` 的动态匹配 `m.value.startswith("skilldev.")` 会自动捕获此枚举，但由于 `interface.py` 中 `skilldev.chat` 的判断在 `_SKILLDEV_METHODS` 之前，新路由优先生效。

### 3.5 第四部分：修改 agent_adapters.py（+12 行）

```python
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

    # ... 后续代码不变 ...


def resolve_sdk_choice():
    # ...
    valid_sdks = {"harness", "pi", "skilldev"}  # 新增 "skilldev"
    # ...
```

---

## 4. 涉及文件全表

### 4.1 新增文件

| 文件路径 | 说明 | 预估行数 |
|---------|------|---------|
| `agentserver/skilldev_agent/__init__.py` | 新适配层包入口 | ~10 |
| `agentserver/skilldev_agent/adapter.py` | SkillDevDeepAdapter 实现 | ~500-600 |
| `agentserver/skilldev_agent/prompts.py` | 完整 Skill 开发系统 Prompt | ~300 |
| `agentserver/skilldev_agent/chat_handler.py` | skilldev.chat 请求处理器 | ~150 |
| `agentserver/skilldev_agent/tools.py` | 工具集注册管理 | ~80 |

### 4.2 修改文件

| 文件路径 | 修改内容 | 行数变化 | 是否删除代码 |
|---------|---------|---------|-------------|
| `agentserver/agent_adapters.py` | 新增 `skilldev` 工厂分支 | +12 行 | **否** |
| `agentserver/interface.py` | 新增 `skilldev.chat` 路由 + 懒初始化 | +20 行 | **否** |
| `schema/message.py` | 新增 `SKILLDEV_CHAT` 枚举 | +1 行 | **否** |

### 4.3 复用的现有模块（零修改）

| 模块 | 复用方式 |
|------|---------|
| `tools/ask_user_question_tool.py` | 直接导入 `get_ask_user_question_tool()` |
| `tools/todo_toolkits.py` | 直接导入 `TodoToolkit` 并调用 `get_tools()` |
| `tools/subagent_tools.py` | 直接导入 `fork_agent`、`spawn_subagent` |
| `tools/subagent_executor/` | 直接导入 `ForkAgentExecutor` 及上下文工具 |
| `tools/harness_named_web_tools.py` | 直接导入搜索/抓取工具 |
| `deep_agent/rails/` | 复用 `JiuClawStreamEventRail` 等 |
| `skilldev/context.py` | 参考 `HARNESS_TOOL_CLASSES` 工具映射 |

### 4.4 不变文件

| 文件/目录 | 说明 |
|-----------|------|
| `agentserver/deep_agent/interface_deep.py` | 原适配层完全不动 |
| `agentserver/skilldev/` 全部 | Pipeline 及所有 Stage 完全不动 |
| `agentserver/agent_ws_server.py` | WS 入口不变 |
| `agentserver/agent_manager.py` | 实例管理不变 |
| `agentserver/team/`、`memory/`、其他 | 全部不变 |

---

## 5. 分批实施计划

### 批次 0：Prompt 与工具集（基础层）

**范围：** 创建 `skilldev_agent/` 模块，实现 Prompt 和工具注册

**详细步骤：**
1. 创建 `agentserver/skilldev_agent/` 目录和 `__init__.py`
2. 实现 `prompts.py`：从各 Stage Prompt 中提炼、整合完整的系统 Prompt
3. 实现 `tools.py`：集中管理工具注册（Harness 工具 + ask_user + TODO + subagent）
4. 编写单元测试：验证工具列表完整、无冲突

**验证：** 工具列表正确实例化，无 import 错误

**预计工作量：** 1 天

### 批次 1：SkillDevDeepAdapter（核心）

**范围：** 实现新适配层

**详细步骤：**
1. 实现 `adapter.py`：
   - 构造函数
   - `create_instance()` → 构建 DeepAgent（Prompt + 工具 + Rails）
   - `process_message_impl()` / `process_message_stream_impl()` → Agent 执行
   - `process_interrupt()` → cancel 支持
   - `handle_user_answer()` → InteractiveInput（兼容 ask_user_question）
   - `handle_heartbeat()` → 返回 None
   - `is_working()` → 状态检查
   - `get_instance()` → 返回 DeepAgent（兼容 interface.py）
   - `set_skill_manager()` → 接收 SkillManager
   - `_init_subagent_context()` → 初始化 fork/spawn 上下文
2. 修改 `agent_adapters.py`：新增 `skilldev` 工厂分支
3. 验证：`JIUWENCLAW_AGENT_SDK=skilldev` 启动，通用 chat 正常工作

**验证方法：**
```bash
export JIUWENCLAW_AGENT_SDK=skilldev
python -m jiuwenclaw.app_agentserver
# 发送通用 chat，确认 Skill 专用 Prompt 生效
# 确认工具列表包含 ask_user_question、todo_*、fork/spawn
```

**预计工作量：** 1-2 天

### 批次 2：skilldev.chat 路由与处理器

**范围：** 实现 `skilldev.chat` 完整链路

**详细步骤：**
1. `schema/message.py`：新增 `SKILLDEV_CHAT` 枚举
2. 实现 `chat_handler.py`：
   - 请求参数解析（message, files, skill_packages, tool_spec_files）
   - 工作区初始化（创建目录、写入上传文件、解压 skill 包）
   - 构建 Agent inputs
   - 委托 adapter 执行
3. 修改 `interface.py`：
   - 在 `process_message_stream` 中新增 `skilldev.chat` 路由（在 `_SKILLDEV_METHODS` 之前）
   - 新增 `_get_skilldev_chat_handler()` 懒初始化方法

**验证方法：**
```bash
# 完整 Skill 创建流程
ws.send({
    "method": "skilldev.chat",
    "params": {
        "message": "帮我创建一个搜索 arXiv 论文的 Skill",
        "files": [...]
    }
})
# 期望：Agent 自主完成澄清 → 生成 → 测试 → 打包的全流程

# 旧 API 不受影响
ws.send({"method": "skilldev.start", ...})  # 仍走旧 Pipeline
```

**预计工作量：** 1 天

### 批次 3：端到端验证与 Prompt 调优

**范围：** 实际 Skill 创建测试 + Prompt 迭代

**详细步骤：**
1. 使用 `skilldev.chat` 创建 3-5 个不同复杂度的 Skill
2. 观察 Agent 行为，记录问题：
   - Prompt 指引不够明确 → 补充指令
   - 工具使用不当 → 调整工具描述
   - 流程卡住 → 增加兜底逻辑
3. 对比新 Agent 和旧 Pipeline 的生成质量
4. 迭代 Prompt 直到稳定

**预计工作量：** 1-2 天

---

## 6. 风险与缓解

### 6.1 Agent 自主性 vs 流程确定性

**风险：** Agent 可能不严格按照预期流程执行（跳过澄清、不做测试等）。

**缓解：**
- Prompt 中明确标注"必须"步骤和"可选"步骤
- 使用 TODO 工具让 Agent 先创建计划再执行
- 通过 `ask_user_question` 在关键节点征求用户确认
- 迭代 Prompt 直到行为稳定

### 6.2 子代理工具初始化

**风险：** `fork_agent`/`spawn_subagent` 依赖 `ForkAgentExecutor` 上下文变量，初始化不正确会报错。

**缓解：**
- 参考 `interface_deep.py` 中 `_init_subagent_tools()` 的实现
- 在每次请求处理前正确设置上下文变量
- 添加工具调用前的空值检查和错误提示

### 6.3 ask_user_question 的交互通道

**风险：** `ask_user_question` 依赖 `AskUserQuestionRegistry` 和 `AgentWebSocketServer` 进行推送，需要交互通道可用。

**缓解：**
- 工具内部已有降级逻辑：非交互模式下以纯文本回退
- 确保 `chat_handler.py` 正确传播 `stream_request_id` 和 `channel_id`

### 6.4 interface.py 路由优先级

**风险：** `SKILLDEV_CHAT` 属于 `_SKILLDEV_METHODS` 集合，需确保新路由判断在前。

**缓解：** 在 `process_message_stream` 中将 `skilldev.chat` 的 `if` 判断放在 `_SKILLDEV_METHODS` 判断**之前**。同理在 `process_message` 中也做同样处理。

### 6.5 工作区初始化

**风险：** 旧 Pipeline 的工作区初始化由 `InitStageHandler` 完成（目录创建、文件解压、类型分类），新 Agent 路径需要复现此逻辑。

**缓解：** `chat_handler.py` 中实现工作区初始化逻辑。可以：
- 方案 A：直接复用 `InitStageHandler._write_resources()` 等方法
- 方案 B：在 `chat_handler.py` 中重新实现（推荐，避免与 Pipeline 耦合）

---

## 7. 改造后架构

```
用户请求
  │
  ├── method: skilldev.chat ──→ SkillDevChatHandler（新）
  │                                  │
  │                                  ├── 解析参数 + 初始化工作区
  │                                  ├── 构建 inputs
  │                                  └── 委托 SkillDevDeepAdapter
  │                                        │
  │                                        └── DeepAgent（完整 Skill Prompt + 18+ 工具）
  │                                              ├── ask_user_question → 用户交互
  │                                              ├── todo_* → 任务跟踪
  │                                              ├── spawn_subagent → 子任务委派
  │                                              ├── fork_agent → 上下文继承子任务
  │                                              ├── file_* / shell / code → 文件生成
  │                                              └── web_search / web_fetch → 信息搜索
  │
  ├── method: skilldev.start/respond ──→ SkillDevService（旧 Pipeline，不变）
  │
  └── method: 其他 ──→ JiuWenClawDeepAdapter 或 SkillDevDeepAdapter
                       （取决于 JIUWENCLAW_AGENT_SDK 环境变量）
```

---

## 8. 与旧方案的对比

| 维度 | v2.0（Agent 辅助） | v2.1（Agent 主导） |
|------|-------------------|-------------------|
| Agent 角色 | 辅助对话，Pipeline 为核心 | **Agent 是 Skill 生成的唯一引擎** |
| Prompt 复杂度 | ~50 行简单角色定义 | **~300 行完整工作流指南** |
| 工具集 | 11 个（文件 + 搜索） | **18+ 个（+ ask_user + TODO + subagent）** |
| `skilldev.chat` 位置 | 在旧 service.py 中 | **在新 skilldev_agent/chat_handler.py 中** |
| Pipeline 关系 | skilldev.chat 委托给 Pipeline | **skilldev.chat 完全独立于 Pipeline** |
| 用户交互 | 无（Pipeline 自有挂起机制） | **ask_user_question 工具** |
| 任务跟踪 | 无 | **todo_create/complete/list** |
| 子任务委派 | 无 | **spawn_subagent / fork_agent** |
| 新增文件 | 3 个 | **5 个** |
| 修改文件 | 3 个（+63 行） | **3 个（+33 行）** |
| 总工作量 | 2-3 天 | **3-5 天** |

---

## 9. 总结

本方案的核心思路是**用 Agent 取代 Pipeline 成为 Skill 生成的主体**：

1. **完整 Prompt**：将 10 个 Stage 的领域知识整合为一个统一的系统 Prompt（~300 行），涵盖 Skill 结构规范、全生命周期工作流、工具使用指南
2. **丰富工具集**：18+ 个工具覆盖文件操作、信息搜索、用户交互（ask_user_question）、任务跟踪（TODO）、子任务委派（fork/spawn）
3. **独立路由**：`skilldev.chat` 在新模块 `skilldev_agent/chat_handler.py` 中处理，完全独立于旧 Pipeline
4. **零破坏**：所有现有代码不变，旧 `skilldev.start/respond` 仍走 Pipeline
5. **环境变量切换**：`JIUWENCLAW_AGENT_SDK=skilldev` 激活新适配层

改造分 4 个批次（含 Prompt 调优），总工作量预计 3-5 天。
