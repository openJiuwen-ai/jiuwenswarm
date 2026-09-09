# -*- coding: utf-8 -*-
"""
Markdown -> docx-pro 大纲 解析器。

用法：
    from md_parser import parse_markdown
    outline = parse_markdown(open("in.md", encoding="utf-8").read())
    renderer.render_outline(outline, "out.docx")

支持：
  - YAML frontmatter（--- 包围，key: value）映射 title/subtitle/theme/
    header/footer/toc/cover/watermark
  - 标题 #~######
  - 段落（**粗体**、*斜体*、`代码`、~~删除线~~、[链接](url)）
  - 无序/有序列表（二级缩进嵌套）
  - 表格（| a | b |），支持单元格内联格式
  - 代码块（``` 围栏）
  - 引用块（>）
  - 图片 ![alt](path)
  - 分隔线（--- / ***）
"""
import os
import re


def _parse_frontmatter(lines):
    """解析开头 YAML frontmatter，返回 (meta dict, 剩余行)。"""
    meta = {}
    if not lines or lines[0].strip() != "---":
        return meta, lines
    rest = lines[1:]
    for i, line in enumerate(rest):
        s = line.strip()
        if s == "---" or s == "...":
            return meta, rest[i + 1:]
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", s)
        if m:
            key, val = m.group(1).lower(), m.group(2).strip().strip('"').strip("'")
            if key in ("toc", "cover"):
                meta[key] = val.lower() in ("true", "yes", "1")
            elif val:
                meta[key] = val
    return meta, []


def _strip_inline(md_text):
    """去掉行内标记，仅留纯文本（用于标题等场景）。"""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", md_text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?<!\w)(\*|_)([^*_]+)\1(?!\w)", r"\2", text)
    return text.replace("`", "").replace("~~", "")


_H_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def _split_row(line):
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def parse_markdown(md_text, base_dir=None):
    """把 Markdown 文本解析为 renderer.render_outline 可用的大纲。"""
    lines = md_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    meta, lines = _parse_frontmatter(lines)

    sections = []
    title = meta.get("title")
    subtitle = meta.get("subtitle")
    i, n = 0, len(lines)
    first_h1_done = False

    def flush_para(buf):
        if buf:
            text = " ".join(x.strip() for x in buf).strip()
            if text:
                sections.append({"type": "para", "text": text})
            buf.clear()

    para_buf = []

    while i < n:
        raw = lines[i]
        line = raw.rstrip()

        if not line.strip():
            flush_para(para_buf)
            i += 1
            continue

        # 代码块
        if line.lstrip().startswith("```"):
            flush_para(para_buf)
            code_lines = []
            i += 1
            while i < n and not lines[i].lstrip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过闭合 ```
            sections.append({"type": "code", "text": "\n".join(code_lines)})
            continue

        # 标题
        m = _H_RE.match(line)
        if m:
            flush_para(para_buf)
            level = len(m.group(1))
            text = _strip_inline(m.group(2))
            if level == 1 and not first_h1_done and not title:
                title, first_h1_done = text, True
            elif level == 2 and not first_h1_done and title and not subtitle:
                # 无 frontmatter 时，二级标题首个视作副标题
                subtitle, first_h1_done = text, True
            else:
                sections.append({"type": "heading", "level": level, "text": text})
            i += 1
            continue

        # 分隔线
        if re.match(r"^\s*(---+|\*\*\*+|___+)\s*$", line):
            flush_para(para_buf)
            sections.append({"type": "divider"})
            i += 1
            continue

        # 引用块（连续 > 合并）
        if line.lstrip().startswith(">"):
            flush_para(para_buf)
            quote_lines = []
            while i < n and lines[i].lstrip().startswith(">"):
                quote_lines.append(lines[i].lstrip().lstrip(">").strip())
                i += 1
            sections.append({"type": "quote", "text": " ".join(x for x in quote_lines if x)})
            continue

        # 表格
        if _TABLE_ROW_RE.match(line) and i + 1 < n and \
                _TABLE_SEP_RE.match(lines[i + 1]) and "-" in lines[i + 1]:
            flush_para(para_buf)
            header = _split_row(line)
            i += 2
            rows = []
            while i < n and _TABLE_ROW_RE.match(lines[i]):
                rows.append(_split_row(lines[i]))
                i += 1
            table = {"type": "table", "header": header, "rows": rows, "zebra": True}
            ncols = len(header)
            if ncols >= 2:
                total = 16.0
                table["widths"] = [round(total / ncols, 2)] * ncols
            sections.append(table)
            continue

        # 图片（独立行）
        im = re.match(r"^\s*!\[([^\]]*)\]\(([^)]+)\)\s*$", line)
        if im:
            flush_para(para_buf)
            path = im.group(2).split(" ")[0]
            if base_dir and not os.path.isabs(path):
                path = os.path.join(base_dir, path)
            block = {"type": "image", "path": path}
            if im.group(1):
                block["caption"] = im.group(1)
            sections.append(block)
            i += 1
            continue

        # 列表（连续项合并为一个 bullets 块）
        lm = _LIST_RE.match(line)
        if lm:
            flush_para(para_buf)
            number = re.match(r"^\d", lm.group(2)) is not None
            items = []
            while i < n:
                lm2 = _LIST_RE.match(lines[i])
                if not lm2:
                    break
                indent_ws, marker, content = lm2.groups()
                indent = 1 if len(indent_ws) >= 2 else 0
                item = {"text": content, "indent": indent}
                # "加粗前缀：正文" 优化为 bold_prefix
                pm = re.match(r"^(\*\*[^*]+\*\*)\s*(.*)$", content)
                if pm:
                    item = {"text": pm.group(2),
                            "bold_prefix": pm.group(1).strip("*"),
                            "indent": indent}
                items.append(item)
                i += 1
            sections.append({"type": "bullets", "items": items, "number": number})
            continue

        # 普通段落行
        para_buf.append(line.strip())
        i += 1

    flush_para(para_buf)

    outline = {
        "title": title or "",
        "sections": sections,
        "theme": meta.get("theme", "business"),
    }
    if subtitle:
        outline["subtitle"] = subtitle
    if meta.get("meta"):
        outline["meta"] = [x.strip() for x in str(meta["meta"]).split(";") if x.strip()]
    for key in ("header", "watermark"):
        if meta.get(key):
            outline[key] = meta[key]
    if "footer" in meta:
        outline["footer"] = meta["footer"]
    if "toc" in meta:
        outline["toc"] = meta["toc"]
    if "cover" in meta:
        outline["cover"] = meta["cover"]
    return outline
