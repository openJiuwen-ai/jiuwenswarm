---
id: F_20261117_1730_diff-files-tool
title: 新增跨平台文件与文本结构化差异对比工具 (diff_files)
type: F
author: zhuxiangju
author_email: zhuxiangjv@gmail.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/agents/harness/common/tools/diff_tools.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/toolCategory.ts
  - tests/unit_tests/agents/test_diff_tools.py
  - tests/unit_tests/agentserver/permissions/test_tool_capabilities.py
  - tests/unit_tests/agentserver/test_tool_retrieval_toolkits.py
designs: []
design_note: 新增基于标准库 difflib 的跨平台文件与文本差异比对工具 diff_files，无独立前置设计文档。
---

# `diff_files` 跨平台文件与文本差异比对工具实施与决策归档

## 关联依据

- 设计：无独立前置设计文档（`designs: []`），按任务规格与既有工具契约落地。
- spec：遵守 `.agents/agent-docs/specs/agentserver/S_01_tool-discovery-language.md`。
- 日期：2026-11-17。

## 决策归属与关系

本记录为 `diff_files` 特性的首次实施归档（基于 `upstream/develop` 基线在独立分支
`feature/diff-files-tool` 落地），不涉及替代历史方案。

## 实际实现与开发决策

1. **零外部依赖的双模式 Unified Diff 比对**：
   - 基于 Python 标准库 `difflib.unified_diff` 实现文件比对模式（`file_a`、`file_b`）
     与内存文本比对模式（`text_a`、`text_b`）。
   - 输出包含变更统计（`+insertions`、`-deletions`、Hunk 数）与双列新旧行号标注的
     紧凑 Unified Diff 补丁块，支持忽略行尾空白与空行。
2. **工作区安全隔离与边界保护**：
   - 复用 `current_command_runtime_paths` 校验文件路径不越出允许的工作区根目录，
     拦截二进制文件、目录与超大文件（5 MB 上限），支持 `max_chars` 截断。
3. **声明式自动接线**：
   - 在 `diff_tools.py` 内声明 `BUILTIN_TOOL_SPECS`，通过 `builtin_tool_registry` 自动接入
     `tools/__init__.py`、`tool_capabilities.py`（`path_read`）、`tool_discovery_catalog.py`
     与 `tool_retrieval_toolkits.py`（`file-diff`），零修改中心注册表；并完成
     `interface_deep.py` 共享工具卡注册与 `toolCategory.ts` 前端映射。

## 与设计的偏差

无。

## 验证结论

- 单测与五点接线测试覆盖文件与文本比对、无差异提示、空白过滤、字符截断、工作区越界与
  二进制拒绝及权限分类，全部通过。
