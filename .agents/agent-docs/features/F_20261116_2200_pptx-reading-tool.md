---
id: F_20261116_2200_pptx-reading-tool
title: read_pptx PowerPoint Presentation Reading Tool
type: F
author: zhuxiangju
author_email: zhuxiangjv@gmail.com
designs: []
design_note: "Atomic tool addition for PowerPoint presentation ingestion"
modules:
  - jiuwenswarm/agents/harness/common/tools
files:
  - jiuwenswarm/agents/harness/common/tools/pptx_tools.py
  - jiuwenswarm/agents/harness/common/tools/__init__.py
  - jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py
  - jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/agents/harness/common/rails/permissions/tool_capabilities.py
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/toolCategory.ts
  - tests/unit_tests/agents/test_pptx_tools.py
---

# read_pptx PowerPoint Presentation Reading Tool

## 1. 背景与目标
在办公协作与多智能体知识处理场景中，用户常需要将演示文稿（PowerPoint .pptx）交由智能体
阅读和总结。此前系统支持了 PDF、Docx 与 Spreadsheet，但缺少对演示文稿幻灯片的结构化提取。

本特性实现 `read_pptx` 工具，将 PPTX 演示文稿解析为高质量结构化 Markdown。同时采用双模式设计：
1. **优先增强**：若环境已安装 `python-pptx`，提取幻灯片形状、表格与演讲者备注（Notes）。
2. **轻量兜底**：在无第三方依赖的精简容器或 CI 容器中，使用标准库 `zipfile` 和
   `xml.etree.ElementTree` 自动解析 OpenXML 幻灯片正文和表格，确保零外部依赖、100% 稳定运行。

## 2. 关键能力与端到端闭环
- **全链路注册与接线**：
  - 注册进 `interface_deep.py` 共享工具链路与 `_tool_cards`，智能体可原生调用；
  - 接入 `tool_capabilities.py` 静态路径只读权限审计；
  - 纳入 `tool_retrieval_toolkits.py` 的 documents 工具检索分组；
  - 前端 `toolCategory.ts` 登记文件只读图标与动作映射；
  - 登记 `tool_discovery_catalog.py` 中英文渐进式检索元数据与 `tools/__init__.py` 懒导出。
- **结构化输出与备注提取**：
  - 按幻灯片编号输出各页标题、段落、列表及 Markdown 表格；
  - OpenXML 回退分支通过 `presentation.xml` 准确映射幻灯片播放顺序，并提取 `notesSlide` 演讲备注。
- **页面过滤与安全防护**：
  - 支持 `slides` 参数（支持单个数字、列表或范围字符串如 `1-3,5`）；
  - 设置单次最大 500 页跨度与 1000 页总数上限，杜绝大区间展开引发的 OOM；
  - 路径解析支持 expanduser()，相对路径校验工作区边界防止越界逃逸，绝对路径支持直接读取；
  - 受 `max_chars` 截断保护，防止超长演示文稿溢出上下文。

## 3. 验证与测试
- 单元测试覆盖于 `tests/unit_tests/agents/test_pptx_tools.py`，全部 11 个测试通过。
- 代码规范：`ruff check` 全部通过，全文件单行字符 <= 100。
