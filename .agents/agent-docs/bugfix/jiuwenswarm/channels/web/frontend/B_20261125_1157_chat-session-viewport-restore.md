---
id: B_20261125_1157_chat-session-viewport-restore
title: 跨主导航返回会话时恢复阅读位置
type: B
author: taozhenyu
author_email: taozhenyu4@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/App.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/MessageList.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/VirtualTimeline.tsx
  - jiuwenswarm/channels/web/frontend/src/components/SessionsPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/features/chatTimeline/sessionViewportCache.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/archivedTasks/ArchivedTasksSettings.tsx
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - jiuwenswarm/channels/web/frontend/src/multi-session/sidebar/useSidebarMenu.tsx
  - jiuwenswarm/channels/web/frontend/package.json
  - jiuwenswarm/channels/web/frontend/tests/sessionViewportCache.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/sessionViewportLifecycle.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/virtualTimeline.test.mjs
designs: []
design_note: 局部体验缺陷修复，沿用现有虚拟时间线和会话滚动恢复契约，无独立设计文档。
---

# 跨主导航返回会话时恢复阅读位置修复记录

## 现象与影响

在任务 A 的会话中停留于非底部的历史阅读位置：

- A → 任务 B → A 能恢复；
- A → 新建任务 → A 或 A → 定时任务 → A，可能跳到其它位置或直接落到底部。

这是前端阅读状态生命周期不一致造成的体验缺陷，不涉及会话内容丢失、消息协议或定时任务执行。已安装 WorkSwarm 0.2.9 和上游 `develop` 均存在相应实现缺口。

真实环境证据：

- 0.2.9 桌面端中，新建任务往返后 `scrollTop` 从 5031.5 变为 5379.5，虚拟内容高度从 8190 变为 8338；定时任务往返后从 2851.5、可见 `message-6` 跳到 5379.5、可见 `message-13/14/15`。
- 未修改源码的固定视口对照中，定时任务往返前 `scrollTop=1336`、`bottomGap=1200`，返回后 `scrollTop=2536`、`bottomGap=0`。同一源码对照的新建任务路径当时也恢复到 1336，说明该路径是否漂移取决于内容高度和测量时序，不能声称每次都失败。

## 关联设计与当前契约

无独立 D。沿用现有两层恢复机制：

1. ChatPanel 按会话保存外层滚动容器 `scrollTop`，会话切换时恢复；无缓存时默认滚底。
2. VirtualTimeline 通过 `TimelineViewportState.measurements` 和 `offset` 恢复动态高度虚拟列表的测量结果与阅读位置。

原有不变量保持不变：用户上翻后不被流式更新强行拉底，原本位于底部的会话继续贴底，历史前插维持已有行身份，静态历史预览和分享导出不共享交互会话状态。

## 根因与修复决策

### 根因

- `sessionScrollTopMapRef` 属于 ChatPanel。切到定时任务会卸载整个 ChatPanel，外层位置缓存随之销毁。
- `viewportByScopeRef` 属于 ChatTimelineList。进入新建欢迎页或定时任务会卸载交互时间线，虚拟测量快照随之销毁。
- ChatPanel 重挂载时把 `lastSessionIdRef` 初始化为当前会话 ID，后续布局 effect 又只在 ID 变化时执行恢复/默认滚底，因此冷挂载可能跳过两条路径。
- ChatPanel 卸载前没有统一保存最新外层位置，快速主导航切换可能留下旧值。
- 首轮共享快照只保存前插前的 `measurements / offset`。面板卸载期间发布已请求的旧历史批次后，稳定 row key 虽不变，旧像素坐标却不再对应原阅读内容。
- 永久删除仅清除正常缓存时，ChatPanel / VirtualTimeline 随后的卸载保存会重新写入该会话；归档任务设置、SessionsPanel 和跨端删除事件也未完整执行清理。
- 初版 20 条 FIFO tombstone 淘汰最早删除 ID 后，旧视图仍可在延迟卸载时重新取得写入权限；仅扩大容量不能消除该时序边界。
- DOM `scrollTop` 已更新但 scroll 事件尚未派发时，ChatPanel 保存实时位置，VirtualTimeline 却使用尚未同步的 `virtualizer.scrollOffset` 计算锚点；父组件让出恢复后，旧锚点覆盖正确位置。
- 按会话获取令牌的 effect 在 A → 新建任务时执行 A 的清理，但共享滚动容器已替换为短欢迎页；父组件随后把 A 错记为贴底，返回时覆盖时间线先恢复的阅读位置。

### 修复

新增应用生命周期内的 `sessionViewportCache`：

