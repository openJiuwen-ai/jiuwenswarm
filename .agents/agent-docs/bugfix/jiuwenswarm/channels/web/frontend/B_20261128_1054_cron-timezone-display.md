---
id: B_20261128_1054_cron-timezone-display
title: 定时任务未来执行计划与执行记录按任务时区展示
type: B
author: liujiayu75
author_email: liujiayu29@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/components/CronPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/CronPanel/cronTimeFormat.ts
  - jiuwenswarm/channels/web/frontend/tests/cronTimeFormat.test.mjs
  - jiuwenswarm/channels/web/frontend/package.json
designs: []
design_note: 仅修正前端时间展示口径，复用 cron 接口返回的绝对时刻与 job.timezone，不改变后端接口或调度契约。
---

# 定时任务未来执行计划与执行记录按任务时区展示

## 现象与影响

定时任务列表的「计划于」列按任务时区展示，但同一行的「更多 → 未来执行计划」弹层（代码内命名为 `preview`，RPC 为 `cron.job.preview`）把下一次执行时间转换成浏览器本地时间，且没有时区标注。

实测任务时区 `America/Los_Angeles`、计划每天 03:00，浏览器时区 `Asia/Shanghai`：

- 列表显示「每天 03:00」「America/Los_Angeles」；
- 「未来执行计划」显示 `2026/11/28 19:00:00`（03:00 PST = 11:00 UTC = 19:00 UTC+8）。

同一页面出现两个相差 16 小时的时间读法，用户无法判断任务究竟按哪个时间执行，可能反复修改计划；跨时区协作时，同一任务在不同成员浏览器上显示也不同。「执行记录」复用同一格式化函数，同样受影响。

## 关联设计与当前契约

后端 `cron.job.preview` 已经按 `job.timezone` 计算下一次触发，返回带偏移的 ISO 字符串（例如 `2026-11-28T03:30:00-05:00`）；`wake_at`/`push_at` 都是绝对时刻。前端只负责展示，不需要也不应重新计算触发时刻。

列表「计划于」列来自 cron 表达式字段，天然是任务时区；「未来执行计划」「执行记录」应与其保持同一口径。Issue #3174 曾描述同类现象，但该 issue 因 0.0.28r 关闭时区编辑功能而结单；当前版本时区编辑已重新开放，缺陷复现。

## 根因与修复决策

`CronPanel` 中的 `formatRunTime` 使用 `new Date(ms).toLocaleString()` 且不传 `timeZone`/`locale`，因此按浏览器时区和浏览器语言渲染；`formatPreviewTime` 直接复用它。结果是任务时区的绝对时刻被转换成浏览器本地时间，且没有任何标注。

修复决策：

- 抽出纯函数 `formatCronRunTime` / `formatCronPreviewTime`（`CronPanel/cronTimeFormat.ts`），统一处理秒/毫秒、ISO 字符串、空值与非法值；
- 传入 `job.timezone` 与 `i18n.language`，用 `Intl.DateTimeFormat` 按任务时区格式化，并在末尾追加 `(IANA 时区名)`；
- 「未来执行计划」与「执行记录」共用同一函数，保持两处口径一致；
- 采用 `hourCycle: 'h23'` 而非 `hour12: false`，避免部分引擎在午夜输出 `24:00:00`，与同目录 `scheduleConvert.ts` 的既有口径一致；
- 时区或 locale 非法时回退到原有本地时区渲染且不追加错误标签，避免新增崩溃路径。

## 拒绝的方案

- 不在前端重新换算触发时刻：绝对时刻由后端给出，前端二次换算会引入夏令时与边界错误。
- 不保留无标注的浏览器本地时间：这正是缺陷本身，跨时区时无法解释。
- 不只改「未来执行计划」：两处共用格式化函数，只改一处会让同一菜单下的时间口径继续分裂。
- 不为时区名新增 i18n 文案：IANA 名本身是稳定标识，直接追加即可，避免中英文维护两套。

## 回归证据与适用范围

新增 `tests/cronTimeFormat.test.mjs`，覆盖：任务时区 + 时区名、秒/毫秒一致、空值/0/非法值返回 null、未知时区不抛错且不追加错误标签、午夜 `00:00:00` 不被渲染为 `24:00:00`（zh-CN 与 en-US）、非法时间原样返回。

```text
cd jiuwenswarm/channels/web/frontend
npm run test:cron-time-format
6 passed
```

在本地 dev 页面用真实任务（`America/Los_Angeles`、每天 03:00、浏览器 `Asia/Shanghai`）验证：

- 修复前：`第 1 次：2026/11/28 19:00:00`
- 修复后：`第 1 次：2026/11/28 03:00:00 (America/Los_Angeles)`

「执行记录」使用同一函数，未单独构造有执行记录的样本；按共用实现，其输出与预览一致。未运行前端构建与全量静态检查（仓库协作约束禁止运行格式化/静态检查工具），也未在企业版 HTTP 传输下复验；本次改动仅涉及前端展示层，不区分传输方式。
