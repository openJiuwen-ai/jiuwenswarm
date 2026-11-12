#!/usr/bin/env python3
# pdf_to_images.py — 将 PDF 每页渲染为高清 PNG（本地离线，依赖 PyMuPDF）
# 增强版：支持起止页、批量目录、图片预处理（摆正+灰度）、超大页分块。
#
# 用法:
#   python pdf_to_images.py "<PDF路径>" [-o 输出目录] [-d DPI] [-p 起] [-n 止]
#   python pdf_to_images.py "<PDF目录>" -b            # 批量渲染目录下所有 PDF
#   python pdf_to_images.py "扫描件.pdf" --preprocess # 渲染后自动摆正+灰度
#   python pdf_to_images.py "大图.pdf" --tiles 2       # 每页切 2x2 块便于读图
# --------------------------------------------------------------------------
import sys, os, argparse

# 保证同目录模块可被 import（防止 cwd 不在 scripts 时失败）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 环境自检：缺失依赖自动安装（仅白名单内，透明，首次运行需联网一次）
try:
    import setup
    setup.ensure_deps()
except Exception as _e:
    sys.stderr.write(f"[warn] 依赖自检跳过：{_e}\n")


def render_one(src, out_base, dpi, start_page, end_page, do_preprocess,
               tiles):
    import fitz
    from PIL import Image
    from preprocess_img import preprocess

    doc = fitz.open(src)
    n = len(doc)
    s = max(1, start_page or 1)
    e = min(n, end_page or n)
    base = os.path.splitext(os.path.basename(src))[0]
    out = os.path.join(out_base, f"{base}_pages")
    os.makedirs(out, exist_ok=True)

    count = 0
    for i in range(s - 1, e):
        pix = doc[i].get_pixmap(dpi=dpi)
        png = os.path.join(out, f"page_{i+1:02d}.png")
        pix.save(png)
        # 可选：预处理（摆正 + 灰度）
        if do_preprocess:
            try:
                im = preprocess(Image.open(png), do_deskew=True)
                im.save(png)
            except Exception as ex:
                print(f"    [warn] 预处理跳过: {ex}")
        # 可选：超大页分块
        if tiles and tiles > 1:
            try:
                im = Image.open(png)
                w, h = im.size
                tw, th = w // tiles, h // tiles
                for r in range(tiles):
                    for c in range(tiles):
                        box = (c * tw, r * th, (c + 1) * tw, (r + 1) * th)
                        tile = im.crop(box)
                        tp = os.path.join(out, f"page_{i+1:02d}_tile_r{r+1}c{c+1}.png")
                        tile.save(tp)
                print(f"  page_{i+1:02d}.png  {w}x{h}  "
                      f"{os.path.getsize(png)//1024}KB  +{tiles*tiles}块 -> {out}")
            except Exception as ex:
                print(f"    [warn] 分块跳过: {ex}")
        else:
            print(f"  page_{i+1:02d}.png  {pix.width}x{pix.height}  "
                  f"{os.path.getsize(png)//1024}KB  -> {out}")
        count += 1
    doc.close()
    return count, out


def main():
    ap = argparse.ArgumentParser(
        description="将 PDF 每页渲染为高清 PNG（本地离线，可增强）")
    ap.add_argument("pdf", help="PDF 文件 或 含多个 PDF 的目录（配合 -b）")
    ap.add_argument("-o", "--out", default=None,
                    help="输出目录，默认在 PDF 同目录建 <文件名>_pages")
    ap.add_argument("-d", "--dpi", type=int, default=300,
                    help="渲染 DPI，默认 300（太大可降到 200）")
    ap.add_argument("-p", "--start-page", type=int, default=None,
                    help="起始页（含，1-based），默认第 1 页")
    ap.add_argument("-n", "--end-page", type=int, default=None,
                    help="结束页（含，1-based），默认末页")
    ap.add_argument("-b", "--batch", action="store_true",
                    help="将输入视为目录，批量渲染其中所有 PDF")
    ap.add_argument("--preprocess", action="store_true",
                    help="渲染后自动摆正(倾斜角校正)+灰度增强")
    ap.add_argument("-t", "--tiles", type=int, default=0,
                    help="每页切 N×N 块(如 2)，便于超大页读图；0=不切")
    args = ap.parse_args()

    try:
        import fitz
    except ImportError:
        print("ERROR: 未安装 PyMuPDF，请先执行: pip install -r requirements.txt")
        sys.exit(1)

    if args.batch or os.path.isdir(args.pdf):
        if not os.path.isdir(args.pdf):
            print("ERROR: 启用 -b 时输入必须是目录")
            sys.exit(1)
        pdfs = [os.path.join(args.pdf, f) for f in sorted(os.listdir(args.pdf))
                if f.lower().endswith(".pdf")]
        if not pdfs:
            print("ERROR: 目录中未找到 PDF")
            sys.exit(1)
        out_base = args.out or args.pdf
        total = 0
        for pdf in pdfs:
            print(f"渲染: {pdf}")
            c, _ = render_one(pdf, out_base, args.dpi, args.start_page,
                              args.end_page, args.preprocess, args.tiles)
            total += c
        print(f"DONE 共 {total} 页 -> {out_base}")
    else:
        src = args.pdf
        if not os.path.isfile(src):
            print(f"ERROR: 文件不存在: {src}")
            sys.exit(1)
        out_base = args.out or (os.path.dirname(src) or ".")
        c, out = render_one(src, out_base, args.dpi, args.start_page,
                           args.end_page, args.preprocess, args.tiles)
        print(f"DONE -> {out}  ({c} 页)")


if __name__ == "__main__":
    main()
