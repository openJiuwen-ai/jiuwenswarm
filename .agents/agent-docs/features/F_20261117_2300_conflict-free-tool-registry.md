---
id: F_20261117_2300_conflict-free-tool-registry
title: 内置工具声明式自发现注册表与文档索引无冲突合并机制
type: F
author: zhuxiangju
author_email: zhuxiangjv@gmail.com
modules:
  - jiuwenswarm/agents/harness
files:
  - .gitattributes
  - jiuwenswarm/agents/harness/common/tools/builtin_tool_registry.py
  - jiuwenswarm/agents/harness/common/tools/__init__.py
  - jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py
  - jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py
  - jiuwenswarm/agents/harness/common/rails/permissions/tool_capabilities.py
  - tests/unit_tests/agents/test_builtin_tool_registry.py
designs: []
design_note: 架构级消除多分支并行开发工具与文档时的中心注册表及索引冲突热点。
---

# 内置工具声明式自发现注册表与文档索引无冲突合并机制

日期：2026-11-17。

## 背景与痛点

在多分支/多人并行开发内置工具时，原本每新增一个工具都必须同时修改 `tools/__init__.py`、
`tool_discovery_catalog.py`、`tool_retrieval_toolkits.py`、`tool_capabilities.py` 等中心注册表文件
的队尾位置，且每次同步文档都会修改 `.agents/agent-docs/INDEX.md` 与 `features/INDEX.md` 的相邻行，
导致合入任意一个 PR 后其余所有在途 PR 立即发生合并冲突。

## 架构方案与实施决策

1. **新增基于 AST 的零导入开销声明式工具自发现注册表 (`builtin_tool_registry.py`)**：
   - 定义 `BuiltinToolSpec` 数据结构，支持在各 `*_tools.py` 模块或 `tools/specs/*.py` 独立描述文件
     顶层声明 `BUILTIN_TOOL_SPECS` 常量（支持 `BuiltinToolSpec(...)` 或字典字面量），同时提供编程式
     `register_builtin_tool_spec()` 接口；
   - 使用标准库 `ast.parse` 与 `ast.literal_eval` 静态提取元数据，绝不在发现阶段触发工具模块的真实
     导入，100% 保持 `tools/__init__.py` 的按需延迟加载（Lazy Export）契约；
   - 自动将发现的工具汇入 `tools/__init__.py` (`_EXPORTS` / `__all__`)、`tool_discovery_catalog.py`
     (`BUILTIN_TOOL_DISCOVERY`)、`tool_retrieval_toolkits.py` (`_TOOL_CATEGORIES`) 与
     `tool_capabilities.py`（只读路径、写路径、公开网页读取与内部只读权限集合）。
2. **配置 `.gitattributes` 文档索引 `merge=union` 策略**：
   - 为 `.agents/agent-docs/INDEX.md` 与 `.agents/agent-docs/*/INDEX.md` 配置 `merge=union` 合并驱动，
     使并行分支在追加不同文档索引行时由 Git 自动合并双方新增行，不再阻断并行 PR 合入。

## 验证证据

- `uv run pytest tests/unit_tests/agents/test_builtin_tool_registry.py` 全部通过；
- 权限与目录检索回归测试 `test_tool_capabilities.py`、`test_tool_retrieval_toolkits.py` 全部通过；
- 与在途工具集 PR (`feature/builtin-tools-suite`) 执行 `git merge-tree --write-tree` 验证零冲突。
