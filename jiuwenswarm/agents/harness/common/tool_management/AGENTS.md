---
description: 工具管理模块说明书——渐进式工具披露（ProgressiveToolRail）、元工具（tools_search/invoke_tool）、禁用名单（DisabledToolsRail）、MCP 注册装配边界、工具调用韧性（agent-core 协作）的公开入口、模块地图、架构铁律与 spec 索引。给修改/检视本模块代码的 AI 助手与工程师。
---

# Tool Management（工具管理）

Agent **工具可见性与调用路径**的管理子系统：决定模型在每一轮看到的 tools schema 长什么样、按需工具如何被发现与间接执行、配置黑名单如何生效、以及（与 agent-core 协作）MCP 资源工具与调用重试策略如何挂接。**不包含**具体业务能力工具的实现（浏览器 / 文件系统 / shell / web / 多模态 / skill 工具集 / deepresearch 等）。

本模块**没有**单独的 `ToolManager` Python 包——实现分散在 `agents/harness/common/{rails,tools}/` 与 `server/runtime/agent_adapter/` 装配层；**本目录是 jiuwenswarm 产品侧文档归档点**。渐进式披露的原语 fork 在 jiuwenswarm（`tools_search` → `invoke_tool`）；agent-core 另有同名概念但**不同命名与流程**（`search_tools` → `load_tools`），见 [[S_02]]。

**双仓管辖（就近生效）**：

| 关切 | agent-core | jiuwenswarm（本 archive） |
|---|---|---|
| core `ProgressiveToolRail`（90）`search_tools`→`load_tools`、`tool_discovery` | **OWNS** — `openjiuwen/harness/tool_management/AGENTS.md` | 引用；勿混名 |
| `ToolCallResilienceRail`（70）、`McpRail`（95） | **OWNS** — 同上 | 引用 |
| swarm fork `ProgressiveToolRail`（80）`tools_search`→`invoke_tool`、`DisabledToolsRail` | 引用 | **OWNS** |
| adapter 装配 / `react.tool_lazy_load` / `disabled_tools` | 引用 | **OWNS** |

改 core rails / factory → 先读 **agent-core** `harness/tool_management/`；改 fork / disabled / 产品装配 → 读本目录。

本文件是模块入口索引。**修改任何代码前先读对应 spec**（见「设计文档归档与双向同步」）。

- `docs/specs/S_01_overview-and-boundaries.md` — 总览与包边界（管辖清单 / 与 concrete tools、skill、sys_operation、mcp_toolkits 的边界 / 两仓协作）
- `docs/specs/S_02_progressive-disclosure-models.md` — 两仓渐进披露模型对照（swarm 代理调用 vs core 可见集扩展）
- `docs/specs/S_03_meta-tools-contract.md` — 元工具契约（tools_search / invoke_tool；core 侧 search_tools / load_tools）
- `docs/specs/S_04_visibility-skill-gates-and-prefix-cache.md` — 三桶可见性、skill 门控、prefix cache 稳定、eager 位次、inherit_to_subagents
- `docs/specs/S_05_disabled-tools.md` — DisabledToolsRail 与 progressive 的交互、热重载、subagent 广告对齐
- `docs/specs/S_06_mcp-registration-boundary.md` — MCP 服务端注册（swarm）vs McpRail 资源工具（core）vs mcp_toolkits（邻接）
- `docs/specs/S_07_tool-call-resilience.md` — ToolCallResilienceRail 契约（core；装配与默认开关）
- `docs/specs/S_08_adapter-assembly-and-config.md` — deep / code / flash / team 装配、config 键、热重载与测试地图
- `docs/designs/tool_management_overview.md` — 特性文档（概念层：渐进披露总览 / 两模型 / 术语表 / 能力边界）

**跨模块引用（不复制正文）**：

- MCP 协议与 server 配置细节 → `jiuwenswarm/common/docs/specs/S_09_mcp-integration.md`
- 请求上下文、tool 归属与展示 → `jiuwenswarm/common/docs/specs/S_13_request-context-and-tools.md`

