---
id: B_20261126_1634_desktop-app-permissions
title: 桌面应用鼠标锁定与全屏权限
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules: [jiuwenswarm/channels/desktop/electron]
files: [jiuwenswarm/channels/desktop/electron/app_market_windows.cjs, tests/electron/app_market_windows.test.cjs]
designs: []
design_note: 局部修复以应用市场分端展示契约和网页版已有权限为依据。
---

# 桌面应用鼠标锁定与全屏权限

## 现象与根因

问题 #7146 中，《黄沙镇往事》在桌面版点击游戏画面后无法锁定鼠标。网页版应用容器允许鼠标锁定和全屏，Electron 应用分区安装的权限检查和请求处理器则统一拒绝权限，导致游戏发起的 `pointerLock` 请求失败。

## 修复与契约

请求和检查共用应用分区的允许规则，以 Electron 提供的实际请求 URL 和当前应用入口判定同源。独立及嵌入视图均可申请鼠标锁定，独立窗口也可申请标准全屏。嵌入视图使用面板放大，避免网页全屏切换 WorkSwarm 主窗口。其他权限沿用默认拒绝。参见[应用市场分端展示契约](../../../../../specs/jiuwenswarm/channels/desktop/app-market-browser-containers.md)。

## 回归证据

在 Windows、Node.js 24.15.0 上，以 `releases/0.2.10` 为基线运行 `node --test tests/electron/app_market_windows.test.cjs`，17 项通过。测试从应用窗口创建入口验证权限处理器，覆盖同源允许、其他来源及权限拒绝、视图切换和入口端口变更。真实游戏和已打包客户端留待联调验证。develop 基线仅核对差异。
