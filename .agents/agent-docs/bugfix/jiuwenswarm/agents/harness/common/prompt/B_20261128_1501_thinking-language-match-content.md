---
id: B_20261128_1501_thinking-language-match-content
title: 思考过程语言不跟随 match / 输出语言规则
type: B
author: whds5279
author_email: 2755136018@qq.com
designs: []
design_note: 无独立 D；对既有 preferred_response_language（en/zh/match）输出语言契约的小范围补强，使可见思考叙述与最终回复同语。
modules:
  - jiuwenswarm/agents/harness/common/prompt
files:
  - jiuwenswarm/agents/harness/common/prompt/prompt_builder.py
  - tests/unit_tests/agents/harness/common/prompt/test_output_language_rules.py
---

# 思考过程语言不跟随 match / 输出语言规则

## 现象与影响

`preferred_response_language` 为 `match`（或未提供）时，最终回复正文已按本轮消息
`content` 同语，但可见的思考过程 / 推理叙述仍常默认英文（模型习惯），与正文语种
不一致。用户在展示 thinking 的渠道上会看到「思考英文、回复中文」等割裂体验。

## 关联设计与当前契约

无独立设计文档。输出语言契约落在 `_output_prompt` 的「输出语言 / Output Language」
小节：优先用户明确指定；否则按消息字段 `preferred_response_language` 的
`en` / `zh` / `match`（缺省同 `match`）处理；技术术语保持原文。本修复将该小节
覆盖范围扩到可见思考叙述，不另开独立「思考 / 推理语言」小节，也不钉 UI
`preferred_language`。

## 根因与修复决策

**根因**：输出语言小节原先只约束最终回复语种，未点名思考过程；模型习惯上仍用
英文思考。

**修复决策**：

1. **并入输出语言小节**：中英各增一条——可见思考与推理叙述遵循本小节、与最终
   回复同语，不得因模型习惯改语种。不另开小节，避免与 match 规则分叉。
2. **最终回复规则收窄为「正文」**：中文「用户最终看到的正文只有…」、英文
   `reply body`，避免「用户最终看到的只有最后一条消息」与「思考对用户可见」
   表述冲突；思考可见性由渠道运行时决定，本 PR 只约束提示词层语言。
3. **不引用 `reasoning_content`、不写「不得默认英文思考」旧表述**：规则挂在
   既有 en/zh/match 链上，与正文同一判定。

## 回归证据与适用范围

- UT：`tests/unit_tests/agents/harness/common/prompt/test_output_language_rules.py`
  （含 `CN_THINKING_RULE` / `EN_THINKING_RULE` 正向断言，以及无独立思考语言小节、
  无 `reasoning_content`、无旧英文思考表述、`match` 文案仅出现一次等否定防回退）。
- 检视 worktree 实跑该文件 14 passed；prompt 目录全量 19 passed。
- 适用范围：提示词层；不保证模型 100% 遵循，亦不改变各渠道是否展示 thinking。

## 防错模式

- 输出语言相关规则优先落在「输出语言 / Output Language」小节，避免为思考 /
  推理另开语种小节导致与 match 不一致。
- 改「最终回复规则」措辞时区分「正文」与「思考可见性」，防止表述互斥。
- 文案演进时同步 `test_output_language_rules.py` 中的规则常量与否定断言。

## 遗留与后续

- 提示词遵循度属模型行为；若需硬保证，需在运行时对 reasoning 转写做语言处理
  （NH 观察项，本轮不做）。
- CI 全量偶发无关失败（如 `test_prompt_suggestion.py::test_suggestion_reaches_the_pages_open_on_its_session_only`）
  不作为本修复回归信号。
