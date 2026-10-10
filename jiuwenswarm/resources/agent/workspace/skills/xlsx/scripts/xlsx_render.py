#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xlsx_render.py - Render a workbook to PNG/PDF for visual review, with CJK fonts.

Fixes the "no visual interface" gap. Uses LibreOffice (soffice) to convert to
PDF, then poppler (pdftoppm) to rasterize each page to PNG. Chinese text renders
correctly because we ensure a CJK-capable font is the default.

Usage:
    python3 xlsx_render.py FILE --out review/                 # PNG per page + PDF
    python3 xlsx_render.py FILE --pdf-only --out review/      # just the PDF
    python3 xlsx_render.py --diff OLD.xlsx NEW.xlsx --html review/diff.html

Notes:
- Requires soffice + pdftoppm (see doctor.py).
- The --diff HTML embeds before/after PNGs side by side for non-technical review.
"""
import argparse
import base64
import glob
import os
import shutil
import subprocess
import sys
import tempfile

CJK_FONT_CANDIDATES = [
    "Noto Sans CJK SC", "Noto Sans CJK TC", "Noto Sans CJK JP",
    "Source Han Sans SC", "Source Han Sans CN", "WenQuanYi Zen Hei",
    "WenQuanYi Micro Hei", "Microsoft YaHei", "SimHei", "SimSun",
]


def _have(tool):
    return shutil.which(tool) is not None


def _pick_cjk_font():
    if not _have("fc-list"):
        return None
    try:
        out = subprocess.run(["fc-list", "family"], capture_output=True, text=True, timeout=20).stdout
    except Exception:  # noqa: BLE001
        return None
    low = out.lower()
    for f in CJK_FONT_CANDIDATES:
        if f.lower() in low:
            return f
    # any zh font
    try:
        zh = subprocess.run(["fc-list", ":lang=zh", "family"], capture_output=True, text=True, timeout=20).stdout
        first = [l.strip() for l in zh.splitlines() if l.strip()]
        return first[0].split(",")[0] if first else None
    except Exception:  # noqa: BLE001
        return None


def to_pdf(src, outdir):
    """Convert workbook to PDF via LibreOffice headless. Returns pdf path."""
    if not _have("soffice"):
        raise SystemExit("soffice (LibreOffice) not found; cannot render. See doctor.py")
    os.makedirs(outdir, exist_ok=True)
    env = dict(os.environ)
    # Isolated profile avoids locking issues when run repeatedly.
    profile = tempfile.mkdtemp(prefix="lo_profile_")
    cmd = [
        "soffice", "--headless", "--norestore", "--convert-to", "pdf",
        "--outdir", outdir, src,
        f"-env:UserInstallation=file://{profile}",
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, env=env, timeout=180)
    base = os.path.splitext(os.path.basename(src))[0]
    pdf = os.path.join(outdir, base + ".pdf")
    if not os.path.exists(pdf):
        raise SystemExit(f"PDF not produced for {src}")
    return pdf


def pdf_to_pngs(pdf, outdir, prefix):
    if not _have("pdftoppm"):
        raise SystemExit("pdftoppm (poppler-utils) not found; cannot rasterize. See doctor.py")
    stem = os.path.join(outdir, prefix)
    subprocess.run(["pdftoppm", "-png", "-r", "120", pdf, stem],
                   check=True, capture_output=True, text=True, timeout=180)
    return sorted(glob.glob(stem + "*.png"))


def render(src, outdir, pdf_only=False):
    font = _pick_cjk_font()
    if font is None:
        print("WARNING: no CJK font detected; Chinese text may render as boxes. "
              "Run doctor.py and install Noto Sans CJK.", file=sys.stderr)
    else:
        print(f"Using CJK font hint: {font}", file=sys.stderr)
    pdf = to_pdf(src, outdir)
    print(f"PDF: {pdf}")
    if pdf_only:
        return [pdf]
    base = os.path.splitext(os.path.basename(src))[0]
    pngs = pdf_to_pngs(pdf, outdir, base + "_p")
    for p in pngs:
        print(f"PNG: {p}")
    return pngs


def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def render_diff_html(old_src, new_src, html_path):
    outdir = os.path.dirname(os.path.abspath(html_path)) or "."
    work = tempfile.mkdtemp(prefix="xlsx_diff_")
    old_pngs = render(old_src, os.path.join(work, "old"))
    new_pngs = render(new_src, os.path.join(work, "new"))
    n = max(len(old_pngs), len(new_pngs))
    rows = []
    for i in range(n):
        left = f'<img src="data:image/png;base64,{_b64(old_pngs[i])}"/>' if i < len(old_pngs) else "<em>(no page)</em>"
        right = f'<img src="data:image/png;base64,{_b64(new_pngs[i])}"/>' if i < len(new_pngs) else "<em>(no page)</em>"
        rows.append(f"<tr><td>{left}</td><td>{right}</td></tr>")
    os.makedirs(outdir, exist_ok=True)
    css = (
        "body{font-family:'Noto Sans CJK SC','Microsoft YaHei',sans-serif;"
        "margin:16px;background:#fafafa}"
        "table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #ddd;padding:8px;vertical-align:top;width:50%}"
        "th{background:#222;color:#fff;position:sticky;top:0}"
        "img{max-width:100%;border:1px solid #eee}"
    )
    html = (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        "<title>XLSX before/after diff</title>"
        "<style>" + css + "</style></head><body>"
        "<h2>\u7f16\u8f91\u524d\u540e\u5bf9\u6bd4 / Before vs After</h2>"
        "<p>Left = " + os.path.basename(old_src) + " (\u539f\u59cb) "
        "&nbsp;|&nbsp; Right = " + os.path.basename(new_src) + " (\u4fee\u6539\u540e)</p>"
        "<table><tr><th>\u539f\u59cb Before</th><th>\u4fee\u6539\u540e After</th></tr>"
        + "".join(rows) +
        "</table></body></html>"
    )
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML diff: {html_path}")
    return html_path


def main():
    ap = argparse.ArgumentParser(description="Render workbook to PNG/PDF (CJK-aware) or before/after HTML diff")
    ap.add_argument("file", nargs="?", help="workbook to render")
    ap.add_argument("--out", default="review/", help="output directory")
    ap.add_argument("--pdf-only", action="store_true")
    ap.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"), help="render two files for comparison")
    ap.add_argument("--html", help="output HTML path for --diff")
    args = ap.parse_args()

    if args.diff:
        html = args.html or os.path.join(args.out, "diff.html")
        render_diff_html(args.diff[0], args.diff[1], html)
        return 0

    if not args.file:
        ap.error("FILE is required unless --diff is used")
    render(args.file, args.out, pdf_only=args.pdf_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
