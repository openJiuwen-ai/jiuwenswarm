---
id: F_20261120_1220_enhance-web-fetch-trafilatura
title: 增强 mcp_fetch_webpage 基于 trafilatura 的正文智能抽取与降噪能力
type: F
author: zhuxiangju
author_email: zhuxiangjv@gmail.com
modules:
  - jiuwenswarm/agents/harness
files:
  - pyproject.toml
  - uv.lock
  - jiuwenswarm/agents/harness/common/tools/web_fetch_tools.py
  - tests/unit_tests/agents/test_web_fetch_article_extraction.py
designs: []
design_note: 增强现有 mcp_fetch_webpage 工具的 HTML 正文提取与降噪，保留既有签名与 SSRF 防护链路，无独立前置设计文档。
---

# 增强 `mcp_fetch_webpage` 基于 `trafilatura` 的正文智能抽取与降噪能力

日期：2026-11-20。

## 1. 背景与决策

现有 `mcp_fetch_webpage` 已具备完整的 URL 规范化、公网 HTTPS/重定向逐跳 SSRF 校验及 401/403/429 时降级 Jina Reader 的能力，但对常规 HTML 页面仍采用正则剔除 `<script>`/`<style>` 后全量 `strip_tags` 的方式，会保留导航栏、侧边栏、页脚等大量噪音且丢失标题/表格结构。

为避免与现有工具重复造轮子，不另起新工具名，而是直接在 `web_fetch_tools.py` 中对 `mcp_fetch_webpage` 进行渐进式增强：
1. **保持工具契约 100% 兼容**：入参 schema 严格保持 `url`、`max_chars`、`timeout_seconds` 不变，完全保留 `_PermissionAwareFetch` 与 `public_https` 安全守卫；
2. **`trafilatura` 正文优先 + 自动兜底回退**：在 `pyproject.toml` 显式声明 `trafilatura>=2.0,<3` 并同步 `uv.lock`；对 HTML 响应优先使用 `trafilatura` 提取 Markdown 正文（含标题与表格），剔除导航/广告/页脚噪音；若 HTML 无明显 `<title>` 则通过独立异常处理从 metadata 回填（确保元数据异常不丢弃已提取正文）；当 `trafilatura` 返回空内容（如纯前端 SPA 壳页面）、缺包或抛出异常时，记录 `debug` 日志并无缝回退至原有的 `strip_tags` 提取逻辑；
3. **非 HTML 响应透传不变**：`text/plain`、`application/json` 等非 HTML 内容不经过 `trafilatura`，保持原有压缩空白行为。

## 2. 验证证据

- 既有安全回归与新增离线抽取单测：
  `tests/unit_tests/agents/test_web_fetch_tools_security.py` 与 `tests/unit_tests/agents/test_web_fetch_article_extraction.py` 全部通过；
- 验证覆盖：正文 Markdown 与表格保留、导航/页脚/脚本噪音剔除、SPA 空抽取回退、异常/缺包优雅降级、非 HTML 旁路、`max_chars` 截断及工具 schema 零变化。
