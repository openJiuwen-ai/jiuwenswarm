---
id: B_20261111_0211_prompt-duplicate-guard
title: 推荐短语重复创建员工无提示修复
type: B
author: chendongcai
author_email: chendongcai95@qq.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/createFlowState.ts
  - jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/pages/ColleagueCreatePage.tsx
  - jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/jiuwenmateApi.ts
  - jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/jiuwenmate.css
  - jiuwenswarm/channels/web/frontend/src/features/agentManagement/port.ts
  - jiuwenswarm/channels/web/frontend/src/features/agentManagement/client.ts
  - jiuwenswarm/channels/web/frontend/src/components/AgentManagementPanel/AgentEditor.tsx
  - jiuwenswarm/channels/web/frontend/src/components/AgentManagementPanel/AgentGroupEditor.tsx
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - jiuwenswarm/channels/web/frontend/tests/agentPromptDedup.test.mjs
  - jiuwenswarm/channels/web/frontend/package.json
designs: []
design_note: 无独立 D 设计。修复方案经会话内与用户逐项确认（重复项标红 + 创建置灰；数量上限与 #6387 空态回退不动；后端保持静默去重），按 jw-coding-flow「日常明确修复可以直接接入，不为齐全流程补造设计文档」以 jw-dev 接入；关键取舍记录于本文根因与修复决策一节。
---

# 推荐短语重复创建员工无提示修复

## 现象与影响

- 来源：GitCode issue [#6388](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6388)（SwarmChat 专家协作空间，团队版，推荐短语重复，员工也可以创建成功）。
- 触发条件：在员工/智能体/专家团的编辑入口为推荐短语（jiuwenmate 侧文案为「快捷问」）添加两条完全相同的内容，然后提交创建或保存。
- 实际行为：全程无任何重复提示，创建/保存直接成功。
- 预期行为：重复项应被提示并阻止提交。
- 复现证据：修复前以可执行复现测试确认——`buildAgentGroupCreatePayload` 与 `toWireSettings` 对重复输入原样上送（当时以 `tests/repro6388.test.mjs` 断言重复行为存在，已随修复翻转为回归断言并更名 `tests/agentPromptDedup.test.mjs`）；后端 jiuwenmate `CapabilitySelection._items` 实测对 3 条重复输入静默去重为 1 条、不报错。
- 影响范围：前端三个编辑入口（jiuwenmate 模板路径 `ColleagueCreatePage`、`AgentEditor`、`AgentGroupEditor`）。展示层 `ColleagueWorkspacePage` 欢迎页只显示前 3 条，重复项不易被察觉，问题更隐蔽。

## 关联设计与当前契约

- 无独立 D 设计；依据的当前事实：三个 payload 构造函数中 `memberIds`/`skills` 均以内联 `Array.from(new Set(...))` 去重（`port.ts` L122/L131），推荐短语是同函数内唯一未去重的列表字段，去重属既有语义的自然补全而非新契约。
- 无 spec 变化：payload 输出形状不变（仍为 `string[]`），仅取值域收窄（不含重复项）；`agent_templates.create` / `agent_groups.create` / jiuwenmate 同事创建的请求结构不动。
- 后端契约保持：jiuwenmate `CapabilitySelection._items`（`jiuwenswarm/server/jiuwenmate/models.py`）的静默去重行为不变，作为服务端对应防线；`agent_templates`/`agent_groups` 服务端在 openjiuwen core 仓，本次不涉及。

## 根因与修复决策

- 根因：推荐短语在「表单校验 → payload 构造 → 后端接收」三层均无重复拦截——表单层 `canSubmit`/`errors` 只覆盖名称、描述、人设；payload 层只过滤空串；后端静默去重导致用户无感知。现有交互为「点击添加 → 先出现空输入框 → 后输入内容」，校验只能落在输入/提交时机，而非添加按钮。
- 修复决策（两道防线，均在纯前端）：
  1. 编辑入口即时校验：新增纯函数 `duplicatePromptIndices`（jiuwenmate 与 agentManagement 各一份，单测锁定一致），重复输入框标红并显示「与第 N 条快捷问/推荐短语重复」，重复状态并入既有提交门控（`canSubmit` / `errors`+`hasErrors`），并经 `FormPageLayout.confirmDisabled` 将确认按钮置灰。
  2. payload 构造层去重兜底：三个构造函数对推荐短语采用与同函数既有字段完全一致的内联 Set 去重（trim 后判定、保持首现顺序），防未来新增入口漏接校验时静默上送重复项。
- 考虑过并拒绝的备选：失焦自动去重 + toast（用户输入"消失"造成困惑）；仅提交时静默去重（不满足 issue 的提示诉求）；后端对重复报错（破坏 API 宽容语义，且 agent 链路后端在 core 仓）。
- 明确不动（用户拍板）：数量上限与欢迎页只展示前 3 条的现状；空配置回退默认推荐语（GitCode #6387 的现象，属既有设计行为，另行处理）；后端行为。

## 回归证据与适用范围

- 候选：本地 develop 工作区（基于 `5c486da1c`），未提交状态。
- 自动化（Windows 11，Node v24，前端目录 `npm run`）：
  - `npm run test:agent-prompt-dedup`（新增回归，5/5 通过）：`duplicatePromptIndices` 边界（无重复、多条重复指向首个、trim 等价、空串不参与、空数组）；两份实现同输入一致；`buildAgentGroupCreatePayload`/`toWireSettings` 对重复+空白输入去重且保持首现顺序；正常无重复输入输出不变。
  - 回归：`npm run test:agent-management`（86/86）、`test:agent-group-management`（15/15）、`test:jiuwenmate-create-flow`（8/8）全部通过；`npm run build`（tsc + vite）通过。
- 手工验证（PDG 场景）：模板路径与智能体/团队编辑器的标红、行内提示、创建置灰、修改后恢复，待用户在本地调试服务（http://localhost:5173）上确认后补充记录。
- 未验证范围：`agentTemplateWritePayload`（`client.ts`）的去重改动未入单测——该函数未导出且依赖链较重，与另两个构造函数为同一行式改动，由 tsc 编译与代码检视覆盖；如后续导出可补测。core 仓服务端对重复项的行为未追通（payload 已去重，正常流不可达）。
- 重验条件：若 `port.ts`/`createFlowState.ts`/`jiuwenmateApi.ts` 的去重语义或编辑入口门控再次调整，需重跑上述四组前端测试。

## 防错模式与资料同步

- 值得复用的模式：「同一 payload 构造函数内的列表字段去重语义应一致」——`memberIds`/`skills` 已有内联 Set 去重而 `quickInputs` 遗漏即是本次根因。已核对 `frontend/AGENTS.md` 无对应条目，本次改动范围小，未新增全仓规则；如后续再出现同类遗漏，建议在 `frontend/AGENTS.md` 补充该模式并链接本记录。
- 资料同步：无 spec 变化；i18n 中英文案已随代码同步提供（`zh.json`/`en.json`）。索引同步按用户决定单独处理。

## 遗留与后续

- 智能体/团队链路后端（openjiuwen core 仓）对重复项的行为未核实；前端 payload 去重后正常流不可达，如需服务端强校验在 core 仓另立任务。
- 数量上限（配置可达 64 条、欢迎页仅展示 3 条）的差异维持现状，暂不修改。
