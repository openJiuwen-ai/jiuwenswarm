---
title: E2A method 字符串分发契约
author: mingo007
author_email: maoqiming1@huawei.com
id: S_01_method-dispatch
type: S
modules:
  - jiuwenswarm/server
files:
  - jiuwenswarm/common/schema/message.py
  - jiuwenswarm/server/request_policy.py
  - jiuwenswarm/server/front/router.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/server/http_invoke.py
---

# E2A method 字符串分发契约

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 关联模块 | `jiuwenswarm/common/schema`、`jiuwenswarm/server`、Gateway Web/TUI 分发 |
| 最近一次修订日期 | 2026-11-13 |
| 关联 feature | [F_20261113_1113_method-decoupling](../../../features/F_20261113_1113_method-decoupling.md) |

## 契约

- E2A 请求的 `method` 线上类型是 `str | null`。协议包只承载字符串，不定义业务 `ReqMethod`，也不因未知字符串失败。
- `ReqMethod` 的单一实现是 `jiuwenswarm/common/schema/message.py`。Swarm 入口负责把显式字符串转换为本地 `StrEnum`；未知或非字符串方法返回终态错误，`payload.code` 为 `METHOD_NOT_FOUND`。
- Gateway 只按实际注册的本地 handler 分发。Web 核心清单不是总数上限；TUI、插件、通道绑定等真实存在的本地 handler 必须保留。
- 未命中本地 handler 的 method 默认转发给 Swarm，不维护 Swarm 方法全集、动态补注册或转发白名单。默认转发不得改写业务 `params`。
- ACK 方法由 Gateway 先返回本地确认，再转发给 Swarm；该例外不得扩展到普通业务方法。
- `POST /api/v1/rpc` 使用 `{method, params, is_stream}`，与 WS 使用同一分发边界。`is_stream=true` 走现有 SSE 通道，通用流不定义任意业务事件的自定义终态。
- Swarmflow、enterprise 写限制和 skills 允许清单等业务策略在 Swarm 侧执行，不由 Gateway 依赖 `ReqMethod` 判断。

## 验证索引

- Swarm 方法契约：`tests/unit_tests/common/test_req_method_contract.py`。
- Swarm 业务策略：`tests/unit_tests/common/test_request_method_policy.py`、`tests/unit_tests/common/test_protocol_ids.py`。
- Gateway 透传契约：`tests/unit_tests/channel/test_method_passthrough.py`。
- Web RPC fallback：`jiuwenswarm/channels/web/frontend/tests/webHttpClient.test.mjs`。
