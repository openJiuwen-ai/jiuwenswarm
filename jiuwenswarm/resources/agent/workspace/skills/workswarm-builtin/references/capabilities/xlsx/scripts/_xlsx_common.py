#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for the XLSX XML-editing scripts.

Used by xlsx_add_column.py / xlsx_insert_row.py / xlsx_shift_rows.py.
Keeps all XML UTF-8 and edits worksheet parts in place (zero format loss).
"""
import os
import re
from lxml import etree

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XMLNS = "http://www.w3.org/XML/1998/namespace"
M = "{" + MAIN + "}"
R = "{" + RNS + "}"

_RANGE = re.compile(r"(\$?[A-Z]+\$?)(\d+):(\$?[A-Z]+\$?)(\d+)")


def col_to_num(col):
    col = re.sub(r"[^A-Za-z]", "", col)
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n


def num_to_col(n):
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def split_ref(ref):
    m = re.match(r"([A-Za-z]+)(\d+)", ref)
    return m.group(1).upper(), int(m.group(2))


def parse(path):
    return etree.parse(path, etree.XMLParser(remove_blank_text=False))


def write(tree, path):
    tree.write(path, xml_declaration=True, encoding="UTF-8", standalone=True)


def first_sheet_name(workdir):
    t = parse(os.path.join(workdir, "xl", "workbook.xml"))
    for s in t.getroot().iter(M + "sheet"):
        return s.get("name")
    raise SystemExit("no sheets found in workbook.xml")


def resolve_worksheet(workdir, sheet_name):
    """Map a sheet display name to its worksheet XML path."""
    t = parse(os.path.join(workdir, "xl", "workbook.xml"))
    rid = None
    for s in t.getroot().iter(M + "sheet"):
        if s.get("name") == sheet_name:
            rid = s.get(R + "id")
            break
    if rid is None:
        raise SystemExit("sheet not found: " + str(sheet_name))
    rt = parse(os.path.join(workdir, "xl", "_rels", "workbook.xml.rels"))
    target = None
    for rel in rt.getroot():
        if rel.get("Id") == rid:
            target = rel.get("Target")
            break
    if target is None:
        raise SystemExit("relationship not found for sheet: " + str(sheet_name))
    if target.startswith("/"):
        return os.path.normpath(os.path.join(workdir, target.lstrip("/")))
    return os.path.normpath(os.path.join(workdir, "xl", target))


def get_sheetdata(root):
    sd = root.find(M + "sheetData")
    if sd is None:
        sd = etree.SubElement(root, M + "sheetData")
    return sd


def get_or_make_row(sheetdata, rownum):
    for r in sheetdata.findall(M + "row"):
        if int(r.get("r")) == rownum:
            return r
    r = etree.SubElement(sheetdata, M + "row")
    r.set("r", str(rownum))
    return r


def make_cell(ref, formula=None, value=None, text=None, style=None):
    c = etree.Element(M + "c")
    c.set("r", ref)
    if style is not None:
        c.set("s", str(style))
    if text is not None:
        c.set("t", "inlineStr")
        is_ = etree.SubElement(c, M + "is")
        t = etree.SubElement(is_, M + "t")
        t.set("{" + XMLNS + "}space", "preserve")
        t.text = text
    elif formula is not None:
        f = etree.SubElement(c, M + "f")
        f.text = formula.lstrip("=")
    elif value is not None:
        v = etree.SubElement(c, M + "v")
        v.text = str(value)
    return c


def set_cell_in_row(row, cell):
    """Insert/replace a cell keeping column order within the row."""
    ref = cell.get("r")
    col = col_to_num(split_ref(ref)[0])
    for ex in row.findall(M + "c"):
        if ex.get("r") == ref:
            row.replace(ex, cell)
            return
    for ex in row.findall(M + "c"):
        if col_to_num(split_ref(ex.get("r"))[0]) > col:
            ex.addprevious(cell)
            return
    row.append(cell)


def sort_sheetdata(sheetdata):
    rows = sheetdata.findall(M + "row")
    rows.sort(key=lambda r: int(r.get("r")))
    for r in rows:
        cells = r.findall(M + "c")
        cells.sort(key=lambda c: col_to_num(split_ref(c.get("r"))[0]))
        for c in cells:
            r.append(c)
        sheetdata.append(r)


def shift_rows(sheetdata, at, delta):
    """Shift every row with index >= at by delta (row@r and each cell@r)."""
    for r in sheetdata.findall(M + "row"):
        rn = int(r.get("r"))
        if rn >= at:
            new = rn + delta
            r.set("r", str(new))
            for c in r.findall(M + "c"):
                col, _ = split_ref(c.get("r"))
                c.set("r", col + str(new))


def extend_ranges(sheetdata, at, delta=1):
    """Best-effort: bump A1:A9 style range endpoints with row >= at by delta.
    Handles the common 'total below' SUM case so inserted rows are included.
    Single-cell refs are left untouched (verify totals after)."""
    def repl(m):
        c1, r1, c2, r2 = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
        if r1 >= at:
            r1 += delta
        if r2 >= at:
            r2 += delta
        return c1 + str(r1) + ":" + c2 + str(r2)
    for r in sheetdata.findall(M + "row"):
        for c in r.findall(M + "c"):
            f = c.find(M + "f")
            if f is not None and f.text:
                f.text = _RANGE.sub(repl, f.text)


def remove_rows(sheetdata, at, count):
    for r in list(sheetdata.findall(M + "row")):
        rn = int(r.get("r"))
        if at <= rn < at + count:
            sheetdata.remove(r)


def update_dimension(root):
    sd = root.find(M + "sheetData")
    if sd is None:
        return
    minr = minc = None
    maxr = maxc = 0
    for r in sd.findall(M + "row"):
        rn = int(r.get("r"))
        maxr = max(maxr, rn)
        minr = rn if minr is None else min(minr, rn)
        for c in r.findall(M + "c"):
            cn = col_to_num(split_ref(c.get("r"))[0])
            maxc = max(maxc, cn)
            minc = cn if minc is None else min(minc, cn)
    dim = root.find(M + "dimension")
    if dim is not None and maxr > 0:
        dim.set("ref", num_to_col(minc or 1) + str(minr or 1) + ":"
                + num_to_col(maxc or 1) + str(maxr))


class Styles:
    """Lightweight helper to append numFmt / border / cellXf to xl/styles.xml."""

    def __init__(self, workdir):
        self.path = os.path.join(workdir, "xl", "styles.xml")
        self.tree = parse(self.path)
        self.root = self.tree.getroot()

    def add_numfmt(self, code):
        nfs = self.root.find(M + "numFmts")
        if nfs is None:
            nfs = etree.Element(M + "numFmts")
            self.root.insert(0, nfs)
        for n in nfs.findall(M + "numFmt"):
            if n.get("formatCode") == code:
                return int(n.get("numFmtId"))
        ids = [int(n.get("numFmtId")) for n in nfs.findall(M + "numFmt")]
        nid = max(ids + [163]) + 1
        nf = etree.SubElement(nfs, M + "numFmt")
        nf.set("numFmtId", str(nid))
        nf.set("formatCode", code)
        nfs.set("count", str(len(nfs.findall(M + "numFmt"))))
        return nid

    def add_top_border(self, style="medium"):
        bs = self.root.find(M + "borders")
        if bs is None:
            bs = etree.Element(M + "borders")
            csx = self.root.find(M + "cellStyleXfs")
            if csx is not None:
                csx.addprevious(bs)
            else:
                self.root.append(bs)
        b = etree.SubElement(bs, M + "border")
        etree.SubElement(b, M + "left")
        etree.SubElement(b, M + "right")
        top = etree.SubElement(b, M + "top")
        top.set("style", style)
        etree.SubElement(b, M + "bottom")
        etree.SubElement(b, M + "diagonal")
        bs.set("count", str(len(bs.findall(M + "border"))))
        return len(bs.findall(M + "border")) - 1

    def add_xf(self, numFmtId=0, borderId=0):
        cx = self.root.find(M + "cellXfs")
        if cx is None:
            cx = etree.SubElement(self.root, M + "cellXfs")
        xf = etree.SubElement(cx, M + "xf")
        xf.set("numFmtId", str(numFmtId))
        xf.set("fontId", "0")
        xf.set("fillId", "0")
        xf.set("borderId", str(borderId))
        xf.set("xfId", "0")
        if numFmtId:
            xf.set("applyNumberFormat", "1")
        if borderId:
            xf.set("applyBorder", "1")
        cx.set("count", str(len(cx.findall(M + "xf"))))
        return len(cx.findall(M + "xf")) - 1

    def save(self):
        write(self.tree, self.path)
