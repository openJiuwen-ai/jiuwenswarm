---
id: B_20261105_1500_start-completion-steals-view
title: 应用启动完成抢走用户后来切换的页面
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/tests/appMarketPanel.test.mjs
designs: []
design_note: 依据 S_01_app-market-browser-containers 与 S_02_app-market-stop-and-page-lifecycle 的“按稳定 app_id 复用页面、失败/停止不误关其他页面”契约；本次修复补充用户意图排序，无独立 D。
---

# 应用启动完成抢走用户后来切换的页面

## 现象与影响

多应用场景下，启动较慢的应用完成时会强制把右侧面板切到自己，即使完成后用户已经显式打开了另一个运行中的应用。实测步骤：

1. 轨道之家已运行并显示在面板。
2. 点击启动微缩生活岛（约 1 秒完成）。
3. 150ms 后点击轨道之家的“打开／恢复应用”。
4. 微缩生活岛启动完成后，面板仍被切到微缩生活岛。

两个应用同时启动时也存在同样问题：最终显示哪个应用取决于启动完成顺序，而不是用户最后点击的应用。

## 根因

`AppMarketPanel` 只有 `openRevision` 用于对 `openApplication` 的异步结果排序，且它只在 `openApplication` 内部递增。启动路径在 RPC 返回后才调用 `openApplication`，等于把“启动完成”当成最新一次打开请求；用户在启动期间执行的显式打开、收起或关闭没有留下可比较的意图标记，因此会被覆盖。

## 修复

- 新增 `viewIntent` 计数：点击启动、点击打开/恢复、收起面板、关闭页面时递增。
- `handleStart` 在发起启动时捕获当前意图，RPC 成功后仅当该意图仍是最新时才自动打开；否则只更新运行状态，把打开动作留给用户。
- 显式打开仍在点击时立即递增意图，保证“最后一次用户动作”优先。
- 新增前端用例 `a starting app does not replace a page the user opened later`：启动中切到另一应用后，启动完成不得创建或显示被启动应用的页面。

## 回归证据与适用范围

- `npm run test:app-market`：23 通过（含新增用例）。
- `pytest tests/unit_tests/test_app_market_runtime.py -q -o addopts=""`：17 通过。
- 未覆盖：Electron 独立窗口以 `app_id` 复用，不共享 Web 面板，本修复只影响 Web 单面板的自动切换。

## 遗留与后续

当前一次只展示一个应用页面，运行中的其他应用没有“当前已打开”标记或面板内切换器；本次未调整该交互，仅修正自动切换的归属。
