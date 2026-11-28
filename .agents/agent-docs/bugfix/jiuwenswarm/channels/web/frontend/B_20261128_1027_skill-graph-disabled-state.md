---
id: B_20261128_1027_skill-graph-disabled-state
title: 技能图谱开关关闭时的页面误报
type: B
author: lin-xiaoyu
author_email: xiaoyu.lin.work@outlook.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/components/SkillGraphPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/SkillGraphTab.tsx
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/skillRetrievalStatus.ts
  - jiuwenswarm/channels/web/frontend/tests/skillGraphPanel.test.mjs
designs: []
design_note: 按现有编排开关和技能检索状态契约修复 Web 消费方。
---

# 技能图谱开关关闭时的页面误报

[Issue #7086](https://gitcode.com/openJiuwen/jiuwenswarm/issues/7086) 记录了两个场景。关闭编排时打开图谱，页面仍会主动调用 `skills.graph.get`，Core 按开关拒绝读取。开启编排并启动图谱构建时，页面同时检查独立的技能检索状态。检索关闭时 Core 返回 `disabled`，原有前端枚举将它作为非法值报错。

图谱读取、轮询及手动操作依赖已保存的编排开关。关闭时保留开关入口，空画布提示如何启用。保存后仍由原有开关流程启动增量构建或取消任务。技能检索状态接受 `disabled`，附带的索引构建继续由服务端的 `build_supported` 决定。

基线 `6da23affe` 的两个回归场景分别观察到关闭时的图谱请求和开启后的 `build_status is invalid` 提示。`npm run test:skill-graph-panel` 通过模拟 RPC 验证同一页面入口，并覆盖服务端允许索引构建、启用编排后读取失败仍展示错误等场景。验证范围为 Web 组件及其请求和反馈，使用模拟服务端响应。
