# Adapter 装配、配置与热重载

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_08 / adapter-assembly-and-config |
| 关联模块 | interface_deep.py / interface_code.py / interface_flash.py |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无 |

## 范围 / 边界

- **管**：各 adapter 如何构建 progressive/disabled rails、config 键、subagent lazy 门控、热重载、flash 特例、team/code 路径差异（以 deep 为基准描述）。
- **不管**：AgentManager 选型逻辑全貌（见 server runtime specs）。

## 配置键（react 段）

| 键 | 用途 |
|---|---|
| `tool_lazy_load.enabled` | 是否创建 ProgressiveToolRail |
| `tool_lazy_load.eager_tools` | eager 桶（meta-tools 仍强制 0–1） |
| `tool_lazy_load.enable_for_models` | 模型 allowlist 门控 |
| `tool_lazy_load.subagents.enabled` 等 | 子代理 lazy（`is_subagent_tool_lazy_load_enabled`） |
| `disabled_tools` | DisabledToolsRail + progressive 种子列表 |

解析辅助：`resolve_string_or_list_config`（disabled_tools）；malformed disabled 时 progressive 构建可忽略并 log（interface_deep）。

## 装配入口

| Adapter | Progressive | Disabled | 备注 |
|---|---|---|---|
| **interface_deep** | `_build_progressive_tool_rail(config)` | `_build_disabled_tools_rail(config)` | 主产品路径；热重载 update rail |
| **interface_code** | 同族构建（normal/plan 模式） | 同族 | `test_code_adapter_progressive_tools.py` |
| **interface_flash** | override `_build_progressive_tool_rail`：合并 flash eager 整表 | keep 白名单含 `_disabled_tools_rail` | `tool_lazy_load` 深合并对 list 为**替换** |
| **team** | 消费 deep/code 链或 team helper | 继承父 agent disabled 语义（adapter 注释：parent + inherited 双保险） | 改 team 时查 interface team_helpers |

## 不变量

- **N1（单一构建函数）**：progressive 参数（agent_id、agent_card_id、enable_for_models、disabled_tools、OfficeClaw kwarg）在 `_build_progressive_tool_rail` 集中注入——勿在第二处重复解析 config。
- **N2（热重载原位更新）**：优先 `update_*` / `update_disabled_tools` 更新已挂载 rail，避免重建导致脱离注册链（与 skill 模块热重载铁律同型）。
- **N3（flash eager 替换语义）**：flash 配置对 `eager_tools` 是**整表替换**而非 append——产品 intentional；改 flash 默认须跑 flash 相关测试。
- **N4（AGENT_RELOAD_CONFIG）**：ops 热重载路径须同时刷新 progressive + disabled（`test_progressive_rail_reload.py`）。
- **N5（日志契约）**：构建 progressive 时 log agent_id / enable_for_models / disabled_tools（字段名稳定，便于排障）。

## 执行路径

### P1：Deep agent 首次创建

```
JiuWenSwarmDeepAdapter.create / rebuild harness
  → read react from config cache
  → disabled_tools_rail = _build_disabled_tools_rail
  → progressive_tool_rail = _build_progressive_tool_rail (if enabled)
  → register rails on DeepAgent
```

### P2：Config 热重载

```
AGENT_RELOAD_CONFIG / sync config
  → update progressive eager/disabled/office_claw bindings
  → update DisabledToolsRail disabled set + restore removed cards
```

## 测试地图（swarm）

| 文件 | 覆盖 |
|---|---|
| `test_code_adapter_progressive_tools.py` | code adapter progressive |
| `test_progressive_rail_reload.py` | 热重载 |
| `test_progressive_tool_rail_stale_id.py` | tool id / invoke 代理 |
| `test_progressive_prefix_cache_stability.py` | prefix cache |
| `test_progressive_deepresearch_context.py` | deepresearch 绑定 |
| `test_progressive_disabled_tools_filter.py` | disabled ∩ progressive |
| `test_skill_required_tool_visibility.py` | required_skill |
| `rails/test_progressive_tool_inherit_flag.py` | inherit_to_subagents |
| `test_disabled_tools_rail.py` | DisabledToolsRail |

## 修改检查单

- 新 adapter 形态（如 future interface_*）→ 必须显式决定 progressive/disabled 是否装配
- 改 config 键名 → 迁移文档 + 全链 grep + 单测
- 改 rail 注册顺序 → 对照 priority（disabled 1, resilience 70, progressive 80, mcp 95）

## 与其它 spec 的关系

- [[S_01]] 边界
- [[S_04]] [[S_05]] 行为来源
- server runtime agent_adapter（server 模块文档）
