#!/usr/bin/env python3
# ocr_offline.py — 纯本地离线 OCR 引擎（RapidOCR 驱动）
# --------------------------------------------------------------------------
# 输入：PDF 文件 / 单张图片 / 图片目录
# 输出：<同名>_OCR.md  （按页/按图分段的纯文本，含页码标记）
#       可选：<同名>_OCR_boxes.json（原始文本框，供表格重建/调试）
# 全程不联网，适合涉密安全资料。
#
# 用法:
#   python ocr_offline.py "<文件或目录>"
#   python ocr_offline.py "扫描件.pdf" -o "输出.md" -d 300
#   python ocr_offline.py "图片目录/" --no-preprocess
#   python ocr_offline.py "扫描件.pdf" --start-page 3 --end-page 10
#
# 依赖: rapidocr-onnxruntime, pymupdf(fitz), pillow, numpy
# --------------------------------------------------------------------------
import sys, os, json, argparse, datetime

# 保证同目录模块可被 import（防止 cwd 不在 scripts 时失败）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def _ensure_deps():
    # 环境自检：缺失依赖自动安装（仅白名单内，透明，首次运行需联网一次）
    try:
        import setup
        return setup.ensure_deps()
    except Exception as _e:
        sys.stderr.write(f"[warn] 依赖自检跳过：{_e}\n")
        return False

# 预处理（摆正 + 灰度增强）逻辑在 preprocess_img.py，供多脚本共用
from preprocess_img import preprocess, _estimate_skew


