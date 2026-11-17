---
id: F_20261117_1710_parse-test-failure-tool
title: 新增测试失败诊断与结构化提取工具 (parse_test_failure)
type: F
author: zhuxiangju
author_email: zhuxiangjv@gmail.com
modules:
  - jiuwenswarm/agents/harness/common/tools
files:
  - jiuwenswarm/agents/harness/common/tools/test_failure_tools.py
  - jiuwenswarm/agents/harness/common/tools/__init__.py
  - jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py
  - jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py
  - jiuwenswarm/agents/harness/common/rails/permissions/tool_capabilities.py
  - jiuwenswarm/agents/harness/common/rails/permissions/auto_decision.py
  - jiuwenswarm/agents/harness/common/rails/permissions/_auto_permission/readonly_tool_bindings.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/toolCategory.ts
  - tests/unit_tests/agents/test_test_failure_tools.py
  - tests/unit_tests/agentserver/permissions/test_auto_decision.py
  - tests/unit_tests/agentserver/permissions/test_auto_permission_pipeline.py
  - tests/unit_tests/agentserver/permissions/test_tool_capabilities.py
  - tests/unit_tests/agentserver/test_tool_discovery_language.py
  - tests/unit_tests/agentserver/test_tool_retrieval_toolkits.py
designs: []
design_note: 新增测试失败输出结构化解析与去噪工具 parse_test_failure，无独立前置设计文档。
---

# `parse_test_failure` 测试失败诊断与提取工具实施与决策归档

## 关联依据

- 设计：无独立前置设计文档（`designs: []`），按任务规格与既有工具契约落地。
- spec：遵守 `.agents/agent-docs/specs/agentserver/S_01_tool-discovery-language.md`。
- 日期：2026-11-17。

## 决策归属与关系

本记录为 `parse_test_failure` 特性的首次实施归档（基于 `upstream/develop` 基线在独立分支
`feature/parse-test-failure-tool` 落地），不涉及替代历史方案。

## 实际实现与开发决策

1. **纯内存去噪与多框架结构化提取**：
   - 剥离终端 ANSI CSI/OSC 控制码、进度回车覆盖与 NUL 字符，过滤 pytest 平台头、
     进度点与捕获日志等噪音。
   - 支持 `auto`、`pytest`、`unittest` 框架识别，精准提取失败用例名、阶段
     （`call`/`setup`/`teardown`/`collection`）、异常类型与消息、源码 `file:line`
     及去除了框架内部栈帧的紧凑 Traceback。
2. **权限安全与全链路五点接线**：
   - 归入 `_INTERNAL_READONLY_TOOLS` 并在 `readonly_tool_bindings.py` 绑定真实函数身份，
     在 `auto_decision.py` 校验参数闭集并将 `raw_output` 声明为非 URI 文本载荷。
   - 完成 `interface_deep.py`、`tool_retrieval_toolkits.py`、`toolCategory.ts`、
     `tool_discovery_catalog.py` 与 `tools/__init__.py` 全链路接线。

## 与设计的偏差

无。

## 验证结论

- 单测与权限管线测试覆盖 pytest/unittest 多格式输出、ANSI 剥离、堆栈压缩、参数边界与
  五点接线契约，全部通过。
