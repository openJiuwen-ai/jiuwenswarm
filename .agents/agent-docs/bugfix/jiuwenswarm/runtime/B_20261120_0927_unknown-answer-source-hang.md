---
id: B_20261120_0927_unknown-answer-source-hang
title: 未知 source 的 answers 请求在团队会话中排队死锁、无终止帧
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/runtime
files:
  - jiuwenswarm/runtime/interaction.py
  - jiuwenswarm/runtime/service.py
  - tests/unit_tests/runtime/test_answer_source_validation.py
designs: []
design_note: 仅收紧既有 chat.send answers 校验的适用条件，不改变交互恢复协议、source 白名单与会话 lane 语义；无独立设计。
---

# 未知 source 的 answers 请求在团队会话中排队死锁、无终止帧

## 现象与根因

#6857：集群（Agent Team）模式下发送 `chat.send`，请求体不带 `mode`：

```json
{
  "method": "chat.send",
  "params": {
    "session_id": "web_...",
    "query": "",
    "content": "回答",
    "source": "not_a_source",
    "answers": {"q": "v"},
    "enable_swarmflow": false
  },
  "is_stream": true
}
```

客户端只收到 `chat.processing_status{is_processing:true}` 之后一直无响应，
既没有结果也没有结束帧；同一会话、同一参数补上 `mode` 才正常返回。

链路排查结论：gateway 会把 `params.session_id` 提升为顶层 session_id，
AgentServer 归一化后带/不带 `mode` 都识别为团队请求（`team.work.normal`），
差异不在请求分类层。真正的问题是这帧请求同时命中两个缺口：

1. `answers` 是 `Mapping` 而不是恢复协议要求的答案列表，`source` 又是
   `not_a_source`，所以既不会被 `interface._build_interactive_input_from_answers`
   识别为中断恢复，也不会被 lane 前的校验拒绝。
2. 既有 `validate_answer_source`（#6285）以 `request_id` 非空且 `answers` 为
   `Sequence` 为前提，本载荷两者都不满足，直接 return 绕过校验。

于是它被当成普通回合进入团队 lane，排在仍在等待该答案的交互之后；
团队 lane 不做 gateway 侧「非团队 chat.send 先取消旧流」的处置，导致请求
永久排队，客户端拿不到终止帧。

## 修复决策

- 校验与 lane 准入同源：不再用「`source` 在白名单内」代替恢复判定，而是直接调用
  lane 分类所用的 `agent_gateway.common.interrupt_resume.answers_paused_calls`。
  该谓词要求 `source` 属于 `INTERRUPT_RESUME_SOURCES`、`answers` 为非空 `list`，
  且除 `client_tool_interrupt`（答案自带每个调用）外 `request_id` 非空。
- 去掉 `validate_answer_source` 的 `request_id` 前置条件：缺 `request_id` 的
  恢复请求也要校验，否则「白名单 `source` + 缺 `request_id`」会被 lane 当成普通
  回合，复现同一排队死锁。
- `answers` 为非空但非 `list`（`Mapping`、`str`、`bytes`、`tuple` 等）一律拒绝：
  运行时只把非空答案列表识别为恢复，其它形态永远不可能恢复。空列表、空字典、
  空串继续按「无 answers」放行，避免普通回合把空载荷误判为非法作答。
- 空白 `source` 不声明恢复，继续保持普通回合行为；遗留
  `skill_evolution_approval` 恢复走 adapter 侧路径，只要求 `request_id` 非空。
- 校验位置不变：仍在 `normalize_request` 之后、`session_work_kind` 团队
  分类与 lane 准入之前，`InteractionAnswerError` 直接作为错误响应帧返回客户端。
- 保持既有豁免：无 `answers` 的普通回合即使带非白名单 `source`
  （如视频场景的 `video_tool`）继续放行；`chat.answer` 等非 `chat.send`
  方法不经过该校验。

## 验证

先红后绿，`tests/unit_tests/runtime/test_answer_source_validation.py` 全部通过，
`tests/unit_tests/runtime/test_sdk_runtime_api.py` 72 passed：

- `test_mapping_answers_without_request_id_are_rejected`（复现 #6857 字典载荷）
- `test_mapping_answers_with_whitelisted_source_are_rejected`
  （检视意见：白名单 source 也不能放行字典载荷）
- `test_mapping_answers_without_source_are_rejected`（字典形态先于 source 判定被拒绝）
- `test_chat_send_with_mapping_answers_and_no_mode_is_rejected`
  （AgentRuntime 层复现原始请求体：无 `mode`、`answers` 为字典、source 非法）
- `test_empty_mapping_answers_are_ignored`（检视意见：空字典与空列表口径一致）
- `test_missing_request_id_still_rejects_unknown_source`
- `test_missing_request_id_with_client_tool_source_passes`（唯一无需 `request_id` 的恢复）
- `test_missing_request_id_with_whitelisted_source_is_rejected`
  （检视意见：白名单 source 缺 `request_id` 不再放行）
- `test_non_list_answers_are_rejected`（字符串/字节/数字/元组形态均拒绝）
- `test_guard_matches_the_lane_resume_predicate`
  （对 `INTERRUPT_RESUME_SOURCES` × `request_id` 组合断言校验结果与
  `answers_paused_calls` 完全一致）
- `test_unknown_source_without_answers_passes`（`video_tool` 普通回合不回归）
- 既有白名单、空 `answers`、空 `source`、非 mapping 用例保持通过。

## 遗留

- 本次只保证非法载荷快速报错而非静默卡住；客户端如需真正作答，应使用
  恢复协议要求的 `request_id` + 答案列表 + 白名单 `source`（或 `chat.answer`）。
