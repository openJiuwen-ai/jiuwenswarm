#!/usr/bin/env python3
"""几何自检：直接解析 .pptx，抓重叠、遮挡、越界和轴线不齐。

不依赖生成代码，对任何 pptx 都能跑。设计目的是补上人眼 QA 的盲区——
contact sheet 缩略图看不见 0.1 英寸级的重叠和错位。

用法:
    python3 qa_geometry.py deck.pptx
    python3 qa_geometry.py deck.pptx --json
    python3 qa_geometry.py deck.pptx --slide 6

坐标一律换算回 10 × 5.625 创作坐标系报告，因为那是 slides.js 里写的数值。
"""

import argparse
import json
import logging
import re
import sys
import zipfile
from dataclasses import dataclass, field

EMU_PER_INCH = 914400
AUTHOR_W, AUTHOR_H = 10.0, 5.625

# 总结条上沿。内容必须在此之上结束（base/template-contract.md）。
SUMMARY_TOP = 4.65

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}


# Program output (report bodies, --json payloads) goes to stdout, diagnostics
# to stderr. Both travel through logging; this logger owns stdout, keeps a bare
# "%(message)s" format so the text is unchanged, and does not propagate so the
# stderr root handler never sees it.
STDOUT_LOGGER = logging.getLogger("qa_geometry.stdout")
STDOUT_LOGGER.propagate = False
STDOUT_LOGGER.setLevel(logging.INFO)
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(logging.Formatter("%(message)s"))
STDOUT_LOGGER.addHandler(_stdout_handler)


def emit(line: str) -> None:
    """报告正文是工具输出，走 stdout logger，不与 stderr 诊断混在一起。"""
    STDOUT_LOGGER.info(line)


@dataclass(frozen=True)
class Offset:
    """group 造成的坐标偏移（已换算为英寸）。"""
    dx: float = 0.0
    dy: float = 0.0


@dataclass
class Element:
    z: int                      # 文档顺序 = z-order，越大越靠上
    kind: str                   # sp | pic | graphicFrame | grpSp
    name: str
    x: float
    y: float
    w: float
    h: float
    filled: bool = False        # 有不透明填充
    text: str = ""
    font_pts: list = field(default_factory=list)

    @property
    def x2(self):
        return self.x + self.w

    @property
    def y2(self):
        return self.y + self.h

    @property
    def area(self):
        return max(0.0, self.w) * max(0.0, self.h)

    @property
    def has_text(self):
        return bool(self.text.strip())

    def label(self):
        t = self.text.strip().replace("\n", " ")
        if len(t) > 22:
            t = t[:22] + "…"
        return f'"{t}"' if t else f"<{self.kind} {self.name}>"


def intersect(a: Element, b: Element):
    """返回 (相交面积, 相交矩形)。"""
    x1, y1 = max(a.x, b.x), max(a.y, b.y)
    x2, y2 = min(a.x2, b.x2), min(a.y2, b.y2)
    if x2 <= x1 or y2 <= y1:
        return 0.0, None
    return (x2 - x1) * (y2 - y1), (x1, y1, x2 - x1, y2 - y1)


def contains(outer: Element, inner: Element, tol=0.02):
    return (outer.x - tol <= inner.x and outer.y - tol <= inner.y
            and outer.x2 + tol >= inner.x2 and outer.y2 + tol >= inner.y2)


# ---------------------------------------------------------------- XML 解析

def _lname(tag):
    return tag.split("}")[-1]


def _xfrm(node):
    """从 spPr/xfrm 或 grpSpPr/xfrm 读 off/ext，返回 EMU。"""
    for child in node:
        if _lname(child.tag) in ("spPr", "grpSpPr", "xfrm"):
            xfrm = child if _lname(child.tag) == "xfrm" else child.find("a:xfrm", NS)
            if xfrm is None:
                continue
            off = xfrm.find("a:off", NS)
            ext = xfrm.find("a:ext", NS)
            if off is None or ext is None:
                continue
            return (int(off.get("x", 0)), int(off.get("y", 0)),
                    int(ext.get("cx", 0)), int(ext.get("cy", 0)))
    # graphicFrame 的 xfrm 是直接子元素
    xfrm = node.find("p:xfrm", NS)
    if xfrm is not None:
        off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
        if off is not None and ext is not None:
            return (int(off.get("x", 0)), int(off.get("y", 0)),
                    int(ext.get("cx", 0)), int(ext.get("cy", 0)))
    return None


