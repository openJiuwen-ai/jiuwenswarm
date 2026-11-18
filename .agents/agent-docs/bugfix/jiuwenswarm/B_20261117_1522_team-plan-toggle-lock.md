---
id: B_20261117_1522_team-plan-toggle-lock
title: 集群会话计划模式开关是假开关：建队后冻结并让位动态工作流
type: B
author: chenchunzhou
author_email: chenchunzhou1@huawei.com
designs: []
design_note: 由 issue 6361 现象逆向定位，无独立设计文档；方案与取舍记录在 OpenSpec 变更 issue-6361-team-plan-toggle-lock 的 proposal/design/specs，修复范围限定 Web 前端开关闸门与提示文案。
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/features/planMode/planModeGate.ts
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/InputArea.tsx
  - jiuwenswarm/channels/web/frontend/src/App.tsx
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - jiuwenswarm/channels/web/frontend/tests/permissionAnswerTransport.test.mjs
---

# 集群会话计划模式开关是假开关

Issue: #6361

## 现象

团队（team）模式会话：

1. 新建对话页开启动态工作流，发一轮对话；
2. 回复结束后再开启计划模式，**动态工作流被静默关闭**，无任何提示；
3. 切到别的会话再切回来，**计划与动态工作流两个开关同时亮着**。

## 根因

两条独立缺陷叠加，核心是**集群的计划能力是建队时的一次性决定，前端却把它当成可随时拨动的开关**。

### 根因一：集群计划模式没有运行期切换通道（代码推断）

| 维度 | 单 agent（`agent.*`） | 集群（`team.*`） |
|---|---|---|
| plan rails 挂载 | 常挂，与子模式无关（`jiuwenswarm/server/runtime/agent_adapter/interface_deep.py:12098`、`:14373`） | 建队时按 mode 条件挂（`jiuwenswarm/agents/swarm/config_specs.py:714` 的 `is_team_plan_mode(mode)` 分支挂 `CODE_AGENT_MODE` + `TEAM_PLAN_APPROVAL`） |
| 运行期状态切换 | 每轮 `switch_mode`（`jiuwenswarm/runtime/plan.py:195-247`） | **无**：`jiuwenswarm/runtime/plan.py:186` 的 `if resolved.is_team: return PlanStateResult()` 直接返回 |
| 建队 / 复用判定 | — | 只看 runtime 在不在（`jiuwenswarm/server/runtime/agent_adapter/team_helpers.py:4100`），无"mode 变了就重建"的判定 |

所以建队后拨动计划开关只改变 wire mode 字符串与 `metadata.mode`，队伍行为分毫不动。前端闸门
`planModeGate.evaluatePlanToggle` 此前只看"会话忙 / 有未完成目标"，与模式无关，于是集群会话建队后仍放行。

### 根因二：互斥副作用绕过动态工作流的会话冻结锁（代码推断）

动态工作流开关有会话级冻结锁：`InputArea.tsx` 的
`swarmflowToggleDisabled = isProcessing || (非新会话 && hasHistory)`，注释写明"进入真实会话后开关只读"。
但"开启计划模式"的互斥副作用 `setSwarmflowActive(activeSessionId, false)` 不检查这把锁，直接改写前端内存，
且不落盘——服务端 `session_swarmflow_config.enable_swarmflow` 仍为 `true`。

### 现象第 3 步的状态源冲突（代码推断）

- 动态工作流：恢复时从服务端 metadata 读回，且 `App.tsx` 只在 `enable_swarmflow` 为真时写 `true`、从不写 `false` → 恢复为开启；
- 计划：`App.tsx` 的 `session.plan_status` effect 拿 `in_plan` 回填（对集群只由持久化的 `metadata.mode` 推导，见
  `jiuwenswarm/server/agent_ws_server.py:4389` 的 `_combine_session_in_plan`）→ 也恢复为开启。

两条状态源互不知情，于是两个开关同时亮。这条链路解释了为何该现象连整页刷新都能复现，而不只是内存残留。

## 修复

**范围裁定（用户 2026-11-17）**：只锁集群，单 agent 不动。单 agent 的 plan rails 常挂、每轮走运行期状态同步，
计划模式本就设计为可反复开关，一刀切禁用会使其在第一轮之后永久不可用。

1. **集群会话建队后计划开关双向只读**：`planModeGate.ts` 新增 `isTeamPlanToggleLocked`
   （`mode === 'team'` && 非新建对话页 && 有历史），在 `evaluatePlanToggle` 中作为开、关两个方向的共同前置条件。
   落在这个唯一决策层，使输入框旁开关、`+` 菜单项、「计划」chip 关闭按钮、`/plan` 命令四个入口一次生效。
2. **冻结判定优先于忙态判定**：集群会话在队伍活着期间 `isProcessing` 常驻为真，若忙态先判则冻结文案永远轮不到
   （手工验证阶段实测两个开关都显示忙态文案）。冻结是永久的，报忙态等于"稍后可试"，而稍后也不行。
   `planModeGate` 与 `InputArea.tsx` 两处同改；与 `teamDispatch.readonlyHint` 的既有做法一致。
