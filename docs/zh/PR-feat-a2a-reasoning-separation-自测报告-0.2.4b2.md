# A2A Reasoning 分离 — 自测报告（jiuwenswarm 0.2.4b2）

| 项目 | 内容 |
|------|------|
| 包版本 | `jiuwenswarm[a2a]==0.2.4b2`（PyPI 最新预发布） |
| 关联能力 | A2A 入站思考（reasoning）与正文（response artifact）分离 |
| 关联 MR | https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/3404 |
| 测试时间 | 2026-08-06 |
| 测试环境 | Linux 6.8.0-101-generic / Python 3.11.15 / pytest 9.0.3 |
| 安装方式 | `pip install --pre -U "jiuwenswarm[a2a]"`（清华镜像） |
| 运行路径 | `/home/ycz/jiuwenclaw/.venv/lib/python3.11/site-packages/jiuwenswarm` |

---

## 1. 测试目标

验证已发布包 `0.2.4b2` 中 A2A channel 对 **思考内容** 与 **正文答案** 的区分行为：

1. 思考内容永不写入 `response` artifact。
2. `A2A_SERVER_EXPOSE_REASONING=true` 时，思考以 working 状态 `statusUpdate` 输出，并携带 `{"jiuwen_thought": true}`。
3. `A2A_SERVER_EXPOSE_REASONING=false` 时，思考被丢弃，正文仍正常输出。
4. Gateway + AgentServer 端到端流式链路可用，任务可正常完成。

---

## 2. 安装与环境

### 2.1 安装命令

```bash
pip install --pre -U "jiuwenswarm[a2a]" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2.2 环境变量（`~/.jiuwenswarm/config/.env`）

```bash
A2A_SERVER_ENABLED=true
A2A_SERVER_HOST=127.0.0.1
A2A_SERVER_PORT=19100
A2A_SERVER_EXPOSE_REASONING=true   # 场景 B 测试时改为 false
```

### 2.3 服务启动

```bash
cd ~/.jiuwenswarm
python3 -m jiuwenswarm.server.app_agentserver &
python3 -m jiuwenswarm.gateway.app_gateway &
```

| 检查项 | 结果 |
|--------|------|
| 安装版本 | `0.2.4b2` |
| Agent Card | `GET http://127.0.0.1:19100/.well-known/agent-card.json` → HTTP 200 |
| A2A 启动日志 | `[A2AChannel] started: http://127.0.0.1:19100/a2a` |
| 端口 | `18092`（AgentServer）、`19000`（Gateway Web）、`19100`（A2A） |

---

## 3. 自动化单元测试

### 3.1 执行命令

```bash
PYTHONPATH=/home/ycz/jiuwenclaw/.venv/lib/python3.11/site-packages \
  python -m pytest tests/unit_tests/channel/test_a2a_channel.py -v --tb=short --no-cov
```

> 使用 `PYTHONPATH` 指向 site-packages，避免仓库本地源码遮蔽已安装的 `0.2.4b2`。

### 3.2 执行结果

| 指标 | 结果 |
|------|------|
| 用例总数 | 13 |
| 通过 | 13 |
| 失败 | 0 |
| 跳过 | 0 |
| 耗时 | 1.58s |
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
| 7 | `test_dispatch_a2a_request_defaults_metadata_to_empty_dict` | PASS | metadata 默认空 dict |
| 8 | `test_executor_empty_query_emits_failed_task_lifecycle` | PASS | 空 query 失败生命周期 |
| 9 | `test_is_reasoning_message_detection` | PASS | `chat.reasoning` / `llm_reasoning` 识别 |
| 10 | `test_message_to_a2a_parts_marks_thought_metadata` | PASS | 思考 part 标记 `jiuwen_thought` |
| 11 | `test_executor_streams_reasoning_as_thought_status_updates_by_default` | PASS | 默认开启时思考走 statusUpdate |
| 12 | `test_executor_drops_reasoning_when_disabled` | PASS | 关闭时思考被丢弃 |
| 13 | `test_executor_terminal_reasoning_chunk_does_not_leak_into_artifact` | PASS | 终止 reasoning 块不泄漏到 artifact |

---

## 4. 端到端联调（Gateway + AgentServer）

### 4.1 请求命令模板

```bash
curl -sS -N --max-time 120 -X POST "http://127.0.0.1:19100/a2a" \
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
        "parts": [{"text": "计算 12*13，先思考再给出最终答案数字"}]
      }
    }
  }' | tee /tmp/a2a-e2e.log
```