## 统一术语

```
tools schema（模型可见）     ProgressiveToolRail 在每轮 model call 前组装的 tool definitions
├── eager_tools              始终出现在 schema（含强制插入的 meta-tools）
├── skill-gated              required_skill 匹配当前 active skill 时直接可见
└── deferred                 注册在 AbilityManager 但不出现在 schema；经 meta-tools 间接访问

meta-tools（swarm）          tools_search（查 deferred schema）→ invoke_tool（代理执行）
meta-tools（core）           search_tools（检索）→ load_tools（写入 session 可见集）→ 直接 tool call

disabled_tools               react.disabled_tools 配置黑名单；DisabledToolsRail 从当前 agent  detach
```

| 术语 | 含义 |
|---|---|
| **ProgressiveToolRail** | swarm：`priority=80`，`inherit_to_subagents=False`；三桶 + 固定 meta-tools 位次 0–1（[[S_04]]） |
| **ToolsSearchTool / InvokeToolTool** | swarm 元工具实现（`tools_search_tool.py` / `invoke_tool_tool.py`） |
| **SearchToolsTool / LoadToolsTool** | agent-core 元工具（`openjiuwen/harness/tools/tool_discovery/`） |
| **DisabledToolsRail** | `priority=1`，在每轮前从当前 agent 的 `ability_manager` detach 黑名单工具（[[S_05]]） |
| **McpRail（core）** | `priority=95`，仅 MCP **resource** 的 list/read 工具，**不是** MCP server 注册（[[S_06]]） |
| **ToolCallResilienceRail（core）** | `priority=70`，可配置重试；**非幂等工具永不重试**（[[S_07]]） |
| **tool_lazy_load** | `react.tool_lazy_load.{enabled,eager_tools,enable_for_models,subagents.*}`（[[S_08]]） |
| **OfficeClaw MCP allowlist** | 请求域 MCP 工具 ID 与 progressive rail 绑定（OfficeClaw 集成，[[S_04]] / [[S_06]]） |
| **deepresearch bind context** | eager deepresearch 工具集的上下文绑定（progressive rail 内，[[S_04]]） |
| **prefix cache stability** | eager schema 固定、deferred 不进 schema，利于 LLM prefix caching（[[S_04]]） |
| **mcp_toolkits** | 具体 MCP 聚合工具实现（`mcp_toolkits.py`）——**邻接，非本模块管辖** |

## 公开入口（public API）

本模块**无**统一 `__init__.py` 门面——消费方通过装配层取得 rail 实例，或直接 import rail / meta-tool 类。

| 域 | 入口 | 说明 |
|---|---|---|
| Progressive rail | `ProgressiveToolRail`（`agents/harness/common/rails/progressive_tool_rail.py`） | 由 `JiuWenSwarmDeepAdapter._build_progressive_tool_rail` / `interface_code` / `interface_flash` 构建（[[S_08]]） |
| Disabled rail | `DisabledToolsRail`（`agents/harness/common/rails/disabled_tools_rail.py`） | `_build_disabled_tools_rail`；flash keep 白名单保留（[[S_08]]） |
| Meta-tools（swarm） | `ToolsSearchTool` / `InvokeToolTool` | 由 `ProgressiveToolRail` 构造并注册；回调闭包在 rail 内 |
| 配置解析 | `is_subagent_tool_lazy_load_enabled` / `_build_progressive_tool_rail` 辅助（`interface_deep.py`） | subagent 与主 agent 的 lazy 门控 |
| Core 协作 | `openjiuwen.harness.rails.progressive_tool_rail.ProgressiveToolRail`（priority 90） | DeepAgent `progressive_tool_enabled`；**勿与 swarm fork 混用语义**（[[S_02]]） |
| Core 协作 | `ToolCallResilienceRail` / factory `enable_tool_resilience_rail` | 默认 true；装配在 agent-core harness factory（[[S_07]]） |
| Core 协作 | `McpRail` | MCP resource list/read only（[[S_06]]） |
| MCP 注册（swarm） | `jiuwenswarm/common/mcp_*` + DeepAgent `cfg.mcps` → `_register_pending_mcps` | 详见 [[S_06]] 与 common `S_09` |

