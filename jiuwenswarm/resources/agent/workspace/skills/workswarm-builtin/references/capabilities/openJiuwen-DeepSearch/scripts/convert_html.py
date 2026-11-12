from pathlib import Path
import html
import math
import re
import unicodedata

import markdown


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Document</title>
    <style>
        :root {{
            --text: #222;
            --muted: #666;
            --border: #e5e7eb;
            --bg-soft: #f6f8fa;
            --link: #2563eb;
        }}

        * {{
            box-sizing: border-box;
        }}

        html {{
            -webkit-text-size-adjust: 100%;
            text-rendering: optimizeLegibility;
        }}

        body {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 32px 24px 64px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                         "Helvetica Neue", Arial, "PingFang SC", "Hiragino Sans GB",
                         "Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC", sans-serif;
            line-height: 1.8;
            color: var(--text);
            background: #fff;
            word-break: break-word;
            overflow-wrap: anywhere;
        }}

        h1, h2, h3, h4, h5, h6 {{
            line-height: 1.35;
            margin-top: 1.6em;
            margin-bottom: 0.7em;
        }}

        h1 {{
            padding-bottom: 0.3em;
            border-bottom: 1px solid var(--border);
        }}

        p {{
            margin: 0.9em 0;
        }}

        a {{
            color: var(--link);
            text-decoration: none;
        }}

        a:hover {{
            text-decoration: underline;
        }}

        img {{
            max-width: 100%;
            height: auto;
            display: block;
            margin: 20px auto 12px;
        }}

        .figure-caption {{
            text-align: center;
            color: var(--muted);
            font-size: 0.95rem;
            margin: 0.2rem auto 1.4rem;
        }}

        .figure-caption p {{
            margin: 0.2rem 0;
        }}

        .citation {{
            vertical-align: super;
            font-size: 0.78em;
            line-height: 0;
            white-space: nowrap;
        }}

        .citation a {{
            color: var(--muted);
            text-decoration: none;
        }}

        .citation a:hover {{
            color: var(--link);
            text-decoration: underline;
        }}

        .citation + .citation {{
            margin-left: 0.18em;
        }}

        pre {{
            background: var(--bg-soft);
            padding: 16px;
            border-radius: 10px;
            overflow-x: auto;
            border: 1px solid var(--border);
        }}

        code {{
            font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
        }}

        p code, li code, td code, th code {{
            background: #f3f4f6;
            padding: 0.12em 0.35em;
            border-radius: 6px;
        }}

        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 16px 0 24px;
            display: block;
            overflow-x: auto;
            white-space: nowrap;
        }}

        th, td {{
            border: 1px solid var(--border);
            padding: 10px 12px;
            text-align: left;
            vertical-align: top;
        }}

        th {{
            background: #f8fafc;
        }}

        ul, ol {{
            padding-left: 1.5em;
        }}

        blockquote {{
            margin: 1em 0;
            padding: 0.2em 1em;
            color: var(--muted);
            border-left: 4px solid var(--border);
            background: #fafafa;
        }}

        hr {{
            border: 0;
            border-top: 1px solid var(--border);
            margin: 2em 0;
        }}

        .mermaid-wrap {{
            width: 100%;
            overflow-x: auto;
            overflow-y: hidden;
            margin: 24px 0 12px;
            padding-bottom: 8px;
        }}

        .mermaid {{
            min-width: max-content;
            text-align: center;
        }}

        .mermaid svg {{
            height: auto;
            display: block;
            margin: 0 auto;
            max-width: none !important;
        }}

        .timeline-notes {{
            margin: 10px 0 24px;
            padding: 12px 16px;
            border: 1px solid var(--border);
            border-radius: 10px;
            background: #fafafa;
            font-size: 0.96rem;
        }}

        .timeline-notes-title {{
            margin: 0 0 8px;
            font-weight: 600;
            color: var(--text);
        }}

        .timeline-notes ul {{
            margin: 0;
            padding-left: 1.4em;
        }}

        .timeline-notes li {{
            margin: 0.45em 0;
        }}

        .timeline-notes .date {{
            font-weight: 600;
        }}
    </style>
