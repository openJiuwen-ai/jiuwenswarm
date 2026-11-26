---
id: B_20261126_0812_probe-failure-credential-classification
title: MCP 连接探活失败被无条件当成凭据错误回滚，网络抖动静默清空用户已保存 token
type: B
author: CharlesWang2030
author_email: 93426525@qq.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/server
  - jiuwenswarm/server/runtime/mcp
  - jiuwenswarm/channels/web/frontend
  - tests/unit_tests/agentserver
files:
  - jiuwenswarm/common/mcp_config.py
  - jiuwenswarm/server/runtime/mcp/registry.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/server/handlers/mcp.py
  - jiuwenswarm/server/handlers/mcp_hub.py
  - jiuwenswarm/channels/web/frontend/src/stores/connectorStore.ts
  - tests/unit_tests/agentserver/mcp/test_mcp_probe_rollback_credentials.py
designs: []
design_note: 既有探活回滚契约的数据丢失缺陷修复，不新增架构方案；取舍依据沉淀自 Issue #7029 与 PR !10905 两轮检视。
---

# MCP 连接探活失败被无条件当成凭据错误回滚

## 现象与影响

连接 MCP 连接器时，`probe_mcp_live_connection` 对服务端做真实连接探活。服务端
10 秒内超时、网络不可达（ConnectError/NetworkError）或网关 5xx 时，探活失败，
`rollback_failed_connect` 把记录回滚，并且**删除整个凭据文件**
（marketplace 分支还连带卸载 skills、删除安装记录）。

这些失败原因下用户的 token **从未被服务端 auth 层验证过**——一次瞬时网络抖动
即升级为"凭据丢失 + 连接器整体重来"：下次连接触发 missing-token 检查，用户被迫
重新填写 token / 重新走 OAuth 浏览器授权。host-OAuth 路径的 access_token 刚落盘
即被删，损失最直接。

隔离 workspace 最小复现：连接态置 connecting、凭据文件落盘、模拟 probe 返回
`unreachable` → 调 `rollback_failed_connect` → 凭据文件被删除、状态置回
registered。GitHub Issue #7029、PR !10905。

## 根因

探活失败只有一个返回通道 `(False, reason)`，但失败原因涵盖两类性质完全不同的
故障：

| 失败原因（mcp_config.py preflight/stdio 分支） | token 是否被验证过 |
|---|---|
| `http probe timed out after Ns` | 否 |
| `unreachable: ConnectError/NetworkError` | 否 |
| `http 5xx from server` | 否（反向代理层挡回） |
| `command not found on PATH`（stdio） | 否（本地环境） |
| `auth rejected (HTTP 401/403)` | **是——真正的凭据错误** |

`rollback_failed_connect` 的 docstring 前提是"连接失败 = token 没通过认证"，
5 个探活失败调用点一律无条件回滚并清凭据，只有最后一行应当清，其余行是
网络/环境/服务端故障。

## 修复决策与被拒备选

方案：失败原因分流，仅对确证凭据被拒清凭据。

1. `common/mcp_config.py` 新增 `probe_failure_is_auth_rejected(reason)`：仅当
   reason 含 `auth rejected (HTTP 401` / `auth rejected (HTTP 403` 边界标记
   （preflight 419/442 行产生，精确匹配）返回 True。空串、timeout、unreachable、
   5xx（含正文混入 "401" 字样的负例）、stdio 环境类均为 False。加入 `__all__`。
2. `registry.rollback_failed_connect(name, *, wipe_credentials: bool = True)`：
   仅 `wipe_credentials=True` 时执行 `_wipe_stored_credentials`；state 回滚不变。
3. 5 个探活失败调用点（`_run_mcp_connect_flow` 双副本、`_persist_and_probe_mcp`
   双副本、`_await_host_oauth_auth`）分类传递；错误码分流
   `MCP_CREDENTIALS_INVALID`（确证被拒）/ `MCP_UNREACHABLE`（其余）。
4. 前端 `connectorStore.ts` `friendlyMcpFailure` 按 code 分流文案
   （zh/en i18n `probeAuthRejected` / `probeUnreachable`）。

关键取舍（检视确认为有意决策，防"顺手优化"回归）：

- **`wipe_credentials` 默认 True**：保持 `cancel_connect`（registry.py:780 无参
  调用）等未分类调用方的旧契约——用户主动取消/断开时凭据不残留。fail-safe 的
  False 会改变 cancel 语义，被拒。
- **400/5xx 正文含 "token fail" 不判凭据拒绝**："不确定不清除"。非标准网关行为
  不足以确证；保留凭据下次重连自然再探活，不造成数据损失。HTTP 401/403 才是
  auth 层的确证信号。
- **local_env 透传白名单刻意排除 Runner 失败串**（第二轮修复，PR !10905
  atomgit-bot P2 + AI 检视 CR-10905-01）：`probe_mcp_live_connection` 会把
  Runner `add_mcp_server` 异常原文 `str(exc)` 作为 reason 返回，该串可内嵌完整
  server_config（env 凭据）。`probe_failure_is_local_env` 仅白名单本地自构造
  标记（`not found on PATH` / `invalid url:` / `invalid mcp server entry`），
  connect_flow 与 command.mcp（`_persist_and_probe_mcp`）双副本共 4 处入口
  统一契约：默认固定文案 "MCP live-connect probe failed"，仅非凭据被拒且命中
  local_env 白名单时透传原文。AK-SENTINEL 负向测试钉子钉住不外泄。
- **marketplace 回滚保留凭据**：wipe=False 时 state 回滚（卸 skills + 删记录）
  语义不变，仅凭据文件保留，Hub 安装路径 `rollback_on_probe_failure=False`
  可重试语义不变。

未选择"全部探活失败保留凭据"：401/403 是服务端确证拒绝，保留已证死的 token
会让前端无法区分"该重新填 token"与"该稍后重试"，错误码分流依赖确证分类。

## 回归证据与适用范围

- `tests/unit_tests/agentserver/mcp/test_mcp_probe_rollback_credentials.py`：
  分类 helper 正/负例（含 5xx 正文混入 "401" 不误判）、registry 回滚各分支、
  connect_flow / persist_and_probe 双副本的 wipe / code / redact 断言
  （AK-SENTINEL 不外泄）、host-OAuth 网络失败保留 token。
- 前端 `connectorStoreProbeFailureCopy.test.mjs`（PATH 缺失拼细节 / 无细节纯
  文案 / 凭据被拒文案）+ `npm run test:connector-store-probe-failure-copy`。
- `tests/unit_tests/agentserver/mcp/` 全量通过（`test_mcp_package_manifest.py`
  的 symlink 用例在 Windows 无符号链接特权环境基线即失败，与本修复无关）。
- CI（PR !10905 两轮流水线）静态检查/禁用词/防投毒/开源合规/UT/build/
  ruff/issue 关联/pr size 全 SUCCESS。

## 防错模式与资料同步

"失败原因只有一个返回通道"的分类缺口：调用方按最坏假设处理全部失败时，数据
破坏面大于故障本身。探活/验证类函数的失败 reason 若可能内嵌凭据（异常原文、
server_config 序列化），下发前端必须走白名单透传，默认固定文案。

## 遗留与后续

- host-OAuth `auth_failed` 响应仅含 error 文案、无 code 字段，前端无法按错误码
  分流（AI 检视 NH-10905-01，牵扯前后端契约，留待后续一致性整理）。
- 前端以硬编码字符串 `detail !== 'MCP live-connect probe failed'` 判定是否拼接
  细节，与后端固定文案耦合（NH-10905-02，LOW，后端文案变更时仅致冗余括号）。
