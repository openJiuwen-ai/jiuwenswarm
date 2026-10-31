# usage_board：用量与费用看板

只读统计插件：把会话历史里的 token 用量、缓存 token 与费用汇总成一张看板，并显式区分
**模型上报的金额**与**按价格表估算的金额**，没有依据时显示 `—` 而不是 `0`。

- 插件目录：`jiuwenswarm/extensions/usage_board/`，清单 id `usage-board`，前端 `frontend/dist/index.html`（iframe）
- 聚合引擎：`usage_engine.py`（纯标准库）；RPC 层：`extension.py`
- **只读**：只读 `<data_dir>/agent/sessions/*/history.jsonl` 与可选的价格表；不写任何宿主文件、不联网、不执行被检对象
- 依据基线：`openJiuwen/jiuwenswarm` `develop`（`c4440433`）

## 数据来源（宿主事实）

| 事件 | 形状 | 依据 |
|---|---|---|
| `chat.usage_metadata` | 每次 model call 一条，用量在 `metadata.usage_metadata`：`input/output/total/cache_tokens` + `input/output/total_cost` | `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py:18772-18798` |
| `chat.usage_summary` | 每个回合一条，含 `usage`/`model`；**登录（积分计量）模型不报金额** | `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py:19239-19249`、`:19193-19197` |
| `context.usage` | 上下文窗口快照：`context_window{limit_tokens,input_tokens,occupancy_rate}` + `model/provider/agent_id` | `jiuwenswarm/server/utils/stream_utils.py:79-126`、`jiuwenswarm/channels/web/frontend/tests/fixtures/contextUsage.v1.json` |

历史文件是 JSONL，每行一条记录，键包含 `id/role/request_id/timestamp/content/event_type`，
事件载荷（`usage`、`metadata`、`context_window` 等）平铺在同一行
（`jiuwenswarm/server/runtime/session/session_history.py:1134-1151`）。

## 计总口径（避免重复计数）

1. 一个会话若存在 `chat.usage_summary`，**用它计总**（宿主同口径：`jiuwenswarm/channels/process_cli/machine_result.py:191-198`）；
2. 否则用 `chat.usage_metadata` 逐条累加计总；
3. 两者都存在时额外交叉核对：相对偏差 > 1% 记 `UB003`，两个数都返回给前端；
4. `calls`（模型调用次数）只来自 `chat.usage_metadata`，没有时为 `null`；
5. 费用：`cost_reported`（事件里的 `total_cost`）优先；缺失时按价格表估算；两者都没有计 `unpriced_turns`；
   显式上报 `0`（免费/按积分计量）算**已上报**，不会回退成估算；
6. 负值/非有限数由**本插件**按 0 处理并记 `UB007`（宿主对非法金额是直接判 `BUDGET_METER_UNAVAILABLE`，
   对非法 token 计数是跳过，都不按 0：`jiuwenswarm/channels/process_cli/machine_policy.py:74-79`、
   `jiuwenswarm/channels/process_cli/machine_result.py:99-104`）；
7. 按天分桶把 epoch 秒按**本机时区**归到自然日（`datetime.fromtimestamp`），跨时区机器的分桶可能不同。

## 结论编码表

| code | 级别 | 触发条件 | 依据 |
|---|---|---|---|
| UB001 | info | 会话目录里没有可解析的 chat.usage_summary / chat.usage_metadata | `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py:18772-18798; jiuwenswarm/server/runtime/agent_adapter/interface_deep.py:19239-19249` |
| UB002 | warning | 没有 <data_dir>/usage_board/prices.json 时无法给出任何金额（价格表是插件自定义格式，宿主没有对应概念） | `自身：价格表为插件自定义格式（README「价格表」小节）; jiuwenswarm/server/runtime/agent_adapter/interface_deep.py:19193-19197` |
| UB003 | info | 两者 total_tokens 相对偏差 > 1% | `jiuwenswarm/channels/process_cli/machine_result.py:191-198` |
| UB004 | warning | 单行不是 JSON 对象或超过单行上限，已跳过 | `jiuwenswarm/server/runtime/session/session_history.py:401-423` |
| UB005 | info | 登录/积分模型不报金额，价格表也可能未覆盖该模型 | `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py:19193-19197` |
| UB006 | info | 事件条数或会话数超过上限，统计结果只是下界（上限是插件常量，宿主对超限是直接判失败，语义不同） | `自身：扫描上限为本插件常量（宿主超限语义见 jiuwenswarm/channels/process_cli/machine_policy.py:83-85）` |
| UB007 | warning | 该字段已按 0 处理，避免污染总计；宿主对非法金额是直接判 BUDGET_METER_UNAVAILABLE、对非法 token 计数是跳过，都不按 0 | `jiuwenswarm/channels/process_cli/machine_policy.py:74-79` |
| UB008 | warning | prices.json 不是合法 JSON/映射，费用一律按未配置处理 | `自身：价格表为插件自定义格式（README「价格表」小节）` |
| UB009 | info | 取不到 <data_dir>/agent/sessions 时无法统计 | `jiuwenswarm/common/utils.py:2361-2362` |

