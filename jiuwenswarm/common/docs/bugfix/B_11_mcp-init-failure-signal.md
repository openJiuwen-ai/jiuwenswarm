# MCP worker 初始化失败只有一句「interrupted」：真实原因丢失、模型在同一请求里空转重试

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-28 |
| 严重程度 | 高：Excel 轮次在同一错误上重试 16 次（每次间隔 6–8s），模型无法判断原因，最终整轮失败且丢掉已完成的表格 |
| 范围 | mcp_config.py：`_run_mcp_worker` 的 init 失败分支、`acquire_request_scoped_mcp_session`、`release_request_scoped_mcp_sessions`；mcp_call_timeout_patch.py：httpx 工厂的失败响应观测 |
| 测试基线 | Python 3.11，`tests/unit_tests/common -k mcp` → 268 passed / 3 failed，三项失败在干净树上同样失败（两项是 Windows GBK 解码、一项是既有 sse 扫描超时） |
| 关联 spec | S_09_mcp-integration.md |
| 外部问题 | OfficeAce 1.9.0 日志包 2026-11-25T02-46-52-307Z（会话 `officeclaw_44980b4214ebf170ae1f4b54`） |

## 现象

同一轮 Excel 请求里连续出现：

```
[182301] execute invoke failed, error='MCP worker initialization interrupted: server=xlsx-mcp'
```

共 16 次，时间集中在 08:51:14–08:56:58，间隔 6–8s；轮次开始于 08:19:55（首字 2008s），
最后一次成功的 xlsx 调用在 08:30:38 —— 距轮次开始 31.3 分钟。整份 service 日志里
**没有任何一条**能说明这次中断的真实原因：

- `logger.warning("request-scoped MCP worker init failed: ... error=%s", exc)` 用的是 `%s`，
  而 init 被取消时 `exc` 是 `CancelledError`，`str(CancelledError())` 是空字符串，
  于是日志只剩 `error=`；
- 映射到调用者时只保留固定句式 `MCP worker initialization interrupted: server=...`，
  `type(exc)`、`worker.close_reason`、task 是否被取消全部丢失。

## 根因

分两层，必须一起修，否则「原因」还是看不到。

### 1. 失败信息在 worker 边界被降级

`except BaseException` 分支只把非 `Exception` 映射成一句固定文案，异常类型与关闭原因
既没进日志，也没进模型可见的错误。

### 2. 真实状态码在到达 worker 之前就被 SDK 吃掉了

用真实 SDK 复现（本地假端点对 `initialize` 返回 HTTP 403）后发现：

```
_enter_remote_mcp_session(...) 抛出
  asyncio.CancelledError: Cancelled via cancel scope 15f460fe3e0
  __context__: anyio.WouldBlock()
```

MCP SDK 的横向 task group 在 `initialize` 收到 401/403 后取消整个 scope，异常链里
**没有任何状态码或消息**。因此只给 worker 边界补类型/close_reason 仍然区分不出
「凭证过期」和「传输抖动」——必须回到 HTTP 层把状态码留下来。

### 3. 没有「本请求内不可恢复」的概念

`_invoke_with_active_binding` 在 `call_tool` 失败后会 `force_rebuild=True` 重建一次；
重建再失败，模型继续调用时又会起一个新 worker。对 401/403 这类确定性鉴权失败，
重建与重试都不可能成功，纯属空转（每次重建还要等 `shutdown_pooled_mcp_worker` 的
5s grace 才被强杀，正好是观测到的 6–8s 间隔）。

## 修复

| 位置 | 行为 |
|---|---|
| `mcp_call_timeout_patch.py` `_mcp_http_client_factory` | httpx 客户端挂 `event_hooks={"response": [_record_mcp_http_response]}`，把 `>=400` 的状态、method、url 写进本次建连的观测 holder |
| `begin_mcp_http_observation()` / `observed_mcp_http_failure()` | 建连前开窗、失败时读取；holder 用「可变 dict + ContextVar」，因为响应钩子可能跑在子 task，值型 ContextVar 的写入不会回流 |
| 工厂注入条件 | 即使共享 SSL 上下文构建失败也注入工厂（`verify` 回退到 httpx 默认 `True`）：凭证诊断不能依赖 CA 缓存是否成功 |
| init 失败日志 | 改为 `error_type=%s error=%r http_status=%s close_reason=%s cancelling=%s auth_dead=%s consecutive=%s`；`CancelledError` 也有可读信息，且能直接看出是被谁关的 |
| 失败文案 | `_mcp_init_failure_reason()`：401/403 → `(HTTP 403 Unauthorized)` + 「凭证已失效、本轮不要再重试、提示用户重发消息」；其他异常 → 附带 `Type: message`；取消 → 附带 `CancelledError, close_reason=...` |
| 连续失败上限 | 同一 `(request_id, server)` 连续 init 失败达 `_MCP_INIT_FAILURE_LIMIT=3` 后，本轮不再重建，文案带「已失败 3 次、停止重试」 |
| 快失败 | `_request_scoped_mcp_dead` 记录不可恢复的 key；`acquire_request_scoped_mcp_session` 命中即抛 `McpWorkerStoppedError`，不再起 worker、不再等 5s 强杀 |
| 回收 | `_close_pooled_worker` / `release_request_scoped_mcp_sessions` 一并清理失败计数与死标记（含只有标记没有 worker 的请求），避免进程级残留 |

