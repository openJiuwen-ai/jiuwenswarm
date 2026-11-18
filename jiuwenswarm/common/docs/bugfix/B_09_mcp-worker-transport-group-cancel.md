# MCP transport 子任务崩溃以裸取消作废整轮请求（PPT_TRANSPORT_CANCELLED）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-18 |
| 严重程度 | 高：长时 PPT 任务（30 分钟级）在一次工具调用失败时整轮作废，已完成的工作全部丢弃 |
| 范围 | mcp_config.py：`_run_mcp_worker`、`shutdown_pooled_mcp_worker`，Streamable HTTP / SSE / stdio 池化 worker |
| 测试基线 | Python 3.11，MCP 相关 8 个测试文件 281 项，除 1 项既有失败外全部通过 |
| 关联 spec | S_09_mcp-integration.md，N7 |
| 外部问题 | OfficeAce 1.7.0 反馈 E-1110-1816-2fd8 / E-1110-1107-4061 |

## 现象

用户用 pptx-craft 生成答辩 PPT，请求运行 30.7 分钟、41 轮 LLM、0 次工具失败。
18:16:34 模型发起 `apply_ops`，0.8 秒后整轮以
`PPT_TRANSPORT_CANCELLED: reconnect the document before continuing` 结束。这个报错由
relay-claw `extensions/pptx_session_scope/tool_profile.py` 的 `probe()` 抛出：ping 收到
CancelledError，而当前任务并未被取消。

## 定位证据与根因

1. **触发（relay-claw 1.7.0 现场构建 ≈ c734fc9543，dev-stable 已修）**：按运行日志行号
   （`tool_profile:489 installed` / `:64 acquired`）比对，现场构建对应 c734fc9543，
   不含 0f25dc99da。API 每次请求 POST live PPT MCP `/bootstrap` 时不带 `ttlMs`，服务端默认
   给 30 分钟；`LiveClientSessions.create` 只设一个 `setTimeout(release)`，中途不续期。
   到期释放后，已建立的 MCP 会话在 `authorizeCapability` 查不到客户端会话，返回 401。请求 4 在 17:45:51
   注册，最后一次成功调用在 18:15:32，18:15:51 租约到期，18:16:34 下一次调用失败。
   时间吻合；同族案例 E-1110-1107-4061 失败前同样运行了 30 分钟以上。dev-stable
   的 0f25dc99da 已改为滑动续租（每次工具调用及 API 心跳 `/renew` 续 10 分钟，绝对上限
   2 小时）。
2. **放大（本仓，dev-stable 仍存在）**：租约过期后，同一 MCP 会话的 POST 返回 401。
   mcp SDK `_handle_post_request` 在 task group 子任务里执行，404 以外的非 2xx 由
   `raise_for_status()` 抛出且无人兜底，AnyIO 随即取消整个 task group。该 task group
   进入在 worker owner 内，因此被取消的是 owner 本身（`cancelling()>0`）。
   `mcp_call_timeout_patch` 的单次 I/O 判定（B_04）因此把它当成外部取消直接传播。
   `_run_mcp_worker` 只在有 `close_reason` 时才映射取消，于是调用者的 future 拿到
   裸 CancelledError，绕过了 invoke 的 `except Exception` 重建重试，上层就把整轮
   当作取消处理。
3. **二次放大**：即使映射成普通错误，重建重试路径 `_close_pooled_worker` →
   `shutdown_pooled_mcp_worker` 去 `await` 一个以取消结束的 owner，也会得到
   CancelledError，而 `except CancelledError` 分支把它当成关闭方被取消原样上抛，
   重试同样变成裸取消。

## 决策

- `_run_mcp_worker`：owner 收到 CancelledError 时，一律向在途及排队调用者映射为
  `McpWorkerStoppedError`。有 `close_reason` 时文案不变；无 `close_reason` 时为
  "transport cancelled internally"。owner 自身仍向上传播取消，AnyIO 作用域语义不变。
  调用者只等 future，自己的真实取消由自己的 await 抛出，所以这个映射不会吞掉用户取消。
- `shutdown_pooled_mcp_worker`：只有关闭方自身 `cancelling()>0` 时才传播取消；owner
  已以取消结束、而关闭方未被取消时，记 warning 视为已停止。
- 请求级工具复用既有的一次重建重试（N7 at-least-once），不新增重试层。租约仍然有效时
  （瞬时故障）第二次在新会话上成功；租约确已失效时，新会话 initialize 失败，结果是
  普通工具错误。模型能看到失败并收尾、汇报进度，不会整轮作废。

## 拒绝的方案

- 在 relay-claw `tool_profile.probe()` 把 CancelledError 吞成工具不可用：只能修 PPT 一个
  消费方，其它请求级 MCP 连接器仍会被同一路径整轮取消；而且根因在 worker 映射层。
- 改 mcp SDK 或包装 `_handle_post_request`：属于上游私有实现，SDK 升级后容易失效；
  worker 边界是本仓唯一稳定的 owner 归属点。
- 在单次 I/O 判定里放宽 `cancelling()`：AnyIO 作用域取消与真实外部取消在 owner 内
  不可区分，放宽会破坏 B_04 对真实取消的保证。

## 验证

新增 2 项测试，用真实 AnyIO task group 子任务崩溃（取消 owner 本身）复现：
- `test_worker_transport_group_cancel_is_tool_error`：调用者拿到
  `McpWorkerStoppedError` 且 `cancelling()==0`，worker 退出，关闭不冒取消。
- `test_request_scoped_tool_rebuilds_after_transport_group_cancel`：请求级工具首调遇到
  transport 崩溃后重建一次即返回成功，连接次数为 2。

修复前两项均失败（调用者收到 `CancelledError: Cancelled via cancel scope`），修复后通过。

真实链路复现另见 `.agents/testing/repro_ppt_transport_cancel.py`（Git 忽略），以本地文件
形式提供，不随代码提交。它用 FastMCP + uvicorn 起真实 streamable-http 服务端，模拟租约
失效后返回 401；客户端走生产的请求级工具、worker 和 timeout 补丁。dev-stable 上两个
场景（租约永久失效 / 瞬时 401 一次）都以裸 CancelledError 结束，按 probe 规则判为
PPT_TRANSPORT_CANCELLED；修复后，前者是普通工具错误，后者重建一次后成功。
`test_registry_scan_timeout_and_shutdown[sse]` 在修复前后都失败，属既有问题，与本次无关。

## 已知遗留

- relay-claw 侧：租约达到 2 小时绝对上限、心跳续租失败或桌面端重启时，
  调用仍会失败。本修复只保证失败以普通工具错误呈现，不会整轮作废；真正的断线重绑
  需要 relay-claw 侧单独设计。
- `_reap_retired_mcp_worker` 的取消分支沿用旧逻辑，只影响后台回收任务自身，不影响调用者。
