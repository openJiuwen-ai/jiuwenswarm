# 可见性、skill 门控与 prefix cache

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_04 / visibility-skill-gates-and-prefix-cache |
| 关联模块 | progressive_tool_rail.py |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无 |

## 范围 / 边界

- **管**：三桶分类、`required_skill` 门控、active skill 读取、eager 稳定 schema、OfficeClaw MCP allowlist、deepresearch eager 绑定、`inherit_to_subagents=False`。
- **不管**：技能如何激活（`SkillActiveStateRail` 等见 skill 模块）、OfficeClaw 远端 MCP 协议（common S_09）。

## 三桶模型

| 桶 | 进入 model tools schema | 访问方式 |
|---|---|---|
| **eager_tools** | 是（含强制 meta-tools 0–1） | 直接 tool call |
| **skill-gated** | 仅当工具 card 声明 `required_skill` 且 **等于** 当前 active skill | 直接 tool call |
| **deferred** | 否 | `tools_search` → `invoke_tool` |

工具 card 上 `required_skill` 键名常量：`_REQUIRED_SKILL_KEY = "required_skill"`（progressive_tool_rail.py）。

## 不变量

- **N1（分类在 rail 内闭合）**：是否 eager 由配置名单 + rail 内注册顺序决定；是否 skill-gated 由 card metadata + `_current_active_skill()` 决定；其余 registered 且非 disabled 的默认 deferred（以实现分支为准，**不得**在 toolkit 重复分类）。
- **N2（meta-tools 位次）**：`tools_search`、`invoke_tool` 必须在 eager 列表位置 0 和 1（自动 insert）。
- **N3（不继承子代理）**：`ProgressiveToolRail.inherit_to_subagents = False`——子代理工具可见性由 subagent 配置与 `tool_lazy_load.subagents` 控制（[[S_08]]）。
- **N4（prefix cache 设计目标）**：eager 段稳定、deferred 不进入 schema，减少每轮 schema 抖动——改 eager 默认名单或动态注入 eager 工具会破坏 prefix cache 测试基线（`test_progressive_prefix_cache_stability.py`）。
- **N5（disabled 全路径排除）**：disabled 工具不进 schema、不进导航、不进 tools_search/invoke（与 [[S_05]] 一致；模块头注释明确列出）。
- **N6（OfficeClaw 请求域 MCP）**：progressive rail 通过 `jiuwenswarm.common.mcp_config` 绑定请求域 MCP tool id（`bind_office_claw_from_agent` / allowlist kwarg 等）；并发 live 检查 `is_office_claw_tool_name_live_concurrent`——属**可见性/可执行性**交界，非 MCP server 注册（[[S_06]]）。
- **N7（deepresearch eager 绑定）**：deepresearch 相关工具名集合 `_DEEPRESEARCH_CONTEXT_TOOLS` 等在 eager 绑上下文（`_EAGER_DEEPRESEARCH_BINDINGS_KEY`）；改工具名须同步常量与 `test_progressive_deepresearch_context.py`。

## 数据结构 / 会话

- **active skill**：rail 从 skill 活跃状态 API 读取（与 skill 模块协作）；无 active 时 skill-gated **不可见**。
- **导航列表**：prompt section 注入 deferred 工具名列表（供模型知道可 search 的目标），与 schema 分离。

## 执行路径

### P1：before_model_call 组装 schema

```
ProgressiveToolRail.before_model_call
  → 遍历 ability_manager 中 tool cards
  → 应用 disabled 过滤
  → 分 eager / skill-gated / deferred
  → 写 model tools + 导航 prompt
```

### P2：skill 切换后可见性

```
SkillActiveStateRail 更新 active skill（skill 模块）
  → 下一轮 progressive before_model_call
  → skill-gated 集合随 _current_active_skill() 变化
```

## Good / Bad

**Good**：为新业务工具设 `required_skill`，只在对应技能激活时暴露重型工具。

**Bad**：把大量不常用工具放进 `eager_tools`「方便模型」——破坏 prefix cache 与 token 预算。

## 修改检查单

- 改 `_DEEPRESEARCH_CONTEXT_TOOLS` / OfficeClaw 绑定 → 对应单测 + 不复制 S_09 正文
- 改 inherit 标志 → `rails/test_progressive_tool_inherit_flag.py`
- 改 required_skill 解析 → `test_skill_required_tool_visibility.py`

## 与其它 spec 的关系

- [[S_03]] meta-tools
- [[S_05]] disabled
- [[S_06]] MCP allowlist vs 注册
- skill 模块 S_05（active skill 来源）