### 4.2 场景 A：`A2A_SERVER_EXPOSE_REASONING=true`

| 指标 | 实测 |
|------|------|
| 结论 | **通过** |
| 日志 | `/tmp/a2a-reasoning-full.log`（约 23KB） |
| `statusUpdate` | 49 |
| `artifactUpdate` | 2 |
| `jiuwen_thought` parts | **47** |
| artifact 中含 `jiuwen_thought` | **0（无泄漏）** |
| 思考预览 | `用户要求计算 12*13…12*10+12*3=120+36=156…` |
| `response` artifact 正文 | `156` |
| 终态 | `TASK_STATE_COMPLETED` |

观察：

- 思考内容全部走 `statusUpdate` / `TASK_STATE_WORKING`，parts 带 `{"jiuwen_thought": true}`。
- 正文仅出现在 `artifactUpdate`，与思考通道分离。
- 思考含计算过程，正文仅保留最终答案。

### 4.3 场景 B：`A2A_SERVER_EXPOSE_REASONING=false`

> 修改 `.env` 为 `false` 后重启 Gateway，测完再改回 `true`。

| 指标 | 实测 |
|------|------|
| 结论 | **通过** |
| 日志 | `/tmp/a2a-off2.log`（约 1.5KB） |
| `statusUpdate` | 2（含 `Processing...` 与终态） |
| `artifactUpdate` | 2 |
| `jiuwen_thought` 出现次数 | **0** |
| `response` artifact 正文 | `121`（题目：11×11） |
| 终态 | `TASK_STATE_COMPLETED` |

观察：

- 关闭后 SSE 中无 `jiuwen_thought`，思考流被丢弃。
- 正文答案仍正常输出，任务可完成。

### 4.4 E2E 对照总结

| 检查点 | expose=true | expose=false |
|--------|-------------|--------------|
| 思考 statusUpdate + `jiuwen_thought` | 有（47） | 无（0） |
| 正文 `response` artifact | 有（`156`） | 有（`121`） |
| 思考泄漏进 artifact 元数据 | 否 | 否 |
| 任务完成态 | COMPLETED | COMPLETED |
| 结论 | **PASS** | **PASS** |

---

## 5. 核心行为说明

### 5.1 Reasoning 识别规则

内部消息满足以下任一条件即视为思考内容：

- `event_type == chat.reasoning`
- `payload.source_chunk_type == llm_reasoning`
- `payload.event_type == chat.reasoning`

### 5.2 通道映射

| 内容类型 | `EXPOSE_REASONING=true` | `EXPOSE_REASONING=false` |
|----------|-------------------------|--------------------------|
| 思考 | `statusUpdate` + `jiuwen_thought` | 丢弃 |
| 正文 | `artifactUpdate` / `response` | `artifactUpdate` / `response` |

---

## 6. 兼容性与风险

| 项 | 说明 |
|----|------|
| 默认行为 | 包内 `expose_reasoning` 默认开启；未设置环境变量时暴露思考流 |
| 关闭方式 | `A2A_SERVER_EXPOSE_REASONING=false`（需重启 Gateway） |
| Artifact 隔离 | 无论是否暴露思考，思考均不写入 `response` artifact |
| 协议要求 | 请求需携带 `A2A-Version: 1.0` |
| 依赖提示 | 同环境若仍安装旧包 `jiuwenclaw` / `openjiuwen-deepsearch`，pip 可能报告无关依赖冲突，不影响本次 A2A 验证 |

---

## 7. 未覆盖项

| 项 | 原因 |
|----|------|
| 多轮会话 reasoning 累积 | 本次 E2E 为单轮问答 |
| Team 模式下 teammate reasoning | 非本次包验证范围 |
| 非流式 `SendMessage` | 本次只验证 `SendStreamingMessage` |

---

## 8. 结论

- 单元测试 **13/13 通过**（针对已安装 `0.2.4b2`）。
- 端到端联调 **2/2 通过**：
  - `expose_reasoning=true`：思考与正文通道分离，`jiuwen_thought` 正确暴露，artifact 无泄漏。
  - `expose_reasoning=false`：思考丢弃，正文与完成态正常。
- **自测结论：通过（安装包验证 + 单元测试 + Gateway/AgentServer 端到端）**。

---

*报告生成时间：2026-08-06*
