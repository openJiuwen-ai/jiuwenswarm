---
name: docx-pro
description: Professional Word (.docx) document generation, faithful text replacement, Markdown round-trip conversion, document inspection, TOC insertion and watermarking. Use when the user asks to create rich-formatted Word documents (cover page, TOC, multi-level headings, styled tables with merged cells, images, code blocks, quotes, headers/footers, page numbers, watermarks), replace or batch-replace text in existing docx files while preserving every other byte and all formatting (handles text split across runs, tables, headers/footers), convert Markdown to docx or docx to Markdown, inspect document structure, insert TOC fields, or add text watermarks to existing docx files. Triggers on docx-pro, 生成 Word 文档, 生成报告文档, 富格式文档, Word 替换, Word 文本替换, Word 保真替换, 批量替换, 合同替换, 替换甲方, Markdown 转 Word, Word 转 Markdown, md 转 docx, docx 转 md, 插入目录, 添加水印, 文档结构分析, 专业排版.
description_cn: 专业 Word 文档工具集：从零生成富格式 Word（封面/目录/主题/合并单元格表格/图片/代码块/页眉页脚/水印）、保真文本替换（跨 run 定位、表格/页眉页脚全覆盖、其余字节零改动、支持批量映射与预览）、Markdown 双向转换、文档结构检查、目录插入与水印添加。
---

# docx-pro：专业 Word 文档操作技能

基于 python-docx 的 docx 进阶工具集，提供 7 个子命令，覆盖「生成 → 转换 → 编辑 → 检查 → 后处理」完整链路。

## STOP：执行前检查

1. **依赖**：运行 `python scripts/setup_check.py`，确认 python-docx 已安装（缺则 `pip install python-docx`）。
2. **路径**：确认输入文件存在、输出路径不会覆盖任何输入文件。
3. **控制台编码**：本技能脚本已内置 GBK 控制台兜底（errors=replace），直接用 `python scripts/docx_pro.py ...` 即可；若仍见乱码，是显示问题，不影响文件内容。
4. **替换前先预览**：用 `replace --dry-run` 确认命中次数与预期一致，再实际替换。

## 子命令速查

脚本统一入口：`scripts/docx_pro.py`（以下 `PY` 代表 `python`）。

### 1. create — JSON 大纲 → 富格式 docx

```
PY scripts/docx_pro.py create -j outline.json -o 报告.docx --theme business
```

可选参数：`--header "页眉文字"`、`--footer center|X/Y`、`--toc true`、`--cover true`、`--watermark "草稿"`。

三套内置主题：
- **business** 商务蓝（默认）：深蓝封面标题、深蓝表头白字、浅蓝斑马纹
- **academic** 学术黑白：衬线正文、黑底表头、Times New Roman
- **minimal** 极简：无底色表头（粗下边框）、大量留白

支持的块类型（outline.sections 内元素）：

| type | 关键字段 |
| --- | --- |
| heading | level(1-4), text |
| para | text（支持 **粗体** `代码` ~~删除~~ [链接](url) 行内语法）, indent, align |
| bullets | items: ["文本" 或 {text, bold_prefix, indent}], number(编号列表) |
| table | header[], rows[]（单元格可为 {t, cs, rs} 支持合并）, widths[], zebra, align |
| kv | items: {键: 值} —— 自动渲染两列规格表 |
| image | path（相对 JSON 所在目录）, width_cm, caption |
| code | text（多行代码，等宽字体+灰底） |
| quote | text（左边框引用块） |
| divider / pagebreak | 无字段 |

大纲顶层字段：title, subtitle, meta[], cover, toc, header, footer(center|"X/Y"), theme, watermark。

### 2. from-md — Markdown → docx

```
PY scripts/docx_pro.py from-md 输入.md -o 输出.docx --theme academic --toc --cover
```

支持：YAML frontmatter（title/subtitle/theme/toc/cover/header/footer）、标题层级、行内格式（粗体/斜体/行内代码/删除线/链接）、无序/有序/嵌套列表、表格、围栏代码块、引用块、图片、分隔线。列表项 `**加粗前缀** 正文` 自动渲染为彩色加粗前缀样式。

### 3. to-md — docx → Markdown

```
PY scripts/docx_pro.py to-md 输入.docx -o 输出.md --images
```

`--images` 时导出文档内图片到 `输出名_images/` 目录。注意：定位是**内容提取转换**而非像素级还原——样式细节（颜色/主题/水印）会丢失，正文、表格、列表、代码块、引用、图片可完整还原。

### 4. replace — docx 保真文本替换（合同/公文等格式敏感文档首选）

```
PY scripts/docx_pro.py replace 输入.docx -o 输出.docx -f "甲方：A 公司" -t "甲方：B公司"
PY scripts/docx_pro.py replace 输入.docx -o 输出.docx --map pairs.json --dry-run
```

- **跨 run 定位**：Word 常因拼写检查、协同编辑把一段连续文字拆成多个 run（w:r/w:t 节点），朴素字符串替换会大量漏换。本命令把同一段落内相邻 run 的文本拼接后定位目标串——替换文本写入首个命中节点，其余被覆盖节点文本清空，**run 结构与全部格式属性零改动**。
- **字节级保真**：zip 包内仅含目标文本的 XML 部件被修改，其余条目（样式表、主题、编号、字体表、图片……）按原始字节复制；命中部件也只改被替换节点的文本内容，保留原 XML 声明、属性顺序、命名空间前缀。
- **覆盖范围**：默认 `--scope all` = 正文（含表格单元格、文本框、超链接内文本）+ 页眉 + 页脚 + 脚注/尾注/批注；`--scope body` 仅正文。
- **批量替换**：`--map pairs.json`（`[["旧","新"], ...]` 或 `{"旧": "新"}`，也支持 `[{"find":..,"to":..}]`），按顺序链式执行；或多次 `-f/-t` 成对使用。
- **预览**：`--dry-run` 只统计各部件命中次数，不写出文件。
- **自动验证**：替换后逐 zip 条目字节比对、报告跨 run 命中数、残留计数，并用 python-docx 复检段落数/表格数与可打开性。

