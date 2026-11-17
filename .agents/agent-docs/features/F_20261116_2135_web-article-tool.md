---
id: F_20261116_2135_web-article-tool
title: 补齐智能网页长文提取与降噪工具 (read_web_article)
type: F
author: zhuxiangju
author_email: zhuxiangjv@gmail.com
modules:
  - jiuwenswarm/agents/harness/common/tools
  - tests/unit_tests/agents
files:
  - jiuwenswarm/agents/harness/common/tools/web_article_tools.py
  - jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py
  - jiuwenswarm/agents/harness/common/tools/__init__.py
  - tests/unit_tests/agents/test_web_article_tools.py
designs: []
design_note: 接入内部安全守卫与 trafilatura 的智能长文提取落地。
---

# 补齐智能网页长文提取与降噪工具 (read_web_article)

日期：2026-11-16。

## 背景与实际决策

当智能体需要阅读技术博客、新闻或官方文档网页时，直接请求 HTML 包含大量广告、导航、脚本及
页脚噪音，消耗巨额上下文 Token。

决策如下：
1. **新建 `web_article_tools.py` (`read_web_article`)**：
   - 基于 `trafilatura`（openjiuwen 原生依赖）智能清洗网页，提取正文、作者、日期及表格为
     Markdown；
   - 相比原生抓取减少达 90% 冗余 Token；
   - 支持 `max_chars` 截断保护与客户端渲染（SPA）降级提示，降级引导对齐仓内真实工具
     `mcp_fetch_webpage`；
   - 支持可选 `timeout_seconds` 超时配置（1~60 秒，默认 15 秒）。
2. **网络安全与合规治理**：
   - 对所有 URL（包括 `http://` 与 `https://`）均无死角执行 `_check_fetch_url` 安全审查；
   - `fetch_response` 强制开启 `public_https=True`，通过请求链路自动对多跳 302 重定向
     目标逐跳校验，彻底杜绝内网 IP 与云元数据服务（169.254.169.254）的 SSRF 逃逸；
   - 复用 `decode_response_text` 统合字符集解码，提升国际化网页编码兼容性；
   - 严格遵循开源合规版权声明与全文件单行字符 <= 100 规范。
3. **注册发现与导出**：
   - 在 `tool_discovery_catalog.py` 补充中英文检索项；
   - 在 `tools/__init__.py` 中按需延迟导出。

## 验证结论

- 测试套件：`tests/unit_tests/agents/test_web_article_tools.py`
- 执行结果：7 passed in 9.70s (100%)
- 代码规范：`ruff check` 全部通过，全文件单行字符 <= 100。
