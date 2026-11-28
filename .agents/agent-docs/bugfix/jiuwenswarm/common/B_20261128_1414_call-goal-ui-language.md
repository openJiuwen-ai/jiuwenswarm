---
id: B_20261128_1414_call-goal-ui-language
title: call_goal 副标题语言与 UI preferred_language 不一致
type: B
author: wangxin
author_email: wangxin375@huawei.com
designs: []
design_note: 在既有 call_goal schema 注入与 preferred_language 请求契约上的局部缺陷修复，不新增架构方案。
modules:
  - jiuwenswarm/common
  - jiuwenswarm/agents/harness/common/rails
  - jiuwenswarm/enterprise/config
files:
  - jiuwenswarm/common/tool_display.py
  - jiuwenswarm/agents/harness/common/rails/stream_event_rail.py
  - tests/unit_tests/common/test_tool_display_call_goal.py
  - tests/unit_tests/enterprise/test_preferred_language_memo.py
---

# call_goal 副标题语言与 UI preferred_language 不一致修复记录

## 现象与影响

用户面已切到英文模式时，工具树标题壳子（`Completed:` / `Search ·` / `View skill` /
`Find`）跟前端 i18n 正确出英文，但同行副标题 `call_goal` 仍常出现中文，例如：

- `Completed: Search · Tool search 查找 mcp_exec_command 工具以执行历史检索脚本`
- `Completed: Search · Find 定位 search_history.py 脚本的实际安装路径`

而同轮 `skill_tool` 的 `call_goal` 又可能是英文
（`Read the cross-channel-history-retrieval skill instructions`），同一会话内中英混杂。

影响：英文 UI 下工具步骤可读性差，看起来像「半套未翻完」；语言开关无法约束工具目标文案。

## 关联设计与当前契约

无独立设计。相关既有契约：

- 前端标题：`chatUi.toolGroup.completed` + `chatUi.toolDisplay.title`（category · action），
  跟 UI locale；`call_goal` 单独作副标题渲染（`ToolGroupDisplay`）。
- 后端：`StreamEventRail` 给送入 LLM 的 ToolInfo 注入可选 `call_goal`，执行前
  `extract_call_goal` 剥离；语言解析走 `resolve_preferred_language()`
  （企业版 memo/`ui_language` 优先，个人版读 yaml；见
  `B_20261126_1045_ui-lang-over-yaml-zh`）。

## 根因与修复决策

**一句话**：注入给模型的 `call_goal` schema 描述要求「跟当前对话语言」，而 skill
正文等多是中文，模型于是在英文 UI 下仍产出中文 goal；前端只 i18n 了标题壳，不翻译
模型自由文本。

根因拆分：

1. `_CALL_GOAL_SCHEMA` 双语说明绑定「对话语言 / conversation language」，单测还锁死了
   这一语义，与 UI `preferred_language` 脱钩。
2. 中文 `SKILL.md` 等上下文会进一步把后续 `call_goal` 拉向中文。
3. 标题 `Completed: Search · Tool search` 来自前端 registry/i18n；`Tool search` 因
   `tool_search` 未注册动作键而 humanize 工具名，与本次语言绑定无关。

修复决策：

1. **按 UI 语言注入单语 schema**：`call_goal_schema_for_language(language)` 产出 zh/en
   两套 description；`inject_call_goal_schema(..., language=)` 写入当前
   `resolve_preferred_language()` 结果（缺省 `en`）。
2. **强调填写且匹配界面语言**：文案要求 Always fill / 每次必须填写，并写明跟 UI
   语言；避免过硬的 “MUST / Do not use Chinese” 导致模型在中文 skill 上下文下
   **宁可不填**可选字段（曾在 wx 复现：`skill_tool` 有英文 goal，`tool_search`/`glob`
   的 `call_goal` 为空）。
3. **marker 识别“我们注入的”schema**：`is_our_call_goal_schema` 用
   「仅界面展示」/`UI display only` 等标记区分工具自带 `call_goal`；仅对我们的注入
   按语言刷新，不覆盖业务定义。`StreamEventRail` 在已有非我方 `call_goal` 时跳过。
4. **个人版仍只读 yaml**：讨论中一度让个人版也吃 memo，但个人版设置会
   `locale.set_conf` 写回 yaml，设计上以 yaml 为准；最终不改
   `resolve_preferred_language` 个人版分支，仅补 UT 锁定「个人版忽略 memo」。

备选与拒绝：

- **前端按语种启发式隐藏不匹配的 call_goal**：可作兜底，但不解决模型产出语言；
  本次以 schema 绑定为主。
- **结构化参数 i18n 模板替代自由文本**：最稳但覆盖面大，超出本缺陷范围。
- **事后机器翻译 call_goal**：贵、慢、质量不稳，不采用。

## 回归证据与适用范围

- UT：`tests/unit_tests/common/test_tool_display_call_goal.py`（schema 中/英、刷新、
  不覆盖工具自带字段、extract）；`tests/unit_tests/enterprise/test_preferred_language_memo.py`
  （个人版忽略 memo 锁定）。
- wx 命名空间：hostPath 挂载本仓后重启 agentserver；历史
  `history.jsonl` 可核对 `tool_call.call_goal` 是否随 UI 语言产出；过硬英文约束时
  曾出现后续工具 goal 缺失，放宽「必须填写 + 匹配 UI 语言」后需再验英文会话。
- 适用范围：`call_goal` 注入路径；不保证模型 100% 遵守语言（可选字段）。个人版语言
  仍以 yaml 为准；企业版跟请求 memo / `ui_language`。
- 未纳入本次：为 `tool_search` 补前端动作 i18n 注册（仍显示 humanize 的 `Tool search`）。

## 防错模式与资料同步

- 新增/改写 `call_goal` schema 文案时：语言跟 `resolve_preferred_language()`，不要
  写「跟对话语言」；保留可识别的 UI-only marker，以便刷新与清理。
- 调整语言约束强度时：同步验证模型是否仍会填写该可选字段（避免「宁可不填」）。
- 个人版 vs 企业版语言源不同，调用点不要假设 memo 在个人版生效。
