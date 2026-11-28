---
id: S_01_external-output-policy
title: 外部渠道输出分级与收敛门契约
type: S
author: nawenyu139
author_note: JWS-BUG-1 外发未收敛修复（官方 issue #6685）
modules:
  - jiuwenswarm/server/supervisor
files:
  - jiuwenswarm/server/supervisor/output_policy.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
---

# 外部渠道输出分级与收敛门契约

## 范围与配置来源

外部 IM 渠道（飞书/钉钉等）一次会话曾被投递数百条过程噪音（派单回执、轮询、子代理中间回报），淹没真实交付物。本契约在 Front→网关出站咽喉（`RuntimeSupervisor._forward_wire`）设唯一收敛门。

配置 `output.external`（config.yaml）：
- `verbosity: final | progress | verbose`，**默认 final**；非法值回退 final 并告警。环境变量 `JIUWENSWARM_OUTPUT_VERBOSITY` 运行期覆盖（线上止血通道），非法 env 值忽略。
- `channels`：受管辖渠道 id 列表，默认 `{feishu, dingtalk, welink}`。web/desktop 等交互面渲染进度事件为 UI 状态，**永不收敛**；`__cron__` 等控制渠道默认不在名单，cron 通知面行为不变。

## 帧分类与放行规则

事件识别镜像 worker event_store 视角：`payload.body`（或 `payload.payload`，嵌套 `delta`/`result` 解包）取 `event_type`，回落 `response_kind`。三类：
1. **always（任何级别必达）**：含 `final/error/ask_user/permission/approval/confirm/interrupt` 子串的事件、`chat.file`、`video.core.result`。硬约束：收敛层不得吞掉 owner 待决策项与失败上报。
2. **progress（final 下丢弃）**：`chat.delta/processing_status/reasoning/tool_call/tool_result/subtask_update/subagent_activity/moa_status`、`todo.updated`、`goal.snapshot`、`runtime.accepted`、`context.compression_state`、`history.message`、`models.updated`、`starter_*`、`team.*` 前缀。
3. **unknown**：final 下**失败关闭**（丢弃），progress/verbose 放行——新事件默认不进外部渠道，防再次刷屏。

判定序：verbose 全放行 → always 放行 → progress 级放行 → 其余丢弃。

## 生命周期与失败行为

- 渠道归属登记：`dispatch_parsed_request` 受理请求时把 `channel_id` 记入 `_route_channels`（dispatch_id 与 request_id 双键），请求 finally 清理。
- **无归属帧永不收敛（fail-open）**：server push、台账占位卡、控制流量查不到渠道即放行——门只能削减可正面归因于受管会话的流量，宁多投不漏投（漏投交付物比多投噪音危害大）。
- 丢弃帧按**已投递** ack（worker 关键帧 ACK 链不因策略停摆），INFO 日志留痕 request/channel/verbosity/event/累计丢弃数，事后可追溯。
- 策略在 supervisor 构造时加载一次；配置错误不阻塞启动（异常吞掉回退默认 final）。

## 验证索引

`tests/unit_tests/agentserver/test_external_output_gate.py`：分类矩阵（A~C）、放行矩阵与非法值回退（D~E）、渠道作用域（F）、配置/环境变量加载三分支（G~I）、咽喉集成——final 下噪音丢弃且 ack+计数、ask_user/error 必达、web 渠道零打扰、无归属 fail-open、verbose 全放行（J~N）。
