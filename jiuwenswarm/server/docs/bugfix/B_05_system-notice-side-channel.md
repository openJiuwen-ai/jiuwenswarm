# system_notice 恢复通知未显式映射，可能经兜底链路混入助手正文

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-20 |
| 严重程度 | 中（agent_ras 恢复通知在 deep 主链路会被 dict 兜底转 chat.delta 污染助手正文；stream_utils 入口虽兜底产出正确 event_type 但不丢弃空白帧） |
| 范围 | `jiuwenswarm/server/runtime/agent_adapter/deep/stream_parse.py`、`jiuwenswarm/server/utils/stream_utils.py`、`tests/unit_tests/agentserver/test_system_notice_stream_parse.py`、本文档；上游发端（agent-core `recovery/operations.py` 改发 `type="system_notice"`）与下游消费（relay-claw transform → web/office warning 渲染）随各自仓库单独提交 |
| 测试基线 | 定向回归全绿：`tests/unit_tests/agentserver/test_system_notice_stream_parse.py` 5 passed（新增）；`test_llm_output_payload_output_key.py` + `test_stream_chunk_whitespace.py` 13 passed；`test_interface_deep_stream_outbound.py` + `test_parse_task_stream_events.py` 等 47 passed（改动前跑） |
| 关联 spec | 无（S_20 约束的是 write_stream 单一入口与六类受管事件，不约束 chunk type 取值集合；本次未触及） |

## 现象

agent_ras 检测到思考语义死锁（semantic_deadlock）并执行恢复时，会向用户发一条
"⚠️ 检测到思考语义死锁异常…" 通知。该通知原先以 `type="llm_output"` 伪装成模型
输出流下发——经本仓流解析映射为 `chat.delta` 后，前端把通知文本拼进助手回答正文，
形成"回答里突然插着一行 ⚠️ 警告"的污染形态（semantic_deadlock 缺陷 P0）。

上游修复后通知改用独立 chunk type `system_notice`（payload `{"content": ...}`），
本仓需要保证两个流解析入口都把它映射为侧道事件 `chat.system_notice`，绝不落入
正文通道。

## 根因

本仓有两个 OutputSchema 解析入口，对未登记类型的兜底行为不同：

1. **deep/stream_parse.py `_parse_stream_chunk`**（interface_deep 主链路）：
   dict payload 兜底会把含 `"content"` 键的未知类型转成 `chat.delta`——
   `system_notice` 不显式拦截就会继续污染正文。
2. **server/utils/stream_utils.py `parse_stream_chunk`**（通用入口）：
   dict 兜底产出 `chat.{chunk_type}`，`system_notice` 恰好得到正确的
   `chat.system_notice`，但空白 content 不会被丢弃，payload 透传不受控。

## 决策

两个入口各加一个显式 `system_notice` handler（deep 侧插在 dict 兜底之前）：

- 映射为 `{"event_type": "chat.system_notice", "content": <原文>}`；
- content strip 后为空、或显式为 None（`payload.get("content") or ""` 归一）则丢弃；
- 透传 `stream_source_id` / `task_id`（deep 侧经 `_propagate_stream_source_id`
  包裹，与同文件 tool_call 等既有 handler 及 utils 入口对齐）——并发流下前端
  据此把通知路由到对应 subagent 行；
- 不加 ⚠️ 前缀、不包换行——展示前缀由前端 warning 渲染层负责。

配套补齐消费链自洽（检视 CR-10688-02，方案 B）：

- Gateway `web_ws_transport.py` 的 `_WEB_FULL_PAYLOAD_EVENT_TYPES` 登记
  `chat.system_notice`——防未来 payload 增加结构化字段（notice_type/level 等）
  在默认路径被裁剪；
- web 前端（`channels/web/frontend`）补 `chat.system_notice` handler，与
  `chat.notice` 同构渲染 role:system 气泡——覆盖 web 直连渠道（relay-claw
  部署形态由 relay 侧消费映射为 warning 提示条）。

## 拒绝的方案

- **不加 handler、依赖 stream_utils 的 `chat.{chunk_type}` 兜底**：deep 主链路的
  dict 兜底语义完全不同（转 chat.delta），两入口行为无法对齐；空白帧也不受控。
- **在本仓给通知加 ⚠️ 前缀**：web 的 warning 渲染通道会再加一次前缀，产生双 ⚠️；
  前缀职责收敛在展示层。
- **复用既有 `chat.notice` 事件**（检视建议方案 A）：三链（model_factory 生产方 /
  Gateway 名单 / web 前端消费方）确已齐全，但 relay-claw transform 的
  `chat.notice` case 带 `isTeam` 门控——非 team 模式直接丢弃，officeClaw 主部署
  场景（非 team）通知将不可见；放宽该门控会连带改变 image_tool_fallback 等
  既有通知在非 team 下的行为。故保留独立 `chat.system_notice`，relay 链路走
  专用消费映射，web 直连渠道按上述配套补 handler 自洽。

## 验证

新增 `test_system_notice_stream_parse.py` 7 用例：两个入口的 content 映射、
deep 侧不落 chat.delta 的回归守卫、两个入口的空白丢弃、deep 侧
stream_source_id/task_id 透传、content 显式 None 丢弃。存量 stream 解析
回归（llm_output payload / whitespace / interface_deep outbound）全绿。

## 已知遗留

无。relay-claw 侧 `chat.system_notice → system_info {type:"warning"}` 的消费
映射随该仓库提交。
