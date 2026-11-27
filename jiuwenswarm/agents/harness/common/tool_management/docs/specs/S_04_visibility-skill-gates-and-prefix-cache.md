# 可见性、skill 门控与 prefix cache

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_04 / visibility-skill-gates-and-prefix-cache |
| 关联模块 | progressive_tool_rail.py |
| 最近一次修订日期 | 2026-11-26 |
| 关联 feature | 暂无 |

## 范围 / 边界

- **管**：五类可见性分类、`required_skill` 门控、active skill 读取、eager 稳定 schema、OfficeClaw MCP allowlist、deepresearch eager 绑定、`inherit_to_subagents=False`。
- **不管**：技能如何激活（`SkillActiveStateRail` 等见 skill 模块）、OfficeClaw 远端 MCP 协议（common S_09）。

## 五类可见性模型

| 类 | 进入 model tools schema | 访问方式 |
|---|---|---|
| **eager_tools** | 是（含 meta-tools；默认位于 0–1） | 直接 tool call |
| **protected（floor）** | 是：BUG20261117410859——已注册、未 disabled、**未声明** `required_skill` 的 `protected_eager_tools` 名单内工具，注册即可见（懒加载不裁剪）；排序在 eager 之后、保序 | 直接 tool call |
| **skill-gated** | 仅当工具 card 声明 `required_skill` 且 **等于** 当前 active skill；**declared 工具不走 floor**（保持门控，保证"可见 ⟺ 可直接调用"） | 直接 tool call |
| **schema-pinned** | 是：`_PINNED_SCHEMA_TOOL_NAME = "recall_compressed_context"`，已注册、未 disabled、非 skill-gated 时保留，追加在 eager + floor + active skill-gated 之后 | 直接 tool call |
| **deferred** | 否 | `tools_search` → `invoke_tool` |

工具 card 上 `required_skill` 键名常量：`_REQUIRED_SKILL_KEY = "required_skill"`（progressive_tool_rail.py）。
schema 排序 key：`eager_tools` → `protected_eager_tools`（保序）→ active skill-gated → pinned（`_order_tools_by_name_list` 只输出 key 内的名字，漏 key 会被静默丢弃；key 匹配**大小写不敏感**——CR-16：名单项大小写 ≠ 注册名时若精确匹配，会把 floor 分支已保留的工具静默丢出 schema，deferred 池同刻排除导致全失联）。
prompt `# 可用工具` 段 allowlist：`ProgressiveToolRail.prompt_tool_allowlist()`（eager + floor 的稳定集合），与 schema 稳定部分保持同步；已注册且声明 `required_skill` 的名单名同样被剔除（declared 条件可见，宣传面不得提前承诺，与 skill-gate 收窄一致）；输出对齐注册卡片规范名并去重（CR-16：名单项大小写 ≠ 注册名时取卡片规范名，宣传面不得出现 schema 中不存在的名字）。时序（CR-13）：首次装配/热更同步先于卡片注册（缓存空窗），彼时不过滤、返回未过滤名单；适配器须注册 `add_prompt_allowlist_listener`，rail 缓存填充后 allowlist 变化时回调补同步，否则过期名单驻留 ContextAssembleRail。

## 不变量