def _is_filled(node):
    """判断有没有不透明填充。noFill / 无 fill 元素都算透明。"""
    sp_pr = node.find("p:spPr", NS)
    if sp_pr is None:
        return False
    for tag in ("solidFill", "gradFill", "blipFill", "pattFill"):
        el = sp_pr.find(f"a:{tag}", NS)
        if el is not None:
            # 检查 alpha，全透明不算
            alpha = el.find(".//a:alpha", NS)
            if alpha is not None and int(alpha.get("val", 100000)) < 20000:
                return False
            return True
    return False


def _text_and_fonts(node):
    txt, fonts = [], []
    for r in node.iter():
        ln = _lname(r.tag)
        if ln == "t" and r.text:
            txt.append(r.text)
        elif ln in ("rPr", "defRPr", "endParaRPr"):
            sz = r.get("sz")
            if sz:
                fonts.append(int(sz) / 100.0)
    return "".join(txt), fonts


def _walk(container, scale, elements, z_start=0, offset=Offset()):
    """递归收集元素。offset 是 group 造成的偏移（已换算为英寸）。"""
    z = z_start
    for node in container:
        ln = _lname(node.tag)
        if ln not in ("sp", "pic", "graphicFrame", "grpSp"):
            continue
        geom = _xfrm(node)
        if geom is None:
            continue
        x, y, cx, cy = [v / EMU_PER_INCH / scale for v in geom]
        x, y = x + offset.dx, y + offset.dy

        nv = node.find(f"p:nv{'Grp' if ln == 'grpSp' else ''}SpPr/p:cNvPr", NS)
        if nv is None:
            for c in node.iter():
                if _lname(c.tag) == "cNvPr":
                    nv = c
                    break
        name = nv.get("name", "?") if nv is not None else "?"

        if ln == "grpSp":
            # group 内部坐标需按 chOff/chExt 映射；这里做常见情况的近似处理
            z = _walk(node, scale, elements, z, Offset(dx=x, dy=y))
            continue

        text, fonts = _text_and_fonts(node)
        elements.append(Element(
            z=z, kind=ln, name=name, x=x, y=y, w=cx, h=cy,
            filled=_is_filled(node) if ln == "sp" else True,
            text=text, font_pts=fonts,
        ))
        z += 1
    return z


def parse_slide(xml_bytes, scale):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_bytes)
    sp_tree = root.find(".//p:cSld/p:spTree", NS)
    elements = []
    if sp_tree is not None:
        _walk(sp_tree, scale, elements)
    return elements


def _rel_target(zf, part, want):
    """从某个 part 的 .rels 里找指定类型的目标 part 路径。"""
    base, name = part.rsplit("/", 1)
    rels = f"{base}/_rels/{name}.rels"
    if rels not in zf.namelist():
        return None
    xml = zf.read(rels).decode("utf-8", "ignore")
    m = re.search(rf'Target="([^"]*{want}[^"]*)"', xml)
    if not m:
        return None
    tgt = m.group(1)
    if tgt.startswith("../"):
        return "ppt/" + tgt[3:]
    return f"{base}/{tgt}"


def inherited_elements(zf, slide_part, scale):
    """收集从 slideLayout / slideMaster 继承下来的、真正会渲染的元素。

    最典型的错误是「页面自绘页脚 + 母版继承页脚」叠在一起。
    只看 slide XML 永远发现不了，必须把继承层一起算进来。
    空占位符不渲染，按有无文字/图片过滤。
    """
    out = []
    layout = _rel_target(zf, slide_part, "slideLayout")
    parts = []
    if layout:
        parts.append(layout)
        master = _rel_target(zf, layout, "slideMaster")
        if master:
            parts.append(master)
    for p in parts:
        if p not in zf.namelist():
            continue
        for e in parse_slide(zf.read(p), scale):
            if not e.has_text and e.kind != "pic":
                continue                      # 空占位符不渲染
            e.z = -1000                       # 继承层永远画在最底下
            e.name = f"[继承]{e.name}"
            out.append(e)
    return out


