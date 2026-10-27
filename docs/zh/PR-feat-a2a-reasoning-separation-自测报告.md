# A2A Reasoning 分离 — 自测报告

| 项目 | 内容 |
|------|------|
| 分支 | `feat/a2a-reasoning-separation` |
| 提交 | `a74bf9ed` — feat(a2a): separate reasoning from answer in A2A responses |
| 测试时间 | 2026-07-20 |
| 测试环境 | Linux 6.8.0-101-generic / Python 3.11.15 / pytest 9.0.3 |
| 关联 MR | https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/3404 |

---

## 1. 测试目标

验证 A2A 入站响应中 **思考内容（reasoning）** 与 **正文答案（response artifact）** 的分离行为：

1. 思考内容永不写入 `response` artifact。
2. 默认开启 `expose_reasoning` 时，思考以 working 状态 `TaskStatusUpdateEvent` 输出，并携带 `{"jiuwen_thought": true}` 元数据。
3. 显式关闭 `A2A_SERVER_EXPOSE_REASONING` 时，思考被丢弃，正文仍正常输出。
4. 既有 A2A 入站/出站映射、生命周期、错误处理逻辑不受影响。
5. Gateway + AgentServer 端到端流式链路行为符合预期。

---

## 2. 变更范围

| 文件 | 变更说明 |
|------|----------|
| `jiuwenswarm/gateway/channel_manager/protocol/a2a/a2a_connect.py` | reasoning 识别与分流；`expose_reasoning` 默认 `True` |
| `jiuwenswarm/gateway/app_gateway.py` | 读取 `A2A_SERVER_EXPOSE_REASONING`，未设置时默认开启 |
| `docs/zh/A2A.md`、`docs/en/A2A.md` | 环境变量与行为说明更新 |
| `tests/unit_tests/channel/test_a2a_channel.py` | 新增/调整 reasoning 相关单测 |

---

## 3. 自动化单元测试

### 3.1 执行命令

```bash
uv run pytest tests/unit_tests/channel/test_a2a_channel.py -v --tb=short --no-cov
```

### 3.2 执行结果

| 指标 | 结果 |
|------|------|
| 用例总数 | 13 |
| 通过 | 13 |
| 失败 | 0 |
| 跳过 | 0 |
| 耗时 | 8.14s |
| 结论 | **通过** |

### 3.3 用例明细

| # | 用例 | 结果 | 覆盖点 |
|---|------|------|--------|
| 1 | `test_map_a2a_parts_to_params_text_and_files` | PASS | A2A 入站 parts → query/files 映射 |
| 2 | `test_message_to_a2a_parts_filters_completion_sentinel_text` | PASS | 完成哨兵文本过滤 |
| 3 | `test_message_to_a2a_parts_maps_tool_events` | PASS | 工具调用/结果事件映射 |
| 4 | `test_is_terminal_message_rules` | PASS | 终止消息判定 |
| 5 | `test_dispatch_a2a_request_requires_on_message_callback` | PASS | 未注册回调时报错 |
| 6 | `test_dispatch_a2a_request_and_send_queue_roundtrip` | PASS | 请求分发与响应队列回传 |
| 7 | `test_a2a_channel_start_serves_agent_card` | PASS | A2A 服务启动与 Agent Card 可访问 |
| 8 | `test_executor_empty_query_emits_failed_task_lifecycle` | PASS | 空 query 失败生命周期 |
| 9 | `test_is_reasoning_message_detection` | PASS | `chat.reasoning` / `llm_reasoning` 识别 |
| 10 | `test_message_to_a2a_parts_marks_thought_metadata` | PASS | 思考 part 标记 `jiuwen_thought` |
| 11 | `test_executor_streams_reasoning_as_thought_status_updates_by_default` | PASS | **默认开启**时思考走 statusUpdate |
| 12 | `test_executor_drops_reasoning_when_disabled` | PASS | **关闭**时思考被丢弃 |
| 13 | `test_executor_terminal_reasoning_chunk_does_not_leak_into_artifact` | PASS | 终止 reasoning 块不泄漏到 artifact |

---

## 4. 核心行为验证（设计）

### 4.1 Reasoning 识别规则

内部消息满足以下任一条件即视为思考内容：

- `event_type == chat.reasoning`
- `payload.source_chunk_type == llm_reasoning`
- `payload.event_type == chat.reasoning`

### 4.2 默认行为（`expose_reasoning=true`）

| 内容类型 | 输出通道 | 是否进入 `response` artifact |
|----------|----------|-------------------------------|
| 思考内容 | `TaskStatusUpdateEvent`（working）+ `jiuwen_thought` | 否 |
| 正文答案 | `TaskArtifactUpdateEvent` / `response` | 是 |

### 4.3 关闭行为（`A2A_SERVER_EXPOSE_REASONING=false/0/no/off`）

| 内容类型 | 行为 |
|----------|------|
| 思考内容 | 丢弃 |
| 正文答案 | 正常输出到 `response` artifact |

### 4.4 环境变量解析

