---
id: B_20261116_2135_subagent-cost-dropped
title: 会话用量账本丢弃子代理与浏览器代理的计费金额：记录 provider total_cost
type: B
author: MisakaMikasa
author_email: sharlockqiao@gmail.com
designs: []
design_note: 依据实测会话数据与代码路径定位，无独立设计文档；修复范围限定会话用量账本记录与快照，不改变 chat.usage_summary 现有字段语义。
modules:
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/agents/harness/common/rails
files:
  - jiuwenswarm/server/runtime/session/session_token_usage.py
  - jiuwenswarm/agents/harness/common/rails/session_token_usage_rail.py
  - tests/unit_tests/agentserver/test_session_token_usage.py
  - tests/unit_tests/agentserver/test_stream_event_rail_todo.py
  - tests/unit_tests/agentserver/test_front_phase1.py
---

# 会话用量账本丢弃子代理与浏览器代理的计费金额修复记录

## 现象与影响

agent 模式开启子代理（`react.subagent_runtime.enabled: true`）后，一轮对话结束时
`chat.usage_summary.usage.total_cost` 只包含主代理的模型调用费用；子代理（`subagent_spawn`）
与浏览器代理（`task_tool` → `browser_agent`）的调用虽然计入 `session_usage.others` 的 token 数，
但其 provider 返回的计费金额被丢弃，任何接口都拿不到。

实测（WorkSwarm 0.2.8，模型 `openai/gpt-6-luna` 经 OpenRouter，主代理派生 4 个子代理的一轮任务）：

| 来源 | 金额 |
|---|---|
| `usage_summary.usage.total_cost`（主代理） | $0.0247 |
| OpenRouter 实际计费：主代理 36 次调用 | $0.0247 |
| OpenRouter 实际计费：子代理 624 次调用 | $0.7270 |

界面与接口显示的费用约为实际的 1/30。账本中 `others` 有 624 次调用、千万级 input token，但没有金额。

## 关联设计与当前契约

无独立设计文档。当前契约：

- 模型客户端 `openjiuwen.core.foundation.llm.model_clients.base_model_client._extract_cost_info`
  把 provider 返回的 `usage.cost`（OpenRouter）写入 `UsageMetadata.total_cost`；provider 未返回费用时为 `0.0`。
- `chat.usage_summary.usage.*_cost` 由 `interface_deep._process_message_stream_impl` 累加主流
  `llm_usage` chunk 得到，只覆盖主代理。本次不改变其语义。
- `session_usage` 快照（`SessionTokenUsageLedger.snapshot`）是会话累计用量，按 `main` / `others`
  分类，随 `usage_summary`、`history.message` 下发。

## 根因与修复决策

根因：所有模型调用（主代理经 `stream_event_rail`、子代理/浏览器代理经 `SessionTokenUsageRail`）都会进入
`record_model_token_usage`，此处 `usage` 已包含 `total_cost`，但只把 `input_tokens` / `output_tokens`
传给 `record_session_token_usage`，账本表 `usage_events` 也没有费用列，子代理费用在此丢失。

修复：

1. `record_model_token_usage` 透传 `usage["total_cost"]`。
2. `usage_events` 新增可空列 `total_cost REAL`；旧账本在下次写入时 `ALTER TABLE ADD COLUMN`，旧行保持 NULL。
3. 费用 `<= 0`、非数值、NaN/inf 记为 NULL（未知），不记为 0：模型客户端在 provider 未返回费用时填 `0.0`，
   无法与"免费"区分。
4. 快照每个分类新增 `priced_calls`（有费用的调用数）与 `total_cost`（6 位小数，无费用时为 `null`）。
   `priced_calls < calls_total` 表示金额不完整（修复前写入或 provider 未计价）。
   读取尚未升级的旧账本时不访问缺失列。`schema_version` 保持 1：只新增字段，前端解析只校验既有字段。

备选与取舍：

- 直接让 `usage_summary.usage.total_cost` 包含子代理费用：会改变既有字段语义，且需按轮次计算账本差值，
  与进行中的费用展示工作（#2481 / #2482）可能冲突，留作后续讨论。
- 同时记录 `input_cost` / `output_cost` / `cache_tokens`：OpenRouter 并非总返回拆分，本次只记录最小必要的计费总额。
- 仅在有费用时才在快照中出现费用字段：快照形状随数据变化，消费方更难处理；改为固定字段并更新精确比较的测试。

## 回归证据与适用范围

环境：Windows 11，Python 3.12，`develop` @ a239f43 + 本修复。

- 复现（修复前，`develop`）：子代理调用 `usage_metadata.total_cost = 0.25` 经 `record_model_token_usage` 落账后，
  快照 `others` 只有 token：`{'input_tokens': 1000, 'output_tokens': 50, 'input_calls': 1, 'output_calls': 1, 'calls_total': 1}`。
- 修复后同一复现：`{..., 'calls_total': 1, 'priced_calls': 1, 'total_cost': 0.25}`。
- `tests/unit_tests/agentserver/test_session_token_usage.py`：26 passed（新增分类计费汇总、无效/缺失费用保持未知、
  旧账本升级、主/子代理经 rail 透传费用、公共写入接口）。
- 相关文件 `test_session_token_usage.py`、`test_stream_event_rail_todo.py`、`test_front_phase1.py`、
  `test_deep_adapter_stream_end_final.py`、`test_external_harness_helpers.py`、`test_history_get_stream_split.py`：
  126 passed，1 error。该 error（`test_front_phase1.py::test_runtime_construct_is_off_front_process`，
  异步 fixture 未被插件处理）在未修改的 `develop` 上同样出现，与本修复无关。

未验证：前端尚未展示新字段（解析器忽略未知字段，行为不变）。

## 防错模式与资料同步

模型调用的计量数据（token、费用）应在统一的 rail 入口一次性完整落账，不在下游按单一流（主流）重新累加；
新增账本字段时保持"未知"与"零"可区分。

## 遗留与后续

- `chat.usage_summary.usage.total_cost` 仍为主代理口径；是否改为整轮口径待与维护者确认。
- Web 前端可在 `ContextUsageIndicator` 中展示 `session_usage.*.total_cost` 与 `priced_calls`。
