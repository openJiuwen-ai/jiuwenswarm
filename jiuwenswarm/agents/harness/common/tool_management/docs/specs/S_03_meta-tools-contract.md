# 元工具契约（meta-tools）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_03 / meta-tools-contract |
| 关联模块 | tools_search_tool.py / invoke_tool_tool.py + core tool_discovery |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无 |

## 范围 / 边界

- **管**：swarm `tools_search` / `invoke_tool` 的 ToolCard、输入 schema、调用顺序、错误形状；core 侧 `search_tools` / `load_tools` 的**对照**（不展开 core 源码）。
- **不管**：deferred 工具自身的 business logic 与 output 格式（由 concrete tool 定义）。

## Swarm：ToolsSearchTool

| 项 | 值 |
|---|---|
| TOOL_NAME | `tools_search` |
| TOOL_ID | `ToolsSearchTool` |
| 输入 | `ToolsSearchInput.tool_name: str` — 与导航列表注册名**精确匹配**（忽略大小写） |
| 输出 | 成功时含目标工具 `input_schema` 等字段；失败 `success: false` + `error` |
| 注入 | `ProgressiveToolRail` 构造时 `insert(0)` 进 eager；`search_tools` 回调由 rail 提供 |
| ID 限定 | `qualify_tool_id(card, agent_card_id|agent_id)`（common tool_ownership） |

**调用顺序不变量**：对任意 deferred 工具，**必须先** `tools_search` **再** `invoke_tool`（invoke 描述中明确声明）。

## Swarm：InvokeToolTool

| 项 | 值 |
|---|---|
| TOOL_NAME | `invoke_tool` |
| TOOL_ID | `InvokeToolTool` |
| 输入 | `InvokeToolInput`: `tool_name`, `arguments: dict` |
| 输入约束 | `tool_name` 须与 tools_search / 导航一致；`arguments` 须符合 search 返回的 schema |
| Card 属性 | `properties.resilience.timeout_s: None`（与 resilience rail 交界，见 [[S_07]]） |
| 执行 | rail 内 `_invoke_target_tool(session, parsed)` — 代理 Runner 调用真实 tool |

## Core（协作对照）

| TOOL_NAME（典型） | 职责 |
|---|---|
| `search_tools` | 查询/发现工具（query 驱动） |
| `load_tools` | 按名称加载到 session 可见集 |

**不得**在 swarm 实现中把 `load_tools` 别名成 `invoke_tool` 而不改 session 可见集语义。

## 不变量

- **N1（双 meta-tool 常驻 eager）**：`ProgressiveToolRail.__init__` 若 eager 缺 `tools_search` / `invoke_tool` 则 insert 0/1——禁止仅改配置删除二者而不改 rail 代码。
- **N2（disabled 不可搜不可调）**：disabled 工具不得出现在 tools_search 结果或 invoke 目标（与 [[S_05]]、progressive 内 filter 一致）。
- **N3（错误不抛到模型外）**：ToolsSearchTool.invoke 捕获异常 → `{success: false, error, matches: []}` 形状；避免 uncaught 打断 agent 循环（具体字段以代码为准，**形状稳定**为契约）。
- **N4（工具描述即契约）**：ToolCard.description 中文说明 search→invoke 顺序；改文案需评估模型行为回归。

## 执行路径

### P1：tools_search

```
模型 call tools_search(tool_name)
  → ToolsSearchTool.invoke
  → ProgressiveToolRail._search_tools(session, ToolsSearchInput)
  → 校验 deferred + 非 disabled + allowlist
  → 返回 schema
```

### P2：invoke_tool

```
模型 call invoke_tool(tool_name, arguments)
  → InvokeToolTool.invoke
  → ProgressiveToolRail._invoke_target_tool(...)
  → 真实 tool invoke + JSON-safe 结果包装（rail 内 _json_safe_value）
```

## 修改检查单

- 改 Input pydantic 字段 → 更新 ToolCard input_params + 单测 + 模型 prompt（如有）
- 改 invoke 代理路径（session/kwargs 传递）→ `test_progressive_tool_rail_stale_id` + deepresearch context 测试
- 改 tool id 限定规则 → 对齐 common S_13

## 与其它 spec 的关系

- [[S_02]] 两模型
- [[S_04]] eager 位次
- [[S_05]] disabled 过滤
- [[S_07]] resilience 属性