安全边界：匹配不跨段落、不跨表格单元格（相邻文本节点之间出现段落结束、制表符、换行、域代码即视为不连续，这正是"不该替换的不替换"的保证）；域代码（w:instrText）与修订删除文本（w:delText）不参与匹配。跨格式 run 替换时，替换文本沿用首个命中 run 的格式。

### 5. inspect — 查看文档结构

```
PY scripts/docx_pro.py inspect 输入.docx [--json]
```

输出页面尺寸、边距、非空段落数、表格数与形状、内联图片数、完整标题树。`--json` 供程序化消费。

### 6. toc — 为已有 docx 插入目录域

```
PY scripts/docx_pro.py toc 输入.docx -o 带目录.docx --levels 1-3
```

插入 TOC 域到文档开头；**在 Word 中打开后需按 F9（或右键→更新域）生成实际目录**（要求文档标题使用 Heading 样式，本技能 create 生成的文档天然满足）。

### 7. watermark — 为已有 docx 添加文字水印

```
PY scripts/docx_pro.py watermark 输入.docx -o 加水印.docx -t "DRAFT" --color C0C0C0 --opacity 0.45
```

斜向 315° 文字水印，全部页面生效（页眉 VML 实现，兼容 Word/WPS）。

## CHECKPOINT：完成后的验证

1. 用 `inspect` 检查生成文档：标题树、表格数、图片数与预期一致。
2. create 含表格时，确认表格首行是表头内容（数据未被写入表头位置）。
3. toc/watermark/replace 后处理命令的输出路径与输入不同（不覆盖原文件）。
4. **replace 后确认**：命中次数与预期一致（先 `--dry-run` 后实跑）、"其余 zip 条目字节级一致: True"、段落数/表格数不变、残留计数为 0（若新文本本身包含旧文本，残留>0 属正常）。
5. 中文字符在 Windows 控制台显示乱码时，检查文件本身（read_file / inspect）确认内容正确。

## 大纲 JSON 完整示例

```json
{
  "title": "产品评测报告",
  "subtitle": "2026 年度智能硬件横评",
  "meta": ["评测机构：极客实验室", "发布日期：2026-09-04"],
  "cover": true, "toc": true,
  "header": "极客实验室 · 产品评测", "footer": "X/Y",
  "theme": "business", "watermark": "内部资料",
  "sections": [
    {"type": "heading", "level": 1, "text": "一、评测概述"},
    {"type": "para", "text": "本报告覆盖 **12 款** 设备，原始数据见 `data/raw.csv`。"},
    {"type": "bullets", "items": [
      {"text": "共 12 款设备", "bold_prefix": "设备数量："},
      {"text": "子项目 A", "indent": 1}
    ]},
    {"type": "table", "header": ["设备", "得分"], "zebra": true,
     "rows": [["设备 A", "94.5"], [{"t": "合并单元格", "cs": 2}]]},
    {"type": "kv", "items": {"总测试项": "348"}},
    {"type": "image", "path": "chart.png", "width_cm": 12, "caption": "图 1 示例"},
    {"type": "code", "text": "print('hello')"},
    {"type": "quote", "text": "结论仅供参考。"},
    {"type": "divider"}, {"type": "pagebreak"}
  ]
}
```

## 批量替换映射示例（pairs.json）

```json
[
  ["甲方：A 公司", "甲方：B公司"],
  ["乙方：C 公司", "乙方：D公司"],
  {"find": "2025 年度", "to": "2026 年度"}
]
```

## 边界与已知限制

- to-md 是内容级转换：主题配色、水印、封面版式等样式信息不参与回转。
- TOC 域需在 Word/WPS 中手动更新（F9）后才显示实际目录，这是域机制决定的行为。
- 水印为整页文字水印（斜向 315°）；不支持图片水印。
- create 的表格支持横向(cs)/纵向(rs)合并，但 to-md 回转时合并单元格会退化为普通单元格。
- replace 不跨段落/跨单元格匹配；不处理域代码（w:instrText）与修订删除文本（w:delText）；假定正文命名空间前缀为 `w:`（Word 标准写法）；`--dry-run` 的统计基于原文（前一对替换产生的新文本不计入后一对的预览）。
- 依赖：python-docx（必需，replace 的复检环节也用到）；Pillow 仅当需要处理特殊格式图片时可能用到，非必需。

## Directory layout

```
docx-pro/
├── SKILL.md
└── scripts/
    ├── docx_pro.py     # 统一 CLI 入口（7 个子命令）
    ├── docx_replace.py # 保真替换引擎（跨 run 定位 / 字节级保真 / 自动验证）
    ├── renderer.py     # 主题系统 + 块渲染引擎
    ├── md_parser.py    # Markdown -> 大纲 解析器
    ├── md_export.py    # docx -> Markdown 导出器
    └── setup_check.py  # 依赖自检
```
