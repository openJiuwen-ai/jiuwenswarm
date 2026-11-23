---
id: B_20261116_1540_interrupt-intent-contract
title: chat.interrupt 未知 intent 静默假成功：WS/HTTP 双入口统一契约校验
type: B
author: ZHANGPP_
author_email: zhangpu161@163.com
designs: []
design_note: 依据联调现场与 PR 检视意见定位，无独立设计文档；修复限定 intent 契约校验范围，并补回归测试。
modules:
  - jiuwenswarm/server
files:
  - jiuwenswarm/server/interrupt_intent.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/server/handlers/chat.py
  - tests/unit_tests/server/test_interrupt_intent_contract.py
---

# chat.interrupt 未知 intent 静默假成功

## 现象

采购文件生成对接时，适配层（procurement-gateway）发送
`chat.interrupt{intent:"user_cancel"}` 停止请求，服务端回 `success=true`，
但流式任务没有停止，平台侧 Agent 继续把生成跑完。调用方以为停止成功。

## 根因

`chat.cancel`（`chat.interrupt`）的 intent 此前只被当成分支条件使用，没有契约
校验：

1. WS 入口 `AgentWebSocketServer.dispatch_parsed_request` 的 CHAT_CANCEL 分支
   用 `params.get("intent", "cancel")` 取值，未知值照常走 `_handle_cancel`
   应答记账，而流式任务取消只发生在 `intent in ("cancel", "supplement")`
   分支内——错误值被静默吞掉；
2. 中立适配层 `handlers/chat.py::handle_chat_cancel` 是 HTTP/Gateway
   （`chat.interrupt` → `POST /chat/{session_id}/actions/interrupt`）的活路径，
   同样没有任何校验，同一协议字段在两个入口行为分叉；
3. 首轮修复只覆盖 WS 入口（P2 检视意见），HTTP 侧仍静默假成功；
4. 取值为 null/空串时未按仓内既有 `str(params.get("intent") or "cancel")`
   惯例归一化，硬拒绝会误伤合法缺省请求（P3 检视意见）。

## 修复

1. 新增共享契约模块 `jiuwenswarm/server/interrupt_intent.py`：
   - `KNOWN_INTERRUPT_INTENTS = ("cancel", "supplement", "pause", "resume", "stop")`；
     `stop` 是 cancel 的等价别名（runtime/service.py Team 分支按
     `intent in {"cancel", "stop"}` 处理），入口不得比下游更严；
   - `normalize_interrupt_intent(params)`：缺省 / null / 空串回落 `"cancel"`，
     与 runtime 的 `str(... or "cancel").strip().lower()` 一致，并把 `stop`
     归一为 `cancel`，下游只看到规范取值；
   - `unknown_interrupt_intent_error(intent)`：合法返回 `None`，未知值返回
     含实际取值与期望集合的错误消息；
   - `reject_interrupt_result_payload(intent, error)`：拒绝应答 payload，
     沿用既有 `chat.interrupt_result` 字段契约（intent+success+message），
     另附 `error` 便于适配层排查。
2. WS 入口与 `handle_chat_cancel` 都改为在 CHAT_CANCEL 分支入口（早退分支
   之前）做契约校验：先 `normalize_interrupt_intent` 归一化、把规范 intent
   回写进 `request.params`（保证下游大小写敏感比较一致），再
   `unknown_interrupt_intent_error` 校验。未知 intent 记录 `logger.error`，
   回 `chat.interrupt_result{success:false, error:...}` 并 `return` 终止处理，
   不做任何会话/任务操作；合法取值路径（含 target/org/wait_for_stop 与
   `cleanup_after_cancel` 收尾）逻辑不变。
3. 新增回归测试 `tests/unit_tests/server/test_interrupt_intent_contract.py`：
   白名单纯函数、null/空串/大小写/空白归一化、`stop` 等价 cancel（含流式
   任务取消）、`handle_chat_cancel` 拒绝路径零副作用、合法路径仍取消任务，
   以及 WS 分支复用共享契约的源码级防回退断言。

## 验证

- `uv run pytest tests/unit_tests/server/test_interrupt_intent_contract.py`：
  25 passed；
- `tests/unit_tests/server/test_chat_middleware.py`、
  `tests/unit_tests/runtime/test_web_cron_cancellation.py`、
  `tests/unit_tests/server/test_handler_dispatch_agents.py`、
  `tests/unit_tests/agentserver/test_client_tool_interrupt.py`：全部通过；
- `intent: "user_cancel"` 在 WS 与 HTTP/Gateway 两个入口均返回明确错误
  `unknown interrupt intent: 'user_cancel' (expected one of: cancel, supplement, pause, resume)`。

## 遗留

- `cancel_targeted_expert`（`target_team_id` 分支）对非 `cancel` intent 已显式
  返回失败，但错误文案是通用提示，未纳入本次契约文案；
- 外部 harness 适配器 `runtime/agent_adapter/external_harness_helpers.py` 的
  intent 归一化未纳入共享模块，后续可统一。