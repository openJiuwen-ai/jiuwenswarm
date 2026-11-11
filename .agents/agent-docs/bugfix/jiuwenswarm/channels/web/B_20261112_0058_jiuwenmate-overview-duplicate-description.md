---
id: B_20261112_0058_jiuwenmate-overview-duplicate-description
title: 员工中心概览编辑态重复展示能力介绍
type: B
author: chendongcai
author_email: chendongcai95@qq.com
modules:
  - jiuwenswarm/channels/web
files:
  - jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/pages/ColleagueProfilePage.tsx
  - jiuwenswarm/channels/web/frontend/tests/jiuwenmateProfile.test.mjs
designs: []
design_note: 对既有概览页信息架构的局部 UI 修复，读态员工名片摘要保持原设计（ebf035ca3 对齐口径），无独立设计。
---

# 员工中心概览编辑态重复展示能力介绍

## 现象与影响

GitCode #6444（配对 GitHub #11262，里程碑 WorkSwarm 0.2.9）：团队版 SwarmChat 专家
协作空间，员工中心 → 概览页面，点击员工名称旁铅笔进入编辑态后，IdentityEditor
表单（含"能力介绍"输入框）下方、"保存并发布"按钮之下又原样渲染一行已保存的
能力介绍文本，同屏出现两份；且该行绑定的是已保存值，与输入框实时内容脱节。
报告人认为该行多余，要求删除（其截图红框仅圈中描述行，"入职时间"行不在其中）。

影响范围仅概览页编辑态的展示层，不涉及数据与接口；读态（保存成功后自动回到
读态）下该行是名称下方员工名片摘要的一部分，为有意设计，保持不变。

## 根因

`ColleagueProfilePage.tsx` 概览页由两部分组成：`IdentityEditor`（编辑表单）与
`ProfileOverview`（概览统计块）。`ProfileOverview` 开头无条件渲染
`<p class="jiuwenmate-profile__description">{colleague.description}</p>`。该组件
早于铅笔编辑态存在（`ebf035ca3` 对齐设计时作为读态摘要加入），IdentityEditor
引入后未随编辑态隐藏，形成同屏重复。`ProfileOverview` 仅在 `section === 'overview'`
时渲染一次，无其他调用方。

## 修复决策

为 `ProfileOverview` 增加 `editing: boolean` 属性（由概览渲染处传入页面编辑态），
编辑态跳过静态描述段落；"入职时间"行、快速配置渠道、工作日志等其余内容不受影响。
读态行为与修复前完全一致。不删除读态摘要，避免超出报告范围改变信息架构。

## 回归证据

`tests/jiuwenmateProfile.test.mjs` 新增用例
"the overview hides the saved capability intro while the identity editor is open"：

- 编辑态（`initialEditing: true`）渲染概览，断言 IdentityEditor 存在且
  `jiuwenmate-profile__description` 不出现（修复前该断言失败，先红）。
- 读态渲染概览，断言 `jiuwenmate-profile__description` 仍展示已保存描述（防读态回归）。

本地 `npm run test:jiuwenmate-profile`：89 通过 0 失败（修复前 88 通过 1 失败）；
`test:jiuwenmate-presentation`（13）、`test:jiuwenmate-office`（9）、
`test:jiuwenmate-integration`（2）、`test:jiuwenmate-mode`（2）均全部通过。
