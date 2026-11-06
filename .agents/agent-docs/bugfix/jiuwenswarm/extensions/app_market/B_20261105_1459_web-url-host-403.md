---
id: B_20261105_1459_web-url-host-403
title: Web 应用入口 403：静态服务未放行应用专属 localhost 主机
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/extensions/app_market
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder
files:
  - jiuwenswarm/extensions/app_market/app_market/static_server.py
  - tests/unit_tests/test_app_market_runtime.py
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md
designs: []
design_note: 依据 S_02_app-market-stop-and-page-lifecycle 的 Web 入口与来源隔离契约；本次修复使实现重新满足既有契约，无独立 D。
---

# Web 应用入口 403：静态服务未放行应用专属 localhost 主机

## 现象与影响

2026-11-05 14:30 左右，浏览器实测应用市场：4 个已上架应用中 3 个启动后卡片显示“运行中”，右侧 iframe 却是 `Error response 403 Forbidden`；仅「轨道之家」正常。前端据此仍显示“应用已启动”，用户无法区分启动成功与应用页面不可达。

Web 入口地址由市场生成，形如 `http://workswarm-app-<摘要>.localhost:<port>/index.html`。直接以该 Host 请求应用端口返回 403，以 `127.0.0.1:<port>` 请求返回 200。

## 根因

- `manager._instance_urls` 为 Web 容器下发应用专属 `*.localhost` 来源，用于隔离不同应用的浏览器存储与缓存。
- 市场内置静态托管 `static_server.py` 的 Host 校验仍只允许 `127.0.0.1:<port>` 与 `localhost:<port>`，把市场自己下发的 `web_url` 拒绝为 403。
- 由旧版应用开发技能生成的 3 个本地应用（微缩生活岛、常晴镇、黄沙镇）在各自 `scripts/app_server.py` 中复制了同一份严格 Host 校验；轨道之家未校验 Host，因此只有它能显示。
- 既有后端测试只请求 `url` 并仅断言 `web_url` 的形状，从未真正以 Web Host 请求页面，未覆盖该路径。

## 修复

- `static_server.py` 新增 `_host_allowed`：在端口一致的前提下放行 `127.0.0.1`、`localhost` 与任意 `*.localhost`，其他主机继续 403。
- 新增 `test_static_server_accepts_market_web_origin_only`：以 `web_url` 的 Host 请求入口应 200，以外部主机请求应 403。
- 应用开发技能接口表明确 Host 校验需按端口放行三种回环主机，避免新应用继续复制旧模板。
- 仓库外 3 个本地应用的 `_host_ok()` 已同步按端口放行 `*.localhost`，用于端到端复验；该改动不属于本仓库提交。

## 回归证据与适用范围

- `pytest tests/unit_tests/test_app_market_runtime.py -q -o addopts=""`：17 通过。
- `npm run test:app-market`：23 通过。
- 未覆盖：其他平台/浏览器；仓库外应用需随应用自身发版或本地同步修改。

## 遗留与后续

应用页面自身无法从跨源 iframe 读取 HTTP 状态，市场目前仍无法把“页面加载失败”转化为友好提示；若后续需要，可让后端在实例列表返回 Web 入口的探测结果，前端据此展示重试入口。
