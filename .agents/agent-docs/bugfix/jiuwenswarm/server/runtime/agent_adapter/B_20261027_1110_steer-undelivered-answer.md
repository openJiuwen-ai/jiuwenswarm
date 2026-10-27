---
id: B_20261027_1110_steer-undelivered-answer
title: steer 后原问题答案未交付
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/agents/harness/common/prompt
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/server/runtime/agent_adapter/session_input.py
  - jiuwenswarm/agents/harness/common/prompt/priority_registry.py
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

最初候选 `45a08027f962bf2bcb75a16c91aefb09c0d52d9b` 通过一次内部 UserMessage 说明引用旧回复，在短 system 的 SDK fixture 中通过三例，但完整 Work 服务复验仍漏答。真实 foreground model-visible-envelope 显示：工具轮及最终读图轮均保留原请求、9801 和完整交付说明，模型实际输入约 23k token，没有发现说明被过滤或压缩。因此不能把短 fixture 通过当成完整 Work 验收，或把故障误判为输入丢失。

修复改为由现有 `SessionInputGuard` 保持当前执行的原请求、补充要求和未完整交付结果，通过公开 system prompt builder 注册独立 `session_input_delivery` section，在每个后续模型调用中持续提供交付约束，包括工具续接及重试。真正终态、新普通请求及卸载移除 section；同一执行的权限/用户回答恢复保留状态。仍有效的原要求必须交付，明确取消和替换遵从，不重新执行工具。回执、媒体引用和用户原文保持现有协议。

没有取消旧阶段抑制，也没有无条件拼接旧答案：这两种做法会把用户已经取消或替换的内容重新展示。此次不修改独立的旧 `chat.error` 过滤缺口，不修改 Core 或依赖锁。

## 当前契约与回归证据

当前契约见 [S_02_session-steering-delivery](../../../../../specs/jiuwenswarm/server/runtime/agent_adapter/S_02_session-steering-delivery.md)，产品说明同步 `docs/zh/Harness.md` 和 `docs/en/Harness.md`。

2026-10-27 持续交付约束最终候选的 `session_input.py` SHA256 为 `d4ea29ba5e20455efff4bfd41f12e73abb28f48e72059e92c85dda290d905c34`，priority registry SHA256 为 `0211a9dca0ead8b80d0b6a234f8654e62c267f78a4be8c6d0ec6d7b770c2f0db`。Core 保持 `0ab5913b250d565b35bff3828dae438939cb1acd`，协议依赖保持 `10a03dd263c33042558bbd67a815952458a25fa9`。跨会话 Agent 正文不复制到 system，只保留来源引用，维持原有工具消息权限。

- Python 总回归 263 passed、2 skipped、1 deselected，覆盖新交付状态、原有适配器、Runtime/Gateway、图片校验、优先级与实际 SDK。使用 pytest 的 `--asyncio-mode=auto`；两项需要 Windows symlink 权限的用例跳过，明确排除此前已在未修改基线复现的 Goal clear 用例，未修改该行为。
- 新增 19 项交付状态单测及 3 项实际 SDK/formatter 原答续接场景；原有四项 ask-user resume 场景额外核对 system section 和实际旧回复，Agent 跨会话来源另有单测及实际 mailbox SDK 断言。
- 前端 session-input 65 项通过：实时 React 展示与历史恢复的原问题、图片和联合回答相同；明确取消及连续补充不恢复旧答案、不重复显示最终结果。
- 持续约束候选的实际 Qwen3.8-Flash 三例通过：在真实首答生成 `9801`、尚未向 SDK 释放时投递 steer，再原样释放首答 chunks。自然补图使用完整服务失败时相同的原句，不额外提示合并，最终答包含 `9801` 和图片独有文字；补文本最终答包含 `9801` 和 `15`；明确取消原题后最终仅有 `15`。记录实际模型输入、每个工具前后调用的持续 system section、隐藏输出、请求身份、真实读图及图片字节 SHA，62 项捕获证据断言通过；终态 section、Runner 和队列完成清理。本轮视觉 probe 获得支持结论，没有改写用户配置。
- 较早 UserMessage 候选的 probe 超时、过严 final 形态断言及完整服务失败记录均保留，未混入当前候选通过证据。外层 PowerShell 对真实模型日志返回 1、Python 原生退出码未捕获的记录单独保留；三例实际捕获及清理证据完整且经独立断言复核，不将外壳状态改写为 0。
- Vite 生产资源构建通过。完整服务的同源 WebSocket、独立会话及浏览器验收结果随当前 PR 交付记录保存。

先前图片传输矩阵仅验证引用、字节、顺序和读图，未覆盖原回答被隐藏后的交付完整性，不沿用为本项验收通过证据。

## 遗留与适用边界

模型生成的最终语义结果仍需实际模型及完整 Work 服务验证；事件身份、状态清理、回执及展示规则由确定性测试约束。自定义 MINIMAL/NONE 提示模式会过滤该 section，不将其计入默认 FULL 模式的验证保证。改动交付 section、Core 的 steer 消费或输出阶段协议后需重新执行相关验证。
