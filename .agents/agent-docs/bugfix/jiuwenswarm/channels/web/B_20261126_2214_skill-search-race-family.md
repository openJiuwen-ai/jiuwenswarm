---
id: B_20261126_2214_skill-search-race-family
title: 在线技能搜索竞态与预览反馈定时器修复
type: B
author: gunner_beat
author_email: gunner_heart@163.com
modules:
  - jiuwenswarm/channels/web
files:
  - jiuwenswarm/channels/web/frontend/src/features/ClawHubSearchModal/index.tsx
  - jiuwenswarm/channels/web/frontend/src/features/SkillNetSearchModal/index.tsx
  - jiuwenswarm/channels/web/frontend/src/features/TeamSkillsHubModal/index.tsx
  - jiuwenswarm/channels/web/frontend/src/features/EnterpriseSkillSourcePanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ui/FilePreviewPanel/FilePreviewContent.tsx
  - jiuwenswarm/channels/web/frontend/scripts/test-skill-search-races.mjs
  - jiuwenswarm/channels/web/frontend/tests/clawHubSearchModal.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/skillSourceModalsEmbedded.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/enterpriseSourcePanel.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/filePreviewContent.test.mjs
designs: []
design_note: 对搜索/预览既有交互契约的局部修复（晚到响应丢弃与定时器互斥），无跨模块契约变化，无独立设计。
---

# 在线技能搜索竞态与预览反馈定时器修复

## 现象与根因

基线为 develop 的 b1d8dba7f（组件已被应用市场迁移挪至 `src/features/`，核验以新位置为准）。六处相互独立的前端缺陷：

1. **四处在线搜索无晚到守卫**（`ClawHubSearchModal`、`SkillNetSearchModal`、
   `TeamSkillsHubModal` 的 `handleSearch` 与 `EnterpriseSkillSourcePanel` 的 `runSearch`）：
   均为"发请求 → await → 无条件 setState"。先搜 A（慢）再搜 B（快），B 落地后 A 的
   响应晚到把结果区覆盖回 A。三个 modal 的 embedded 自动搜索 effect
   （`externalSearchQuery` 变化驱动，effect 内自发请求，与 `handleSearch`
   分离）是同实现的程序化入口，同样无守卫；`EnterpriseSkillSourcePanel`
   的失败回退缓存分支也依赖旧闭包无守卫。
2. **Enter 绕过 loading 禁用**：四处搜索输入框 `onKeyDown` 直接调搜索函数，
   loading 时按钮 `disabled` 但 Enter 仍可再发请求，形成并发搜索。
3. **消息定时器无互斥**（`ClawHubSearchModal` `showMessage`）：每次消息新起 3s
   定时器不清旧，3s 内第二条消息被第一条的定时器提前清掉。
   （`TeamSkillsHubModal` 同款已在上游修复，本批对齐其模式。）
4. **复制反馈定时器无互斥**（`FilePreviewContent` `handleCopy`）：每次复制新起
   1.6s 定时器不清旧，连续复制时新反馈被上一个定时器提前归 idle。
5. **只读文件加载无晚到守卫**（`ReadOnlyFileModal`）：加载 effect 无请求序号，
   deps（`filePath`/`fileEncoding`）一变立即重发，慢的第一响应后到覆盖新内容，
   `finally` 还把进行中请求的 loading 提前熄掉。（经全仓核验该组件无任何消费方
   ——死组件，缺陷不可达，本批不修，见"可达性说明"。）

## 当前契约与修复决策

接线为"latest-wins 序号守卫 + 单槽定时器"两个既有模式：

- 搜索函数开头 `const seq = ++searchSeqRef.current`，await 后所有 setState
  （含失败消息、Enterprise 的缓存回退）先比对 `seq !== searchSeqRef.current` 再写；
  晚到响应静默丢弃。三个 modal 的 embedded 自动搜索 effect 在 effect 内取同一
  `searchSeqRef` 序号，await 后与 catch 开头同样先比对再写。手动入口的 Enter 加
  `loadState !== "loading"` 条件与按钮 disabled 语义对齐；程序化入口（embedded
  effect、来源切换 effect、分页）不短路，靠序号守卫保证最新赢。
- `showMessage`/`handleCopy` 改单槽定时器：触发前清旧，卸载时清理
  （与 `TeamSkillsHubModal` 既有的 `messageTimerRef` 模式一致）。

## 拒绝的方案

- 守卫抽公共纯模块（如批间的 `SequenceGuard`）：`SkillNetSearchModal` 已有
  `evaluateSeqRef` 局部序号、`TeamSkillsHubModal` 已有 `messageTimerRef` 单槽定时器
  先例，四处沿用组件内局部模式 diff 最小且与现状风格一致；守卫语句极薄，
  抽模块的测试价值不如组件行为测试。