- 以 session ID 为键，同时保存 `scrollTop`、离开时是否贴底和 `TimelineViewportState`，写其中一类字段不覆盖其它字段。
- 交互时间线读写共享缓存；`staticTimeline` 或无 session ID 时继续使用组件局部 Map。
- 最多保留 20 个最近写入的会话，超出后淘汰最旧项。
- ChatPanel 卸载时保存最新位置；`lastSessionIdRef` 从 `null` 开始，确保冷挂载也进入恢复或默认滚底分支。
- 非贴底快照额外保存稳定 `anchorKey` 和 `anchorOffset = offset - anchor.start`。卸载时优先读取实时 DOM `scrollTop`，再以同一坐标计算锚点，仅在无滚动容器时回退 `virtualizer.scrollOffset`。重挂载且 `scrollMargin` 就绪后，按当前 items 和测量缓存执行一次 key 锚点恢复；key 不存在时回退旧 offset，不另写行高估算算法。
- 上次贴底时按返回时的当前 `scrollHeight` 重新滚底，并跳过一次 key 锚点恢复，避免 VirtualTimeline 覆盖 ChatPanel 的最新底部位置。只有非贴底冷挂载/切 session 才让时间线锚点最终定位；`visibilitychange` 仍直接恢复外层位置。
- ChatPanel 和交互 VirtualTimeline 各自在挂载时取得写入令牌，同一会话共享可失效代次；缓存只接受有效且未释放的令牌写入。VirtualTimeline 清理仍看到旧会话 DOM，因此用自身令牌同时保存实时外层位置和时间线快照；ChatPanel 在会话切换清理中只释放令牌，不再读取已属于下一页面的共享容器。最后一个写入方释放时回收代次记录，两种卸载顺序都不阻断正常保存。
- 永久删除立即使对应代次失效并清缓存，旧视图令牌不会因后续其它会话删除而恢复写入权限，不再依赖有界 FIFO tombstone。App、侧边栏、归档任务设置、SessionsPanel 和 WebSocket `session.deleted` 继续使用统一删除入口；归档、撤销归档和失败请求不误清缓存。

未采用以下方案：

- 用 CSS 隐藏整个聊天面板保活：会让长会话 DOM、观察器和后台效果无意义常驻，也不能修复冷挂载分支。
- 写入 localStorage：本次只要求应用运行期间的导航一致性，无需新增持久数据和版本迁移。
- 重写虚拟滚动算法：继续复用 `initialMeasurementsCache / initialOffset` 和 virtualizer 的前插补偿，只补充跨卸载所需的稳定 key 锚点。

## 回归证据与适用范围

候选基线为 `upstream/develop` `74b66149001742611c7145dcfd584c421183bdc9`，独立分支 `fix/chat-session-viewport-restore`。

### 真实页面前后对照

同一 Playwright Chrome 客户端、1440×900 视口、同一批真实会话、同一 6173 源码服务。先等待历史高度稳定，再从底部上移 1200px，避免历史分页影响测量。

| 路径 | 修复前 | 修复后 |
| --- | --- | --- |
| A → B → A | `top=1336`、`bottomGap=1200` | 数值一致，返回前后可见 key 完全一致 |
| A → 新建任务 → A | 本组对照恢复 `top=1336` | `top=1336`，可见 key 完全一致 |
| A → 定时任务 → A | `top=2536`、`bottomGap=0`，直接落到底部 | `top=1336`、`bottomGap=1200`，可见 key 完全一致 |

修复后补充冷启动和底部路径：冷打开、底部会话经新建任务返回、底部会话经定时任务返回，三者均为 `top=2536`、`bottomGap=0`。

针对首轮 AI 检视指出的动态增长风险，在 Cron 面板显示期间注入仅影响布局的临时 1000px padding，模拟离开期间内容高度变化，不修改任务数据：

- 离开时贴底：`top=2536`、`height=3003` → 返回后 `top=3536`、`height=4003`、`bottomGap=0`，继续贴在新内容末尾。
- 离开时上翻：返回后仍为 `top=1336`，`bottomGap` 随新增高度从 1200 变为 2200，可见消息 key 不变，没有被强行拉底。

页面 `pageerror` 均为空。结构化结果见工作目录 `viewport_before_metrics.json`、`viewport_after_metrics.json`；`viewport_after_bf0645_metrics.json` 单独保留首个 head 的复验批次，不与最新结果混用。修复前后聊天区域截图一并保存。

