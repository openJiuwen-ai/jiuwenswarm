---
id: B_20261117_1117_edition_switch_alignment
title: 个人版与团队版切换按钮对齐
type: B
author: huanghongyun
author_email: huanghongyun1@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/index.css
  - jiuwenswarm/channels/web/frontend/src/components/SessionSidebar/SessionSidebar.css
  - jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/jiuwenmate.css
designs: []
design_note: 对齐现有两种模式的共享切换按钮。
---

## 现象与根因

[Issue #6574](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6574) 中，两种模式共用按钮组件，但团队侧栏另有 12px 底部内边距。760px 及以下时，团队侧栏还单独缩窄到 56px。

## 修复与验证

两种模式的侧栏和网格共用响应式宽度，底部留白由按钮的共享样式提供。企业上下文弹层锚定侧栏边缘。

- 开发分支基线 `2cce9d32f`，使用锁定的 agent-core `2f4bedb9c`，在 macOS Chrome 双向切换。
- 1280×800 和 720×800 下，两种模式的按钮边界框分别相等。前端模式切换用例全部通过。