**新增工具管理能力**优先扩展 rail 钩子或装配层注入 config，**不要**在 concrete toolkit 文件里重复做可见性/filter 逻辑。

## 模块地图

```
agents/harness/common/tool_management/     ← 文档归档点（无运行时包代码）
└── docs/                                  # 本设计文档体系

agents/harness/common/rails/               ← 本模块管辖（2 文件）
├── progressive_tool_rail.py               # ProgressiveToolRail（80）；meta-tools 注入；OfficeClaw / deepresearch
└── disabled_tools_rail.py                 # DisabledToolsRail（1）；detach + subagent 广告刷新

agents/harness/common/tools/               ← 本模块管辖（2 文件）
├── tools_search_tool.py                   # ToolsSearchTool（tools_search）
└── invoke_tool_tool.py                    # InvokeToolTool（invoke_tool）

server/runtime/agent_adapter/              ← 装配管辖（非文档主体，契约在 [[S_08]]）
├── interface_deep.py                      # _build_progressive_tool_rail / _build_disabled_tools_rail；热重载
├── interface_code.py                      # normal / plan 模式 progressive + disabled
└── interface_flash.py                     # flash eager 名单整表替换；keep 白名单含 disabled_tools rail

jiuwenswarm/common/                        ← MCP / 归属（邻接协作）
├── mcp_config.py                          # OfficeClaw 请求域 MCP 工具绑定（progressive 消费）
├── tool_ownership.py                      # tool id qualify；见 S_13
└── docs/specs/S_09_mcp-integration.md     # MCP 集成规约（引用）

agent-core openjiuwen/harness/             ← 姊妹实现归档（改 core 读那边）
├── tool_management/AGENTS.md (+ docs/)    # core 工具管理契约 hub
├── rails/progressive_tool_rail.py         # priority 90；search_tools + load_tools 流
├── rails/mcp_rail.py                      # McpRail 95 — resource tools only
├── rails/tool_call_resilience_rail.py     # ToolCallResilienceRail 70
└── tools/tool_discovery/                  # SearchToolsTool / LoadToolsTool

相邻但不归本模块（concrete tools / 其它域）：
├── agents/harness/common/tools/*_toolkits.py   # browser / filesystem / shell / web / multimodal / skill / …
├── agents/harness/common/tools/mcp_toolkits.py # MCP 聚合工具实现 — 仅邻接引用
├── server/runtime/skill/                       # 技能管理（required_skill 门控消费 active skill，见 skill AGENTS.md）
├── agents/harness/common/rails/skill_*.py      # skill rails（活跃技能状态供 progressive 读取；非本模块）
└── sys_operation / sandbox / permissions       # 系统操作与安全域
```

## 架构铁律

以下 10 条从各 spec 不变量提炼（全量不变量以 spec 为准，条目可被 code review 单条引用）：

