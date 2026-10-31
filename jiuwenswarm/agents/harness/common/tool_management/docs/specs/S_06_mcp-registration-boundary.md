# MCP 注册边界

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_06 / mcp-registration-boundary |
| 关联模块 | jiuwenswarm/common/mcp_* + agent-core mcp_rail + mcp_toolkits（邻接） |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本 spec 管什么

- **tool-mgmt 视角**下三条线的分工：
  1. **MCP server 注册与 pending mcps**（swarm / DeepAgent 装配）
  2. **McpRail 资源工具**（agent-core，仅 list/read resource）
  3. **mcp_toolkits**（具体 MCP 聚合工具实现——**OUT OF SCOPE**，仅邻接）

### 不管什么

- MCP JSON 配置 schema、stdio/SSE 传输、Gateway handler——见 **`jiuwenswarm/common/docs/specs/S_09_mcp-integration.md`**（引用，不复制）。

## 分工表

| 能力 | 位置 | tool-mgmt 关系 |
|---|---|---|
| Server add/remove/update/list（RPC） | server handlers + common mcp 运行时 | 注册结果**影响** ability_manager 工具集；规约在 S_09 |
| DeepAgent `cfg.mcps` → `_register_pending_mcps` | agent-core DeepAgent + swarm adapter | 装配时挂接；属 **注册 plumbing** |
| **McpRail**（priority 95） | openjiuwen/harness/rails/mcp_rail.py | 只暴露 MCP **resource** 的 list/read 工具，**不是** server 生命周期 |
| OfficeClaw 请求域 allowlist | common/mcp_config + [[S_04]] progressive | 请求 scoped tool id 绑定与 live 检查 |
| **mcp_toolkits.py** | agents/harness/common/tools/ |  concrete 工具聚合；**非** tool-mgmt 管辖 |

## 不变量

- **N1（McpRail ≠ 注册）**：文档与 code review 不得把 `McpRail` 描述为「MCP 服务器注册 rail」。
- **N2（晚注册工具由 DisabledToolsRail 再 detach）**：MCP 运行时注册的工具可能在 progressive init 之后出现——DisabledToolsRail 每轮 before_model_call 重复 enforce（[[S_05]] N1）。
- **N3（progressive + MCP 交界）**：OfficeClaw 相关 tool 名/id 在 progressive rail 内做 allowlist/bind；细节不重复 S_09，只记录**依赖方向**：progressive 消费 mcp_config，不实现 server 握手。
- **N4（mcp_toolkits 仅邻接）**：改 MCP 聚合工具行为时，不归档到 tool-mgmt specs，除非改变了**全局可见性/禁用/渐进规则**。

## 执行路径

### P1：Agent 启动 pending MCP

```
DeepAgent 创建 + cfg.mcps
  → _register_pending_mcps（core/swarm 交界）
  → 工具进入 Runner / ability_manager
  → DisabledToolsRail + ProgressiveToolRail 后续轮次生效
```

### P2：McpRail 资源工具

```
McpRail 注册 list/read resource 工具（与 business MCP tools 并存）
  → 不参与 tools_search deferred 分类逻辑 unless 注册为普通 tool card
```

## 修改检查单

- 动 MCP server 生命周期 → 改 common **S_09** + server handlers，本模块只更新「交界」一节若 progressive/disabled 行为变
- 动 McpRail → agent-core 测试 `rails/test_mcp_rail.py`
- 动 mcp_toolkits → **不**改 tool-mgmt S_06 正文，除非边界表需新增邻接项

## 与其它 spec 的关系

- common **S_09** MCP 集成（单一真相源）
- [[S_04]] OfficeClaw allowlist
- [[S_05]] 晚注册 detach
- [[S_08]] adapter 装配 mcps
