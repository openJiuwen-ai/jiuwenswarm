---
id: F_20261117_1700_code-outline-tool
title: 新增代码地图与符号大纲工具 (code_outline)
type: F
author: zhuxiangju
author_email: zhuxiangjv@gmail.com
modules:
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/agents/harness/common/tools/code_outline_tools.py
  - jiuwenswarm/agents/harness/common/tools/__init__.py
  - jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py
  - jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py
  - jiuwenswarm/agents/harness/common/rails/permissions/tool_capabilities.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/toolCategory.ts
  - tests/unit_tests/agents/test_code_outline_tools.py
  - tests/unit_tests/agentserver/permissions/test_tool_capabilities.py
  - tests/unit_tests/agentserver/test_tool_retrieval_toolkits.py
designs: []
design_note: 新增零依赖代码地图与符号大纲提取工具 code_outline，无独立前置设计文档。
---

# `code_outline` 代码地图与符号大纲工具实施与决策归档

## 关联依据

- 设计：无独立前置设计文档（`designs: []`），按任务规格与既有工具契约落地。
- spec：遵守 `.agents/agent-docs/specs/agentserver/S_01_tool-discovery-language.md`。
- 日期：2026-11-17。

## 决策归属与关系

本记录为 `code_outline` 特性的首次实施归档（基于 `upstream/develop` 基线在独立分支
`feature/code-outline-tool` 落地），不涉及替代历史方案。

## 实际实现与开发决策

1. **Python 原生 AST + JS/TS 轻量语法大纲双引擎**：
   - Python（`.py`/`.pyi`）基于标准库 `ast` 精准提取类、方法、函数签名、参数类型注解、
     默认值、返回值注解、装饰器及首行 Docstring 摘要，并附带源码行号范围。
   - JS/TS（`.js`/`.jsx`/`.ts`/`.tsx`/`.mjs`/`.cjs`）先屏蔽单行/块注释与字符串/模板字面量，
     再按花括号层级与正则提取顶层 `class`/`interface`/`type`/`enum`/`function`/箭头函数
     及类成员方法，零外部依赖。
2. **工作区安全隔离与遍历预算控制**：
   - 复用 `current_command_runtime_paths` 校验目标路径位于允许的工作区根目录内，
     目录遍历时默认剪枝 `.git`、`node_modules`、`.venv` 等目录并跳过外部符号链接。
   - 支持 `max_depth`（0–10）、`max_files`（1–200）、`max_chars`（1000–100000）与
     `include_private` 参数裁剪。
3. **全链路五点接线**：
   - 注册 `interface_deep.py` 共享工具卡，接入 `tool_capabilities.py` 的 `_PATH_TOOLS`，
     登记 `tool_retrieval_toolkits.py`、`toolCategory.ts`、`tool_discovery_catalog.py` 与
     `tools/__init__.py`。

## 与设计的偏差

无。

## 验证结论

- 单测与五点接线测试覆盖 Python AST、语法错误容错、JS/TS 注释与字符串屏蔽、目录深度/文件数/
  字符数上限、二进制与超大文件跳过、工作区越界与外部软链拦截，全部通过。
