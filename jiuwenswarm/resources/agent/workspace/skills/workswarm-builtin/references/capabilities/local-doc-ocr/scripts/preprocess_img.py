#!/usr/bin/env python3
# preprocess_img.py — 图片预处理（自动摆正 + 灰度增强）
# 供 pdf_to_images.py 与 ocr_offline.py 共用，避免逻辑分叉。
#
# 用法（独立运行）:
#   python preprocess_img.py "<图片>" [-o 输出png] [--no-deskew]
# --------------------------------------------------------------------------
import sys, os, argparse


def _estimate_skew(gray):
    """用水平投影方差估计倾斜角（±10°，步进 0.5）。返回角度(度)。"""
    import numpy as np
    from PIL import Image
    arr = np.asarray(gray)
    h, w = arr.shape
    scale = max(1, int(min(h, w) / 600))
    small = (arr[::scale, ::scale] if scale > 1 else arr).astype(np.float32)
    thr = small.mean()
    bin0 = (small < thr).astype(np.float32)
    base = float(np.var(bin0.sum(axis=1)))
    best, best_score = 0.0, base
    sim = Image.fromarray(small.astype(np.uint8))
    for ang in [a * 0.5 for a in range(-20, 21) if a != 0]:
        rot = np.asarray(sim.rotate(ang, expand=False,
                                    resample=Image.BILINEAR)).astype(np.float32)
        t = rot.mean()
        proj = (rot < t).astype(np.float32).sum(axis=1)
        score = float(np.var(proj))
        if score > best_score:
            best_score, best = score, ang
    return best


def preprocess(img, do_deskew=True):
    """返回预处理后的 PIL.Image（灰度 'L'）。

    img: PIL.Image。流程：EXIF 方向摆正 → 灰度 → (可选)倾斜角校正 → 锐化。
    """
    from PIL import Image, ImageOps, ImageFilter
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    gray = img.convert("L")
    if do_deskew:
        try:
            ang = _estimate_skew(gray)
            if abs(ang) >= 0.5:
                gray = gray.rotate(ang, expand=True,
                                   resample=Image.BICUBIC, fillcolor=255)
        except Exception:
            pass
    try:
        gray = gray.filter(ImageFilter.SHARPEN)
    except Exception:
        pass
    return gray


def main():
    ap = argparse.ArgumentParser(description="图片预处理：摆正 + 灰度增强")
    ap.add_argument("image", help="输入图片路径")
    ap.add_argument("-o", "--out", default=None, help="输出 PNG，默认 <名>_pre.png")
    ap.add_argument("--no-deskew", action="store_true", help="跳过倾斜角校正")
    args = ap.parse_args()
    from PIL import Image
    img = Image.open(args.image)
    out = args.out or (os.path.splitext(args.image)[0] + "_pre.png")
    proc = preprocess(img, do_deskew=not args.no_deskew)
    proc.save(out)
    print(f"DONE -> {out}  ({proc.width}x{proc.height})")


if __name__ == "__main__":
    main()
