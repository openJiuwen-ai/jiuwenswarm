# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rich text pivot format and renderers (E1 connector SDK).

One neutral representation of formatted text; one small renderer per
platform (N translators instead of N x N). Channels whose
``capabilities.rich_text`` is False use :func:`render_plain`.
"""

from dataclasses import dataclass, field
from enum import Enum


class SpanStyle(str, Enum):
    PLAIN = "plain"
    BOLD = "bold"
    CODE = "code"


@dataclass(frozen=True)
class Span:
    """A run of text with one style."""

    text: str
    style: SpanStyle = SpanStyle.PLAIN


@dataclass(frozen=True)
class RichText:
    """Pivot representation: an ordered list of styled spans."""

    spans: tuple[Span, ...] = field(default_factory=tuple)

    @classmethod
    def of(cls, *spans: Span) -> "RichText":
        return cls(spans=tuple(spans))


def render_plain(rich: RichText) -> str:
    """Degradation: styles stripped, text preserved — never lost."""
    return "".join(span.text for span in rich.spans)


def render_markdown(rich: RichText) -> str:
    """Markdown dialect (Telegram parse_mode, Slack mrkdwn-compatible subset)."""
    parts: list[str] = []
    for span in rich.spans:
        if span.style is SpanStyle.BOLD:
            parts.append(f"*{span.text}*")
        elif span.style is SpanStyle.CODE:
            parts.append(f"`{span.text}`")
        else:
            parts.append(span.text)
    return "".join(parts)


RENDERERS = {
    "plain": render_plain,
    "markdown": render_markdown,
}


def render(rich: RichText, dialect: str, rich_text_supported: bool) -> str:
    """Capability-aware entry point: degrades to plain when unsupported."""
    if not rich_text_supported:
        return render_plain(rich)
    renderer = RENDERERS.get(dialect, render_plain)
    return renderer(rich)