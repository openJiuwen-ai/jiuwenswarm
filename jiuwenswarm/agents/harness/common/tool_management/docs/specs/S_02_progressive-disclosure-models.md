# 两仓渐进披露模型

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_02 / progressive-disclosure-models |
| 关联模块 | jiuwenswarm progressive_tool_rail + agent-core progressive_tool_rail |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无 |

## 范围 / 边界

- **管**：swarm fork 与 agent-core 默认 progressive 的**语义对照**、选型与混用禁忌。
- **不管**：各 concrete toolkit 是否声明 `required_skill`、工具注册来源（扩展/MCP/cron）。

## 模型对照

| 维度 | jiuwenswarm（产品 fork） | agent-core |
|---|---|---|
| Rail 类 | `jiuwenswarm...ProgressiveToolRail` | `openjiuwen.harness.rails.progressive_tool_rail.ProgressiveToolRail` |
| priority | 80 | 90 |
| inherit_to_subagents | **False**（类属性） | 以实现为准（swarm 显式 False） |
| 元工具 A | `tools_search(tool_name)` → 返回 deferred 的 **完整 input_schema** | `search_tools(query)` → 检索候选 |
| 元工具 B | `invoke_tool(tool_name, arguments)` → **代理执行** deferred | `load_tools(names)` → 更新 session **可见工具集** |
| deferred 调用方式 | 模型 schema 中**无** deferred；必须 search → invoke | load 后 deferred 可**直接**出现在后续 schema / tool call |
| meta-tools 在 eager 的位置 | 强制 `tools_search` @0、`invoke_tool` @1 | core 使用 `search_tools` / `load_tools` 命名（见 [[S_03]]） |
| 典型启用 | `react.tool_lazy_load.enabled` + adapter 装配 | DeepAgent `progressive_tool_enabled` + factory |

## 不变量

- **N1（禁止混流）**：在同一 agent 配置说明、系统 prompt、或工具描述中，**不得**假设 core 的 `load_tools` 与 swarm 的 `invoke_tool` 可互换；二者 session 状态机不同。
- **N2（fork 优先产品行为）**：jiuwenswarm 生产路径以 **tools_search → invoke_tool** 为准；阅读 core 测试（`test_progressive_tool_rail.py`）时只借鉴**不变量**（如 disabled 过滤），不借鉴工具名。
- **N3（子代理）**：swarm 主 agent progressive **不继承**子代理（[[S_04]] N3）；子代理 lazy 由 `react.tool_lazy_load.subagents` 单独门控（[[S_08]]）。

## 接口契约（行为摘要）

### Swarm 流（[[S_03]] 详述）

1. 模型从 schema 看到 eager + 当前 skill-gated + meta-tools。
2. 对 deferred：先 `tools_search` 拿 schema，再 `invoke_tool` 执行。
3. `ProgressiveToolRail` 内闭包校验：disabled、OfficeClaw allowlist、deepresearch 上下文等。

### Core 流

1. `search_tools` 返回匹配工具名/摘要。
2. `load_tools` 将选定工具加入 session visible set。
3. 后续轮次 model call 的 schema 含已 load 工具，**直接** tool call。

## 执行路径

### P1：Swarm deep chat 默认

```
interface_deep._build_progressive_tool_rail
  → ProgressiveToolRail(eager_tools, disabled_tools, ...)
  → init: 注册 ToolsSearchTool + InvokeToolTool + 绑定 _search_tools / _invoke_target_tool
```

### P2：Core DeepAgent（协作）

```
harness factory + progressive_tool_enabled
  → core ProgressiveToolRail + SearchToolsTool + LoadToolsTool
```

## Good / Bad

**Good**：在 feature 文档中明确「仅 swarm 路径」或「core 移植需重命名 meta-tools 全链路」。

**Bad**：在 swarm 代码注释里写「同 load_tools」而不加 fork 限定——会误导后续把 core 行为 port 进来。

## 修改检查单

- 改 swarm meta-tool 名 / 参数 → [[S_03]] + 全部 progressive 单测
- 对齐 core 行为 → 单独 feature 评估是否**放弃** invoke 代理模型（重大产品决策，非小修）

## 与其它 spec 的关系

- [[S_03]] 元工具字段级契约
- [[S_04]] 三桶与 prefix cache
- [[S_08]] 哪条 adapter 启用哪条模型