## 本地 RPC（全部 `local_only=True`）

| 方法 | 参数 | 返回 |
|---|---|---|
| `usage.board.paths` | — | 数据根、会话目录、会话数、价格表路径与状态 |
| `usage.board.overview` | `days`(0=全部，默认 30)、`limit`(默认 50)、`session_limit`(默认 200)、`refresh` | 合计 + 按模型/按天/按会话分桶 + 交叉核对 + 预算 + 结论 |
| `usage.board.session` | `session_id` | 单会话回合明细、模型分桶、最新上下文窗口、结论 |
| `usage.board.export` | `format` = `json` / `markdown` / `csv`（+ 同上范围参数） | `{format, filename, content, bytes}` |
| `usage.board.prices` | — | 价格表是否配置、路径、条目、解析错误 |

参数上限：`limit` ≤ 200、`session_limit` ≤ 2000；非法参数返回 `BAD_REQUEST`，不会静默兜底。
总览结果在进程内缓存 15 秒，`refresh=true` 强制重扫。

## 价格表（可选，用户自备）

放在 `<data_dir>/usage_board/prices.json`；没有它时费用列显示 `—`，并记 `UB002`：

```json
{
  "currency": "USD",
  "models": {
    "gpt-5": {"input_per_1m": 5.0, "output_per_1m": 15.0},
    "claude-*-sonnet": {"input_per_1m": 3.0, "output_per_1m": 15.0}
  }
}
```

匹配顺序：完全相等 > 含 `*` 的通配（按字面长度降序、同长度按 pattern 字符串序）；
不含 `*` 的非精确项不参与匹配；大小写不敏感；未命中不计费。

## 已知边界（不夸大能力）

| 边界 | 说明 |
|---|---|
| 不读 `.trace/*.sqlite` | 那是观测库内部 schema，不是稳定接口；本插件只读有文档的 `history.jsonl` |
| 不统计 `subagents/` 目录 | 只统计顶层会话的 `history.jsonl`，子代理目录不计入 |
| 费用不是账单 | 上报金额来自模型服务端；没有上报时只能按你给的单价估算，标注为「估算」 |
| 预算口径不同 | `swarmflow_budget` 是**团队级** token 预算，由该团队**所有** swarmflow run 共享（`jiuwenswarm/resources/config.yaml:1626-1629`），与扫描范围口径不同，只作参照 |
| 单会话上限 | 每会话最多解析 20000 条事件（超限记 `UB006`）；详情最多返回 500 行回合（超限只置 `turns_truncated`，不记结论） |
| 按天分桶 | 把 epoch 秒按本机时区（`datetime.fromtimestamp`）归到自然日，跨时区机器结果可能不同 |
| 过期会话粗筛 | 按天过滤时先用 `history.jsonl` 的 mtime 判断：mtime 早于窗口下界的文件直接跳过、不解析（避免过期会话很多时空读盘）；回填旧文件或时钟异常可能少算，计入 `scanned.skipped_by_days` |
| 导出脱敏 | 导出内容不含 `data_dir`/`sessions_dir`/单会话 `path`，Markdown 里目录只保留最后两段；面板本身是本机的，`usage.board.paths` 仍返回绝对路径方便定位数据 |
| 上报 0 费用 | 显式上报 `0`（免费/按积分计量）算「已上报」，不会回退成价格表估算；`cost_source` 仍为 `reported` |

## 自测

```bash
export JIUWENSWARM_DATA_DIR=/tmp/jiuwenswarm-home
pytest tests/unit_tests/extensions/test_usage_board_engine.py \
       tests/unit_tests/extensions/test_usage_board_plugin.py \
       tests/unit_tests/extensions/test_usage_board_loader_contract.py -q
```
