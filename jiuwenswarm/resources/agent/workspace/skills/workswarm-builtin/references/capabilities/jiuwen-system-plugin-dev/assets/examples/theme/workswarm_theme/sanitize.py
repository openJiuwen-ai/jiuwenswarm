"""主题包内容的安全检查。

主题样式表会被注入 WorkSwarm 主界面，SVG 与主题文件会从应用同源地址提供，
因此这里只放行不会执行脚本、不会向外部发送数据的内容。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Callable

MAX_STYLESHEET_BYTES = 256 * 1024

_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_CSS_HEX_ESCAPE_RE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?")
_CSS_CHAR_ESCAPE_RE = re.compile(r"\\(.)", re.S)
_CSS_URL_RE = re.compile(r"url\(\s*(?P<q>['\"]?)(?P<value>[^'\")]*)(?P=q)\s*\)", re.I)

# 归一化（去注释、去转义、小写）后仍不允许出现的片段。
_CSS_FORBIDDEN = (
    ("@import", "不允许 @import 引入外部样式"),
    ("@namespace", "不允许 @namespace"),
    ("expression(", "不允许 expression()"),
    ("javascript:", "不允许 javascript: 地址"),
    ("vbscript:", "不允许 vbscript: 地址"),
    ("-moz-binding", "不允许 -moz-binding"),
    ("behavior:", "不允许 behavior 属性"),
    ("[value", "不允许按输入框内容匹配的属性选择器"),
    ("</style", "样式表中不允许出现 </style"),
)

_SVG_FORBIDDEN = (
    (re.compile(r"<\s*script", re.I), "包含 <script>"),
    (re.compile(r"<\s*foreignobject", re.I), "包含 <foreignObject>"),
    (re.compile(r"<\s*(iframe|embed|object)\b", re.I), "包含嵌入式文档元素"),
    (re.compile(r"\son[a-z]+\s*=", re.I), "包含事件处理属性"),
    (re.compile(r"javascript\s*:", re.I), "包含 javascript: 地址"),
    (re.compile(r"(?:href|src)\s*=\s*['\"]\s*(?:https?:|//|data:text/html)", re.I), "引用外部资源"),
    (re.compile(r"<!ENTITY", re.I), "包含 XML 实体声明"),
)


class UnsafeContentError(ValueError):
    """主题内容未通过安全检查。"""


def _unescape_css(text: str) -> str:
    def hex_sub(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 16))
        except (ValueError, OverflowError):
            return ""

    text = _CSS_HEX_ESCAPE_RE.sub(hex_sub, text)
    return _CSS_CHAR_ESCAPE_RE.sub(lambda m: m.group(1), text)


def _is_relative_asset(value: str) -> bool:
    if not value or value.startswith(("/", "#")) or "\\" in value or ":" in value:
        return False
    parts = PurePosixPath(value).parts
    return bool(parts) and all(part not in ("..", ".") and not part.startswith(".") for part in parts)


def sanitize_stylesheet(
    text: str,
    *,
    resolve_url: Callable[[str], str | None],
) -> str:
    """返回可注入宿主页面的样式表文本。

    ``resolve_url`` 接收主题包内的相对路径，返回发布后的同源地址；
    返回 ``None`` 表示文件不存在或类型不允许。
    """

    if len(text.encode("utf-8")) > MAX_STYLESHEET_BYTES:
        raise UnsafeContentError("样式表超过 256 KiB")
    stripped = _CSS_COMMENT_RE.sub("", text)
    normalized = _unescape_css(stripped).lower()
    for needle, message in _CSS_FORBIDDEN:
        if needle in normalized:
            raise UnsafeContentError(message)

    matches = list(_CSS_URL_RE.finditer(stripped))
    if normalized.count("url(") != len(matches):
        raise UnsafeContentError("url() 写法无法识别，请使用 url(相对路径)")
    if "image-set(" in normalized and not matches:
        raise UnsafeContentError("image-set() 中的地址必须写成 url(相对路径)")

    out: list[str] = []
    cursor = 0
    for match in matches:
        raw = match.group("value").strip()
        if "\\" in raw or not _is_relative_asset(raw):
            raise UnsafeContentError(f"url() 只能引用主题包内的相对路径：{raw or '(空)'}")
        resolved = resolve_url(raw)
        if resolved is None:
            raise UnsafeContentError(f"url() 引用的文件不存在或类型不允许：{raw}")
        out.append(stripped[cursor:match.start()])
        out.append(f'url("{resolved}")')
        cursor = match.end()
    out.append(stripped[cursor:])
    return "".join(out).strip()


def check_svg(data: bytes) -> None:
    """拒绝可能在同源地址下执行脚本或外联的 SVG。"""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsafeContentError("SVG 必须是 UTF-8 编码") from exc
    for pattern, message in _SVG_FORBIDDEN:
        if pattern.search(text):
            raise UnsafeContentError(f"SVG {message}")