| 环境变量值 | `expose_reasoning` |
|------------|-------------------|
| 未设置 | `true`（默认开启） |
| `true` / `1` / `yes` / `on` | `true` |
| `false` / `0` / `no` / `off` | `false` |

---

## 5. 端到端联调（Gateway + AgentServer）

> 已在本地完整服务栈上执行。启动方式：`uv run jiuwenswarm-start app`；A2A 监听 `http://127.0.0.1:19100/a2a`。

### 5.1 前置检查

| 检查项 | 结果 |
|--------|------|
| Agent Card | `GET /.well-known/agent-card.json` → HTTP 200 |
| A2A 启动日志 | `[A2AChannel] started: http://127.0.0.1:19100/a2a` |
| 端口 | `127.0.0.1:19100`（A2A）、`127.0.0.1:19000`（Gateway）监听正常 |

### 5.2 请求命令

```bash
curl -sS -N --max-time 150 -X POST "http://127.0.0.1:19100/a2a" \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -H 'A2A-Version: 1.0' \
  -d '{
    "jsonrpc": "2.0",
    "id": "e2e",
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "m-e2e",
        "contextId": "e2e-ctx",
        "role": "ROLE_USER",
        "parts": [{"text": "23*47=? 请先思考再回答，只给出最终数字"}]
      }
    }
  }' | tee /tmp/a2a-e2e.log
```

### 5.3 场景 A：`A2A_SERVER_EXPOSE_REASONING=true`（默认开启）

| 指标 | 实测 |
|------|------|
| 结论 | **通过** |
| 耗时 | ~6.5s |
| `jiuwen_thought` 出现次数 | **88** |
| 思考 chunk（statusUpdate + thought 元数据） | **88** |
| `artifactUpdate` 次数 | 3 |
| `statusUpdate` 次数 | 90 |
| 终态 | `TASK_STATE_COMPLETED` |
| `response` artifact 拼接正文 | `1081`（正确答案） |
| artifact 中是否含 `jiuwen_thought` 元数据 | **否** |

观察：

- 思考内容以 `statusUpdate` / `TASK_STATE_WORKING` 流式输出，parts 带 `{"jiuwen_thought": true}`。
- 正文仅出现在 `artifactUpdate` / `response`，与思考通道分离。
- 思考预览含计算过程（如 `23×47=1081`），正文只保留最终答案 `1081`。

### 5.4 场景 B：`A2A_SERVER_EXPOSE_REASONING=false`

| 指标 | 实测 |
|------|------|
| 结论 | **通过** |
| 耗时 | ~6.0s |
| `jiuwen_thought` 出现次数 | **0** |
| 思考 chunk | **0** |
| `artifactUpdate` 次数 | 3 |
| `statusUpdate` 次数 | 2（含 `Processing...` 与终态） |
| 终态 | `TASK_STATE_COMPLETED` |
| `response` artifact 拼接正文 | `1081` |
| 是否仍输出 Processing 提示 | 是 |

观察：

- 关闭后思考流被丢弃，SSE 中无 `jiuwen_thought`。
- 正文答案仍正常输出且任务完成。

### 5.5 E2E 对照总结

| 检查点 | expose=true | expose=false |
|--------|-------------|--------------|
| 思考 statusUpdate + `jiuwen_thought` | 有（88） | 无（0） |
| 正文 `response` artifact | 有（`1081`） | 有（`1081`） |
| 思考泄漏进 artifact 元数据 | 否 | 否 |
| 任务完成态 | COMPLETED | COMPLETED |
| 结论 | **PASS** | **PASS** |

---

## 6. 兼容性与风险

| 项 | 说明 |
|----|------|
| 行为变化 | `expose_reasoning` 默认由关闭改为开启；未显式配置环境的部署将开始向 A2A 调用方暴露思考流 |
| 关闭方式 | 设置 `A2A_SERVER_EXPOSE_REASONING=false` 可恢复旧行为（丢弃思考） |
| Artifact 隔离 | 无论是否暴露思考，思考内容均不会写入 `response` artifact（E2E 已验证） |
| 协议要求 | A2A 请求需携带 `A2A-Version: 1.0`（a2a-sdk 1.0） |
| 日志 | A2A 回复内容本身不打 info/debug 日志；仅启动/停止/异常有日志 |

---

## 7. 未覆盖项

| 项 | 原因 |
|----|------|
| 多轮会话 reasoning 累积 | 本次 E2E 为单轮问答 |
| Team 模式下 teammate reasoning 过滤 | 属于 `team_helpers` 逻辑，非本 PR 直接改动范围 |
| 非流式 `SendMessage` 路径 | 本次只验证 `SendStreamingMessage` |

---

## 8. 结论

- 单元测试 **13/13 通过**。
- 端到端联调 **2/2 通过**：
  - `expose_reasoning=true`：思考与正文通道分离，`jiuwen_thought` 正确暴露，artifact 无泄漏。
  - `expose_reasoning=false`：思考丢弃，正文与完成态正常。
- **自测结论：通过（单元测试 + Gateway/AgentServer 端到端）**。

---

*报告生成时间：2026-07-20*