最终边界复验再次执行 A → B → A、A → 新建任务 → A、A → 定时任务 → A，三者均恢复 `top=1336`、`bottomGap=1200`，返回前后可见 key 完全一致；贴底增长和上翻增长结果分别为 `bottomGap=0` 与 `top=1336` 不变。结果保存在 `viewport_after_boundary_metrics.json`。该页面验证使用布局增长模拟卸载期间内容变化；多批旧历史前插、变高行、非零行内偏移及 scrollMargin 由 JSDOM 行为测试覆盖，不把布局增长描述成真实历史前插联调。

本轮 P2 / P3 使用实际 VirtualTimeline 的 JSDOM 回归和缓存生命周期测试验证：实时 DOM 从 2560 移至 3200 且未派发 scroll 事件时，卸载与重挂载均保持 3200；双写入方在删除后处理 20 个其它删除，再按两种顺序卸载，两类快照均不复活。未把跨端批量删除的真实触发时序表述为已联调。

会话切换覆盖修复后，真实页面再次验证非贴底 A → 新建任务 → A：离开前与返回后均为 `scrollTop=1643`、`bottomGap=1200`、`scrollHeight=3371`、`clientHeight=528`，可见行键 `message-4 / message-5 / turn-summary-message-7 / message-6 / message-7` 及顺序完全一致；继续等待后仍未拉底。切换过程中没有新增控制台错误。初次页面加载出现一条 `ResizeObserver loop completed with undelivered notifications`，没有证据将其归因于本次差异，也不计为导航新增错误。

### 自动化测试

- 最终 `npm run test:virtual-timeline`：25 passed。覆盖缓存隔离、字段共存、空 ID、正常容量淘汰、稳定 key 重复前插、变高行、行内偏移、非零 scrollMargin、key 丢失回退、贴底与锚点互斥、实时 DOM 卸载快照、双写入方正常释放与重挂载、删除代次失效，以及全部永久删除入口和归档保留的源码契约。
- 其中 6 项关键生命周期测试直接挂载真实 ChatPanel / MessageList / VirtualTimeline，使用实际 store 和共享缓存恢复路径，不在夹具中复制生产恢复或清理逻辑：覆盖非贴底 A → 新建任务 → A、返回后下一次 scroll 前收到新消息、原本贴底的 A 继续跟随增长、无时间线锚点时的冷挂载外层恢复、历史前插后的冷挂载锚点恢复、P2 即时 DOM 滚动，以及 P3 永久删除后的延迟卸载。关键生命周期断言不再依赖源码正则，仅删除入口与归档保留分支继续使用源码契约。
- 红—绿证据：P2 新用例修复前为 19 passed / 1 failed（实际 2560、预期 3200），修复后通过；P3 临时取消删除代次失效后为 19 passed / 2 failed，恢复后最终为 21 passed。会话切换覆盖的回归契约修复前同样为 19 passed / 2 failed，最小调整后恢复为 21 passed。早期前插与 tombstone 用例也分别做过红—绿验证，真实页面曾发现贴底被 key 锚点覆盖为 `bottomGap=1000`，补互斥修复后恢复为 0。
- 新增真实组件测试也完成有效红—绿：临时在 ChatPanel 父级清理中恢复欢迎页视口写入后，非贴底用例实际为 `top=3536`、`height=4003`、`bottomGap=0`，预期为 `top=1336`、`bottomGap=2200`；移除缺陷变体后最终 25 passed。返回恢复及新消息到达期间禁止同步 scroll 事件，避免共享缓存被提前纠正而形成假绿。
- 补充覆盖缺口的针对性红—绿：冷挂载初始化缺陷变体使外层-only 快照恢复为 `top=2536`、预期 1336；断开 MessageList 的 `initialState` 接线后，历史前插后的阅读位置不再补偿，两个新增用例均按预期失败。恢复生产实现后通过。
- 最终复核还修正了滚动容器 `offsetHeight=0` 造成的空可见 key 假绿，并以非估算行高验证真实 measurements；通过初始化真实 authStore 隔离登录状态请求，消除 60 秒退避定时器和网络依赖，最终视口套件耗时约 9.3 秒。
- `test:build-turn-timeline` + `test:timeline-row-state`：30 passed。
- `test:history-restore-pagination` + `test:history-pagination` + `test:history-recovery`：23 passed。
- `test:new-conversation-draft-preserve` + `test:new-conversation-entry-settings` + `test:new-conversation-previous-session`：19 passed。
- `test:front-query-responses`：40 passed；`test:archived-task-grouping`：7 passed；`webSocketStreamBoundary.test.mjs`：15 passed。该套件保留既有 React `act(...)` 警告和模拟离线 TTS 日志，但退出码为 0、无失败项。
- 上述本轮重跑测试合计 159 passed。生产代码本轮未变，既有 `npx tsc --noEmit` 通过记录不变。
- `test:share-image` 历史复跑为 62 passed / 2 failed；未修改基线在匹配负载下也为 62 passed / 2 failed，均为 continuous data 请求截止用例的 `ECONNRESET`。本轮未重跑该分享图套件，不据此声称 64 项全部通过。
- 完整 `npm run build` 历史验证在候选和未修改基线上均失败于同一 `vite:html-inline-proxy` / `index.html?html-proxy&inline-css&index=0.css` 错误，属于基线构建问题，未计为本次通过项，也未顺手修改无关 HTML。
- `git diff --check`、`git diff --cached --check` 通过。

