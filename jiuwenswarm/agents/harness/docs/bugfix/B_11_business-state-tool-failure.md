# 业务拒绝态被当成成功：`invoke_tool` 与 `tool_result` 帧的失败判定缺口

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-26 |
| 严重程度 | 高：模型看到 `success: true`，据此认为改动已生效，前端也不提示失败 |
| 范围 | `tool_result_schema.py`（新增 `business_failure_signal`）、`progressive_tool_rail.py`（`_unwrap_tool_failure`）、`stream_event_rail.py`（`_infer_tool_result_error`） |
| 测试基线 | Python 3.11；`test_tool_result_business_failure.py` 25 项、rails / 事件流域回归 151 项通过；既有失败 `test_flash_profile_keeps_normalize_rail`（venv 内 openjiuwen 版本缺 `rsi.harness_rsi.auto_harness`）与本次无关 |
| 关联 spec | S_08_tool-loop-circuit-breaking.md（N10 / 失败计数）、S_20_stream-event-emission-channel.md（`tool_result` 帧） |

## 现象与根因

PPT 轮次里 `invoke_tool("apply_ops", ...)` 返回：

```json
{"success": true, "tool_name": "apply_ops",
 "result": {"result": "{\"applied\":false,\"status\":\"rejected\",\"failures\":[{...}]}"}}
```

以及 `{"success": true, "result": "MCP error -32602 ..."}`。两处判定都漏了：

1. `tool_result_schema.normalize_tool_result` 的失败枚举只收 `error / failed / failure`；
   `rejected` / `denied` / `applied:false` 属于**合法业务态**（S1：业务状态不得映射为工具
   status），`failures` 列表与 `MCP error` 文本也都不在识别范围。于是 `_unwrap_tool_failure`
   判定成功，`invoke_tool` 返回 `success: true`。
2. `stream_event_rail._infer_tool_result_error` 在 dict 分支遇到 `success` 为真时**提前
   `return False`**，后面的嵌套递归永远不会执行，所以 `{"success": true, "result": <失败>}`
   这类信封先被判成功，`tool_result` 帧不带 `is_error`。
3. `business_failure_signal` 之所以不能直接扩进 `has_failure_signal`：后者与
   `ToolResultErrorDetector` 的 `_dict_has_error` **逐字同源**，有 parity 测试钉住（S_08 N10），
   扩大它的判定面会同时改变熔断器的计数口径。

## 决策

- 新增独立纯函数 `business_failure_signal(value) -> (failed, error_text)`，只认三种高置信信号：
  1. `status ∈ {rejected, denied, blocked}`；
  2. `failures` / `errors` 列表非空；
  3. `applied=false` 且非 dry-run（`dryRun` / `dry_run` 为真时 `applied:false` 是正常结果）。

  字符串入参先尝试 JSON 解析，解析不成再走 `normalize_tool_result`（因此 `MCP error` 前缀、
  `[ERROR]`、`Exit code: N` 一并覆盖）。深度上限 4 层；list 与 `normalize_tool_result`
  同口径——**全部单项失败才算失败**（部分成功是真实业务语义，混合批量结果不能整体判失败，
  否则会被计入 `tool_failure_loop`）。同一层显式 `success=true` 优先（与 `has_failure_signal`
  的 success 优先序一致），包装器的 success 不掩盖内层失败——两个消费方都逐层下探同一组键。
- 判定分层，顶层保持保守：`normalize_tool_result` **不改**业务态映射（S1 不变量），只让
  `_unwrap_tool_failure` 与 `_infer_tool_result_error` 在**每一层信封**上查
  `business_failure_signal`，因此 `{"result": "<json text>"}` 这类包装能被剥开。
  下探键统一为 `result` / `data` / `raw_output` / `rawOutput`（`data` 是 openjiuwen
  `ToolOutput` 的载荷键），两 rail 不得各用一套键；**任一键命中失败即失败**，同一层里干净的
  兄弟信封不能掩盖被拒载荷。
- `_infer_tool_result_error` 改为"嵌套优先、`success` 兜底"：先判 `success=false` / `is_error` /
  `ok=false` / `status` / `exit_code` / 业务拒绝态，再递归 `result` / `data` / `raw_output` / `rawOutput`
  （字符串值只有 JSON / 失败前缀 / 短文本才做全文判定），最后才用 `success=true` 收尾。
  字符串已成功解析为 dict/list 时直接返回结构化结论，不再对序列化原文套全文正则——否则正文里
  恰好提到 `exit code: 1` / `success: False` 的成功结果会被误标失败。
- 嵌套字符串做**有界扫描**：JSON 文本、`MCP error` / `[ERROR]` / `[TOOL_NOT_AVAILABLE]` 前缀、
  或 ≤500 字符的短文本才进入全文判定。`invoke_tool` 的 `result` 常是目标工具的完整正文
  （文件内容、日志），对任意长正文套 `exit code` / `success: False` 正则会误报。
- `normalize_tool_result` 的文本路径新增 `MCP error` 前缀（失败独有，成功输出不会以其开头），
  `error_code=mcp_tool_error`，与 MCP 补偿层同名。

## 拒绝的方案

- 扩展 `has_failure_signal` 的 `status` 白名单：破坏与熔断器的 parity 契约，且会让
  `normalize_tool_result` 把业务状态写进工具级 `status`（违反 S1）。
- 在 `_infer_tool_result_error` 里直接看任意文本的正则：误报面太大（见上）。
- 只修 `invoke_tool` 的返回值、不改 `tool_result` 帧：模型侧修好，但统计 / wire 帧仍按成功。

## 风险与刻意的行为变化

- `tool_failure_loop` 现在会把"同工具 + 同参数 + 同业务拒绝原因"计入连续失败，达到
  `threshold`（默认 3）即收轮。这是目的：原逻辑下模型会无限重试同一句错误文案。参数或错误
  文案变化都会重置计数，正常的多轮修复不会被误停。
- `invoke_tool` 返回 `success: false` 后，模型下一轮会看到真实错误并改写参数。不要把该变化
  当成回归改回 `success: true`。

## 验证

- 新增 `tests/unit_tests/agentserver/test_tool_result_business_failure.py`（25 项）：三个判定
  函数各自覆盖拒绝态 / dry-run / 嵌套 JSON 文本 / `is_error` 信封 / 纯文本成功 / 长正文误报边界。
- 回归：`test_stream_event_rail_tool_result.py`、`test_progressive_tool_rail_failure_loop.py`、
  `test_progressive_tool_rail_stale_id.py`、`test_tool_result_error_detector.py`、
  `test_tool_result_normalize_rail.py` 共 151 项通过（除上述既有环境失败）。
- `has_failure_signal` 与熔断器的 parity 测试未改动、仍通过。

## 已知遗留

- 前端工具卡红叉仍不会亮（relay-claw 只对 deepresearch 生成 `toolResultMeta` 并会丢弃），
  本次只保证模型侧、失败统计与 wire 帧 `is_error` 正确；UI 闭环需 relay-claw 改动。
- 业务态白名单只收 `rejected / denied / blocked`。若后续出现新的"成功 RPC + 失败业务态"
  词表（如 `aborted`），按 S_08 N10 的准入标准评估后加入，不要放宽成模糊匹配。