def check_duplicate_chrome(slide_els, inherited, tol=0.30):
    """页面自绘元素与继承的固定框架元素重合 —— 底部框架画了两遍。"""
    out = []
    for inh in inherited:
        for e in slide_els:
            # 位置接近且都是文字，或都是图片 —— 判为重复
            same_spot = (abs(e.x - inh.x) < tol and abs(e.y - inh.y) < tol)
            if not same_spot:
                continue
            if e.kind == "pic" and inh.kind == "pic":
                out.append({
                    "type": "duplicate-chrome",
                    "severity": "error",
                    "detail": f"页面自绘图片与继承的 {inh.name} 位置重合，"
                              f"Logo/页脚可能画了两遍",
                })
            elif e.has_text and inh.has_text:
                out.append({
                    "type": "duplicate-chrome",
                    "severity": "error",
                    "detail": f"页面 {e.label()} 与继承的 {inh.label()} 位置重合，"
                              f"密级/页码/页脚可能画了两遍",
                })
    return out


# ---------------------------------------------------------------- 检查项

def ink_box(e: Element):
    """估算文字真正占用的矩形，而不是整个文本框。

    文本框通常开得比文字大，直接用框判重叠会把"卡片左标签 + 右正文"
    这种正常排版误判成碰撞。按字符宽度粗估实际墨迹范围。
    """
    if not e.has_text:
        return e
    pt = max(e.font_pts) if e.font_pts else 12.0
    ch = pt / 72.0                                   # 一个全角字的近似宽度（英寸）
    lines = e.text.split("\n")
    widest = 0.0
    for ln in lines:
        w = sum(ch if ord(c) > 0x2E80 else ch * 0.55 for c in ln)
        widest = max(widest, w)
    # 超出框宽会自动折行
    text_w = min(widest, e.w) if e.w > 0 else widest
    wrapped = sum(max(1, int(sum(ch if ord(c) > 0x2E80 else ch * 0.55
                                 for c in ln) / e.w) + 1) if e.w > 0 else 1
                  for ln in lines)
    text_h = min(e.h, wrapped * pt / 72.0 * 1.35)

    # 水平方向按居中估计（居中和左对齐都常见，居中是较松的假设）
    cx = e.x + e.w / 2
    x = max(e.x, cx - text_w / 2)
    # 垂直方向同样按居中
    cy = e.y + e.h / 2
    y = max(e.y, cy - text_h / 2)
    return Element(z=e.z, kind=e.kind, name=e.name, x=x, y=y,
                   w=min(text_w, e.w), h=min(text_h, e.h),
                   filled=e.filled, text=e.text, font_pts=e.font_pts)


def check_text_collision(els, min_area=0.012, min_frac=0.12):
    """两个元素的实际文字墨迹重叠 —— 字压字，一定是错。"""
    out = []
    texts = [e for e in els if e.has_text]
    inks = {id(e): ink_box(e) for e in texts}
    for i, a in enumerate(texts):
        for b in texts[i + 1:]:
            # 一方完全包住另一方，通常是卡片带标题的正常嵌套
            if contains(a, b) or contains(b, a):
                continue
            ia, ib = inks[id(a)], inks[id(b)]
            area, rect = intersect(ia, ib)
            if area < min_area:
                continue
            if area / min(ia.area, ib.area) < min_frac:
                continue
            out.append({
                "type": "text-collision",
                "severity": "error",
                "detail": f"{a.label()} 与 {b.label()} 文字重叠",
                "overlap_in2": round(area, 4),
                "rect": [round(v, 3) for v in rect],
            })
    return out


def check_occlusion(els, min_area=0.02, min_frac=0.10):
    """后画的不透明形状盖住先画的文字，且没有完全包住 —— 遮挡截断。"""
    out = []
    for a in els:
        if not a.has_text:
            continue
        for b in els:
            if b.z <= a.z or not b.filled or b.has_text:
                continue
            if contains(b, a):
                continue           # 完全覆盖当背景处理，另有检查
            area, rect = intersect(a, b)
            if area < min_area or area / a.area < min_frac:
                continue
            out.append({
                "type": "occlusion",
                "severity": "error",
                "detail": f"{b.label()} 压住 {a.label()}（后者更早绘制）",
                "overlap_in2": round(area, 4),
                "rect": [round(v, 3) for v in rect],
            })
    return out


