# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Plugin-owned, self-contained usage documents with accessible stacked bars."""

from __future__ import annotations

from html import escape
from pathlib import Path
from string import Template
import shutil
from typing import Any

from jiuwenswarm.extensions_v2.webui.models import RenderNode

from .history import CONTEXT_CATEGORY_NAMES, TurnReading
from .locales import MESSAGES, localized
from .formatting import format_context_limit, format_context_percent, format_context_tokens

DETAILS_SLOT = "conversation.panel.right"
DETAILS_INSTANCE = "details"


def _number(value: int | float | None, missing: str) -> str:
    return missing if value is None else f"{value:,}"


def _tone(name: str) -> str:
    return name if name in CONTEXT_CATEGORY_NAMES else "other"


def _composition_chart(readings: list[TurnReading], messages: dict[str, str]) -> str:
    """Use actual category sums on one shared axis, leaving missing turns unfilled."""
    totals = [None if item.context_categories is None else sum(part.tokens for part in item.context_categories)
              for item in readings]
    maximum = max((total for total in totals if total is not None), default=0)
    labels = (format_context_tokens(maximum), format_context_tokens(maximum / 2), "0") if maximum else ("", "", "0")
    columns: list[str] = []
    tips: list[str] = []
    selectors: list[str] = []
    for reading, total in zip(readings, totals):
        turn = messages["round_value"].format(number=reading.number)
        if total is None:
            columns.append(f'<div class="column unobserved" role="img" aria-label="{escape(turn)}: '
                           f'{escape(messages["not_collected"])}"><span class="missing">{escape(messages["not_collected"])}</span>'
                           f'<span class="round-label">{escape(turn)}</span></div>')
            continue
        categories = reading.context_categories or ()
        values = [(messages[part.name] if part.name in CONTEXT_CATEGORY_NAMES else part.name, part.tokens)
                  for part in categories]
        pieces = "".join(f'<span class="{_tone(part.name)}" style="flex:{part.tokens}"></span>'
                         for part in categories) if total else ""
        bar = (f'<div class="bar" style="height:{total / maximum * 150:.4f}px">{pieces}</div>'
               if total else '<div class="zero"></div>')
        rows = "".join(f'<dt>{escape(label)}</dt><dd>{_number(tokens, "")}</dd>' for label, tokens in values)
        columns.append(f'<div class="column c{reading.number}" tabindex="0" role="img" aria-label="{escape(turn)}" aria-describedby="t{reading.number}">'
                       f'<span class="bar-value">{format_context_tokens(total)}</span>{bar}'
                       f'<span class="round-label">{escape(turn)}</span></div>')
        tips.append(f'<dl class="tip" id="t{reading.number}" role="tooltip">'
                    f'{rows}<dt>{escape(messages["category_total"])}</dt><dd>{_number(total, "")}</dd></dl>')
        selectors.append(f'.chart-wrap:has(.c{reading.number}:is(:hover,:focus)) #t{reading.number}'
                         '{visibility:visible}')
    scale = "".join(f'<span>{label}</span>' for label in labels)
    return (f'<div class="chart-wrap"><style>{"".join(selectors)}</style><div class="chart-scroll">'
            f'<div class="chart" style="min-width:{56 + len(readings) * 58}px">'
            f'<div class="scale" aria-hidden="true">{scale}</div><div class="plot">'
            '<div class="grid" aria-hidden="true"><i></i><i></i><i></i></div>'
            f'<div class="columns" style="--count:{len(readings)}">{"".join(columns)}</div></div></div></div>'
            f'{"".join(tips)}</div>')


def _document(usage: dict[str, Any], readings: list[TurnReading], language: str) -> str:
    messages = MESSAGES[language]
    context = usage["context"]
    percent = format_context_percent(context)
    summary = "".join(f'<div><dt>{escape(messages[label])}</dt><dd>{escape(value)}</dd></div>' for label, value in (
        ("current_context", format_context_tokens(context["tokens"]) if "tokens" in context else messages["unknown"]),
        ("window", format_context_limit(context["window"]) if "window" in context else messages["unknown"]),
        ("occupancy", percent if percent is not None else messages["unknown"]),
    ))
    if readings:
        names = list(CONTEXT_CATEGORY_NAMES)
        if any(part.name not in CONTEXT_CATEGORY_NAMES for reading in readings for part in reading.context_categories or ()):
            names.append("other_categories")
        legend = "".join(f'<span><i class="dot {_tone(name)}"></i>{escape(messages[name])}</span>' for name in names)
        headers = "".join(f'<th scope="col">{escape(messages[key])}</th>' for key in
                          ("round", "input", "output", "cache", "total", "context"))
        rows = "".join("<tr>" + "".join(f"<td>{escape(value)}</td>" for value in (
            str(item.number), _number(item.input_tokens, ""), _number(item.output_tokens, ""),
            _number(item.cache_tokens, ""), _number(item.total_tokens, ""),
            _number(item.context_tokens, messages["unknown"]),
        )) + "</tr>" for item in readings)
        content = (f'<section><h2>{escape(messages["context_trend"])}</h2><p>{escape(messages["context_note"])}</p>'
                   f'<div class="legend">{legend}</div>{_composition_chart(readings, messages)}'
                   f'<p class="note">{escape(messages["category_note"])}</p></section>'
                   f'<section><h2>{escape(messages["rounds"])}</h2><p>{escape(messages["usage_note"])}</p><div class="table-scroll">'
                   f'<table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table></div></section>')
    else:
        content = (f'<section class="empty"><strong>{escape(messages["waiting"])}</strong>'
                   f'<p>{escape(messages["waiting_note"])}</p></section>')
    recent = messages["recent"].format(count=len(readings))
    template = Template((Path(__file__).parent.parent / "assets/details.html").read_text(encoding="utf-8"))
    return template.substitute(
        language=language, title=escape(messages["details"]), recent=escape(recent),
        summary=summary, content=content, history_note=escape(messages["history_note"]),
    )


def write_details(directory: Path, usage: dict[str, Any], readings: list[TurnReading]) -> None:
    """Write a completed page version into the plugin's host-provided output directory."""
    for language in MESSAGES:
        (directory / f"{language}.html").write_text(_document(usage, readings, language), encoding="utf-8")
    shutil.copyfile(Path(__file__).parent.parent / "assets/details.css", directory / "details.css")


def details_view(sources: dict[str, str]) -> RenderNode:
    """Send only localized resource addresses through the render protocol."""
    return RenderNode("Iframe", {"title": localized("details"), "minWidth": 360, "src": sources})
