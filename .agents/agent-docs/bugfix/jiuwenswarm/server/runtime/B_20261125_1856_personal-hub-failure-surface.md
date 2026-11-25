---
id: B_20261125_1856_personal-hub-failure-surface
title: 个人版连接器广场 Hub 目录失败被静默吞成空结果
type: B
author: CharlesWang2030
author_email: 93426525@qq.com
designs: []
design_note: 依据个人/企业双路径源码对比与既有单测契约定位，无独立设计文档；修复对齐同函数族企业版既有语义（失败显式 raise），不引入新契约。
modules:
  - jiuwenswarm/server/runtime/mcp
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/server/runtime/mcp/marketplace.py
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketplacePage.tsx
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - tests/unit_tests/agentserver/mcp/test_mcp_hub_marketplace.py
  - tests/unit_tests/agentserver/mcp/test_command_mcp_handler.py
---

# 个人版连接器广场 Hub 目录失败被静默吞成空结果

## 现象

个人版（单机）部署的连接器广场，Hub 目录拉取失败（断网、Hub 维护、鉴权
过期、分页无进展、超 100 页）时，用户在搜索框输入关键词得到空结果，无任何
错误提示、无降级标识、无重试入口——与「确实没有这个连接器」表现完全一致。
刷新后同样静默；网络恢复前用户无从判断是故障。

对照面证明这是失败被吞而非设计：

- 同一 Hub 故障在**应用市场页**明确报「无法访问 Hub」
  （`extensions/app_market/manager.py` `_hub_call` → `hub_unavailable`）；
- **企业版**连接器广场同一失败面后端显式 `logger.exception + raise`
  （`marketplace.py` `list_mcps_from_enterprise`，注释原文
  "Do not mask catalogue failures as an empty success"）；
- 连接器页**无搜索词**时走 `prefer_cache` 缓存路径，响应带 cache 元数据，
  前端 `CatalogCacheNotice` 可提示——唯独搜索路径（高频操作）无任何提示。

## 根因

**一句话**：个人版 `list_mcps_with_hub` 的 Hub 拉取分支对 `_all_remote`
的任何异常执行裸 `except Exception: return list(cards.values())`——无日志、
无错误标记，把目录故障伪装成「市场只有本地项」的成功结果，短路上游
`mcp.list` 处理器本就完整的 `ok=false` 错误通道。

链路：

1. 前端 `connectorApi.ts` `preferCache: !query`——有搜索词时请求不带
   `cache_mode`，必走后端 `_all_remote` 直连 Hub；
2. Hub 不可达 → `_all_remote` 抛异常（网络 / `HubProtocolError` / 分页
   防御 `ValueError`）；
3. `marketplace.py:655` 附近裸 except 吞掉异常，返回本地卡片（按搜索词
   过滤后通常为空）；
4. `mcp.list` 处理器（`agent_ws_server.py` / `handlers/mcp_hub.py`）正常
   路径返回 `ok=true`；其 `except → ok=false + MCP_INTERNAL` 通道被短路；
5. 前端 `MarketplacePage.tsx` 搜索 catch 又把（本不会发生的）错误直接置
   `setMarketConnectorResults([])`，双保险静默。

`_all_remote` 主动 raise 的分页防御 `ValueError`（无进展 / 超 100 页）是
数据源异常信号，也被一并吞掉，日志侧同样无从排查（无 `logger.exception`）。

既有单测 `test_list_falls_back_to_local_when_hub_is_unavailable` 钉住的
正是这个静默回退行为——即缺陷本身被当作契约固化，佐证是实现遗漏。

## 修复

采纳与企业版对齐的语义（失败显式上报，不伪装空成功）：

后端 `marketplace.py`：

- `list_mcps_with_hub` 个人版分支：失败时 `logger.exception` 后 **raise**
  （注释与企业版同款），由 `mcp.list` 处理器按既有通道返回
  `ok=false + MCP_INTERNAL`，异常消息（HubProtocolError 的
  「无法连接 Team Skills Hub…请检查网络后重试」等）直达前端；
- `_all_remote`：每页拉取失败 `logger.warning(exc_info=True)`；分页无
  进展 / 超 100 页的 `ValueError` 在 raise 前单独 `logger.warning`——
  数据源信号与传输失败在日志可区分。

前端 `MarketplacePage.tsx` + i18n：

- 搜索请求 catch：保留空结果降级，但**同时弹出错误 Toast**（复用
  `CONNECTOR_ERROR_TOAST_ID` 去重通道），文案走新增
  `connectorMarket.errors.hubSearchFailed` 键（zh「连接器广场暂时不可用，
  请稍后重试」/ en 同义），有后端错误消息时拼接展示；
- 仅 MCP 搜索路径弹提示；插件搜索路径不在本缺陷范围，行为不变。

顺带修正：`_EmptyHub` 测试桩的过时方法名 `search_mcp` → `search_assets`
（协议方法名），旧静默 except 连这个 `AttributeError` 都在掩盖。

## 兼容性

- 行为变化（预期内）：个人版 Hub 故障时 `mcp.list` 从 `ok=true`（本地
  子集）变为 `ok=false`。全仓消费点仅两处 `mcp.list` 处理器，均有完整
  错误通道；前端搜索路径本次同步加提示，无搜索词的缓存路径
  （`prefer_cache`）不受影响；
- 企业版路径、`local` filter、缓存路径行为不变（回归测试钉住）。

## 验证

- `uv run pytest tests/unit_tests/agentserver/mcp/test_mcp_hub_marketplace.py
  tests/unit_tests/agentserver/mcp/test_command_mcp_handler.py`：63 passed，
  含新增 5 个用例（静默回退契约改钉 raise、搜索路径失败必 raise、分页
  ValueError 可区分、正常 Hub 合并回归、处理器层面 ok=false + MCP_INTERNAL）；
- `uv run pytest tests/unit_tests/agentserver/mcp`：536 passed / 3 skipped /
  1 failed（`test_rejects_symlinked_declared_path`，Windows 符号链接特权
  WinError 1314，与本次改动无关，基线同样失败）；
- 缓存路径回归：`tests/test_hub_catalog_cache.py` +
  `tests/unit_tests/server/runtime/test_equipment_hub_catalog.py` 25 passed；
- 前端：`npx tsc --noEmit` 零错误；`test:market-visual-contract` /
  `test:pending-connector-flow` / `test:connector-store-uninstall-guard` /
  `test:connector-store-silent-refresh` 全绿；locale JSON 解析通过；
- 未完成项：未做真实断网环境下的 UI 手工验证（RPC 级语义已由处理器
  单测覆盖）。
