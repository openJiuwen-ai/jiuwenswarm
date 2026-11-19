---
id: B_20261102_1104_cron-stable-model-selection
title: 定时任务模型选择丢失稳定 ID 与执行失败报错不可见修复
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/channels/web
files:
  - jiuwenswarm/channels/web/frontend/src/types/cron.ts
  - jiuwenswarm/channels/web/frontend/src/components/CronPanel/CronTaskDrawer.tsx
  - jiuwenswarm/channels/web/frontend/src/components/CronPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/CronPanel/cronModelSelection.ts
  - jiuwenswarm/channels/web/frontend/src/utils/wsEventDedup.ts
  - jiuwenswarm/channels/web/frontend/src/features/free-models/chatError.ts
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - jiuwenswarm/channels/web/frontend/package.json
  - jiuwenswarm/channels/web/frontend/tests/cronModelSelection.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/modelPicker.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/wsEventDedup.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/chatError.test.mjs
designs: []
design_note: 稳定选择契约（ModelPicker selection/onSelectionChange、CronJob.model_selection、
  chat.error 去重窗口）均为既有实现，本次是 cron 抽屉这一旧契约调用方的迁移补接与
  去重键缺维修复，无独立设计。
---

# 定时任务模型选择丢失稳定 ID 与执行失败报错不可见修复

## 现象与影响

用户在模型目录存在重名条目（两条 `model_name: deepseek-flash`，不同 api_key，
`models.groups` 为空）的本地环境，从 web 会话创建一次性定时任务并在会话页等待直播。
任务到点创建执行会话后 0.7 秒内即失败，会话内没有任何输出，也没有任何错误提示；
错误文案（`ambiguous legacy model name; select a stable model_id`）只落在
`history.jsonl` 的 `chat.error` 记录里，前端全程不可见。

同一创建源头的普通 chat 会话不受影响：会话 metadata 存有稳定
`model_selection`（`model:mdl_92a669f0...`），每次请求在
`interface_deep.py` 的会话选择短路处直接命中，从不走名字反查。

## 根因

两个前端缺口叠加；后端链路（CronJob 字段、create/update 校验、存储 round-trip、
调度器下发、启动迁移 `migrate_legacy_cron_selections`）全部就绪，未做改动。

1. **提交链路丢失稳定选择**。cron 抽屉是 `ModelPicker` 注释中"keep
   model_name-only until their contracts are migrated"的旧契约调用方：
   `CronTaskFormValue` 只有 `modelName`，提交 payload 只带 `model_name`
   （旧名）。执行会话是全新会话，无已存选择，模型解析落入旧名字反查：重名 2
   命中 + 无默认分组 + cron 会话被刻意排除在单智能体直通之外（`interface_deep.py:8432-8483`）
   → `ModelSelectionError`。
2. **失败报错被前端去重吞掉**。执行失败时后端实际推送了两条 `chat.error`：
   流失败的即时帧（gateway `_finish_failed_stream`，约 0.118s）与调度器的失败
   记录推送（`scheduler.py` `cron-failure-` 帧，约 0.837s，带完整文案
   `[cron] 任务执行失败: ...`）。二者共用同一 `request_id`，而
   `makeEventDedupKey` 对 `chat.error` 只认 `rid`，间隔 720ms <
   `EVENT_DEDUP_WINDOW_MS`(1500ms)，第二条被当作重复投递丢弃；第一条的
   渲染又被会话打开时的 `history.get` 重建冲掉（错误记录彼时尚未落盘）。
   净效果：用户全程看不到任何报错。后端给两帧分配了不同消息 id，但
   `WsEvent` 不携带帧 id，前端去重键无从区分。

## 修复决策

1. cron 抽屉迁移到 `ModelPicker` 稳定选择契约：`CronTaskFormValue` 增加
   `modelSelection`，抽屉传入 `selection`/`onSelectionChange`（按 model_id
   反查展示名保持 `model_name` 兼容下发），create/edit payload 经
   `cronModelPayloadFields` 一并携带 `model_selection`。无 `model_id` 的条目
   （登录免费模型）退回纯名称行为不变；编辑抽屉成为重名历史任务的治愈入口
   （重选模型即落库稳定选择）。
