# DisabledToolsRail 与 progressive 交互

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_05 / disabled-tools |
| 关联模块 | disabled_tools_rail.py + progressive_tool_rail.py + interface_deep.py |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无 |

## 范围 / 边界

- **管**：`react.disabled_tools` 解析、DisabledToolsRail detach 语义、与 ProgressiveToolRail 的双重过滤、subagent tools 列表与 task 广告 prune、热重载 `_removed_cards`。
- **不管**：permissions 审批禁用、企业策略下发（若存在，归 permissions/enterprise 域）。

## 配置

```yaml
react:
  disabled_tools: ["bash", "write_file"]  # 或逗号分隔字符串，由 resolve_string_or_list_config 解析
```

## 不变量

- **N1（双轨过滤）**：
  1. **DisabledToolsRail**：`init` + 每轮 `before_model_call` 从当前 agent 的 `ability_manager` **detach** 黑名单工具（priority **1**，注释：低优先级数字 = 靠后执行，确保工具已注册后再 detach）。
  2. **ProgressiveToolRail**：构造/刷新时接收 `disabled_tools` 列表，exclude schema、导航、tools_search、invoke_tool 路径。
  二者必须一致；adapter 在构建 progressive 时从同一 config 读取（malformed 时 progressive 构建可 ignore 并打日志——见 interface_deep）。
- **N2（不碰全局 resource_mgr）**：detach 只影响**当前 agent**；`Runner.resource_mgr` 中工具实例保留供其他 agent 使用（模块 docstring 明确）。
- **N3（subagent 对齐）**：detach 后 prune `deep_config.subagents[*].tools` 并刷新 SubagentRail 的 task_tool「Tools: ...」广告文本——避免子代理描述仍列出已禁用工具。
- **N4（热重载可恢复）**：`_removed_cards` 缓存 detached 的 ToolCard；配置从 disabled 移除时可 re-enable（`update_disabled_tools` 类路径，见 adapter 热重载）。
- **N5（ContextAssemble 隐藏）**：interface_deep 双保险——即便 rail 时序未 detach，`deep_cfg.tool_cards` 也可先剔除黑名单（注释在 adapter 内）。
- **N6（flash 保留 rail）**：interface_flash 将 `_disabled_tools_rail` 放在 keep 白名单——flash 仍执行 disabled 语义（[[S_08]]）。

## 接口契约

| 符号 | 行为 |
|---|---|
| `DisabledToolsRail(disabled_tools: list[str] \| None)` | 空 = no-op rail 仍可能存在但不 detach |
| `init(agent)` | 记录 agent 引用，首次 detach |
| `before_model_call` | 重复 detach（捕获晚注册 MCP/Cron/扩展工具） |

## 执行路径

### P1：Agent 初始化

```
_build_disabled_tools_rail(config)
  → DisabledToolsRail(disabled_list)
  → register on DeepAgent
  → init(agent) → detach matching cards
```

### P2：与 progressive 同请求

```
_build_progressive_tool_rail(..., disabled_tools=disabled_list)
ProgressiveToolRail 内部 filter
  + DisabledToolsRail before_model_call detach
```

## Good / Bad

**Good**：禁用高危工具时同时检查 subagent 模板 tools 列表是否仍引用。

**Bad**：在 concrete tool 内 `if name in disabled: return` 代替 rail——会漏 schema 暴露与 invoke 代理路径。

## 修改检查单

- 改 priority / 调用时机 → `test_disabled_tools_rail.py` + progressive disabled filter 测试
- 改 subagent prune → task_tool 广告相关单测
- 改热重载 API → `test_progressive_rail_reload.py`

## 与其它 spec 的关系

- [[S_04]] progressive 内 disabled 排除
- [[S_08]] adapter 构建与 flash keep
- common S_13 tool ownership（全局实例 vs agent detach）