def check_bounds(els, tol=0.02):
    out = []
    for e in els:
        starts_off_canvas = e.x < -tol or e.y < -tol
        ends_off_canvas = e.x2 > AUTHOR_W + tol or e.y2 > AUTHOR_H + tol
        if starts_off_canvas or ends_off_canvas:
            out.append({
                "type": "out-of-bounds",
                "severity": "error",
                "detail": f"{e.label()} 越出画布：x={e.x:.2f}..{e.x2:.2f} y={e.y:.2f}..{e.y2:.2f}",
            })
        elif e.has_text and e.y2 > SUMMARY_TOP + tol and e.y < SUMMARY_TOP:
            out.append({
                "type": "summary-intrusion",
                "severity": "warning",
                "detail": f"{e.label()} 侵入总结条区域（底端 {e.y2:.2f} > {SUMMARY_TOP}）",
            })
    return out


def check_axis(els, near=0.16, exact=0.012, min_w=3.0):
    """主区块的左右边界近似但不相等 —— 各区各写各的魔数，而非共用常量。

    只看宽度 >= min_w 的大区块（矩阵、路径带、通栏框这类撑起版面的），
    卡片和小标签的边界本来就不该强求对齐。每条轴线只报一次最小偏差。
    """
    out = []
    wide = [e for e in els if e.w >= min_w and e.y < SUMMARY_TOP]
    if len(wide) < 2:
        return out
    for side, get in (("左", lambda e: e.x), ("右", lambda e: e.x2)):
        vals = sorted({round(get(e), 3) for e in wide})
        # 把彼此接近的边界聚成一簇，只对簇内跨度报一次
        cluster = [vals[0]]
        for v in vals[1:] + [float("inf")]:
            if v - cluster[-1] <= near:
                cluster.append(v)
                continue
            if len(cluster) > 1:
                span = cluster[-1] - cluster[0]
                if span > exact:
                    out.append({
                        "type": "axis-drift",
                        "severity": "warning",
                        "detail": f"{side}边界有 {len(cluster)} 个不同取值 "
                                  f"({cluster[0]:.3f}..{cluster[-1]:.3f})，"
                                  f"相差 {span:.3f}\"，应共用同一条轴线常量",
                    })
            cluster = [v]
    return out


# ---------------------------------------------------------------- 主流程

def analyze(path, only_slide=None):
    zf = zipfile.ZipFile(path)

    pres = zf.read("ppt/presentation.xml").decode("utf-8", "ignore")
    m = re.search(r'sldSz[^/]*cx="(\d+)"', pres)
    canvas_w = int(m.group(1)) / EMU_PER_INCH if m else 13.3333
    scale = canvas_w / AUTHOR_W        # 成品画布 / 创作画布

    names = sorted(
        (n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)),
    )

    report = {
        "pptx": path,
        "canvas_in": round(canvas_w, 3),
        "scale_to_authoring": round(scale, 4),
        "slides": len(names),
        "findings": [],
    }

    for idx, n in enumerate(names, 1):
        if only_slide and idx != only_slide:
            continue
        els = parse_slide(zf.read(n), scale)
        inh = inherited_elements(zf, n, scale)
        found = (check_text_collision(els) + check_occlusion(els)
                 + check_bounds(els) + check_axis(els)
                 + check_duplicate_chrome(els, inh))
        for f in found:
            f["slide"] = idx
        report["findings"].extend(found)

    report["errors"] = sum(1 for f in report["findings"] if f["severity"] == "error")
    report["warnings"] = sum(1 for f in report["findings"] if f["severity"] == "warning")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pptx")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--slide", type=int)
    args = ap.parse_args()

    rep = analyze(args.pptx, args.slide)

    if args.json:
        emit(json.dumps(rep, ensure_ascii=False, indent=2))
        return 1 if rep["errors"] else 0

    emit(f"qa-geometry: {rep['errors']} error(s), {rep['warnings']} warning(s), "
         f"slides={rep['slides']}, 坐标已换算回 10×5.625")
    by_slide = {}
    for f in rep["findings"]:
        by_slide.setdefault(f["slide"], []).append(f)
    for slide_no, slide_findings in sorted(by_slide.items()):
        emit(f"\n— slide {slide_no}")
        for f in slide_findings:
            mark = "✗" if f["severity"] == "error" else "!"
            emit(f"  {mark} [{f['type']}] {f['detail']}")
    return 1 if rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
