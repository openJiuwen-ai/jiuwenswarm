---
id: B_20261027_1110_steer-undelivered-answer
title: steer 后原问题答案未交付
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/server/runtime/agent_adapter/session_input.py
  - jiuwenswarm/channels/web/frontend/tests/sessionInput.test.mjs
  - tests/unit_tests/agentserver/test_session_input_delivery.py
  - tests/integration/test_session_steering_sdk.py
  - tests/integration/test_steer_image_paths_sdk.py
designs: []
design_note: 在 Issue 4516 当前 PR 的人工验收中确认，对现有 steer 回答交付语义进行局部修复，无独立设计文档。
---

# steer 后原问题答案未交付

## 现象与影响

Issue #4516 的 PR !8990 在候选 `dcf0e099dc58773387c4578aefcb36850521e431` 的人工验收中出现漏答：10:52 原问题为 `99*99`，补充图片要求识别图片。10:52:26 原模型生成完整的 `9801` 答案，但被标记为 `output_suppressed=true`；10:52:53 后续可见 final 仅识图。前端实时展示与刷新恢复均没有数学答案。

原始事实保存在本地隔离验收数据的 `arithmetic-steer-visibility.json` 和该会话历史中。有限 checkpoint 检查未恢复每轮完整模型输入，不能据此宣称直接观察到了最终模型对原答案的理解。

## 根因与修复决策

锁定 Core 先将完成的 assistant 回答加入上下文，再消费运行中到达的 steer。Swarm 的输出边界却会隐藏输入到达后、输入实际应用前的旧阶段文本。因此模型上下文与用户实际交付之间缺少反馈。

修复通过现有 `SessionInputGuard` 的公开生命周期记录受输入边界影响的回答，并向消费这些输入的模型调用提供内部交付说明。说明保留当前执行中仍有效的原要求，尊重明确取消和替换，不重新执行工具。回执、媒体引用和用户原文保持现有协议。

没有取消旧阶段抑制，也没有无条件拼接旧答案：这两种做法会把用户已经取消或替换的内容重新展示。此次不修改独立的旧 `chat.error` 过滤缺口，不修改 Core 或依赖锁。

## 当前契约与回归证据

当前契约见 [S_02_session-steering-delivery](../../../../../specs/jiuwenswarm/server/runtime/agent_adapter/S_02_session-steering-delivery.md)，产品说明同步 `docs/zh/Harness.md` 和 `docs/en/Harness.md`。

2026-10-27 本次候选为 `dcf0e099dc58773387c4578aefcb36850521e431` 后的当前 PR 增量；真实模型捕获与最终实现的 `session_input.py` SHA256 均为 `83ca87c6d0ff10be63159bdf2cae4e80c48aa64f0e9c5f6dcb0da6f08bade698`。Core 保持 `0ab5913b250d565b35bff3828dae438939cb1acd`，协议依赖保持 `10a03dd263c33042558bbd67a815952458a25fa9`。

- Python 行为回归：252 passed、2 skipped、1 deselected。使用 pytest 的 `--asyncio-mode=auto`，覆盖新交付状态、原有适配器、Runtime/Gateway、图片校验与实际 SDK。跳过两项需要 Windows symlink 权限的用例；明确排除此前已在未修改基线复现的 Goal clear 用例，未修改该行为。
- 新增 13 项交付状态单测及 3 项实际 SDK/formatter 原答续接场景；原有四项 ask-user resume 场景额外核对交付说明和实际旧回复。
- 前端 session-input 65 项通过：实时 React 展示与历史恢复的原问题、图片和联合回答相同；明确取消及连续补充不恢复旧答案、不重复显示最终结果。
- 实际 Qwen3.8-Flash 三例通过：在真实首答生成 `9801`、尚未向 SDK 释放时投递 steer，再原样释放首答 chunks。补图最终答包含 `9801`、图片独有文字及背景色；补文本最终答包含 `9801` 和 `15`；明确取消原题后最终仅有 `15`。记录实际模型输入、隐藏输出、当前执行身份、真实 `read_file` 与图片字节 SHA，64 项捕获证据断言通过，Runner 和队列完成清理。
- 两次固定 5 秒视觉 probe 未获得结论。隔离验收 fixture 显式配置 native image，并以实际模型图片识别和字节核验作为证据，没有伪造 probe 缓存或改写用户配置。原始过严的“每个阶段必须产生 final”测试假设已修正为实际 suppressed delta/final 文本，首次记录保留。
- Vite 生产资源构建通过。完整服务的同源 WebSocket、独立会话及浏览器验收结果随当前 PR 交付记录保存。

先前图片传输矩阵仅验证引用、字节、顺序和读图，未覆盖原回答被隐藏后的交付完整性，不沿用为本项验收通过证据。

## 遗留与适用边界

模型生成的最终语义结果仍需实际模型验证；事件身份、状态清理、回执及展示规则由确定性测试约束。改动交付说明、Core 的 steer 消费或输出阶段协议后需重新执行相关验证。