- **N1（分类在 rail 内闭合）**：是否 eager 由配置名单 + rail 内注册顺序决定；是否 floor 由 `protected_eager_tools` 名单 + card 是否声明 `required_skill` 决定（declared 工具不走 floor）；是否 skill-gated 由 card metadata + `_current_active_skill()` 决定；其余 registered、**非 disabled、非 pinned** 的默认 deferred（以实现分支为准，**不得**在 toolkit 重复分类）。
- **N2（meta-tools 位次）**：构建器保证 `tools_search`、`invoke_tool` 存在；默认/出厂配置位于 eager 位置 0 和 1。自定义 eager 列表已含二者但错位时，当前实现不强制重排。
- **N3（不继承子代理）**：`ProgressiveToolRail.inherit_to_subagents = False`——子代理工具可见性由 subagent 配置与 `tool_lazy_load.subagents` 控制（[[S_08]]）。
- **N4（prefix cache 设计目标）**：eager + floor 段稳定、deferred 不进入 schema，减少每轮 schema 抖动——改 eager / floor 默认名单或动态注入 eager 工具会破坏 prefix cache 测试基线（`test_progressive_prefix_cache_stability.py`）。floor 工具只在已注册时出现在 schema（未注册名字天然 no-op），已注册会话内不抖动。
- **N5（disabled 全路径排除）**：disabled 工具不进 schema、不进导航、不进 tools_search/invoke（与 [[S_05]] 一致；模块头注释明确列出）。
- **N6（OfficeClaw 请求域 MCP）**：progressive rail 通过 `jiuwenswarm.common.mcp_config` 绑定请求域 MCP tool id（`bind_office_claw_from_agent` / allowlist kwarg 等）；并发 live 检查 `is_office_claw_tool_name_live_concurrent`——属**可见性/可执行性**交界，非 MCP server 注册（[[S_06]]）。
- **N7（deepresearch eager 绑定）**：deepresearch 相关工具名集合 `_DEEPRESEARCH_CONTEXT_TOOLS` 等在 eager 绑上下文（`_EAGER_DEEPRESEARCH_BINDINGS_KEY`）；改工具名须同步常量与 `test_progressive_deepresearch_context.py`。
- **N8（schema-pinned 位次与排除）**：`recall_compressed_context` 的进 schema 条件以 disabled 与 skill gate 优先——被 disabled 或声明 skill gate 且当前 skill 不匹配时不 pin；pin 位次随 active skill 变化（追加在 eager + floor + active skill-gated 之后）。它不进 deferred / 导航 / `tools_search` / `invoke_tool` 代理，经代理调用会被拒绝并提示直接调用。
- **N9（floor 与 deferred 池 / 探测链路互斥）**：floor 工具（非 declared、未 disabled）不进 deferred 池、不进导航列表；`tools_search` 反查与 `invoke_tool` 代理对**已注册的** floor 名字返回"已注册且直接可见，请直接调用"指引（与 eager/pinned 同款），不得报"未找到/未注册"——对已注册且可见的工具给出错误陈述会诱导模型改写策略或反复探测。反之，**未注册的名单名**（如 web 模式未挂 pptx 套件）必须回落既有"未找到/未注册"文案，虚假的"已注册"指引会诱导模型调用不存在的工具（TOOL_NOT_AVAILABLE → 空转）。名单匹配与 deferred 池一致采用**大小写不敏感**（小写归一），反查命中卡片后以卡片规范名判断与回复；大小写策略跨路径统一——反查、invoke 代理、schema floor 分支、deferred 池排除同一小写归一（CR-14），两条代理路径对同一查询不得给出相反结论。**disabled 优先级高于 floor/declared（N5）**：反查与 invoke 代理入口的禁用复核同样大小写归一，禁用工具（含非规范大小写查询）统一回落"已禁用/未找到"文案，不得给出"直接可见"指引（CR-14）。排序 key 与 allowlist 输出同样归一对齐（CR-16）：名单项大小写 ≠ 注册名时，schema 不得静默丢出该工具、allowlist 不得输出注册面之外的名字。

## 数据结构 / 会话

- **active skill**：rail 从 skill 活跃状态 API 读取（与 skill 模块协作）；无 active 时 skill-gated **不可见**。
- **导航列表**：prompt section 注入 deferred 工具名列表（供模型知道可 search 的目标），与 schema 分离。生命周期：lazy 对当前模型不生效（`_lazy_load_active_for_ctx` 为假）或 rail `uninit` 时移除 `TOOL_NAVIGATION`，避免模型继续被引导使用已卸载的 meta-tools。

## 执行路径

### P1：before_model_call 组装 schema

```
ProgressiveToolRail.before_model_call
  → 遍历 ability_manager 中 tool cards
  → 应用 disabled 过滤
  → 分 eager / floor / skill-gated / schema-pinned / deferred
  → 写 model tools + 导航 prompt（pinned 追加在 eager + floor + active skill-gated 之后）
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
- 改 pinned 进 schema / 位次 / 导航排除 → `test_progressive_recall_tool_visibility.py`
- 改 floor 反查 / prompt allowlist 语义 → `test_progressive_protected_eager_tools.py` + `test_context_assemble_allowlist_compat.py`

## 与其它 spec 的关系

- [[S_03]] meta-tools
- [[S_05]] disabled
- [[S_06]] MCP allowlist vs 注册
- skill 模块 S_05（active skill 来源）
