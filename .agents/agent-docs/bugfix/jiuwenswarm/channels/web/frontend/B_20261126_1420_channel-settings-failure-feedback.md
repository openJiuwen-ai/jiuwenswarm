---
id: B_20261126_1420_channel-settings-failure-feedback
title: 渠道设置页保存/加载失败反馈缺失——保存失败零提示、单渠道加载失败整列表永久骨架
type: B
author: gunner_beat
author_email: gunner_heart@163.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/channels/useChannelForm.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/channels/useSettingsChannelsController.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/channels/useChannelControllersStatus.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/channels/SettingsChannelsPanel.tsx
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/channels/SettingsChannelsPanel.css
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/channels/components/ChannelConfigDialog.tsx
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/channels/components/ChannelErrorBars.tsx
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - jiuwenswarm/channels/web/frontend/tests/settingsChannelsErrorFeedback.test.mjs
  - jiuwenswarm/channels/web/frontend/scripts/test-channels-error-feedback.mjs
  - jiuwenswarm/channels/web/frontend/package.json
designs: []
design_note: 交互反馈层缺陷修复，复用 FormDialog 既有 status 插槽与骨架/横幅/toast 三层反馈位，无独立设计文档。社区 issue 与修复 PR 为事实源：GitCode issue #7001 / develop PR !10835 / releases/0.2.10 PR !10993。
---

# 渠道设置页保存/加载失败反馈缺失——保存失败零提示、单渠道加载失败整列表永久骨架

## 现象与影响

渠道设置页两处"失败时用户无从感知"的缺陷：

1. **配置对话框保存失败零反馈**：对话框「保存」触发 `controller.save()`，失败时错误只写
   入 `controller.error`，其唯一出口是页面级 fixed toast——位于对话框外、2 秒后由
   `useChannelForm` 的定时器自动清空、关闭对话框 `reset()` 也会清掉。保存失败后对话框
   没有任何变化，用户以为保存成功，配置实际未落库。
2. **任一 `channel.get_conf` 失败 → 整列表永久骨架**：页面挂载并发跑 7 个渠道配置读取，
   `useChannelForm.load` 失败路径不置 `loaded`，而骨架判定 `configurationsLoading` 含
   "任一 controller 未 loaded"，单渠道失败就让整个列表永久卡骨架；错误 toast 2 秒即逝，
   之后页面无任何提示与重试入口，唯一解除方式是切走再切回设置页。

## 关联设计与当前契约

错误展示收敛到三个既有反馈位，按"就近 + 持久优先"分工：

- **对话框内 `status` 错误条**（FormDialog 既有插槽，`role="alert"`，渲染在表单上方）：
  承载当前打开对话框的保存/加载错误，持续到下一次 load/save/reset；
- **「配置加载失败」持久横幅**（新增，含统一重试入口 `reloadChannels`）：承载未完成
  首次加载（`!loaded`）渠道的失败；
- **页面级 toast 恢复 2 秒瞬时**（面板层独立定时器 `toastNotice`）：只承载没有其他展示
  面的失败（启用开关、解绑），对齐基线 UX。

`closeDialog` → `resetActiveDialog` → `reset()` 清错误，关闭对话框不会把陈旧错误回放进
toast。`toggleChannelEnabled` 保留 `!loaded && !(await load())` 守卫，加载失败的渠道
不会被空配置构建的 payload 误操作。

## 根因与修复决策

根因三层：`useChannelForm` 的错误只有页面级 toast 一个出口且 2 秒自动清；骨架判定把
"失败"与"加载中"混为一谈（失败渠道永远不满足 loaded）；`loadChannels` 的重试入口不重
跑各渠道 `get_conf`。

修复决策（含检视复审后的两轮修正）：

- **错误面就近持久化**：save 失败写入对话框 status（`ChannelConfigDialog` 向 FormDialog
  传 `status`）；配置加载失败聚合为 `configurationsError` 横幅 + `reloadChannels` 统一
  重试；`errorNotice`（toast 数据源）排除已被上述两者覆盖的错误（未完成首次加载的、
  当前打开对话框的）。
- **toast 保持 2 秒瞬时**：`useChannelForm` 内 2 秒自动清错定时器移除后，错误持久化由
  就近展示位承接；页面 toast 由面板层定时器独立管理瞬时性，避免检视第一轮指出的
  "错误永久驻留/双处重复"回退。
- **骨架判定对失败豁免**：`configurationsLoading = (!controller.loaded &&
  controller.error === null) || controller.loading`——失败渠道不再把列表钉在骨架，横幅
  与列表并存。空 message 的 `Error`（`new Error('')`）在 load/save/loadChannels 三处
  失败分支统一兜底为本地化文案，避免空串被 `Boolean(message)` 过滤后彻底无提示。
