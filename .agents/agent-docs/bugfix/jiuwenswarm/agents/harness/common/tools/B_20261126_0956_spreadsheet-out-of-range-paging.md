---
id: B_20261126_0956_spreadsheet-out-of-range-paging
title: read_spreadsheet 越界分页与仅表头文件的矛盾提示
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/agents/harness/common/tools
files:
  - jiuwenswarm/agents/harness/common/tools/spreadsheet_tools.py
  - tests/unit_tests/agents/test_spreadsheet_tools.py
designs: []
design_note: 内置工具只读分页提示的缺陷修复，无独立设计方案，按 Issue #7054 与 PR 检视意见处理。
---

# read_spreadsheet 越界分页与仅表头文件的矛盾提示

## 现象与影响

`read_spreadsheet` 是模型读取表格的内置工具，用户可见输出即模型输入。修复前存在两类错误：

- `start_row` 超出末页时仍输出区间文案：3 行数据的 CSV 在 `start_row=100` 下显示
  `Showing rows 100-100 of 3`，Excel 分支显示 `Showing rows 99-99` 且不带总行数，表体为空。
- 仅表头的文件走同一条越界分支：CSV 输出「表头行的一页」，Excel（有表头、无数据行）输出
  `start_row=1 is beyond the last data row. The last page starts at start_row=1.`，同一句中
  `start_row=1` 既是越界位置又是最后页码。

模型会据此误判表格为空或反复以同一参数重试，属于分页语义错误而非渲染问题。

## 根因

- CSV：`end_row = start_idx + 1` 在无数据时硬凑区间，未区分「越界」与「仅表头」。
- Excel：`_stream_excel_page` 早停时不回传总行数，`_format_excel_output` 无法判断越界；
  越界短路的判断条件 `target_start > total_rows` 在 `total_rows == 0` 时恒成立，
  把仅表头场景误判成越界。

## 修复决策

- CSV 越界改为输出真实数据行数与最后可用页码；仅表头单独分支，输出 `header only` 并附带表头一行。
- `_stream_excel_page` 在完整扫描到表尾时回传总行数；`_format_excel_output` 先处理 `total_rows == 0`
  的仅表头分支，再处理越界分支，两者提示口径与 CSV 对齐。
- 早停分页（`max_rows` 截断）保持原 `Showing rows X-Y` 格式，避免影响既有节流行为。

## 回归与验证

- `tests/unit_tests/agents/test_spreadsheet_tools.py` 新增越界、仅表头、末页总行数用例，本地
  `pytest tests/unit_tests/agents/test_spreadsheet_tools.py` 全部通过。
- 修复后实测：`start_row=100` 输出真实总行数与最后页码；仅表头 CSV/Excel 输出 `header only`
  且包含表头列名，不再出现 `beyond the last data row` 与 `Showing rows` 矛盾文案。

## 遗留与限制

- 仅表头文件不再输出表格形式的分页区间，属有意的输出契约变更，已在 PR 说明。
- 本记录不覆盖 `app_web.py` 的 CSP 豁免争议：该改动与本缺陷无关，已从 PR 中移除。