1. **管辖边界 = 管理，不是工具实现**（[[S_01]] N1）。浏览器、文件、shell、web、多模态、skill 市场工具、deepresearch 执行链、`mcp_toolkits` 聚合等**具体工具**不在本模块；改可见性/禁用/MCP 注册时只动 rails + 装配 + 元工具。
2. **两仓两套渐进模型，禁止混名混流**（[[S_02]] N1）。swarm：`tools_search` → `invoke_tool` 代理 deferred；core：`search_tools` → `load_tools` 扩 session 可见集后直接调用。文档、配置说明、模型 prompt 不得交叉假设对方语义。
3. **meta-tools 位次与 presence 不可破坏**（[[S_03]] N1 / [[S_04]] N2）。swarm `ProgressiveToolRail` 强制 `tools_search`、`invoke_tool` 在 `eager_tools` 位置 0–1（若缺失则 insert）；deferred 工具**不得**绕过 `invoke_tool` 直接进 schema。
4. **Progressive 不继承子代理**（[[S_04]] N3）。`inherit_to_subagents = False`——子代理工具体系独立配置；勿为"省事"改为 True 而不改 subagent 装配与测试。
5. **三桶可见性 + skill 门控单一来源**（[[S_04]] N1）。eager / skill-gated（`required_skill` vs active skill）/ deferred 分类在 rail 内完成；skill 活跃状态从 skill rails 读取，不在 toolkit 层重复判定。
6. **disabled_tools 双轨一致**（[[S_05]] N1）。`DisabledToolsRail` detach 当前 agent + progressive 构建时传入 `disabled_tools` 过滤 schema / search / invoke；`deep_config.subagents[*].tools` 与 task 广告须同步 prune（rail 负责）。
7. **全局 Runner 与 per-agent detach 分离**（[[S_05]] N2）。DisabledToolsRail **不**删 `Runner.resource_mgr` 全局注册——多 agent 共享无状态工具实例；归属与清理见 common `S_13`。
8. **MCP 注册 ≠ McpRail**（[[S_06]] N1）。swarm/common 负责 MCP **server** 注册与 pending mcps；core `McpRail` 只暴露 resource list/read；`mcp_toolkits` 是 concrete 聚合——三者勿合并文档或职责。
9. **韧性重试不碰非幂等**（[[S_07]] N1）。`ToolCallResilienceRail` 对标记非幂等的 tool **永不 retry**；改 resilience 属性或 rail 顺序前先读 core spec 与单测。
10. **prefix cache 优先于"全量 schema"**（[[S_04]] N4）。渐进式的目标是 stable eager schema + deferred 按需；勿把大批量 deferred 工具塞回 eager 破坏缓存与 token 预算——flash 的 eager 整表替换是产品特例（[[S_08]]）。

## 生命周期 / 配置消费

### 配置项（config.yaml → `react` 段，装配层读取）

| 配置 | 消费点 | 默认 / 说明 |
|---|---|---|
| `react.tool_lazy_load.enabled` | 是否装配 `ProgressiveToolRail` | 见 adapter（[[S_08]]） |
| `react.tool_lazy_load.eager_tools` | eager 桶名单（meta-tools 仍强制 0–1） | 可配置；flash 整表替换 |
| `react.tool_lazy_load.enable_for_models` | 按模型门控 lazy | 空 = 不额外限制（以实现为准） |
| `react.tool_lazy_load.subagents.*` | 子代理 lazy 开关 | 配合 `is_subagent_tool_lazy_load_enabled` |
| `react.disabled_tools` | `DisabledToolsRail` + progressive 过滤 + ContextAssemble 隐藏 | 字符串或列表 |
| `enable_tool_resilience_rail`（core factory） | `ToolCallResilienceRail` | 默认 true（[[S_07]]） |
| DeepAgent `cfg.mcps` | pending MCP 注册 | [[S_06]] + common S_09 |

### 关键协作路径

| 路径 | 用途 |
|---|---|
| `agents/harness/common/rails/progressive_tool_rail.py` | 可见性 + meta-tools + OfficeClaw / deepresearch |
| `server/runtime/agent_adapter/interface_deep.py` | deep 模式 rail 装配与热重载 |
| `jiuwenswarm/common/mcp_config.py` | OfficeClaw 请求域 MCP 工具 ID |
| agent-core `openjiuwen/harness/rails/tool_call_resilience_rail.py` | 调用重试 |

## 测试

pytest.ini 的 addopts 含全仓 coverage，日常跑请清掉（`-o addopts=""`）。

