---
id: B_20261127_1615_team-leader-context-usage
title: TokenWeather 在集群会话中缺少主 Agent 上下文用量
type: B
author: lizhiyuan
author_email: lizhiyuan65@huawei.com
designs: []
design_note: 在现有插件用量查询与事件契约上修复集群主 Agent 的归属判断，无独立设计文档。
modules:
  - jiuwenswarm/agents/harness/common/rails
  - jiuwenswarm/extensions_v2
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/server/runtime/session
files:
  - jiuwenswarm/agents/harness/common/rails/stream_event_rail.py
  - jiuwenswarm/extensions_v2/event_types/context_usage.py
  - jiuwenswarm/extensions_v2/docs/specs/S_07_session-usage-ops.md
  - jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
  - jiuwenswarm/server/runtime/session/session_statistics.py
  - jiuwenswarm/server/utils/stream_utils.py
  - tests/unit_tests/agentserver/test_team_helpers.py
  - tests/unit_tests/extensions_v2/test_context_usage.py
  - tests/unit_tests/extensions_v2/test_session_usage_ops.py
---

# TokenWeather 在集群会话中缺少主 Agent 上下文用量

## 现象与影响

TokenWeather 开启后可显示单 Agent 会话的用量。集群会话有模型调用时，插件未显示主 Agent 的上下文窗口。故障报告来自 0.2.10 Windows 安装版。[Issue #7198](https://gitcode.com/openJiuwen/jiuwenswarm/issues/7198)

## 根因与修复决策

1. 集群主 Agent 的 `context.usage` 记录带有 `member_name` 和 `team_id`，既有主会话过滤会跳过这类记录。`session.usage` 回看已提交历史时需先识别会话模式，集群会话只读取主 Agent 的顶层窗口，保持单 Agent 的既有查询口径。
2. 集群轮次此前缺少匹配主 Agent 实际模型用量与上下文窗口的链路。流式层将主 Agent 的 `llm_call_end` 计数与同一 Core 调用 ID 的 `post_call` 上下文配对。轮结束时按各计数字段分别汇总该轮所有完整配对，窗口取最后一组有效配对，并发布一次 `context.usage.updated`。
3. 轮次 ID 隔离先后请求。轮次 ID 暂时缺失时保留已收集的数据，无法确认归属的完成事件记录告警。流中角色兼容归一化后的字符串形式，明确标为主 Agent 的帧可以省略来源成员，明确指向其他成员的帧仍被排除。配对额外核对会话和调用归属，成员、跨会话、嵌套、委派与子代理调用均不参与统计。内部配对字段在事件广播和持久化前移除。
4. `build_usage_snapshot` 通过显式 `team_leader` 开关接受集群主 Agent 快照。历史查询遇到缺失或非字典的会话元数据时沿用普通会话过滤。读取权限或会话生命周期错误转换为 `SessionStatisticsError`，并保留原异常。

当前公开字段和事件语义见 [S_07 用量查询](../../../../../jiuwenswarm/extensions_v2/docs/specs/S_07_session-usage-ops.md) 和 [S_08 会话事件](../../../../../jiuwenswarm/extensions_v2/docs/specs/S_08_session-query-events.md)。

## 回归证据与适用范围

- 0.2.10 侧原提交 `888d347bb` 已由用户启动并验证集群插件效果。
- Develop 侧原提交 `171a1b20f` 的 `test_team_helpers.py`、`test_context_usage.py` 和 `test_session_usage_ops.py` 共 326 项单元测试通过。覆盖多轮调用配对、用量通知绑定窗口、已提交历史回读和干扰帧过滤。
- 检视修订后两分支分别运行上述三个测试文件，各通过 330 项。测试使用现有 Windows 虚拟环境和隔离数据目录。新增和扩展的用例覆盖字符串角色、缺失来源、显式非主成员排除、轮次 ID 暂时缺失、占用率的两种格式、元数据缺失与读取异常。[0.2.10 检视](https://gitcode.com/openJiuwen/jiuwenswarm/pull/11114) [Develop 检视](https://gitcode.com/openJiuwen/jiuwenswarm/pull/11120)
