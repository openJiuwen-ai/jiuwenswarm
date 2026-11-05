---
id: B_20261105_1130_stale-page-cache-port-reuse
title: 端口复用后启动新应用显示上一个应用的缓存页面
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/extensions/app_market
files:
  - jiuwenswarm/extensions/app_market/app_market/static_server.py
  - jiuwenswarm/extensions/app_market/manager.py
  - jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md
designs: []
design_note: 依据页面容器契约（specs/jiuwenswarm/channels/desktop/S_01_app-market-windows.md 与 specs/jiuwenswarm/extensions/app_market/S_02_app-stop-and-page-lifecycle.md）；缓存约束已补入 S_02 不变量与应用开发技能，无独立 D。
---

# 端口复用后启动新应用显示上一个应用的缓存页面修复记录

## 现象与影响

2026-11-05 11:08，Web 端（Chrome，市场内嵌 iframe 容器）：用户先启动"轨道之家 · ISS Explorer"（端口 8080），停止后启动"微缩生活岛 · Living Island"，页面显示的仍是轨道之家。怀疑关闭或打开路径有错。

## 关联设计与当前契约

无独立 D。容器契约见 specs/jiuwenswarm/channels/desktop/S_01_app-market-windows.md；本修复新增的缓存约束已写入 S_02 不变量与应用开发技能接口表。

## 根因与修复决策

**日志证明关闭与打开路径均正常**：`gateway.log` 11:08:10 启动的确是微缩生活岛（`work_dir=D:\project\temp\living-island`，端口 8080，就绪探活通过），URL 生成正确。两个应用默认端口均为 8080、入口均为 `index.html`，停止轨道之家释放 8080 后，微缩生活岛按默认端口策略拿到同一端口——`http://127.0.0.1:8080/index.html` 两个应用完全同 URL。

**根因**：轨道之家的 `app_server.py` 静态文件走 `SimpleHTTPRequestHandler` 默认行为，仅带 `Last-Modified`、无 `Cache-Control`；Chrome 按 RFC 启发式（约 10% × (Date − Last-Modified)）把页面当"新鲜"缓存——index.html mtime 距故障约 26 小时，新鲜窗口约 2.6 小时。微缩生活岛再次占住 8080 时，浏览器直接命中缓存渲染轨道之家页面，未向新服务发起请求。市场模板 `static_server.py` 的静态文件路径同样存在该缺陷。

**修复**：

- 市场模板 `static_server.py`：`end_headers` 统一注入 `Cache-Control: no-store`（所有静态、JSON 与错误响应），`_json` 内的重复显式头移除。
- 应用开发技能接口表新增"页面缓存"行：自带 `app_server.py` 的应用必须为全部响应禁缓存，并说明端口复用会串台的原理。
- 本机轨道之家 `D:\project\空间站\scripts\app_server.py`（仓库外用户应用）已同步补 `end_headers` 注入；微缩生活岛 `app_server.py` 原有逐响应 `no-store`，无需改动。

**备选与拒绝**：给每个实例固定独立端口段避免复用——端口是稀缺资源且无法覆盖"删除应用后端口回到池中"的合法复用；改为在打开 URL 上加实例查询参数破坏缓存——污染应用对自身路径的假设且治标不治本。均拒绝。

## 回归证据与适用范围

候选：fix/app-market-stop-page-close 工作区。环境：Windows 11，仓库 `.venv`。

- `pytest tests/unit_tests/test_app_market_runtime.py -q -o addopts=""` 16 通过，新增 `test_static_pages_are_served_uncached` 断言入口 HTML 响应携带 `Cache-Control: no-store`。
- 未覆盖：真实 Chrome 下"先 A 后 B 占同端口"的端到端串台复现（依赖浏览器缓存行为，已由响应头语义与单测覆盖代理）；用户本地轨道之家应用为仓库外代码，其改动需重启该应用后人工验证。
- 重验条件：Chrome 强制刷新或清缓存后，按"启动轨道之家 → 停止 → 启动微缩生活岛"顺序操作，页面应显示微缩生活岛。

## 防错模式与资料同步

本地托管页面一律禁缓存：端口/地址会在应用间复用，任何可启发式缓存的响应都可能在新应用名下渲染旧内容。已写入 S_02 不变量与应用开发技能。本次同步：static_server.py、SKILL.md、S_02、本记录；索引经 `sync_documents.py` 刷新。

## 遗留与后续

用户本地其余存量应用若同样裸用 `SimpleHTTPRequestHandler`，仍需按技能契约补响应头。以下修订在市场入口处理已有缓存的影响。

## 检视后修订（2026-11-05）

`04e7cacf7` 增加的 `no-store` 只能作用于新响应。修订后，Web 端通过 `web_url` 使用每个应用专属的 `*.localhost` 来源。即使端口和入口路径复用，HTML 和同源子资源也不会命中其他应用的缓存。入口查询参数只能隔离入口 HTML，因此采用独立来源。旧来源的浏览器存储不会自动迁移。现行契约见 `S_02_app-stop-and-page-lifecycle`。

Windows 上以仓库 `.venv` 驱动 Chrome 156 的独立浏览器配置，使用两个应用的实际 URL 生成逻辑和一个可切换应用响应的 loopback 服务。用 iframe 预热 A 的入口 HTML 和同源 JS 后，在同一端口切换到 B。旧地址仍渲染 A 的 HTML 和 JS，而 B 的独立地址渲染 B 的 HTML 和 JS。后端 `test_static_app_start_open_stop_and_restart` 通过，验证启动与轮询返回同一 Web 入口，原服务地址仍能访问。
