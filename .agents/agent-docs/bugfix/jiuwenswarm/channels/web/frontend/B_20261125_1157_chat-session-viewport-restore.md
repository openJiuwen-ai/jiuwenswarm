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
  - jiuwenswarm/channels/web/frontend/src/features/chatTimeline/sessionViewportCache.ts
  - jiuwenswarm/channels/web/frontend/src/multi-session/sidebar/useSidebarMenu.tsx
  - jiuwenswarm/channels/web/frontend/package.json
  - jiuwenswarm/channels/web/frontend/tests/sessionViewportCache.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/sessionViewportLifecycle.test.mjs
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

### 修复

新增应用生命周期内的 `sessionViewportCache`：

- 以 session ID 为键，同时保存 `scrollTop`、离开时是否贴底和 `TimelineViewportState`，写其中一类字段不覆盖其它字段。
- 交互时间线读写共享缓存；`staticTimeline` 或无 session ID 时继续使用组件局部 Map。
- 最多保留 20 个最近写入的会话，超出后淘汰最旧项；永久删除会话时在 App 和侧边栏两个既有清理入口同步删除缓存。归档及撤销不归入永久删除清理。
- ChatPanel 卸载时保存最新位置；`lastSessionIdRef` 从 `null` 开始，确保冷挂载也进入恢复或默认滚底分支。
- 上次贴底时按返回时的当前 `scrollHeight` 重新滚底，避免离开期间内容增长后恢复过期像素位置并错误标记为用户上翻；只有上次确实上翻时才恢复保存的像素锚点。

未采用以下方案：

- 用 CSS 隐藏整个聊天面板保活：会让长会话 DOM、观察器和后台效果无意义常驻，也不能修复冷挂载分支。
- 写入 localStorage：本次只要求应用运行期间的导航一致性，无需新增持久数据和版本迁移。
- 重写虚拟滚动算法：现有 `initialMeasurementsCache` / `initialOffset` 已能恢复，不应扩大风险。

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

### 自动化测试

- `npm run test:virtual-timeline`：11 passed。新增缓存隔离、字段共存、删除、空 ID、容量淘汰、贴底增长恢复及源码生命周期契约检查。首轮临时撤掉 tracked 生产修复后为 7 passed / 3 failed，恢复后为 10 passed；新增贴底用例单独做红—绿验证，移除按当前高度贴底后为 10 passed / 1 failed，恢复后为 11 passed。
- `test:build-turn-timeline` + `test:timeline-row-state`：30 passed。
- `test:history-restore-pagination` + `test:history-pagination` + `test:history-recovery`：23 passed。
- `test:new-conversation-draft-preserve` + `test:new-conversation-entry-settings` + `test:new-conversation-previous-session`：19 passed。
- 上述非分享图相关测试合计 83 passed。
- `test:share-image` 最新单独复跑为 62 passed / 2 failed；未修改基线在匹配负载下也为 62 passed / 2 failed，均为 continuous data 请求截止用例的 `ECONNRESET`：`/share-api/jobs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/browser` 和 `/file-api/upload`。失败位置在多次运行间会变化，属于基线时序问题，不计为本次通过项，也不据此声称 64 项全部通过。
- `npx tsc --noEmit` 在候选和基线上均通过。完整 `npm run build` 在候选和未修改基线上均失败于同一 `vite:html-inline-proxy` / `index.html?html-proxy&inline-css&index=0.css` 错误，属于基线构建问题，未计为本次通过项，也未顺手修改无关 HTML。
- `git diff --check`、`git diff --cached --check` 通过。

### CI 与 AI 检视

- 首个 head `bf0645e6329396393db84e498f8a34aaf5f23336` 与后续代码 head `12ac74543aafd3d9866e6dbf056ea1581963725c` 的静态检查、禁用词、防投毒、开源合规、build、ruff 和 Issue 关联检查均通过，CLA 均通过。
- 首个 head 的 CI UT 报告为 29205 passed / 2 failed / 247 skipped。失败为 `test_inline_verified_download_sandboxes_documents` 和 `test_raw_file_document_responses_are_sandboxed_and_never_sniffed`，均断言响应不含 `Content-Security-Policy`；使用项目 `.venv` 在未修改基线复跑，两个用例同样失败，且本 PR 不修改相关 Python 代码或测试。
- 后续代码 head 的 CI UT 报告为 29206 passed / 1 failed / 247 skipped。唯一失败为 `test_watchdog_still_closes_a_silent_worker`，CI Linux / xdist 环境中出现 `_Connection` 缺少 `probe_heartbeat`；该用例使用项目 `.venv` 在未修改基线和候选代码上分别单独复跑均为 1 passed，本地未能复现 CI 失败，不能表述为“基线同样失败”。相关 supervisor 源码和测试均不在本 PR 差异中。
- 首轮 AI 检视为 Must Fix 0、Should Fix 1、Nice to Have 2。Should Fix 指出离开时贴底但内容增长后的过期像素恢复问题，后续通过保存贴底语义、按当前高度恢复以及上述行为和真实页面验证处理；Nice to Have 的测试保护力建议由新增行为测试补强。
- 后续代码 head `12ac74543aafd3d9866e6dbf056ea1581963725c` 的第二轮 AI 复检总体判定和门禁均为 PASS，Must Fix、Should Fix、Nice to Have 均为 0；首轮 CR-10921-01 判定 fixed。

## 防错模式与资料同步

滚动状态的宿主生命周期必须覆盖其所有使用路径。会话级阅读状态不能只依赖会随欢迎页或主导航卸载的组件 ref；同时保留恢复所需的像素位置与动态测量快照，并明确清理和容量边界。

本文件记录实现与验证；`.agents/agent-docs/` 索引通过 `sync_documents.py` 刷新。未新增模块 spec 或全局规则，因为本次不改变公开协议或建立新的跨模块契约。

## 遗留与后续

- 新建任务路径在已安装 0.2.9 中有漂移证据，但本组未修改源码对照恰好能恢复，因此 PR 中应准确表述为“消除该路径的测量快照丢失风险”，不声称本组源码对照在修复前必然失败。
- 基线 Vite 构建的 HTML inline-css 代理错误需独立排查，不属于本次修复范围。
- Issue：[#7043](https://gitcode.com/openJiuwen/jiuwenswarm/issues/7043)；PR：[!10921](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/10921)。后续代码 head 的 AI 复检通过；CI 因上述本地未复现的 supervisor 用例保持 `ci-failed`，未自行合并。