- **`loadSeqRef` 请求序号守卫**：`load()` 进入时取号，成功/失败/finally 三条落地路径
  均比对序号，过期即返回、不写任何状态。动机：成功分支原来的 `setError(null)` 对并发
  load 场景冗余（后启动的 load 开始时已清），它唯一实际覆盖的是"load 落地前其他写者
  写入的错误"——恰会误抹并发操作错误；序号守卫使晚到的旧 load 失败不再污染新状态，
  同时移除成功分支清，错误面归"最新动作"所有。守卫只覆盖 load：save/persistPayload
  有 `saving` 单飞守卫，写者语义（用户动作拥有错误面）本就正确，不扩散。
- **toast 触发按失败次数事件化**：`useChannelForm` 为每次 load/persist 失败记录全局
  递增的失败序号（`failureSeq`，模块级计数器保证跨渠道全序），聚合层取「已加载、非
  当前对话框渠道」中序号最新的一条失败作为 `toastNotice`（序号+文案），
  `ChannelErrorBars` 仅在序号上升时弹 toast、只展示该条文案。演进背景：第一版按
  「聚合字符串变化」触发并放开 Set 去重——同渠道重复失败本就会重弹（persistPayload/
  load 开始时先清错，字符串在"空→写入"间循环变化），但文案面存在两个缺陷：已驻留的
  旧错误会随 join 累加成重复文案（"A和A"，检视第五轮指出，红证据
  `save rejected和save rejected`）；旧错误被排除/清除时字符串回缩还会在无新失败时
  重弹旧文案。序号单调递增一并杜绝两者。横幅侧 `configurationsError` 保留 Set 去重
  （持久展示不依赖变化触发）。
- **按拆分守护抽离组件与 hook**：仓内 `tests/settingsRefactor.test.mjs` 对
  `SettingsChannelsPanel`（≤100 行，须保持 composition root）与
  `useSettingsChannelsController`（≤450 行）有行数守护，本修复使两文件越界（137/472，
  且该守护不在新增的 `test:channels-error-feedback` 入口内）。按守护本意抽离而非上调
  阈值：面板的失败反馈条（2s toast + 两条持久横幅）抽为 `components/ChannelErrorBars.tsx`；
  控制器的跨渠道状态聚合与重试入口（`loadConfigurations`/`reloadChannels`/挂载加载、
  `toastNotice`/`configurationsError`/`configurationsLoading`）抽为
  `useChannelControllersStatus.ts`。hook 内以各渠道稳定的 load 回调数组作为依赖维持
  `loadConfigurations` 引用稳定（controllers 记录每渲染重建，不进依赖列表；React 对
  hook 依赖逐元素做 Object.is 比较、不比较数组引用，故每次渲染新建的数组本身不破坏
  记忆化），挂载加载语义不变；两文件回到 91/431 行，DOM 结构与 testid 均未变。

## 回归证据与适用范围

2026-11-26 验证（结论级）：

- 新增组件级 UT 7 用例全绿（横幅可见+重试双跑断言 channel.get 与 get_conf 各 2 次 /
  空 message 兜底释放骨架 / 对话框保存失败 status 持续 2.3s 无页顶 toast / toggle 失败
  toast 瞬时 / 晚到旧 load 失败不覆盖新成功状态 / 跨渠道同文案第二次失败仍弹 toast 且
  文案只含最新一条失败（修复前红：`save rejected和save rejected`）/ 同渠道同文案重复
  失败重弹 toast（该行为修复前即成立，用例固化为回归守卫）），
  入口 `npm run test:channels-error-feedback`；检视驱动的用例均做过旧代码红、
  修复后绿的双向验证。
- `npm run build`（tsc + vite）通过；`git diff --check` 干净；i18n zh/en 均含新增文案
  `channels.configLoadFailed`。
- `npm run test:settings-panel` 守护测试 74/75：拆分守护（面板 composition root、各文件
  行数）与 Xiaoyi 确认流等断言全绿；唯一失败 `every visible Settings control maps to an
  exact persistence field or RPC` 为上游基线自带（develop 与 releases/0.2.10 均在）——
  `settingsContract.ts:164` 含 `rea_enabled` 字段而守护白名单未同步，本 PR 变更文件不含
  settingsContract/settingsRefactor/experimental，与本修复无关，随上游收敛。
- mock 后端 + 构建版 dist + CDP 真实 UI 交互复现：基线保存失败对话框零反馈/列表永久
  骨架，修复后对话框错误条持续可见/列表保持+横幅+重试（截图见 PR 描述）。

外部链接：GitCode issue #7001（现象、复现步骤、预期/实际结果）、develop PR !10835
（基线修复）与本跟进 PR、releases/0.2.10 PR !10993（0.2.10 侧同修复）。适用范围：
develop 与 releases/0.2.10 两分支；检视指出的行为分叉（序号守卫、空 message 兜底、
toast 事件化、拆分抽离）由本跟进 PR 在 develop 收敛，本 PR 合入后两分支行为一致。
