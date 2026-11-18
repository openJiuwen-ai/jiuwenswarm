---
id: F_20261116_2215_grep-code-tool
title: grep_code Cross-Platform Code Search and Pattern Matching Tool
type: F
author: zhuxiangju
author_email: zhuxiangjv@gmail.com
designs: []
design_note: "Cross-platform regex/literal code search tool for agent codebase navigation"
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/agents/harness/common/tools/grep_tools.py
  - jiuwenswarm/agents/harness/common/tools/__init__.py
  - jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py
  - jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py
  - jiuwenswarm/agents/harness/common/rails/permissions/tool_capabilities.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/toolCategory.ts
  - tests/unit_tests/agents/test_grep_tools.py
---

# grep_code Cross-Platform Code Search and Pattern Matching Tool

## 1. 背景与目标
在多智能体代码阅读、问题定位和函数调用溯源场景中，智能体通常需要精准检索关键词、类/函数声明。
现有工具仅支持读取整个文件或依赖宿主机 shell（不同操作系统语法差异大、容易出现字符集乱码或溢出）。

本特性新增 `grep_code` 工具，提供基于 Python 标准库实现的跨平台高效代码检索能力：
- 零第三方外部依赖，跨 Windows、macOS 和 Linux 行为 100% 一致；
- 结构化 Markdown 返回匹配行号、前后上下文行（带行号高亮）；
- 自动跳过二进制文件与无关依赖目录（`.git`、`node_modules`、`.venv` 等）；
- 支持正则匹配与普通字面量检索，支持文件通配符（如 `*.py`）和最大匹配上限防护；
- 严格受工作区限制，防止路径穿越；大文件防护（超过 10MB 自动跳过）；
- 完整接入系统共享工具、权限集与前端展示分类。

## 2. 关键能力
- **跨平台一致性**：使用标准库 `os.walk`、`fnmatchcase` 与 `re`，不受宿主 shell 差异影响。
- **安全与边界防护**：对路径严格基于 `get_workspace()` 进行解析与相对校验，禁止越界；自动跳过二进制与大于 10MB 文件。
- **上下文展示**：支持 `context_lines`（默认 1 行，上限 10 行），清晰还原代码段。
- **容量保护**：受 `max_matches` 限制（默认 50，上限 200），精准判断截断状态，防止模型上下文窗口被大量匹配撑爆。
- **系统接线**：完成 `interface_deep.py` 共享工具注册、`tool_capabilities.py` 权限集合定义、`toolCategory.ts` 前端映射及目录导出。

## 3. 验证与测试
- 单元测试覆盖于 `tests/unit_tests/agents/test_grep_tools.py`，覆盖字面量与正则搜索、
  上下文行提取、文件通配过滤、黑名单过滤、截断与边界匹配未截断、路径越界防护拦截、
  超大文件跳过、错误正则处理等 13 项测试，13/13 全部通过。