- loading 短路放进搜索函数本体：会误拦程序化入口——来源切换 effect 在旧搜索
  loading 中发起的新搜索会被吞掉，列表停在旧来源结果。短路与守卫分层：
  手动入口短路（禁用语义等价化），程序化入口只靠守卫。
- 用 AbortController 取消旧请求：`webRequest`/`gatewayFetch` 的取消语义需要
  传播到传输层，守卫丢弃已满足"晚到不写状态"，引入取消属于扩大改动面。

## 回归证据与适用范围

环境为 Windows、Node 24、前端目录 npm ci 后执行（未运行格式化或静态检查）：

```text
npm run test:skill-search-races  # 8 passed（红绿双向：首轮 4 个用例接线前全红，
                                 # 红因恰为上述缺陷的基线行为复现；检视补强
                                 # 4 例，摘守卫复验亦红，见下）
npm run build                    # tsc + vite 通过
npm run test:skill-marketplace   # 回归通过（6 passed）
npm run test:skillpack-install   # 回归通过（11 passed）
```

`npm run test:skill-install-toast` 存在 1 个与本次改动无关的失败
（"install success messages prefer the marketplace display name"——源码字符串
断言要求 `EnterpriseSkillSourcePanel` 含 display_name 偏好回退行，该行在基线
b1d8dba7f 就已不存在，属应用市场迁移（20b5193f9）后测试与实现脱节，本批未触及
该文案）。

组件级测试（esbuild bundle + jsdom + node:test，webRequest/writeClipboard 经
esbuild 插桩换为可编程桩；`TeamSkillsHubModal` 的依赖链拉入 `webClient` 相关
具名导出，桩补齐 inert 值）共 8 个用例：loading 中 Enter 不再触发第二次搜索、
ClawHub embedded 连续搜索晚到旧响应不覆盖、连续两条错误消息不被旧定时器提前清、
连续复制反馈不被旧定时器提前清；检视补强 4 例——`SkillNetSearchModal`/
`TeamSkillsHubModal` 的 embedded 自动搜索晚到旧响应不覆盖、
`EnterpriseSkillSourcePanel` 晚到成功不回写与晚到失败时 searchCache 旧快照
回退被序号丢弃。补强用例均做了摘守卫复验（stash 源码 / 临时删除守卫行后用例
转红，恢复后回绿）；Enterprise 两例直接覆盖唯一可达入口的守卫与缓存回退
分支。jsdom 的 `window.setTimeout` 在各测试文件统一显式桥接到全局，node:test
mock timers 才能推进组件内经 `window.setTimeout` 起的消息/toast 定时器
（jsdom 内部碰巧委托全局实现，桥接把该前提显式化，各测试文件口径一致）。

CDP 复现对照（本地 mock WS 后端，`skills.source.search` 对关键词 alpha 注入 4s
延迟，企业版技能广场）：基线（b1d8dba7f 构建）先搜 alpha 再改搜 beta，beta 落地
后晚到的 alpha 响应把结果区覆盖回"慢响应技能 Alpha"；修复版结果保持"快响应技能
Beta"。断言与截图在案，复现环境维护在仓库外。

可达性说明：四个搜索入口中仅 `EnterpriseSkillSourcePanel`（企业版技能广场，
externalSearchQuery 防抖驱动）在当前 develop 有打开路径；`ClawHubSearchModal`/
`SkillNetSearchModal`/`TeamSkillsHubModal` 在 SkillPanel 中已挂载但应用市场迁移
（20b5193f9）后无打开调用点（`open` 恒为 false），属过渡态死入口——守卫按同构
加固保留，入口恢复即生效，行为零风险。原候选 B-L11（`ReadOnlyFileModal` 加载
守卫）经全仓核验无任何消费方（死组件），按 LogsPanel 先例不修，本批未改动该文件。

适用范围：四个在线搜索入口、只读文件加载、复制反馈定时器；不改变
`skills.clawhub.search`/`skills.skillnet.search`/`skills.teamskillshub.search`/
`skills.source.search`/`file-api/file-content` 协议与后端行为。

## 资料同步与遗留

无用户文档触及（交互修复，不改变用法说明）。遗留：四处搜索的守卫是组件各自
持有的序号，若后续把在线搜索收敛到统一 hook，守卫可合并；
`EnterpriseSkillSourcePanel` 的 `searchCache`（缓存快照回退）依赖 `Date.now()`
新鲜度判断，未纳入本批守卫语义，行为不变。
