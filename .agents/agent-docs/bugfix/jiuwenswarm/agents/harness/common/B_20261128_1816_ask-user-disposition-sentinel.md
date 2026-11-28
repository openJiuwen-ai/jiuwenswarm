---
id: B_20261128_1816_ask-user-disposition-sentinel
title: ask_user Skip/Cancel 改为语言无关 disposition 哨兵
type: B
author: whds5279
author_email: 2755136018@qq.com
designs: []
design_note: 无独立 D；对既有 ask_user 答案协议做语言解耦：Skip/Cancel 从写入中英固定文案改为 disposition 字段 + 裸哨兵，legacy 短语仅识别兼容。
modules:
  - jiuwenswarm/agents/harness/common/rails
files:
  - jiuwenswarm/agents/harness/common/rails/ask_user_disposition.py
  - jiuwenswarm/agents/harness/common/rails/ask_user_contract.py
  - jiuwenswarm/server/runtime/agent_adapter/interface.py
  - jiuwenswarm/agents/harness/code/rails/sdd/design_rail/rail.py
  - jiuwenswarm/agents/harness/common/rails/symphony/orchestration_rail.py
  - jiuwenswarm/channels/web/frontend/src/types/askUserDisposition.ts
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - jiuwenswarm/channels/web/frontend/src/components/InteractionSlot/InteractionPrompt.tsx
  - tests/unit_tests/agentserver/test_ask_user_disposition.py
---

# ask_user Skip/Cancel 改为语言无关 disposition 哨兵

## 现象与影响

ask_user 的 Skip/Cancel 原先把中/英文固定短语写入 `custom_input` / tool_result。
协议语义与 UI 语种耦合：新客户端若换语言会写出新文案，门禁与编排消费方难以稳定识别；
历史会话又必须继续识别旧中英文短语。

## 关联设计与当前契约

无独立设计文档。当前契约：

1. **协议字段**：可选 `disposition`（`skipped` / `cancelled`）与裸哨兵
   `[ASK_USER_SKIPPED]` / `[ASK_USER_CANCELLED]` 配对写入；UI 本地化文案只做展示。
2. **legacy**：中英四条固定短语仅识别兼容，新客户端不再写入。
3. **归一化**：`build_ask_user_answers_dict` 优先按 disposition / 哨兵 / legacy 归一为裸哨兵。

## 根因与修复决策

**根因**：Skip/Cancel 信号用展示文案充当协议值，语言与判定逻辑绑死。

**修复决策**：

1. **disposition + 裸哨兵双通道**：新客户端写字段与哨兵；旧客户端与历史仅靠文案识别。
2. **消费方口径有意差异**：后端 `is_skipped_or_cancelled` 保留子串匹配，适配 DesignRail
   `"question: value"` 行文本；前端 `isAskUserSkipOrCancelText` 对纯 `custom_input` 做全等，
   覆盖旧客户端精确短语即可。
3. **DesignRail / Symphony**：skip/cancel 分别按「无修改/批准」与 decline 集处理，纳入哨兵与
   legacy 标记。
4. **检视跟进（CR-11214-01）**：`swarmflow_human` 的整轮 skip 判定与 `answerText` 对齐，
   **只看 `answers[0]`**，避免多题部分跳过时 `answers.some(disposition)` 把真答案静默丢弃。
5. **检视跟进（死代码）**：删除后端无调用方的 `display_key_for_disposition_text`；展示映射
   留在前端 `askUserDispositionDisplayKey`。

## 回归证据与适用范围

- UT：`tests/unit_tests/agentserver/test_ask_user_disposition.py`（归一化、legacy、contract）；
  以及 design_rail / skillpack decline 相关既有用例。
- 前端：InteractionPrompt 写 disposition+哨兵；historyRestore / ToolGroupDisplay i18n 重映射；
  useWebSocket swarmflow skip 仅看首条答案。
- 适用范围：ask_user 答案协议与相关门禁/编排/用户面展示；不改变 SwarmFlow 生产端当前单题 payload。

## 防错模式与资料同步

- 协议值用语言无关哨兵或枚举字段；本地化字符串不得新写入 wire/tool_result。
- 整轮 skip/cancel 判定必须与答案取值口径一致（本仓 swarmflow 与 `answers[0]` 对齐）。
- 后端子串匹配仅用于「行内含答案」场景；纯值判定用全等，避免误伤普通自定义输入。

## 遗留与后续

- legacy 短语兼容窗口可在确认无旧客户端后下线（需另开变更）。
- 前端 useWebSocket / InteractionPrompt 尚无针对多题部分跳过的专用单测脚手架。
