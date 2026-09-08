#!/usr/bin/env python3
# write_ocr_md.py — 把分页识别结果汇总为统一 .md（多模态路线落盘用）
# --------------------------------------------------------------------------
# 输入：JSON 文件 或 stdin。格式:
# {
#   "source": "扫描件.pdf",
#   "engine": "多模态读图（本机模型）",
#   "pages": [
#     {"label": "第 1 页", "text": "……识别文字……"},
#     {"label": "第 2 页", "text": "……"}
#   ]
# }
# 输出：<同名或 -o 指定>_OCR.md
#
# 用法:
#   python write_ocr_md.py result.json -o out.md
#   echo '{...}' | python write_ocr_md.py -
# --------------------------------------------------------------------------
import sys, os, json, argparse, datetime


def build_md(data):
    src = data.get("source", "（未知来源）")
    engine = data.get("engine", "多模态读图（本机模型）")
    pages = data.get("pages", [])
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = []
    md.append("# OCR 识别结果（本地离线 · 多模态增强）")
    md.append("")
    md.append(f"> 来源：{src}")
    md.append(f"> 生成时间：{now}")
    md.append(f"> 引擎：{engine}（本机处理，不联网、不上传）")
    md.append(f"> 共 {len(pages)} 页/图")
    md.append("")
    md.append("---")
    md.append("")
    for p in pages:
        label = p.get("label", "未命名")
        text = p.get("text", "")
        md.append(f"## {label}")
        md.append("")
        md.append(text.strip() if text else "（本页未识别到内容）")
        md.append("")
    md.append("---")
    md.append("")
    md.append("⚠️ OCR 结果由本机多模态识别，关键数字、签名、印章请人工核对。"
              "表格已尽量还原为 Markdown，复杂跨页表格建议人工复核。")
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser(description="汇总分页识别结果为统一 .md")
    ap.add_argument("json", help="JSON 文件，或 '-' 从 stdin 读取")
    ap.add_argument("-o", "--out", default=None, help="输出 .md，默认 <同名>_OCR.md")
    args = ap.parse_args()

    if args.json == "-":
        raw = sys.stdin.read()
    else:
        with open(args.json, "r", encoding="utf-8") as f:
            raw = f.read()
    data = json.loads(raw)

    if not args.out:
        stem = os.path.splitext(os.path.basename(
            data.get("source", "ocr")))[0]
        args.out = os.path.join(os.path.dirname(args.json) or ".",
                                f"{stem}_OCR.md") if args.json != "-" else "ocr_OCR.md"
    md = build_md(data)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"DONE -> {args.out}  ({len(data.get('pages', []))} 页)")


if __name__ == "__main__":
    main()
