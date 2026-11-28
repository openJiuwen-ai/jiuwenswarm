# S_01 停止级联与子代理错误归约（issue #6685 P-2/P-4）

## 背景

用户在 IM/Web 侧按下停止后，只终止当前轮（rail.abort /
cancel_round / scheduler cancel），深度代理持有的子代理 runtime
不受触碰：后台与异步子代理继续 RUNNING、继续消耗 token，且对用
户完全不可见（P-2）。子代理异常若携带空 message，主代理只能收到
无因 `errored`，被迫用文件 mtime 反推“是否跑完”（P-4）。

## 方案

### F-4 停止级联（interface_deep.py）

- 新增 `JiuWenSwarmDeepAdapter.stop_session_subagents(session_id)`：
  按根会话键取 `_subagent_controls[sid]`，调用 core
  `SubagentControl.cancel_all("user_cancel")`（强停全部活体子代
  理并唤醒排队的 subagent_wait），`asyncio.wait_for` 4s 上界，
  best-effort 不抛异常；成功时同步 `_clear_subagent_progress_batch`。
- 挂接点（intent ∈ cancel/supplement 的两条路径）：
  - interaction 路径：`cancel_round` finally 收敛后级联，停止延
    迟不叠加在 cancel_round 之外（级联自身 ≤4s）；
  - 共享 teardown 路径：cancel 分支停 todo 后级联。
- `interrupt_result` payload 附带 `stopped_subagents: [id...]`，
  前端可展示“已同时停止 N 个后台任务”。

### F-5 errored 必带归因（subagent_compat.py）

- `with_error_reason(status)`：`errored` 且 message 为空时回填
  `REASONLESS_ERROR_MESSAGE` 与 `error_code=UNKNOWN`。
- `CompatibleSubagentControl` 覆写 `get_status` / `wait`，在兼容
  边界统一兜底，core 各抛点无需逐个修改。

## 验收

- 单测 `tests/unit_tests/agents/harness/test_stop_cascade.py`：
  级联调用与 id 回报、core 失败降级为空、无 control/无会话空操
  作、errored 空因回填、有因不改写、非 errored 不触碰、wait 批
  量兜底（8 例）。
- 存量 `test_subagent_compat.py` 零回归。

## 非目标

- 不改 core（openjiuwen）代码；不动 heartbeat/goal 自主续跑链路。
- 不承诺“停止后已花费 token 可回收”，只做停止后的止损。
