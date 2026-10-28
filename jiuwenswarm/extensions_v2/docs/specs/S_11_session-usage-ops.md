# 会话轮次与用量查询规约

| 项 | 值 |
| --- | --- |
| 编号 | S_11 / session-usage-ops |
| 最近修订 | 2026-10-28 |
| 关联 feature | [F_21](../features/F_21_session-usage-ops.md) |

## 1. 范围与条件

PR05 两个无写入的 BUILTIN Ops。由现有 `ops.build` 的 session BUILTIN 登记，用户在任意被宿主绑定有效 SessionContext 的 Handler 调用；查询目标只取 `ops.session_context.session_id`。不依赖 PR04 或用量通知的触发，不新增查询事件。本项契约已实现并通过 F_21 所列验证。

## 2. 接口

### `await ops.session.turns() -> int | None`

统计当前主会话已提交用户输入记录的不同 request_id 数；旧记录无 request_id 时按 id，二者均无时逐条计数。不统计 assistant/tool/system、子 Agent、跨会话消息、扩展追加、压缩摘要、已有心跳标记、cron/heartbeat 渠道和带系统 source 的记录；无来源标记的旧 user/human 记录按历史用户记录解释。存在但无输入为 0，未知或已删除会话为 None。读取反映当前保留的历史分支，非生命周期累计计数。

### `await ops.session.usage(breakdown: bool = False) -> dict | None`

返回 `schema_version="session-usage.v1"`、session_id、source（host/history）、context 和 turn；二者均缺失时为 None。context 和 turn 可以独立缺失、时点和 request_id 不同，不强行合并。context 保存最新主 Agent context.usage 的 request_id、phase、timestamp、sequence、context_window 和 measurement；context_window 的 occupancy_rate 是 0～1 比例，measurement.estimated 区分估算，总量来源保持 Core 的语义。turn 保存最近 chat.usage_summary 的 request_id、timestamp、input_tokens、output_tokens、total_tokens、cache_tokens（可缺失）、source="provider_usage"；是单轮 provider 汇总而非窗口占用或累计账单。breakdown=True 时 context 额外包含 parts；默认不返回明细。未知数值为 None，0 仍为 0。

宿主通知优先返回边界已捕获的用量快照副本，其他事件读取历史最新已提交快照。返回值为副本；不返回文本、凭据、模型连接或其他会话数据。布尔参数不接受字符串/数字替代。

## 3. 不变量与失败

- SU-01：无 SessionContext 抛 RuntimeError；非法会话 ID 抛 ValueError；路径越界拒绝。
- SU-02：未知会话和缺失事实不创建目录、不激活 Agent、不调用模型、不触发新事件。
- SU-03：历史读取在线程内等待至多 10 秒的队列边界；超时、读取失败或结构损坏抛 SessionStatisticsError，不能返回伪造 0。
- SU-04：主历史与原写锁串行读取；两会话隔离，无统计缓存。取消不撤销原历史队列，但查询无业务写入。
- SU-05：通知参数只由平台提供并复制为快照；Event payload 不是 Ops 的数据来源。平台负责可信 SessionContext，用户不修改平台 Adapter。
- SU-06：宿主上下文与历史用量可来自不同测量时刻，保留 request_id/phase，不将旧窗口描述为实时值；不重复计数模型调用或流片段。

## 4. 验证入口与限制

测试和实际执行证据由 [F_21](../features/F_21_session-usage-ops.md#5-验证设计执行记录与结论) 维护。查询扫描历史为 O(N)；本项不提供实时重新测量、Team 汇总或累计计费。删除与显式截断依现有生命周期及历史事实生效。
