# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from __future__ import annotations

import argparse
import html
import logging
import re
import warnings
from dataclasses import dataclass
from pathlib import Path

import markdown
from jiuwenclaw.agentserver.tools.deepresearch_plugin.conversion_utils import (
    postprocess_html,
    preprocess_markdown_text,
    read_text_with_fallback,
)

logger = logging.getLogger(__name__)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
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
    </style>
</head>
<body>
{content}
</body>
</html>
"""


@dataclass(slots=True)
class ConvertOptions:
    title: str = "Document"
    # Legacy compatibility fields kept as no-ops so existing callers do not break.
    mermaid_security_level: str = "strict"
    timeline_max_label_len: int = 18
    scale_xychart: bool = True
    warn_on_invalid_number: bool = True
    show_xychart_value_labels: bool = True
    max_mermaid_blocks: int = 50
    max_single_mermaid_bytes: int = 100 * 1024
    max_mermaid_total_bytes: int = 5 * 1024 * 1024
    max_render_time_seconds: int = 300


def preprocess_markdown(text: str, options: ConvertOptions) -> str:
    """Normalize markdown before HTML conversion."""
    _ = options
    return preprocess_markdown_text(text)


def _sanitize_html_content(html_content: str) -> str:
    """Sanitize HTML content by removing dangerous elements and attributes."""
    html_content = re.sub(
        r"<script[^>]*>.*?</script>",
        "",
        html_content,
        flags=re.IGNORECASE | re.DOTALL,
    )

    dangerous_tags = ["iframe", "object", "embed", "applet", "form", "meta", "link", "base"]
    for tag in dangerous_tags:
        html_content = re.sub(
            rf"<{tag}[^>]*>.*?</{tag}>",
            "",
            html_content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        html_content = re.sub(
            rf"<{tag}[^>]*>",
            "",
            html_content,
            flags=re.IGNORECASE,
        )

    html_content = re.sub(
        r'\s+on[a-z]+\s*=\s*["\'][^"\']*["\']',
        "",
        html_content,
        flags=re.IGNORECASE,
    )
    html_content = re.sub(
        r"\s+on[a-z]+\s*=\s*[^\s>]+",
        "",
        html_content,
        flags=re.IGNORECASE,
    )

    dangerous_schemes = ["javascript:", "vbscript:", "data:text/html"]
    url_attrs_pattern = "href|src|action|formaction|data|poster"
    for scheme in dangerous_schemes:
        html_content = re.sub(
            rf'({url_attrs_pattern})\s*=\s*["\']\s*{scheme}[^"\']*["\']',
            "",
            html_content,
            flags=re.IGNORECASE,
        )
        html_content = re.sub(
            rf"({url_attrs_pattern})\s*=\s*{scheme}[^\s>]*",
            "",
            html_content,
            flags=re.IGNORECASE,
        )

    return html_content


def convert_md_to_html(
    input_md: str | Path,
    output_html: str | Path,
    *,
    options: ConvertOptions | None = None,
) -> None:
    """Convert a Markdown file to styled HTML."""
    options = options or ConvertOptions()
    input_path = Path(input_md)
    output_path = Path(output_html)

    if not input_path.exists():
        raise FileNotFoundError(f"Markdown file does not exist: {input_path}")

    if input_path.suffix.lower() != ".md":
        warnings.warn(f"Input file does not look like Markdown: {input_path.name}", stacklevel=2)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    md_content = read_text_with_fallback(input_path)
    md_content = preprocess_markdown(md_content, options)
    html_body = markdown.markdown(
        md_content,
        extensions=["extra", "toc", "md_in_html"],
        output_format="html",
    )
    html_body = _sanitize_html_content(html_body)
    full_html = HTML_TEMPLATE.format(
        title=html.escape(options.title, quote=True),
        content=postprocess_html(html_body),
    )
    output_path.write_text(full_html, encoding="utf-8", newline="\n")

    logger.info("HTML generated successfully: %s", output_path.resolve())


def build_arg_parser() -> argparse.ArgumentParser:
    """Build and configure the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Convert Markdown to styled HTML."
    )
    parser.add_argument("input", nargs="?", default="input.md", help="Input Markdown file")
    parser.add_argument("output", nargs="?", default="output.html", help="Output HTML file")
    parser.add_argument("--title", default="Document", help="HTML document title")
    parser.add_argument(
        "--mermaid-security-level",
        default="strict",
        choices=["strict", "loose", "antiscript", "sandbox"],
        help="Legacy compatibility option; ignored.",
    )
    parser.add_argument(
        "--timeline-max-label-len",
        type=int,
        default=18,
        help="Legacy compatibility option; ignored.",
    )
    parser.add_argument(
        "--no-scale-xychart",
        action="store_true",
        help="Legacy compatibility option; ignored.",
    )
    parser.add_argument(
        "--quiet-invalid-number-warning",
        action="store_true",
        help="Legacy compatibility option; ignored.",
    )
    parser.add_argument(
        "--no-xychart-value-labels",
        action="store_true",
        help="Legacy compatibility option; ignored.",
    )
    return parser


def main() -> None:
    """Main entry point for command-line Markdown to HTML conversion."""
    parser = build_arg_parser()
    args = parser.parse_args()

    options = ConvertOptions(
        title=args.title,
        mermaid_security_level=args.mermaid_security_level,
        timeline_max_label_len=args.timeline_max_label_len,
        scale_xychart=not args.no_scale_xychart,
        warn_on_invalid_number=not args.quiet_invalid_number_warning,
        show_xychart_value_labels=not args.no_xychart_value_labels,
    )
    convert_md_to_html(args.input, args.output, options=options)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("HTML conversion failed")
        raise