# ----------------------------------------------------------------------------
# 文本框按阅读顺序重排（先按行 y 聚类，行内按 x 排序）
# ----------------------------------------------------------------------------
def _sort_boxes(boxes_texts):
    """boxes_texts: list of (box[[x,y]*4], text)。返回按行/列排好的 text 列表。"""
    import numpy as np
    if not boxes_texts:
        return []
    items = []
    for box, txt in boxes_texts:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append((np.mean(xs), np.mean(ys), txt))
    ys_sorted = sorted(i[1] for i in items)
    gaps = [ys_sorted[i + 1] - ys_sorted[i] for i in range(len(ys_sorted) - 1)]
    line_h = (sorted(gaps)[len(gaps) // 2] if gaps else 20) or 20
    tol = max(line_h * 0.6, 8)
    items.sort(key=lambda t: t[1])
    lines, cur, cur_y = [], [], None
    for x, y, t in items:
        if cur_y is None or abs(y - cur_y) <= tol:
            cur.append((x, t))
            cur_y = y if cur_y is None else (cur_y + y) / 2
        else:
            lines.append(cur)
            cur, cur_y = [(x, t)], y
    if cur:
        lines.append(cur)
    out = []
    for line in lines:
        line.sort(key=lambda t: t[0])
        out.append(" ".join(t for _, t in line))
    return out


# ----------------------------------------------------------------------------
# 单图 OCR
# ----------------------------------------------------------------------------
def ocr_image(engine, path, do_preprocess=True):
    from PIL import Image
    import numpy as np
    img = Image.open(path)
    if do_preprocess:
        proc = preprocess(img, do_deskew=True)
    else:
        proc = img.convert("L")
    arr = np.asarray(proc)
    result, _ = engine(arr)
    if not result:
        return [], []
    boxes_texts = [(b, t) for b, t, s in result]
    lines = _sort_boxes(boxes_texts)
    return lines, result


# ----------------------------------------------------------------------------
# PDF 渲染为临时 PNG（复用 fitz）
# ----------------------------------------------------------------------------
def render_pdf(pdf, out_dir, dpi, start_page, end_page):
    import fitz
    base = os.path.splitext(os.path.basename(pdf))[0]
    pages_dir = os.path.join(out_dir, f"{base}_pages")
    os.makedirs(pages_dir, exist_ok=True)
    doc = fitz.open(pdf)
    n = len(doc)
    s = max(1, start_page or 1)
    e = min(n, end_page or n)
    paths = []
    for i in range(s - 1, e):
        pix = doc[i].get_pixmap(dpi=dpi)
        p = os.path.join(pages_dir, f"page_{i+1:02d}.png")
        pix.save(p)
        paths.append(p)
    doc.close()
    return paths, s, e


def collect_images(inp):
    exts = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")
    if os.path.isdir(inp):
        files = [os.path.join(inp, f) for f in sorted(os.listdir(inp))
                 if f.lower().endswith(exts)]
        return files, None
    if inp.lower().endswith(exts):
        return [inp], None
    if inp.lower().endswith(".pdf"):
        return [inp], "pdf"
    print(f"ERROR: 不支持的输入类型: {inp}")
    sys.exit(1)


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="纯本地离线 OCR（RapidOCR）：PDF/图片 → Markdown")
    ap.add_argument("input", help="PDF 文件 / 图片 / 图片目录")
    ap.add_argument("-o", "--out", default=None,
                    help="输出 .md 路径，默认 <同名>_OCR.md")
    ap.add_argument("-d", "--dpi", type=int, default=300,
                    help="PDF 渲染 DPI，默认 300")
    ap.add_argument("--no-preprocess", action="store_true",
                    help="跳过摆正/灰度增强（原文直读）")
    ap.add_argument("--start-page", type=int, default=None,
                    help="PDF 起始页（含，1-based）")
    ap.add_argument("--end-page", type=int, default=None,
                    help="PDF 结束页（含，1-based）")
    ap.add_argument("--boxes-json", action="store_true",
                    help="额外导出原始文本框 _OCR_boxes.json")
    args = ap.parse_args()

    # 环境自检：缺失依赖自动安装（仅白名单，透明，首次需联网一次）
    _ensure_deps()

    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        print("ERROR: 未安装 rapidocr-onnxruntime，请先执行:\n"
              "  pip install -r requirements.txt")
        sys.exit(1)

    print("加载 RapidOCR 引擎（首次加载本地 ONNX 模型，稍候）...")
    engine = RapidOCR()

    inp = args.input
    out_md = args.out
    if not out_md:
        stem = os.path.splitext(os.path.basename(inp))[0]
        if os.path.isdir(inp):
            out_md = os.path.join(inp, f"{stem}_OCR.md")
        else:
            out_md = os.path.join(os.path.dirname(inp) or ".", f"{stem}_OCR.md")

    files, kind = collect_images(inp)
    if not files:
        print("ERROR: 未在输入中找到任何可处理文件")
        sys.exit(1)

    pages = []  # (label, lines, boxes, src_path)
    if kind == "pdf":
        print(f"渲染 PDF: {inp}")
        paths, s, e = render_pdf(inp, os.path.dirname(out_md) or ".",
                                 args.dpi, args.start_page, args.end_page)
        for idx, p in enumerate(paths):
            label = f"第 {s + idx} 页"
            print(f"  OCR {label} <- {os.path.basename(p)}  [{idx+1}/{len(paths)}]")
            lines, boxes = ocr_image(engine, p, not args.no_preprocess)
            pages.append((label, lines, boxes, p))
    else:
        for i, p in enumerate(files):
            label = os.path.splitext(os.path.basename(p))[0]
            print(f"  OCR {label} <- {p}  [{i+1}/{len(files)}]")
            lines, boxes = ocr_image(engine, p, not args.no_preprocess)
            pages.append((label, lines, boxes, p))

    # 组装 Markdown
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = []
    md.append("# OCR 识别结果（本地离线 · RapidOCR）")
    md.append("")
    md.append(f"> 来源：{inp}")
    md.append(f"> 生成时间：{now}")
    md.append("> 引擎：RapidOCR（纯本地 ONNX，不联网、不上传）")
    md.append(f"> 共 {len(pages)} 页/图")
    md.append("")
    md.append("---")
    md.append("")
    for label, lines, boxes, src in pages:
        md.append(f"## {label}")
        md.append("")
        if lines:
            md.extend(lines)
        else:
            md.append("（本页未识别到文字，可能原因与排查建议：")
            md.append("- 图片分辨率过低：尝试用更高 DPI 重新渲染，或加 `-d 300/400`；")
            md.append("- 内容为手写体 / 印章 / 严重倾斜：引擎 A 对手写弱，建议走「多模态读图」路线 B 复核；")
            md.append("- 确需保留原图：可加 `--no-preprocess` 关闭摆正后再试；")
            md.append("- 确认该页确无印刷体文字（纯图 / 空白页）。）")
        md.append("")
    md.append("---")
    md.append("")
    md.append("⚠️ OCR 结果由离线引擎识别，关键数字、签名、印章请人工核对。"
              "手写体与倾斜严重页面识别率较低，可用本技能「多模态增强」路线补强。")

    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print(f"DONE -> {out_md}")

    if args.boxes_json:
        jpath = os.path.splitext(out_md)[0] + "_boxes.json"
        dump = [{"label": l, "src": s, "boxes": b} for l, _, b, s in pages]
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=2)
        print(f"BOXES -> {jpath}")

    # 提示 Agent：可用多模态读图对 PNG 做手写/表格增强
    print("提示：以下 PNG 可供支持多模态读图的 Agent（如 WorkBuddy 的 Read 工具）以增强手写/表格识别——")
    for label, _, _, src in pages:
        print(f"  [{label}] {src}")


if __name__ == "__main__":
    main()
