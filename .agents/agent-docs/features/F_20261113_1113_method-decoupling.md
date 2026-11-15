---
id: F_20261113_1113_method-decoupling
title: E2A method 字符串化并回归 Swarm 解析
type: F
author: mingo007
author_email: maoqiming1@huawei.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/server
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/common/schema/message.py
  - jiuwenswarm/common/schema/agent.py
  - jiuwenswarm/server/request_policy.py
  - jiuwenswarm/server/front/router.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/server/http_invoke.py
  - jiuwenswarm/channels/web/frontend/src/services/webHttpClient.ts
  - tests/unit_tests/common/test_request_method_policy.py
  - tests/unit_tests/common/test_req_method_contract.py
  - tests/unit_tests/gateway/method_passthrough_harness.py
  - docs/zh/E2A-protocol.md
  - docs/en/E2A-protocol.md
designs: []
design_note: 用户确认的 method 解耦任务，无独立 D 设计编号。
---

# E2A method 字符串化并回归 Swarm 解析

## 背景

E2A 协议包曾同时持有 `ReqMethod`、`EventType`、`Mode` 和消息模型。Gateway 为识别转发方法又维护方法全集、动态补注册和转发白名单，导致 Swarm 新增 method 时必须同步修改 Gateway。该耦合把业务方法词汇表放在传输边界，协议包也承担了业务解析职责。

## 实施与决策

- **线协议**：E2A / Gateway 请求中的业务 method 是 `str`。协议包 `e2a` 不定义 `ReqMethod`，不在类创建时解析业务方法；`EventType`、`Mode` 和信封模型继续由协议包承载。
- **Swarm 归属**：`jiuwenswarm/common/schema/message.py` 是 `ReqMethod(StrEnum)` 的单一实现；Swarm Front、IPC、HTTP 与 Agent schema 入口将字符串解析为本地枚举。未知或非字符串方法由 Swarm Front 返回 `METHOD_NOT_FOUND`。
- **Gateway 分发**：Gateway 不维护 Swarm 方法全集、动态补注册或转发白名单。实际注册的本地 handler 优先执行；Web 主文件的核心清单不是数量上限，TUI、插件、通道绑定等所有实际本地 handler 均保留。未命中本地 handler 的 method 按字符串原样转发，业务 `params` 不额外改写。
- **ACK 例外**：`chat.send`、`chat.resume`、`chat.interrupt`、`chat.user_answer`、`chat.swarmflow_reply`、`history.get` 保留 Gateway 本地确认，同时继续转发，避免连接层等待语义变化。
- **业务校验归属**：Swarmflow `run_id` 校验、enterprise 写限制、skills enterprise 允许清单等由 `jiuwenswarm/server/request_policy.py` 持有。Web HTTP 限制仅作用于 channel 为 `web` 且 `web_http=True` 或 metadata transport 为 `web-http` 的请求；内部 cron、TUI、Gateway 和原 WS 路径不额外套用该限制。
- **HTTP 通用入口**：Gateway 新增 `POST /api/v1/rpc`，请求体为 `{method, params, is_stream}`，复用统一 unary / stream 分发。前端没有专用 REST 映射的 method 自动走该入口；通用流事件继续交给前端 handler，沿用现有 SSE 终态与超时规则，不为任意业务事件定义自定义终态。
- **文档**：Gateway 与 Swarm 中英文 E2A 文档统一说明方法归属、字符串线协议、本地 handler 优先和默认透传；Gateway 旧 Web API 参考中 `protocol_compat` / `ReqMethod` 的过时描述已同步修正。

## 验证

- Protocol：`pytest tests`，25 passed。
- Gateway：`tests/unit_tests/channel/test_method_passthrough.py` 与 `tests/unit_tests/gateway/test_multimodal_write_policy.py`，64 passed。覆盖 WS/HTTP 未知 method 保真透传、本地与插件 handler 优先、ACK 本地确认并转发、bus 转发、无 callback/bus 时 `SERVICE_UNAVAILABLE`、默认转发不改 params、`/api/v1/rpc` unary/stream。
- Gateway rebase 回归：转发、E2A 代理、消息处理与 cron 定向组合 337 passed；`test_tui_kv_cache_session_lifecycle.py` 收集被既有缺失 `bs4` 阻断。`test_persistent_web_bindings.py` 在 Windows 默认 GBK 下 8 例读 UTF-8 YAML 失败，使用 `PYTHONUTF8=1` 复核为 27 passed。
- Swarm method/policy：98 passed。pytest 主体通过后在 `tests/conftest.py` 清理临时 home 时因 Windows 日志文件句柄未释放出现 `PermissionError`，进程退出码为 1，但测试结果行显示 98 passed。
- Swarm front/process 定向组合：122 passed，1 failed，1 error。失败与错误分别来自缺失 `bs4` 和 `alembic` 的既有环境依赖，收集点在 `test_front_phase1.py`，不是 method 分发逻辑。
- Web 前端：`tests/webHttpClient.test.mjs` 24 passed；`chatErrorSseLifecycle` 中 method 相关定向 5 passed；完整套件为 17 passed、5 failed、1 skipped，失败仍是既有 harness 漂移：缺少 `src/hooks/requestEventFilter.ts` 与 `isTeamOrganizationUiEnabled`。

## 遗留与交接

- 三个仓均已 rebase 到各自 origin 最新提交，但改动尚未提交或推送。
- `e2a` 依赖仍指向 `feature/E2A` 分支，Swarm 的 `agent-gateway` 仍钉在旧提交；协议、Gateway、Swarm 需要按真实提交顺序发布并更新 lock / pin。当前验证依赖本地工作区联调。
- 未执行真实部署 E2E；Python 3.14 兼容性仅通过元数据调整，未实测。