### CI 与 AI 检视

- 首个 head `bf0645e6329396393db84e498f8a34aaf5f23336` 与后续代码 head `12ac74543aafd3d9866e6dbf056ea1581963725c` 的静态检查、禁用词、防投毒、开源合规、build、ruff 和 Issue 关联检查均通过，CLA 均通过。
- 首个 head 的 CI UT 报告为 29205 passed / 2 failed / 247 skipped。失败为 `test_inline_verified_download_sandboxes_documents` 和 `test_raw_file_document_responses_are_sandboxed_and_never_sniffed`，均断言响应不含 `Content-Security-Policy`；使用项目 `.venv` 在未修改基线复跑，两个用例同样失败，且本 PR 不修改相关 Python 代码或测试。
- 后续代码 head 的 CI UT 报告为 29206 passed / 1 failed / 247 skipped。唯一失败为 `test_watchdog_still_closes_a_silent_worker`，CI Linux / xdist 环境中出现 `_Connection` 缺少 `probe_heartbeat`；该用例使用项目 `.venv` 在未修改基线和候选代码上分别单独复跑均为 1 passed，本地未能复现 CI 失败，不能表述为“基线同样失败”。相关 supervisor 源码和测试均不在本 PR 差异中。
- 首轮 AI 检视为 Must Fix 0、Should Fix 1、Nice to Have 2。Should Fix 指出离开时贴底但内容增长后的过期像素恢复问题，后续通过保存贴底语义、按当前高度恢复以及上述行为和真实页面验证处理；Nice to Have 的测试保护力建议由新增行为测试补强。
- 后续代码 head `12ac74543aafd3d9866e6dbf056ea1581963725c` 的第二轮 AI 复检总体判定和门禁均为 PASS，Must Fix、Should Fix、Nice to Have 均为 0；首轮 CR-10921-01 判定 fixed。这些结论只适用于对应历史 head。
- 单提交 head `a8fd0c14db7c000a1064586bfb920467eaea4651` 的 AI 服务曾经失败，后续复检为 PASS，Must Fix / Should Fix 为 0、Nice to Have 为 3。
- head `802eb11bd8936ece97977b5605576334c54e9972` 的 AI 总体 / 门禁为 PASS，Must Fix / Should Fix 为 0、Nice to Have 为 3。其 CI 静态检查、禁用词、防投毒、开源合规、build、ruff、Issue 关联及 CLA 均通过；2026-11-26 13:45 的流水线表格将 UT 标为 FAILED，但对应 HTML 报告显示 29387 passed / 0 failed / 250 skipped。现有证据不能解释该状态差异，因此不表述为 CI 全绿，也不归因于具体测试。本轮新 head 仍需重新检查。

## 防错模式与资料同步

滚动状态的宿主生命周期必须覆盖其所有使用路径。会话级阅读状态不能只依赖会随欢迎页或主导航卸载的组件 ref；动态内容除了保存测量快照，还要用实时 DOM 坐标和稳定 key 表达阅读锚点，并明确贴底优先级。共享 DOM 已切换到下一页面后，不能再为上一会话保存视口；应由仍看到旧 DOM 的清理方完成保存。删除保护必须绑定写入方的挂载代次，使旧视图在删除后永久失去写入权限，而不是依赖可被淘汰的 ID 集合。

本文件记录实现与验证；`.agents/agent-docs/` 索引通过 `sync_documents.py` 刷新。未新增模块 spec 或全局规则，因为本次不改变公开协议或建立新的跨模块契约。

## 遗留与后续

- 新建任务路径在已安装 0.2.9 中有漂移证据，但本组未修改源码对照恰好能恢复，因此 PR 中应准确表述为“消除该路径的测量快照丢失风险”，不声称本组源码对照在修复前必然失败。
- 基线 Vite 构建的 HTML inline-css 代理错误需独立排查，不属于本次修复范围。
- Issue：[#7043](https://gitcode.com/openJiuwen/jiuwenswarm/issues/7043)；PR：[!10921](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/10921)。历史 head 的 CI / AI 结论已按上述范围保留；本轮新 head 的检查需重新完成，不自行合并。