2. `makeEventDedupKey` 为 `chat.error` 追加错误文案维度（同
   `model_retry`/`processing_status` 的既有特例模式）：不同文案不再互吞，
   真·重复投递（同 rid 同文案）仍去重。修复后失败记录推送在历史加载之后
   到达，能稳定渲染。
3. 原始报错文案（`ambiguous legacy model name; select a stable model_id`）
   对用户没有可操作性，也不区分场景。`describeChatError`（既有错误翻译层）
   对该签名追加指引（i18n 中英文齐全）：聊天会话在模型选择器重选即可；定时
   任务需编辑任务重选模型或删除重建，并明示"在执行会话里换模型不会回写任务
   配置"（会话与 job 的模型配置是两个独立存储域，编辑抽屉是 job 侧唯一的
   治愈入口）。

拒绝的备选：后端在 create 时按名字快照 `model_selection`——重名场景下后端
无法知道用户选的是哪一条（只有抽屉下拉知道），该方案无法修复本缺陷；顺带
把 model_name 带上（现状）保证列表展示与旧链路不受影响。

## 回归证据与适用范围

分支 `fix/cron-stable-model-selection`，Windows 本机开发环境（Node 前端 +
`.venv` 后端）。前端入口 `node --test`：

- `npm run test:cron-model-selection`（新增）：5/5，含重名目录按 model_id
  命中、payload 必带 `model_selection`、纯名称/未选模型旧行为的回归。
- `npm run test:ws-event-dedup`：12/12（新增 3 例：流错误帧与 cron 失败
  记录互不去重、同文案重复投递仍去重、无文案退回 rid 键）。
- `npm run test:model-picker`：54/54（更新旧契约断言 + 新增组件级回归
  "a scheduled-task model with a stable ID submits its model_selection"）。
- `npm run test:cron-session-live`：12/12（确认直播路径不受去重键变化影响）。
- `npm run test:chat-error`：6/6（新增 1 例：重名报错追加可操作指引，
  覆盖流失败帧与失败记录推送两种载荷形态）。
- `npx tsc --noEmit -p tsconfig.json`：0 错误。
- 后端未改动；`tests/unit_tests/gateway/test_cron_login_model_binding.py` +
  `test_cron_controller_project_binding.py` 31/31 复核既有
  model_selection create/update 行为与文档描述一致。

未验证：真实运行栈上的端到端直播（需到点触发真实任务）；历史任务重名场景
仅由纯函数与组件测试覆盖。重验条件：重名目录下新建一次性任务到点执行，
会话页应能看到流式输出，或（其他失败时）即时看到 `[cron] 任务执行失败: ...`
错误气泡。

## 防错模式与资料同步

- "稳定选择契约调用方必须随提交下发 `model_selection`"——`ModelPicker` 组件
  注释已声明契约方向，本次补齐最后一个调用方；`cronModelSelection.ts` 纯函数
  固化 payload 组装，防再退化。
- "去重键维度必须覆盖同 rid 下语义不同的多帧"——`wsEventDedup` 已有三处
  特例（retry/proc/tool_call_id），本次补第四处；新事件类型接入时应检查
  是否需要同维展开。
- 历史任务治愈路径已有机制承接：名字唯一的由 gateway 启动迁移
  `migrate_legacy_cron_selections` 自动快照；重名的靠编辑抽屉重选（本修复
  使其生效）。存量 `migrate` 对重名有意跳过，非缺陷。

## 遗留与后续

- 会话打开期间 `history.get` 重建可能冲掉先于其到达的直播事件（本例第一条
  错误帧）。失败记录推送已后置于历史加载，实际影响小；若后续要彻底解决，
  需要设计直播事件与历史重建的合并策略，归 jw-design。
- 其他仍只发 `model_name` 的 cron 创建入口（MCP 工具 / AgentServer 会话内
  `cron_create_job`）在重名目录下仍会创建出旧式任务；创建期对歧义名称的
  拒绝校验是否补做，待需要时另行立项。
