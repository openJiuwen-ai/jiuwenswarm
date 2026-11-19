---
id: F_20261116_2130_safe-archive-tools
title: 补齐安全归档与压缩包工具集 (inspect_archive 与 extract_archive)
type: F
author: zhuxiangju
author_email: zhuxiangjv@gmail.com
modules:
  - jiuwenswarm/agents/harness/common/tools
files:
  - jiuwenswarm/agents/harness/common/tools/archive_tools.py
  - jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py
  - jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py
  - jiuwenswarm/agents/harness/common/tools/__init__.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/agents/harness/common/rails/permissions/tool_capabilities.py
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/toolCategory.ts
  - tests/unit_tests/agents/test_archive_tools.py
designs: []
design_note: 安全高效的压缩包预览与解压工具落地，严密防范路径穿越。
---

# 补齐安全归档与压缩包工具集 (inspect_archive 与 extract_archive)

日期：2026-11-16。

## 背景与实际决策

在 Agent 日常开发与任务执行中，经常需要接收并处理 `.zip`、`.tar`、`.tar.gz` 等压缩包或
归档日志。此前智能体缺乏安全的原生归档处理工具，依赖 bash 调用 unzip/tar 或编写 Python 脚本，
容易引发安全隐患并浪费大量上下文。

决策如下：
1. **新建 `archive_tools.py` (`inspect_archive`)**：
   - 基于 Python 标准库 `zipfile` 与 `tarfile`，提供零外部依赖的压缩包只读预览；
   - 输出格式、文件数、目录数、压缩与解压体积；
   - 基于 `PurePosixPath.parts` 与 `PureWindowsPath.parts` 精确检测相对路径穿越特征，
     消除文件名包含连续点（如 `report..final.txt`）的误报；
   - 支持 `max_items` 数量截断保护，防止大压缩包输出刷屏。
2. **新建 `archive_tools.py` (`extract_archive`)**：
   - 提取归档包至目标工作区目录，默认在同级创建 `<archive_name>_extracted` 文件夹；
   - 约束 `target_dir` 必须位于 runtime workspace 内部，杜绝越界写文件；
   - 逐成员执行 `dest_path.is_relative_to(target_dir)` 拦截 Zip Slip 逃逸条目；
   - 对 TAR 归档中的 symlink / hardlink 实行跨目录校验，阻止逃逸链接落盘；
   - 引入 Zip-Bomb 熔断保护（最大 2000 文件、最大 100MB 解压总字节）。
3. **注册发现与导出**：
   - 在 `tool_discovery_catalog.py` 补充中英文检索项；
   - 在 `tools/__init__.py` 中按需延迟导出。
4. **规范与合规治理**：
   - 采用标准开源版权声明，单行严格 <= 100 字符；
   - 测试用例与错误提示不使用恶意或敏感词，符合开源合规与防投毒门禁要求；
   - 纯标准库实现，零新增外部依赖。

## 验证结论

- 测试套件：`tests/unit_tests/agents/test_archive_tools.py`
- 执行结果：13 passed in 8.47s (100%)
- 代码规范：`ruff check` 全部通过，全文件单行字符 <= 100。
