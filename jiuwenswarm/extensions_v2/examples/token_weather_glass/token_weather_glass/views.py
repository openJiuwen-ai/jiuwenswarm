# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Render observed usage with shared host components."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.extensions_v2.webui.models import RenderNode

from .locales import MESSAGES, localized
from .history import TurnReading, sparkline
from .formatting import context_percent, format_context_limit, format_context_percent, format_context_tokens

PACKAGE_ID = "webui.token-weather-glass"
SLOT = "conversation.input.above"
SETTINGS_SLOT = "application.settings"
INSTANCE = "weather"
SETTING = "show-weather"
DETAILS_ACTION = "weather.details"
PREFERENCE = {"key": "visible", "default": True}


def _usage_text(usage: dict[str, Any], language: str) -> str:
    """Keep the same observations in each language, including known zero values."""
    messages = MESSAGES[language]
    context = usage["context"]
    parts: list[str] = []
    percent = format_context_percent(context)
    if percent is not None:
        parts.append(percent)
    if "tokens" in context and "window" in context:
        parts.append(f"{format_context_tokens(context['tokens'])}/{format_context_limit(context['window'])} {messages['tokens']}")
    elif "tokens" in context:
        parts.append(f"{format_context_tokens(context['tokens'])} {messages['tokens']}")
    elif "window" in context:
        parts.append(f"{messages['window']} {format_context_limit(context['window'])} {messages['tokens']}")
    if not parts:
        parts.append(messages["empty"])
    parts.extend(f"{messages.get(item['kind'], item['kind'])} {item['percentUsed']:g}%" for item in usage["rateLimits"])
    if "cost" in usage:
        parts.append(f"${usage['cost']['usd']:.2f}")
    return " · ".join(parts)


def _forecast_view(context: dict[str, Any]) -> list[RenderNode]:
    """Express known context pressure before the usage figures."""
    percent = context_percent(context)
    if percent is None:
        return []
    if percent < 25:
        icon, key, tone = "☀", "clear", "success"
    elif percent < 50:
        icon, key, tone = "☁", "cloudy", "secondary"
    elif percent < 75:
        icon, key, tone = "☂", "showers", "warning"
    elif percent < 90:
        icon, key, tone = "☇", "storm", "danger"
    else:
        icon, key, tone = "↯", "compact_soon", "danger"
    return [RenderNode("Text", {
        "value": {language: f"{icon} {messages[key]}" for language, messages in MESSAGES.items()},
        "type": tone, "strong": True, "size": "sm",
    })]


def weather_view(usage: dict[str, Any], readings: list[TurnReading] | tuple[TurnReading, ...] = ()) -> RenderNode:
    """Missing readings stay distinct from reported zeros."""
    trend: list[RenderNode] = []
    if any(item.context_tokens is not None for item in readings):
        trend.append(RenderNode("Text", {"value": sparkline(readings), "code": True, "size": "sm"}))
    if len(readings) >= 2 and all(item.context_tokens is not None for item in readings[-2:]):
        delta = readings[-1].context_tokens - readings[-2].context_tokens
        text = {language: messages["last_change"].format(sign="+" if delta > 0 else "−", tokens=format_context_tokens(abs(delta)))
                if delta else messages["steady"] for language, messages in MESSAGES.items()}
        trend.append(RenderNode("Text", {"value": text, "size": "sm", "type": "secondary"}))
    return RenderNode("Box", {
        "direction": "row", "align": "center", "justify": "between", "gap": "sm", "wrap": True,
    }, children=(
        RenderNode("Box", {"direction": "row", "align": "center", "gap": "sm", "wrap": True, "flex": 1}, children=(
            *_forecast_view(usage["context"]),
            RenderNode("Text", {"value": {language: _usage_text(usage, language) for language in MESSAGES}, "size": "sm"}),
            *trend,
        )),
        RenderNode("Button", {"value": localized("open_details"), "type": "text", "size": "md", "openPanel": "details"},
                   events={"ui.click": {"id": DETAILS_ACTION, "args": {}}}),
    ))


def setting_view() -> RenderNode:
    """The host supplies the localized SettingRow from manifest metadata."""
    return RenderNode("Switch", {"preference": PREFERENCE, "ariaLabel": localized("show_weather")})
