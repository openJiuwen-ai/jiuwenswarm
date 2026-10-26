# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Dependency-local, non-SDK Markdown to HTML fallback for DeepResearch."""

from __future__ import annotations

import argparse
import html
import os
import stat
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import markdown

from .conversion_utils import (
    postprocess_html,
    preprocess_markdown_text,
    protect_math_spans,
    restore_math_spans,
)

MAX_MARKDOWN_BYTES = 10 * 1024 * 1024

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script>
        window.MathJax = {{
            tex: {{inlineMath: [['\\\\(', '\\\\)']], displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']]}},
            options: {{skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']}}
        }};
    </script>
    <script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
    <style>
        :root {{ --text: #222; --muted: #666; --border: #e5e7eb; --soft: #f6f8fa; }}
        * {{ box-sizing: border-box; }}
        body {{ max-width: 1200px; margin: 0 auto; padding: 32px 24px 64px;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          "PingFang SC", "Microsoft YaHei", sans-serif; line-height: 1.8;
          color: var(--text); overflow-wrap: anywhere; }}
        h1, h2, h3, h4, h5, h6 {{ line-height: 1.35; margin: 1.6em 0 .7em; }}
        h1 {{ padding-bottom: .3em; border-bottom: 1px solid var(--border); }}
        a {{ color: #2563eb; text-decoration: none; }}
        img {{ max-width: 100%; height: auto; display: block; margin: 20px auto 12px; }}
        .figure-caption {{ display: block; text-align: center; color: var(--muted); }}
        .citation {{ vertical-align: super; font-size: .78em; line-height: 0; }}
        pre {{ background: var(--soft); padding: 16px; border-radius: 10px; overflow-x: auto; }}
        code {{ font-family: "SFMono-Regular", Consolas, monospace; }}
        table {{ border-collapse: collapse; max-width: 100%; margin: 16px auto 24px; }}
        th, td {{ border: 1px solid var(--border); padding: 10px 12px; text-align: center; }}
        th {{ background: #f8fafc; }}
        blockquote {{ color: var(--muted); border-left: 4px solid var(--border); padding-left: 1em; }}
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
    del options
    return preprocess_markdown_text(text)


_ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "dd",
    "del",
    "div",
    "dl",
    "dt",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_VOID_TAGS = {"br", "hr", "img"}
_DROP_CONTENT_TAGS = {
    "applet",
    "base",
    "embed",
    "form",
    "iframe",
    "math",
    "meta",
    "noscript",
    "object",
    "plaintext",
    "script",
    "style",
    "svg",
    "template",
    "textarea",
    "title",
    "xmp",
}
_TAG_ATTRIBUTES = {
    "a": {"href", "title"},
    "code": {"class"},
    "div": {"class", "id"},
    "h1": {"id"},
    "h2": {"id"},
    "h3": {"id"},
    "h4": {"id"},
    "h5": {"id"},
    "h6": {"id"},
    "img": {"alt", "src", "title"},
    "ol": {"start"},
    "span": {"class"},
    "sup": {"class"},
    "table": {"class"},
    "td": {"align", "colspan", "rowspan"},
    "th": {"align", "colspan", "rowspan"},
}


def _safe_url(value: str, *, image: bool) -> str | None:
    decoded = html.unescape(value)
    normalized_characters = []
    for character in decoded:
        codepoint = ord(character)
        if not character.isspace() and not (codepoint < 0x20 or codepoint == 0x7F):
            normalized_characters.append(character)
    normalized = "".join(normalized_characters)
    try:
        scheme = urlsplit(normalized).scheme.casefold()
    except ValueError:
        return None
    allowed = {"http", "https"} if image else {"http", "https", "mailto"}
    if scheme and scheme not in allowed:
        return None
    return normalized


class _AllowlistHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.open_tags: list[str] = []
        self.suppressed_tags: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.casefold()
        if self.suppressed_tags:
            if tag in _DROP_CONTENT_TAGS:
                self.suppressed_tags.append(tag)
            return
        if tag in _DROP_CONTENT_TAGS:
            self.suppressed_tags.append(tag)
            return
        if tag not in _ALLOWED_TAGS:
            return

        rendered_attrs: list[str] = []
        seen: set[str] = set()
        allowed_attrs = _TAG_ATTRIBUTES.get(tag, set())
        for raw_name, raw_value in attrs:
            name = raw_name.casefold()
            if name in seen or name not in allowed_attrs:
                continue
            seen.add(name)
            if raw_value is None:
                if tag == "img" and name == "alt":
                    raw_value = "alt"
                else:
                    continue
            value = raw_value
            if name in {"href", "src"}:
                safe = _safe_url(value, image=name == "src")
                if safe is None:
                    continue
                value = safe
            rendered_attrs.append(
                f'{name}="{html.escape(value, quote=True)}"'
            )
        suffix = " " + " ".join(rendered_attrs) if rendered_attrs else ""
        self.output.append(f"<{tag}{suffix}>")
        if tag not in _VOID_TAGS:
            self.open_tags.append(tag)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.suppressed_tags:
            if tag == self.suppressed_tags[-1]:
                self.suppressed_tags.pop()
            return
        if tag not in self.open_tags:
            return
        while self.open_tags:
            opened = self.open_tags.pop()
            self.output.append(f"</{opened}>")
            if opened == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.suppressed_tags:
            self.output.append(html.escape(data, quote=False))

    def close(self) -> None:
        super().close()
        if not self.suppressed_tags:
            while self.open_tags:
                self.output.append(f"</{self.open_tags.pop()}>")


def _sanitize_html_content(html_content: str) -> str:
    sanitizer = _AllowlistHTMLSanitizer()
    sanitizer.feed(html_content)
    sanitizer.close()
    return "".join(sanitizer.output)


def _read_regular_markdown(path: Path) -> str:
    before = path.lstat()
    invalid_named_file = (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > MAX_MARKDOWN_BYTES
    )
    if invalid_named_file:
        raise OSError("unsafe Markdown input")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode=0o600)
    try:
        opened = os.fstat(descriptor)
        invalid_open_file = (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > MAX_MARKDOWN_BYTES
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        )
        if invalid_open_file:
            raise OSError("unsafe Markdown input")
        data = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, MAX_MARKDOWN_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_MARKDOWN_BYTES:
                raise OSError("unsafe Markdown input")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("unsafe Markdown input")
    finally:
        os.close(descriptor)
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return bytes(data).decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeError("Markdown input encoding is unsupported")


def _exclusive_write(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("unsafe HTML output")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            current = path.lstat()
            if (
                stat.S_ISREG(current.st_mode)
                and (current.st_dev, current.st_ino)
                == (metadata.st_dev, metadata.st_ino)
            ):
                path.unlink()
        except (NameError, OSError):
            pass
        raise
    finally:
        os.close(descriptor)


def convert_md_to_html(
    input_md: str | Path,
    output_html: str | Path,
    *,
    options: ConvertOptions | None = None,
) -> None:
    """Convert a bounded regular Markdown file without replacing any output."""
    options = options or ConvertOptions()
    input_path = Path(input_md)
    output_path = Path(output_html)
    markdown_text = preprocess_markdown(_read_regular_markdown(input_path), options)
    markdown_text, math_spans = protect_math_spans(markdown_text)
    html_body = markdown.markdown(
        markdown_text,
        extensions=["extra", "toc", "md_in_html"],
        output_format="html",
    )
    html_body = _sanitize_html_content(html_body)
    html_body = restore_math_spans(html_body, math_spans)
    rendered = HTML_TEMPLATE.format(
        title=html.escape(options.title, quote=True),
        content=postprocess_html(html_body),
    ).encode("utf-8")
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _exclusive_write(output_path, rendered)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert Markdown to styled HTML.")
    parser.add_argument("input", nargs="?", default="input.md")
    parser.add_argument("output", nargs="?", default="output.html")
    parser.add_argument("--title", default="Document")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    convert_md_to_html(args.input, args.output, options=ConvertOptions(title=args.title))


if __name__ == "__main__":
    main()


__all__ = ["ConvertOptions", "convert_md_to_html", "preprocess_markdown"]
