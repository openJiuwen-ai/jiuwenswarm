---
id: F_20261117_1720_fetch-docs-tool
title: 新增技术文档抓取与本地缓存工具 (fetch_docs)
type: F
author: zhuxiangju
author_email: zhuxiangjv@gmail.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/agents/harness/common/tools/fetch_docs_tools.py
  - jiuwenswarm/agents/harness/common/tools/__init__.py
  - jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py
  - jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py
  - jiuwenswarm/agents/harness/common/rails/permissions/tool_capabilities.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/toolCategory.ts
  - tests/unit_tests/agents/test_fetch_docs_tools.py
  - tests/unit_tests/agentserver/permissions/test_tool_capabilities.py
designs: []
design_note: 新增 fetch_docs 工具实现技术文档抓取、提纯与缓存，无独立前置设计文档。
---

# `fetch_docs` 技术文档抓取与轻量索引工具实施与决策归档

## 关联依据

- 设计：无独立前置设计文档（`designs: []`），按任务规格与既有工具契约落地。
- spec：遵守 `.agents/agent-docs/specs/agentserver/S_01_tool-discovery-language.md`。
- 日期：2026-11-17。

## 决策归属与关系

本记录为 `fetch_docs` 特性的首次实施归档（基于 `upstream/develop` 基线在独立分支
`feature/fetch-docs-tool` 落地），不涉及替代历史方案。

## 实际实现与开发决策

1. **零新增第三方依赖（Python 标准库 + `httpx`）**：
   - 使用标准库 `html.parser.HTMLParser`、`ipaddress`、`socket`、`hashlib` 与既有依赖
     `httpx` 提取 HTML/Markdown 文档正文、标题、代码块与表格，不引入任何新依赖。
2. **多层 SSRF 防护与同域同前缀 BFS 抓取**：
   - 对入口 URL 与每次重定向（上限 5 跳）均执行 `unsafe_public_https_url_reason` 与
     `socket.getaddrinfo` 全地址公网校验，单页流式读取上限 1 MiB。
   - 仅沿同主机与同 `path_prefix` 路径前缀执行 BFS 层级抓取（`max_depth` 0–3，
     `max_pages` 1–30）。
3. **工作区本地缓存与关键词检索**：
   - 缓存保存在 `get_agent_workspace_dir() / ".cache" / "fetch_docs"`，支持 `ttl_seconds`、
     `refresh=True` 与 `query` 关键词相关性检索。
4. **全链路五点接线**：
   - 注册 `interface_deep.py` 共享工具卡，归入 `tool_capabilities.py` 的
     `_PUBLIC_WEB_READ_TOOLS`，并登记 `tool_retrieval_toolkits.py`、`toolCategory.ts`、
     `tool_discovery_catalog.py` 与 `tools/__init__.py`。

## 与设计的偏差

无。

## 验证结论

- 离线单元测试（`httpx.MockTransport`）与五点接线测试覆盖 HTML/Markdown 转换、BFS 边界、
  缓存命中/刷新/检索、SSRF 与重定向防护、流式截断及权限分类，全部通过。
