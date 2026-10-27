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
  - jiuwenswarm/channels/process_cli
  - jiuwenswarm/channels/tui/frontend
files:
  - jiuwenswarm/server/runtime/agent_adapter/session_input.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/agent_adapter/steer_delivery.py
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

最初候选 `45a08027f962bf2bcb75a16c91aefb09c0d52d9b` 通过一次内部 UserMessage 说明引用旧回复，随后 `15fa7701d49c370de3ff9f29c12cfb75ab5815c4` 改为持续 system section；两者在短 system 的 SDK fixture 中通过，在完整 Work 服务中仍漏答。真实 foreground model-visible-envelope 显示：工具轮及最终读图轮保留原请求、9801 和完整交付约束，模型输入约 23k–24k token，没有发现说明被过滤或提前清理。因此提示式约束不是完整交付的充分证据，不能把短 fixture 通过当成完整 Work 验收。

修复改为由现有 `SessionInputGuard` 保持当前执行的原请求、补充要求和未完整交付结果，通过公开 system prompt builder 注册独立 `session_input_delivery` section，在每个后续模型调用中持续提供交付约束，包括工具续接及重试。真正终态、新普通请求及卸载移除 section；同一执行的权限/用户回答恢复保留状态。仍有效的原要求必须交付，明确取消和替换遵从，不重新执行工具。回执、媒体引用和用户原文保持现有协议。

最终在普通 human 执行且确有隐藏旧答时增加交付收束：phase 标记待整理，业务草稿继续隐藏；真正 answer 终态保存一次性完整计划，由同一次请求选定的模型在 compact、无工具上下文中整理原实际结果和新草稿，严格核对每个请求 ID 的覆盖及字面证据。先写回并保存 Core context，再发唯一可见 final，主聊天历史和各端展示一致。最多一次格式重试，每次 30 秒，provider/超时/coverage 失败明确报错且不回落草稿；整理用量计入本轮。Goal 和 Agent 原任务保持既有执行语义，权限及工具事件不吞掉。

没有取消旧阶段抑制，也没有无条件拼接旧答案：这两种做法会把用户已经取消或替换的内容重新展示。此次不修改独立的旧 `chat.error` 过滤缺口，不修改 Core 或依赖锁。

## 当前契约与回归证据

当前契约见 [S_02_session-steering-delivery](../../../../../specs/jiuwenswarm/server/runtime/agent_adapter/S_02_session-steering-delivery.md)，产品说明同步 `docs/zh/Harness.md` 和 `docs/en/Harness.md`。

2026-10-27 最终交付收束候选的 `session_input.py` SHA256 为 `01070b3ee4668fb060f381dca33829a890592e0b1f60bbb03dd86a882ea3bbfa`，`steer_delivery.py` 为 `e4f81194e0694671800ff3a96508179dfb7bbd833d744dc6e3e8c8016a1d1504`。Core 保持 `0ab5913b250d565b35bff3828dae438939cb1acd`，协议依赖保持 `10a03dd263c33042558bbd67a815952458a25fa9`。跨会话 Agent 正文不复制到 system，只保留来源引用，维持原有工具消息权限。

- 主 Python 回归 298 passed、2 skipped、1 deselected，覆盖交付状态、严格收束、adapter、Runtime/Gateway、图片校验、优先级与实际 SDK。使用 pytest 的 `--asyncio-mode=auto`；两项需要 Windows symlink 权限的用例跳过，明确排除此前已在未修改基线复现的 Goal clear 用例，未修改该行为。
- 交付状态 unit 32 项、严格整理 helper unit 21 项；图片 SDK 8 项包括业务草稿故意不含 9801 时的图片/文本/取消成功，以及无效 coverage 失败后没有可见草稿。验证一次性完整计划、Core 写回及唯一可见 final。原有四项 ask-user resume 与 Agent mailbox 来源断言保留。
- 前端 session-input 68 项、i18n 4 项及新增错误码定向用例通过；未将既有 chatError 全模块未通过误报为通过。实时 React 和历史恢复一致，隐藏草稿不提前显示或完成，整理失败有中英文提示。
- Process CLI 196 项回归及实际 TUI 源码行为测试通过。修复 render、machine result、duplex 和非交互完成判据；TUI live/history 不泄露隐藏草稿或提前 Idle，原始 JSON/JSONL 和权限/工具/错误事件保留。
- 持续约束候选的实际 Qwen3.8-Flash 三例通过：在真实首答生成 `9801`、尚未向 SDK 释放时投递 steer，再原样释放首答 chunks。自然补图使用完整服务失败时相同的原句，不额外提示合并，最终答包含 `9801` 和图片独有文字；补文本最终答包含 `9801` 和 `15`；明确取消原题后最终仅有 `15`。记录实际模型输入、每个工具前后调用的持续 system section、隐藏输出、请求身份、真实读图及图片字节 SHA，62 项捕获证据断言通过；终态 section、Runner 和队列完成清理。本轮视觉 probe 获得支持结论，没有改写用户配置。
- 较早 UserMessage 候选的 probe 超时、过严 final 形态断言及完整服务失败记录均保留，未混入当前候选通过证据。外层 PowerShell 对真实模型日志返回 1、Python 原生退出码未捕获的记录单独保留；三例实际捕获及清理证据完整且经独立断言复核，不将外壳状态改写为 0。
- 对 `15fa7701` 完整服务的真实失败记录做一次实际 Qwen compact 回放：8.281 秒、单次无工具，严格 coverage 校验通过，最终包含 `9801` 和 `PROOF-122166AA`；569 输入/421 输出 tokens 已记录。没有重跑识图或业务任务。
- Vite 生产资源构建通过。最终完整服务的同源 WebSocket、独立会话及浏览器验收结果随当前 PR 交付记录保存，不能沿用前两个失败候选的服务结果。

先前图片传输矩阵仅验证引用、字节、顺序和读图，未覆盖原回答被隐藏后的交付完整性，不沿用为本项验收通过证据。

## 遗留与适用边界

模型生成的最终语义结果仍需实际模型及完整 Work 服务验证；事件身份、状态清理、回执及展示规则由确定性测试约束。自定义 MINIMAL/NONE 提示模式会过滤该 section，不将其计入默认 FULL 模式的验证保证。改动交付 section、Core 的 steer 消费或输出阶段协议后需重新执行相关验证。