**jiuwenswarm（swarm fork）**：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit_tests/agentserver/test_code_adapter_progressive_tools.py tests/unit_tests/agentserver/test_progressive_rail_reload.py tests/unit_tests/agentserver/test_progressive_tool_rail_stale_id.py tests/unit_tests/agentserver/test_progressive_prefix_cache_stability.py tests/unit_tests/agentserver/test_progressive_deepresearch_context.py tests/unit_tests/agentserver/test_progressive_disabled_tools_filter.py tests/unit_tests/agentserver/test_skill_required_tool_visibility.py tests/unit_tests/agentserver/rails/test_progressive_tool_inherit_flag.py tests/unit_tests/agentserver/test_disabled_tools_rail.py -o addopts=""
```

**agent-core（协作面，在 agent-core 仓路径下）**：

- `test_progressive_tool_rail.py`
- `rails/test_mcp_rail.py`
- `test_tool_call_resilience_rail.py`

- **改 eager 位次 / meta-tools 契约**：跑 prefix_cache_stability + stale_id + progressive_rail_reload。
- **改 disabled 与 progressive 交集**：跑 `test_progressive_disabled_tools_filter.py` + `test_disabled_tools_rail.py`。
- **改 skill-gated 可见性**：跑 `test_skill_required_tool_visibility.py`。
- **改 inherit 行为**：跑 `rails/test_progressive_tool_inherit_flag.py`。
- **修复类改动必须附能复现原故障的测试**（先红后绿）。

## 代码风格

- 类型注解：PEP 585 内置泛型 + PEP 604 联合类型（`X | None`）。
- 注释 / docstring：progressive rail 模块头注释（三桶 + disabled 交互）是契约的一部分；清理时勿删。
- 日志禁止 `print`；meta-tool 对外 error 字段形状是行为契约（[[S_03]]）。

## 设计文档归档与双向同步（本模块强制）

本模块设计文档**全部**落在 `docs/` 下，命名与结构规约见 [`docs/AGENTS.md`](docs/AGENTS.md)。把 **`agents/harness/common/` 下本模块管辖的 4 个实现文件 + `server/runtime/agent_adapter/` 中 progressive/disabled 装配路径 + `docs/specs/` + 本 AGENTS.md** 当作一个一致性单元维护。

**前置铁律——文档先是设计的输入，才是提交的产出。** 动手改任何代码**之前**，先 grep + 读相关 `docs/specs/S_NN_*`，拿它们校准方案；方案定了**当场**同步修订对应 spec。

三条强制约束，提交时必查：

1. **特性更新归档 feature 且三连续提交**：`docs/features/` 新增 `F_NN_<slug>.md`；特性代码（`feat(tool-mgmt)`）、测试（`test(tool-mgmt)`）、文档（`docs(tool-mgmt)`）拆成三个紧邻提交、同一 PR 落地。bugfix 触及 spec 路径/不变量时归档 `docs/bugfix/B_NN_<slug>.md`，代码、测试、文档合并为**一个** `fix(tool-mgmt)` 提交。
2. **规约变动必更新 specs**：rail priority / meta-tool 参数 / 三桶规则 / disabled 交互 / 装配顺序变化时，同步修订对应 `S_NN`（元信息「最近一次修订日期」填当天）。
3. **读到不一致当场修文档**：以代码为准刷新文档；**纯计数类数字（路由条数、工具个数、LOC）不写入文档**。

## 提交约定

本模块改动的 commit message scope 固定用 **`tool-mgmt`**（如 `feat(tool-mgmt): tighten invoke_tool validation`）——含管辖 rails/tools 与 adapter 装配改动。

footer 用 `Refs: #<issue>` 格式关联 issue。issue 号若无法从当前上下文明确，必须先询问用户，不要臆造或留空。

涉及 `docs/features/F_*` / `docs/specs/S_*` 文档更新的特性改动，**特性代码、测试代码、文档拆成三个连续提交**；归档 `docs/bugfix/B_*` 的缺陷修复例外，合并为一个 `fix(tool-mgmt)` 提交。细则见 [`docs/AGENTS.md`](docs/AGENTS.md)。