3. **恢复时动态工作流优先**：新增纯函数 `resolveRestoredPlanActive(inPlan, enableSwarmflow)`，接到既有
   `session.plan_status` 回填处（不新增第二条回填路径）。动态工作流是建队时冻结并落盘的会话级配置，
   优先于可能来自无效拨动的 `in_plan`。
4. **不变量而非时序**（检视反馈补强）：仅靠「要不要点亮」的守卫收不住历史脏会话。恢复有两条链路——
   `handleRestoreSession` 先按 `metadata.mode` 点亮计划，而动态工作流配置要等 `loadSessionMetadata` 回来
   才 hydrate，那一刻读到的 `enableSwarmflow` 必然还是 false，且 `plan_status` 守卫按设计只点亮、从不熄灭
   （`in_plan:false` 不可靠，不能拿它顶掉本地状态）。因此把「动态工作流开 → 计划灭」落成不变量
   `enforceSwarmflowPlanPrecedence(sessionId)`，在 swarmflow 置真的那一刻执行，两条链路与任何先后顺序都成立；
   `handleRestoreSession` 的点亮判定同时接上 `resolveRestoredPlanActive`，避免热切换时先闪一下计划标签。
5. **提示文案**：新增 `plan.toolbarUnavailableTeamLocked` 与 `swarmflow.readonlyHint`，格式对齐
   `teamDispatch.readonlyHint`（中文「团队已启动，X 不可修改」/ 英文 `The team has started; X can no longer be changed`）。
   顺带修掉两处同类问题：`+` 菜单动态工作流项置灰时此前无任何提示；动态工作流 chip 与「计划」chip 的关闭按钮
   此前写死忙态文案，建队冻结时会误报。

**未改动**：后端零改动；`InputArea.tsx` 的 Plan ↔ Swarmflow 互斥副作用保留（新建对话页两者都可改，
互相顶替是合理语义，集群锁落地后该副作用在已建队会话不再可达）；`/plan` 命令对集群本就整体不开放
（`slashCommands/registry.ts` 的 `ctx.mode === 'team'` 前置拦截），比集群锁更严，未动；
`swarmflow.configReadonlyHint`（预算面板只读提示）按用户要求保持原文案。

## 复现场景

团队模式，新建对话页开启动态工作流 → 发一条简短消息（如「你好，用一句话介绍你自己」）→ 等回复结束 →
开启计划模式 → 切到别的会话再切回。

## 验证

- **自动化**：`cd jiuwenswarm/channels/web/frontend && node scripts/test-permission-answer-transport.mjs`
  → 37/37 通过（新增 7 条 `#6361` 用例：已建队双向锁定、已加载消息也算建队、新建对话页可拨、
  单 agent 多轮可反复切、冻结优先于忙态、不受集群锁覆盖的会话仍报忙态、恢复时让位动态工作流）。
  测试先行：实现前 4 条断言失败（Red），2 条反向用例与既有 30 条通过。
- **构建**：`npm run build` 通过（TypeScript 编译 + Vite 构建）。
- **手工**（用户 2026-11-17 验证通过）：集群会话建队后两个开关均置灰并显示「团队已启动，X 不可修改」；
  切走切回与整页刷新后只有动态工作流亮；首条消息即开计划的集群会话切回后计划 chip 仍亮且只读；
  单 agent 多轮后计划仍可反复开关、「执行」后自动复位；中英文文案与调度模式同格式。

## 未确证项

- `/plan` 命令的文案与 `+` 菜单口径不一致（命令说"仅对单 agent 开放"，菜单却为集群提供计划开关）：
  修复前即存在，未在本次核实其历史成因。
- 用户复现时集群会话是否存在"回复早已结束但 `isProcessing` 未自愈"的情况：未取 `session_id` 核实。
  冻结优先后该状态不再影响提示语，与本次改动无关。

## 遗留风险

- **后端缺少"集群会话 mode 不可变"校验**：`_resolve_session_swarmflow_config`
  （`jiuwenswarm/server/runtime/agent_adapter/team_helpers.py:1147`）与 mode 持久化都让请求参数盖过持久值，
  因此 `session.plan_status` 仍可能与队伍真实能力脱钩。本次只在前端收口，建议另立问题。
- **历史脏会话**：修复前误开过计划、又未开动态工作流的集群会话，恢复后仍会亮计划 chip（只读）。
  那是它最后一次持久化的状态，不做数据迁移。动态工作流也开着的那种由
  `enforceSwarmflowPlanPrecedence` 收敛，不再两个同亮。
- **`hasHistory` 有两处实现**（`planModeGate` 与 `InputArea.tsx:1186`）：已让 UI 的置灰与提示全部取闸门结论、
  不自行计算，用例同时断言该结论；两处若将来漂移仍会使视觉与结论不一致。
- **「计划」chip 关闭按钮的提示走 `aria-label`**（动态工作流 chip 用 `title`），鼠标悬停看不到文案。
  这是 chip 关闭按钮的既有写法差异，未统一。
