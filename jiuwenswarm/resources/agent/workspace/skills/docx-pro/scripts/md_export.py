# -*- coding: utf-8 -*-
"""
docx -> Markdown 导出器。

用法：
    from md_export import docx_to_markdown
    md = docx_to_markdown("in.docx", images_dir="images")  # 可选导出图片
    open("out.md", "w", encoding="utf-8").write(md)
"""
import os
import re

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.oxml.ns import qn


def _iter_block_items(parent):
    """按文档顺序迭代段落与表格。"""
    body = parent.element.body if hasattr(parent, "element") else parent._tc
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, parent)
        elif child.tag == qn("w:tbl"):
            yield Table(child, parent)


def _is_mono(run):
    name = (run.font.name or "")
    return "Courier" in name or "Consolas" in name


def _has_field(p):
    """段落是否包含域（如 TOC/PAGE）。"""
    return p._p.findall(".//" + qn("w:fldChar")) or \
        p._p.findall(".//" + qn("w:instrText"))


def _is_quote(p):
    """引用块判定：pPr/pBdr 中存在 left 边框。"""
    ppr = p._p.pPr
    if ppr is None:
        return False
    pbdr = ppr.find(qn("w:pBdr"))
    if pbdr is None:
        return False
    return pbdr.find(qn("w:left")) is not None


def _is_code_para(p):
    """代码行判定：段落有底纹且全部 run 为等宽字体。"""
    ppr = p._p.pPr
    if ppr is None or ppr.find(qn("w:shd")) is None:
        return False
    runs = [r for r in p.runs if r.text]
    if not runs:
        return False
    return all(_is_mono(r) for r in runs)


def _run_markdown(run):
    """run -> 带行内标记的文本（代码块段落由 _is_code_para 单独处理，
    不经过本函数；普通段落的等宽字体 run 还原为 `行内代码`）。"""
    text = run.text or ""
    if not text:
        return ""
    out = text
    if _is_mono(run):
        out = "`%s`" % out
    if run.font.strike:
        out = "~~%s~~" % out
    if run.font.italic:
        out = "*%s*" % out
    if run.font.bold:
        out = "**%s**" % out
    return out


def _para_markdown(p):
    """段落 -> markdown 行；返回 None 表示跳过，list 表示代码块多行。"""
    if _has_field(p):  # 目录域/页码域等跳过
        return None

    style_name = (p.style.name or "") if p.style is not None else ""
    runs = [r for r in p.runs if (r.text or "").strip()]

    # 标题：使用纯文本，避免把样式加粗误认为 ** 标记
    m = re.match(r"^Heading (\d)$", style_name)
    if m:
        level = min(6, int(m.group(1)))
        return "#"*level + " " + p.text.strip()
    if style_name.startswith("Heading"):
        return "## " + p.text.strip()

    # 代码行：等宽 + 底纹
    if _is_code_para(p):
        return ["```", "\n".join(r.text for r in p.runs), "```"]

    # 引用块
    if _is_quote(p):
        return "> " + "".join(_run_markdown(r) for r in p.runs).strip()

    # 列表：第一个 run 是项目符号/编号 marker
    if runs and runs[0].text.strip() in ("\u2022", "\u00b7", "1."):
        numbered = runs[0].text.strip().startswith("1")
        body_runs = runs[1:]
        indent = 0
        pf = p.paragraph_format
        if pf.left_indent is not None and pf.left_indent.pt >= 40:
            indent = 1
        marker = "%s- " % ("  " * indent) if not numbered else "1. "
        text = "".join(_run_markdown(r) for r in body_runs).strip()
        return marker + text

    # 编号列表（Word 原生 numPr）
    ppr = p._p.pPr
    if ppr is not None and ppr.find(qn("w:numPr")) is not None:
        return "1. " + "".join(_run_markdown(r) for r in p.runs).strip()

    text = "".join(_run_markdown(r) for r in p.runs).strip() or p.text.strip()
    return text or None


def _table_markdown(table):
    rows = []
    occupied = set()
    for r, row in enumerate(table.rows):
        cells = []
        for c, cell in enumerate(row.cells):
            if (r, c) in occupied:
                continue
            occupied.add((r, c))
            cells.append(cell.text.strip().replace("\n", " ").replace("|", "\\|"))
        rows.append(cells)
    if not rows:
        return None
    ncols = max(len(r) for r in rows)
    for r in rows:
        r.extend([""] * (ncols - len(r)))
    lines = ["| " + " | ".join(rows[0]) + " |",
             "|" + "|".join([" --- "] * ncols) + "|"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _export_images(doc, images_dir):
    """导出 document part 中的图片，返回 rId -> 相对路径映射。"""
    mapping = {}
    if not images_dir:
        return mapping
    os.makedirs(images_dir, exist_ok=True)
    part = doc.part
    for rel_id, rel in part.rels.items():
        if "image" not in rel.reltype:
            continue
        try:
            blob = rel.target_part.blob
            ext = os.path.splitext(rel.target_part.partname)[1] or ".png"
            fname = "img_%s%s" % (rel_id.replace("rId", ""), ext)
            with open(os.path.join(images_dir, fname), "wb") as f:
                f.write(blob)
            mapping[rel_id] = os.path.join(
                os.path.basename(images_dir), fname).replace("\\", "/")
        except Exception:
            continue
    return mapping


def _find_images_in_xml(doc, mapping):
    """在正文中按出现顺序找出图片引用。"""
    imgs = []
    for blip in doc.element.body.iter(qn("a:blip")):
        rid = blip.get(qn("r:embed"))
        if rid and rid in mapping:
            imgs.append(mapping[rid])
    return imgs


def docx_to_markdown(path, images_dir=None):
    """把 docx 转为 Markdown 文本。images_dir 非空时导出图片。"""
    doc = Document(path)
    mapping = _export_images(doc, images_dir)
    inline_imgs = _find_images_in_xml(doc, mapping)
    img_iter = iter(inline_imgs)

    out = []
    prev_code = False
    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            if block._p.findall(".//" + qn("a:blip")):
                try:
                    out.append("![](%s)" % next(img_iter))
                except StopIteration:
                    pass
                prev_code = False
                continue
            line = _para_markdown(block)
            if line is None:
                continue
            if isinstance(line, list):  # 代码行
                if prev_code and len(out) >= 2 and out[-1] == "```":
                    out.pop()                     # 去掉上一个围栏闭合
                    out[-1] = out[-1] + "\n" + line[1]
                    out.append("```")
                else:
                    out.extend(line)
                prev_code = True
                continue
            prev_code = False
            out.append(line)
        elif isinstance(block, Table):
            prev_code = False
            md = _table_markdown(block)
            if md:
                out.append(md)

    text = "\n\n".join(x for x in out if x)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text + "\n"