</head>
<body>
{content}

<script type="module">
    import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs";

    mermaid.initialize({{
        startOnLoad: true,
        theme: "default",
        securityLevel: "loose",
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC", sans-serif',
        flowchart: {{
            htmlLabels: true
        }},
        themeCSS: `
            .mermaid text {{
                font-size: 14px !important;
            }}
        `
    }});
</script>
</body>
</html>
"""


CITATION_RE = re.compile(r"\[\[(\d+)\]\]\((.+?)\)")
REFERENCE_LINE_RE = re.compile(r"^\[(\d+)\]\.\s+(.*)$", re.MULTILINE)
MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)

LATIN_UNITS = [
    "TB/s", "GB/s", "MB/s", "KB/s",
    "TB", "GB", "MB", "KB",
    "PFLOPS", "TFLOPS", "GFLOPS",
    "FLOPS", "FLOP",
    "FP16", "FP8", "FP4", "NVFP4",
    "W", "kW", "MW", "GW", "V", "A",
    "Hz", "kHz", "MHz", "GHz",
    "nm", "μm", "mm",
    "GPU", "CPU", "DPU", "LPU",
    "Token", "token",
]

CHINESE_UNITS = [
    "亿美元", "万亿美元", "美元", "亿元",
    "太瓦时", "瓦时",
    "吉瓦", "兆瓦", "千瓦",
    "万人", "万台", "倍", "%"
]


def read_text_with_fallback(path: Path) -> str:
    encodings = ["utf-8-sig", "utf-8", "gb18030", "gbk"]
    last_error = None

    for enc in encodings:
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError as e:
            last_error = e

    raise UnicodeDecodeError(
        getattr(last_error, "encoding", "unknown"),
        getattr(last_error, "object", b""),
        getattr(last_error, "start", 0),
        getattr(last_error, "end", 0),
        f"无法正确解码文件：{path}",
    )


def normalize_whitespace_and_units(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    replacements = {
        "\u00a0": " ",
        "\u3000": " ",
        "端到-end": "端到端",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"[ \t]+([，。；：！？、）】》])", r"\1", text)
    text = re.sub(r"([（【《])[ \t]+", r"\1", text)

    latin_units_pattern = "|".join(sorted(map(re.escape, LATIN_UNITS), key=len, reverse=True))
    text = re.sub(
        rf"(\d[\d,]*(?:\.\d+)?)\s*({latin_units_pattern})\b",
        r"\1 \2",
        text,
    )

    chinese_units_pattern = "|".join(sorted(map(re.escape, CHINESE_UNITS), key=len, reverse=True))
    text = re.sub(
        rf"(\d[\d,]*(?:\.\d+)?)\s+({chinese_units_pattern})",
        r"\1\2",
        text,
    )

    return text


def replace_citations(text: str) -> str:
    def _repl(match: re.Match[str]) -> str:
        idx, url = match.group(1), match.group(2).strip()
        safe_url = html.escape(url, quote=True)
        return (
            f'<sup class="citation">'
            f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">[{idx}]</a>'
            f"</sup>"
        )

    text = CITATION_RE.sub(_repl, text)
    text = re.sub(r"[ \t]+(<sup class=\"citation\">)", r"\1", text)
    return text


def normalize_reference_lines(text: str) -> str:
    return REFERENCE_LINE_RE.sub(r"- [\1] \2", text)


def fix_center_caption_blocks(text: str) -> str:
    return re.sub(
        r'<div\s+style="text-align:\s*center;?">',
        '<div class="figure-caption" markdown="1">',
        text,
        flags=re.IGNORECASE,
    )


def looks_like_mermaid_timeline(lines: list[str]) -> bool:
    return any(line.strip().startswith("timeline") for line in lines)


def looks_like_mermaid_xychart(lines: list[str]) -> bool:
    return any(line.strip().startswith("xychart") for line in lines)


def smart_timeline_summary(text: str, max_len: int = 18) -> str:
    """
    生成适合 timeline 色块的短标签。
    优先取第一个分句；太长则做轻度压缩。
    """
    text = text.strip()
    if len(text) <= max_len:
        return text

    parts = [p.strip() for p in re.split(r"[，；。]", text) if p.strip()]
    if parts:
        first = parts[0]
        if len(first) <= max_len:
            return first
        text = first

    replacements = {
        "英伟达宣布为中国推出": "英伟达推中国特供",
        "英伟达宣布": "英伟达宣布",
        "中国网信办表达安全关切": "网信办表关切",
        "BIS初步管制": "BIS初步管制",
        "BIS批准H20在华销售": "BIS批准H20在华销售",
        "BIS就B30及B40咨询英伟达": "BIS咨询B30/B40",
        "英伟达研发": "英伟达研发",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    if len(text) <= max_len:
        return text

    compact = (
        text.replace("和", "/")
            .replace("以及", "/")
            .replace("及", "/")
            .replace("芯片", "")
            .replace("中国", "中")
    )

    if len(compact) <= max_len:
        return compact

    return compact[:max_len - 1].rstrip("，；：,. ") + "…"


def preprocess_timeline_mermaid(code: str) -> tuple[str, str]:
    """
    timeline 节点只保留短标签，完整说明放到图下注释。
    """
    lines = code.splitlines()
    new_lines = []
    notes = []
    in_timeline = False

    for raw_line in lines:
        line = raw_line.rstrip()

        if line.strip().startswith("timeline"):
            in_timeline = True
            new_lines.append(line)
            continue

        if in_timeline:
            stripped = line.strip()

            if not stripped or stripped.startswith("title ") or stripped.startswith("section "):
                new_lines.append(line)
                continue

            if ":" not in stripped:
                new_lines.append(line)
                continue

            left, right = stripped.split(":", 1)
            date_text = left.strip()
            detail_text = right.strip()
            short_text = smart_timeline_summary(detail_text, max_len=18)

            indent = re.match(r"^\s*", raw_line).group(0)
            new_lines.append(f"{indent}{date_text} : {short_text}")

            if short_text != detail_text:
                notes.append(
                    f'<li><span class="date">{html.escape(date_text)}</span>：{html.escape(detail_text)}</li>'
                )
        else:
            new_lines.append(line)

    notes_html = ""
    if notes:
        notes_html = (
            '<div class="timeline-notes">'
            '<div class="timeline-notes-title">时间轴说明</div>'
            '<ul>'
            + "".join(notes) +
            '</ul></div>'
        )

    return "\n".join(new_lines), notes_html


def format_scaled_number(value: float) -> str:
    if abs(value - round(value)) < 1e-10:
        return str(int(round(value)))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def parse_number_list(content: str) -> list[float]:
    items = [x.strip() for x in content.split(",") if x.strip()]
    values = []
    for item in items:
        try:
            values.append(float(item))
        except ValueError:
            pass
    return values


def replace_number_list(line: str, new_values: list[float]) -> str:
    formatted = ", ".join(format_scaled_number(v) for v in new_values)
    return re.sub(r"\[[^\]]*\]", f"[{formatted}]", line, count=1)


def preprocess_xychart_mermaid(code: str) -> str:
    lines = code.splitlines()

    series_indexes = []
    series_values = []

    for idx, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if stripped.startswith("line ") or stripped.startswith("bar ") or stripped.startswith("area "):
            m = re.search(r"\[([^\]]+)\]", stripped)
            if m:
                values = parse_number_list(m.group(1))
                if values:
                    series_indexes.append(idx)
                    series_values.extend(values)

    if not series_values:
        return code

    max_abs = max(abs(v) for v in series_values)
    if max_abs < 1_000_000:
        return code

    exponent = int(math.floor(math.log10(max_abs)))
    scale_power = max(3, exponent - 1)
    scale_factor = 10 ** scale_power

    new_lines = lines[:]

    for idx in series_indexes:
        stripped = new_lines[idx].strip()
        m = re.search(r"\[([^\]]+)\]", stripped)
        if not m:
            continue
        values = parse_number_list(m.group(1))
        scaled = [v / scale_factor for v in values]
        new_lines[idx] = replace_number_list(new_lines[idx], scaled)

    for i, raw_line in enumerate(new_lines):
        stripped = raw_line.strip()
        if not stripped.startswith("y-axis"):
            continue

        axis_match = re.match(
            r'^(\s*y-axis)(?:\s+"([^"]*)")?(?:\s+([^\s]+))?(?:\s+-->\s+([^\s]+))?\s*$',
            raw_line
        )
        if not axis_match:
            continue

        prefix = axis_match.group(1)
        quoted_label = axis_match.group(2)
        bare_label = axis_match.group(3)
        upper_bound = axis_match.group(4)

        label = quoted_label if quoted_label is not None else (bare_label or "")
        label = label.strip()
        if label:
            label = f'{label} (×1e{scale_power})'
            label_part = f' "{label}"'
        else:
            label_part = f' "×1e{scale_power}"'

        if upper_bound:
            try:
                scaled_upper = float(upper_bound) / scale_factor
                upper_part = f" 0 --> {format_scaled_number(scaled_upper)}"
            except ValueError:
                upper_part = ""
        else:
            scaled_max = max(v / scale_factor for v in series_values)
            upper_part = f" 0 --> {format_scaled_number(scaled_max * 1.1)}"

        indent = re.match(r"^\s*", raw_line).group(0)
        new_lines[i] = f"{indent}{prefix}{label_part}{upper_part}"
        break

    return "\n".join(new_lines)


def preprocess_mermaid_code(code: str) -> tuple[str, str]:
    lines = code.splitlines()

    if looks_like_mermaid_timeline(lines):
        return preprocess_timeline_mermaid(code)

    if looks_like_mermaid_xychart(lines):
        return preprocess_xychart_mermaid(code), ""

    return code, ""


def replace_mermaid_blocks(text: str) -> str:
    def _repl(match: re.Match[str]) -> str:
        mermaid_code = match.group(1).strip()
        mermaid_code, extra_html = preprocess_mermaid_code(mermaid_code)

        escaped = html.escape(mermaid_code)
        return (
            '\n<div class="mermaid-wrap"><div class="mermaid">'
            f'{escaped}</div></div>{extra_html}\n'
        )

    return MERMAID_BLOCK_RE.sub(_repl, text)


def preprocess_markdown(text: str) -> str:
    text = normalize_whitespace_and_units(text)
    text = replace_citations(text)
    text = normalize_reference_lines(text)
    text = fix_center_caption_blocks(text)
    text = replace_mermaid_blocks(text)
    return text


def postprocess_html(html_text: str) -> str:
    html_text = re.sub(
        r'<a href="(https?://[^"]+)"(?![^>]*target=)',
        r'<a href="\1" target="_blank" rel="noopener noreferrer"',
        html_text,
    )
    return html_text


def convert_md_to_html(input_md: str | Path, output_html: str | Path) -> None:
    input_path = Path(input_md)
    output_path = Path(output_html)

    if not input_path.exists():
        raise FileNotFoundError(f"Markdown 文件不存在: {input_path}")

    md_content = read_text_with_fallback(input_path)
    md_content = preprocess_markdown(md_content)

    html_body = markdown.markdown(
        md_content,
        extensions=[
            "extra",
            "toc",
            "md_in_html",
        ],
        output_format="html5",
    )

    full_html = HTML_TEMPLATE.format(content=postprocess_html(html_body))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(full_html, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    convert_md_to_html("input.md", "output.html")