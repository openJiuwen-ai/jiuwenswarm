# 工具管理（Tool Management）特性文档

## 概述

JiuwenSwarm Agent 往往注册**大量**工具（内置 toolkit、扩展、MCP、Cron、技能工具等）。若每轮都把完整 tools schema 发给模型，会消耗 context、破坏 LLM **prefix cache**，并让模型选择困难。**工具管理模块**负责「**谁出现在 schema 里、谁只能按需间接调用、谁被配置禁用**」，而不实现具体业务工具本身。

实现分散在 harness rails（swarm fork 的 `ProgressiveToolRail`、`DisabledToolsRail`）、元工具（`tools_search` / `invoke_tool`）与 `server/runtime/agent_adapter` 装配层；**文档归档**在 `agents/harness/common/tool_management/`。agent-core 仓提供平行的 progressive 模型（`search_tools` / `load_tools`）以及 `McpRail`、`ToolCallResilienceRail`——产品与 core 的行为对照见 `docs/specs/S_02`。

改代码前请先读 `docs/specs/` 对应规约。

---

## 核心概念

### 渐进披露总览（swarm 产品路径）

```
注册（Runner / 扩展 / MCP / 技能工具等）
  → ProgressiveToolRail 分桶
       eager（含 meta-tools @0–1）→ 直接 call
       skill-gated（required_skill == active skill）→ 直接 call
       deferred → 不出现在 schema
  → 模型对 deferred：tools_search 拿 schema → invoke_tool 代理执行
  → DisabledToolsRail：react.disabled_tools 从当前 agent detach（全局 resource_mgr 不动）
```

### 两模型对照（必读）

| | jiuwenswarm | agent-core |
|---|---|---|
| 发现 | `tools_search(name)` | `search_tools(query)` |
| 加载/执行 | `invoke_tool(name, args)` 代理 | `load_tools(names)` 扩可见集后直接 call |

**不要混用术语**——fork 是有意的产品差异（stable schema + 代理调用 vs session 可见集扩展）。

### Prefix cache 与 eager 稳定

渐进式的关键收益是 **eager 段固定**：常用工具 + 两个 meta-tools 始终在固定位置，deferred 仅通过导航列表提示存在性。把 rarely-used 工具塞进 `eager_tools` 会损害缓存命中与测试基线（见 S_04）。

### Skill 门控（与 skill 模块交界）

工具 card 可声明 `required_skill`。仅当会话 **active skill** 匹配时，该工具进入「直接可见」桶——重型技能工具默认不在全量 eager 中。技能如何激活、账本如何管理，归 **skill 模块**；tool-mgmt 只消费 active skill 状态。

### MCP 三条线（摘要）

1. **Server 注册**（配置、pending mcps、RPC）→ common **S_09**
2. **McpRail**（core）→ 仅 MCP **resource** list/read
3. **mcp_toolkits** → 具体 MCP 工具实现（**非** tool-mgmt）

Progressive rail 另含 **OfficeClaw 请求域** MCP tool allowlist 绑定（见 S_04 / S_06）。

### 调用韧性（core）

`ToolCallResilienceRail` 在 tool call 管线提供可配置重试；**非幂等工具永不重试**。与渐进披露正交，但 `invoke_tool` 代理路径同样受影响（见 S_07）。

---

## 能力边界

| 在本模块 | 不在本模块 |
|---|---|
| 三桶可见性、meta-tools、disabled detach | browser/filesystem/shell/web/multimodal 工具实现 |
| adapter 装配与热重载 | skill 安装/账本/授权 |
| 与 core progressive/McpRail/resilience 的协作描述 | mcp_toolkits 聚合实现 |
| OfficeClaw allowlist 与 progressive 交界 | MCP 传输与 server 全生命周期（S_09） |

---

## 术语表（精简）

- **eager / deferred / skill-gated**：三桶可见性分类
- **meta-tools**：swarm 的 tools_search + invoke_tool
- **disabled_tools**：配置级黑名单
- **inherit_to_subagents=False**：主 agent progressive 不下发给子代理

---

## 进一步阅读

- 模块入口：`../AGENTS.md`
- Specs：`../specs/S_01` … `S_08`
- 关联：skill 模块 `server/runtime/skill/AGENTS.md`（active skill）
- 关联：common `S_09_mcp-integration.md`、`S_13_request-context-and-tools.md`