鉴权失败（401/403，来自 HTTP 观测或异常文本）第一次就登记为不可恢复；其它异常保留
原有语义——前两次仍把原始异常抛给上层（不改变既有调用方的错误类型），第三次才切换为
带指引的停止信号。鉴权识别用正则而非裸子串（`4013 ms` 不能被当成 401），误判会让整轮
连接器被判定死亡。

## 为什么不在 relay-claw 侧修

本次事故的触发条件是 relay-claw 侧 xlsx 编辑凭证在 30 分钟固定 TTL 后失效（同一轮次
第 31.3 分钟首次失败，只有换新请求才恢复）。那条租约由 relay-claw 签发与校验，jiuwen
既不能续期也没有重新 bootstrap 的凭据。本修复只解决 jiuwen 边界上的两个问题：
**失败必须可诊断**、**不可恢复的失败不许空转**。凭证续期需求单独评估。

## 验证

### 1. 单测（`tests/unit_tests/common/test_mcp_init_failure_signal.py`，10 项）

- `test_auth_failure_tells_the_model_to_stop_and_ask_for_a_resend`：403 文案含 `401/403`、
  `Do not retry`、`resend`，并写入 `_request_scoped_mcp_dead`；
- `test_observed_http_403_wins_over_an_opaque_cancellation`：HTTP 层记录的 403 能被读取；
- `test_non_failure_responses_are_not_recorded`：2xx 不写入观测，避免误判；
- `test_http_403_makes_an_opaque_cancel_actionable`：**真实 SDK 形态**（无文案的
  `CancelledError` + HTTP 403）→ 文案变成 `HTTP 403 Unauthorized`，且登记不可恢复；
- `test_dead_connector_fails_fast_without_spawning_another_worker`：命中死标记立即抛错，
  不创建 worker；
- `test_interrupted_init_keeps_cause_and_close_reason`：`CancelledError` 文案含类型与
  `close_reason`，不再是原来的裸句子；
- `test_init_failure_log_carries_type_repr_and_close_reason`：日志含 `error_type=`、
  `error=RuntimeError('...')`、`close_reason=`、`cancelling=`；
- `test_repeated_generic_init_failures_stop_at_the_limit`：前两次保留原始异常，第三次
  文案含 `3 times` / `stop retrying`；
- `test_release_clears_failure_markers_for_the_request`：请求结束时标记被回收，别的请求
  的标记不受影响。

回归涉及 `test_mcp_call_timeout_patch.py`：`test_shared_ssl_falls_back_to_sdk_default`
原本断言「SSL 上下文构建失败就不注入工厂」，与本次「诊断不依赖 CA 缓存」冲突，
已改为断言仍注入工厂且能建出客户端。

### 2. 离线回放（mock 建连）

```bash
JIUWENSWARM_DATA_DIR=<repo>/.pytest-data .venv/Scripts/python.exe \
  scripts/verify_mcp_init_failure_signal.py --root .
```

打印四种形态下调用者实际收到的东西：403 文案、二次 acquire 立即失败、取消保留
`close_reason`、连续三次失败切换为停止信号。

### 3. 真实链路（真实 SDK + 本地假 HTTP 403 端点）

```bash
JIUWENSWARM_DATA_DIR=<repo>/.pytest-data .venv/Scripts/python.exe \
  scripts/verify_mcp_auth_failure_live.py --root .
```

脚本在 `127.0.0.1` 临时端口起一个对所有 JSON-RPC 请求返回 403 的端点，走真实
`StreamableHttpClient` 建连（并显式 `apply_mcp_call_timeout_patch()`，与线上
`JiuWenSwarmDeepAdapter.__init__` 一致），断言：

- 调用者拿到 `HTTP 403 Unauthorized ... Do not retry ... resend`；
- 同一请求第二次 acquire 在 0.0ms 内失败（无重建、无 5s grace）。

修复前该脚本会打印 `MCP worker initialization interrupted: server=xlsx-mcp (CancelledError)`。

运行（本机）：

```bash
JIUWENSWARM_DATA_DIR=<repo>/.pytest-data .venv/Scripts/python.exe -m pytest \
  tests/unit_tests/common/test_mcp_init_failure_signal.py -q --no-cov
```

回归：`tests/unit_tests/common -k mcp` → 268 passed / 3 failed；三项失败在干净树
（stash 掉本次改动）上同样失败：

- `test_mcp_patch_test_isolation.py::test_mcp_fixtures_restore_bindings_in_any_order[*-True]`
  —— 子进程输出被 Windows GBK 解码失败；
- `test_mcp_remote_lifecycle.py::test_registry_scan_timeout_and_shutdown[sse]`。

## 已知遗留

- 凭证过期本身不会因本修复消失：30 分钟后 Excel 编辑仍会失败。区别是现在会**一次说清**
  原因并停止重试，用户重发消息即可拿到新凭证。
- 分组池（`_call_through_group_pool`）与全局注册表 worker 未加同一套「死连接」快失败，
  它们不走请求级凭证，本次不动。
- HTTP 观测只覆盖 `mcp_call_timeout_patch` 注入的 httpx 工厂；若某天 SDK 改成自建传输
  且不接受 `httpx_client_factory`，观测会退化为空（此时仍能靠异常文本匹配）。
