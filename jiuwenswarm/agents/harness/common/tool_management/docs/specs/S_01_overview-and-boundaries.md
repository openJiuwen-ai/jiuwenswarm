# 工具管理模块总览与包边界

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_01 / overview-and-boundaries |
| 关联模块 | jiuwenswarm/agents/harness/common/{rails,tools}/（progressive + meta-tools + disabled）+ server/runtime/agent_adapter/ 装配 |
| 最近一次修订日期 | 2026-10-26 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

### 本规约管什么

**工具管理（tool management）** 覆盖 Agent 运行时对工具的**注册可见性、按需披露、配置禁用、以及与 agent-core 协作的 MCP 资源工具 / 调用韧性挂接**：

- **swarm ProgressiveToolRail**（priority 80）：eager / skill-gated / deferred 三桶；meta-tools `tools_search` + `invoke_tool`
- **swarm DisabledToolsRail**（priority 1）：`react.disabled_tools` 黑名单 detach
- **swarm 元工具**：`ToolsSearchTool` / `InvokeToolTool`
- **装配层**：`interface_deep` / `interface_code` / `interface_flash` 中 `_build_progressive_tool_rail`、`_build_disabled_tools_rail`、热重载
- **姊妹仓实现契约**：agent-core `openjiuwen/harness/tool_management/`（`ProgressiveToolRail` 90 / `McpRail` 95 / `ToolCallResilienceRail` 70）；本仓只锁 fork 与产品装配，不复制 core 规约全文

文档归档点：`agents/harness/common/tool_management/docs/`（**无**独立 ToolManager 包）。

### 本规约**不**管什么

| 不归本模块 | 归属 / 说明 |
|---|---|
| 浏览器 / 文件系统 / shell / web / 多模态等 **concrete toolkits** | `agents/harness/common/tools/*_toolkits.py` 及各域 AGENTS |
| **`mcp_toolkits.py`**（MCP 聚合的具体工具） | 邻接：消费 MCP 注册结果，但实现不在 tool-mgmt |
| **Skill 生命周期、SKILL.md、市场安装** | `server/runtime/skill/`（progressive 仅**消费** active skill 做 `required_skill` 门控，见 [[S_04]]） |
| **sys_operation、sandbox、permissions 裁决** | 各自 harness / server 域 |
| **MCP 协议细节、transport、server 生命周期** | `jiuwenswarm/common/docs/specs/S_09_mcp-integration.md` |
| **tool_ownership、请求域展示** | `jiuwenswarm/common/docs/specs/S_13_request-context-and-tools.md` |
| **Core 侧 skill_tool / list_skill / SkillUseRail** | agent-core harness/skills |

## 模块地图（管辖清单）

```
agents/harness/common/tool_management/     # 文档归档（本体系）
agents/harness/common/rails/
  progressive_tool_rail.py                 # ProgressiveToolRail
  disabled_tools_rail.py                   # DisabledToolsRail
agents/harness/common/tools/
  tools_search_tool.py                     # tools_search
  invoke_tool_tool.py                      # invoke_tool
server/runtime/agent_adapter/
  interface_deep.py                        # 主装配 + 热重载
  interface_code.py                        # code/plan 模式
  interface_flash.py                       # flash eager 覆盖 + keep 白名单
```

## 不变量

- **N1（管理 ≠ 实现）**：本模块规约只约束**如何让工具对模型可见/可调用**；不得在 concrete toolkit 内复制 progressive 三桶或 disabled 过滤逻辑（应回调 rail / 装配层）。
- **N2（文档归档在 harness，代码无单包）**：与 skill 模块「server/runtime/skill 主体 + harness 文件清单」不同，tool-mgmt **仅有** harness 侧实现文件 + adapter 装配；归档目录即 `tool_management/`。
- **N3（双仓归档、就近生效）**：agent-core 渐进式 / resilience / McpRail 实现契约在 `harness/tool_management/docs/`；本体系 specs 只写 **fork 差异 + 产品装配 + DisabledTools**，对照表引用 core、禁止复制 core 规约全文。
- **N4（引用 common specs）**：MCP 与 request-context 细节以 `S_09` / `S_13` 为单一真相源，本模块 spec 只写交界行为（[[S_06]]）。

## 接口契约（索引）

| 符号 | 位置 | spec |
|---|---|---|
| `ProgressiveToolRail` | rails/progressive_tool_rail.py | [[S_02]] [[S_03]] [[S_04]] |
| `DisabledToolsRail` | rails/disabled_tools_rail.py | [[S_05]] |
| `_build_progressive_tool_rail` | interface_deep.py 等 | [[S_08]] |
| Core `ProgressiveToolRail` / `McpRail` / `ToolCallResilienceRail` | openjiuwen/harness/rails/ | [[S_02]] [[S_06]] [[S_07]] |

## 执行路径全景

### P1：Deep 模式 Agent 创建时工具 rail 装配

```
JiuWenSwarmDeepAdapter 构建 deep agent
  → _build_disabled_tools_rail(react.disabled_tools)   # 可选
  → _build_progressive_tool_rail(react.tool_lazy_load) # enabled 时
  → rails 注册到 DeepAgent（顺序见 priority）
```

### P2：每轮 model call 前工具集快照

```
DisabledToolsRail.before_model_call（priority 1，靠后执行 detach）
ProgressiveToolRail.before_model_call（priority 80，组装 schema + 导航）
  → eager + skill-gated 进 schema
  → deferred 仅经 tools_search / invoke_tool
```

## 修改检查单

| 改动 | 必须连带 |
|---|---|
| 新增「按需」工具默认行为 | 确认是否 deferred；meta-tool 路径是否可达 [[S_03]] |
| 动 progressive priority / inherit | `test_progressive_tool_inherit_flag` + subagent 装配 [[S_08]] |
| 动 disabled 语义 | progressive 过滤 + DisabledToolsRail + subagent 广告 [[S_05]] |
| 文档写 MCP | 只改交界，细节链到 common S_09 [[S_06]] |

## 与其它 spec 的关系

- [[S_02]] 两仓渐进模型
- [[S_03]] meta-tools
- [[S_04]] 可见性 / skill 门控 / prefix cache
- [[S_05]] disabled
- [[S_06]] MCP 边界
- [[S_07]] resilience
- [[S_08]] 装配与 config
