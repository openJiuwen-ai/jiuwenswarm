---
name: office-document-workflow
description: 办公文档处理工作流：指导PDF、Word、Excel、PPT文档的解析、格式互转、批量处理、编辑修改、数据变换、PDF操控、高级排版、邮件合并、文档比较、打包压缩与文档脱敏。当用户需要处理办公文档时使用。
---

# 办公文档处理工作流

## 目标

完成办公文档（PDF、Word、Excel、PPT）的全链路处理：解析、转换、批量处理、编辑修改、数据变换、PDF操控、高级排版、邮件合并、文档比较、打包压缩与文档脱敏。

## 工作流

### 1. 文档解析

调用 `document_parser` 工具解析源文档：

- 输入 `file_path` 和 `file_type`（pdf/word/excel/ppt/auto）
- 需要表格数据时设置 `extract_tables: true`

**调用时机**：用户提供文档文件并需要提取内容时。

### 2. 数据汇总

调用 `data_summarizer` 工具汇总整理数据：

- 传入 `data_sources` 或 `structured_data`
- 选择 `summary_type`：overview/statistical/categorical/timeline

**调用时机**：需要从多个数据源汇总信息时。

### 3. 文档生成

调用 `document_generator` 工具生成目标文档：

- 指定 `format`、`filename` 和 `content`
- `content` 结构：`{title, paragraphs[], tables[], sheets[], slides[]}`

**调用时机**：需要将处理后的内容输出为文档文件时。

### 4. 格式互转

调用 `format_converter` 工具转换文档格式：

- 输入 `source_path` 和 `target_format`（word/pdf/excel/csv/ppt）
- 支持 PDF→Word、PDF→Excel、Word→PDF、Excel↔CSV、PPT→PDF、Markdown→Word/PPT

**调用时机**：用户需要转换文档格式时。

### 5. 批量处理

调用 `batch_processor` 工具批量处理文件：

- `operation`：rename/merge_excel/merge_word/convert/list_files
- 输入 `input_dir` 和 `pattern`

**调用时机**：用户需要批量处理多个文件时。

### 6. 编辑已有文档

调用 `document_editor` 工具编辑现有文档：

- Word操作：replace_text/append_paragraph/insert_heading/insert_image/add_table/accept_all_changes/reject_all_changes/track_changes
- Excel操作：write_cell/write_range/append_row/add_formula
- PPT操作：replace_placeholder/add_slide_from_template/replace_slide_text

**调用时机**：用户需要修改已有文档时。

### 7. Excel数据变换

调用 `excel_transformer` 工具变换表格数据：

- `operation`：vlookup/pivot/filter/dedupe/encoding_convert/sort/split_text/merge_text
- sort需提供 `options.sort_columns` 和 `options.sort_order`
- split_text需提供 `options.column` 和 `options.delimiter`
- merge_text需提供 `options.columns` 和 `options.separator`

**调用时机**：用户需要对表格数据进行变换操作时。

### 8. PDF专项操控

调用 `pdf_manipulator` 工具操控PDF文件：

- `operation`：merge/split/compress/encrypt/watermark/extract_pages/extract_images

**调用时机**：用户需要对PDF进行合并、拆分、压缩、加密、加水印等操作时。

### 9. 高级排版与报告美化

调用 `report_styler` 工具美化文档排版：

- Word操作：add_toc/add_header_footer/insert_chart/apply_theme/set_margins/add_cover_page
- PPT操作：apply_theme/insert_chart/set_slide_size

**调用时机**：用户需要美化文档排版时。

### 10. 邮件合并

调用 `mail_merge_tool` 工具批量生成个性化文档：

- 输入 `template_path`（Word模板含{{占位符}}）和 `data_path`（Excel/CSV数据源）
- 可指定 `filename_pattern` 用占位符命名输出文件

**调用时机**：用户需要批量生成合同/邀请函/工资条等时。

### 11. 文档比较

调用 `doc_comparator` 工具对比两个文档的差异：

- 输入 `file_path_1`（旧版）和 `file_path_2`（新版）
- 支持 Word/Excel/PDF/CSV 格式
- 输出新增、删除、修改的内容

**调用时机**：用户需要比较文档版本差异时。

### 12. 文档打包压缩

调用 `archive_tool` 工具打包或解压文件：

- `operation`：create（打包成ZIP）/ extract（解压ZIP）
- create需提供 `file_paths`，extract需提供 `archive_path`

**调用时机**：用户需要打包发送文件或解压压缩包时。

### 13. 文档脱敏

调用 `redactor` 工具自动遮盖敏感信息：

- 输入 `file_path`，支持 Word/Excel/PDF/TXT/CSV/Markdown
- 自动识别手机号、身份证号、邮箱、银行卡号
- 可通过 `patterns` 自定义脱敏正则

**调用时机**：用户需要脱敏文档中的敏感信息时。

## 决策规则

- 只需提取文档内容 → `document_parser`
- 需要汇总多文档数据 → `document_parser` → `data_summarizer`
- 需要生成报告 → 解析/汇总 → `document_generator` → `report_styler`
- 需要转换格式 → `format_converter`
- 需要批量处理 → `batch_processor`
- 需要修改已有文档 → `document_editor`（而非 `document_generator` 新建）
- 需要表格数据变换 → `excel_transformer`
- 需要操控PDF → `pdf_manipulator`
- 需要美化排版 → `report_styler`
- 批量生成个性化文档 → `mail_merge_tool`
- 比较文档差异 → `doc_comparator`
- 打包/解压文件 → `archive_tool`
- 脱敏敏感信息 → `redactor`
- 一步到位"整理文档并生成美化报告" → 解析→汇总→生成→美化
- 生成格式由用户指定；未指定时默认 Word

## 输出要求

- 解析结果必须包含完整的文本和表格数据，不得截断
- 汇总报告必须包含总记录数和关键字段统计
- 生成/转换文件必须返回 `path`、`size_bytes`、`exists` 等验证字段
- 文件生成前自校验路径存在且 `size_bytes > 0`
- 编辑已有文档时保留原文档未修改部分
- 批量处理必须返回成功/失败计数和详细列表
- 脱敏结果必须返回遮盖统计
