---
id: F_20261116_0500_office-document-reader-tools
title: 补齐结构化办公文档只读工具 (read_docx 与 read_spreadsheet)
type: F
author: zhuxiangju
author_email: zhuxiangjv@gmail.com
modules:
  - jiuwenswarm/agents/harness/common/tools
files:
  - jiuwenswarm/agents/harness/common/tools/docx_tools.py
  - jiuwenswarm/agents/harness/common/tools/spreadsheet_tools.py
  - jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py
  - jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py
  - jiuwenswarm/agents/harness/common/tools/__init__.py
  - jiuwenswarm/agents/harness/common/rails/permissions/tool_capabilities.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/toolCategory.ts
  - tests/unit_tests/agents/test_docx_tools.py
  - tests/unit_tests/agents/test_spreadsheet_tools.py
designs: []
design_note: 补齐办公文档 Word 与 Excel/CSV 结构化只读能力的工程落地。
---

# 补齐结构化办公文档只读工具 (read_docx 与 read_spreadsheet)

日期：2026-11-16。

## 背景与实际决策

在原有的 Agent Tools 体系中，仅具备针对 PDF 的 `read_pdf`。当用户在会话或 IM 端上传
Word（`.docx`）或表格（`.xlsx`、`.csv`）时，智能体缺乏原生解析工具，往往依赖动态编写脚本
通过 bash 运行，既浪费大量 Token 又易受沙箱和环境依赖影响。

决策如下：
1. **新建 `docx_tools.py` (`read_docx`)**：利用运行环境已有的 `python-docx` 库，保留文档
   原始层级（Headings 转换 Markdown 标题）及表格结构，支持 `start_element` 分页续读与
   `max_chars` 上限防护；
2. **新建 `spreadsheet_tools.py` (`read_spreadsheet`)**：利用 `openpyxl`（`read_only=True`
   低内存流式读取）和标准库 `csv`（支持 UTF-8 / GBK 编码自适应），支持多 Sheet 探测导航、
   `start_row` 翻页续读、50MB 文件体积防护与 `max_rows` 行上限保护；
3. **系统接线与发现**：完成系统共享工具注册、权限集合定义、渐进式检索元数据与前端图标映射；
4. **代码规范与门禁合规**：代码行长严格控制在 100 字符以内，保持开源版权头，零新增三方依赖；
   拆分 `_read_docx_sync`、`_read_csv` 与 `_read_excel` 内部辅助函数以降低函数行数与圈复杂度，
   流式分页读取 CSV 数据避免全量格式化入内存，单测使用构造函数替代同名 fixture 消除 `G.VAR.03`
   外部作用域标识符覆盖告警，并补齐修改文件版权声明头；
5. **单元测试验证**：在 `tests/unit_tests/agents/` 下补充 21 个测试用例，覆盖正常转换、
   表格对齐、超长截断、编码容错、空文件防护、分页续读、体积拦截、异常报错等全路径。

## 验证结论

- 测试套件：`tests/unit_tests/agents/test_docx_tools.py`、
  `tests/unit_tests/agents/test_spreadsheet_tools.py`、
  `tests/unit_tests/agentserver/test_tool_discovery_language.py`
  与 `tests/unit_tests/agentserver/permissions/test_tool_capabilities.py`
- 执行结果：全部通过（100%）
- 代码规范：全文件单行字符 <= 100，函数粒度与推导式符合静态门禁要求。
