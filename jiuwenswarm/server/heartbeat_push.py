# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Heartbeat push framing shared by AgentServer and its unit tests.

An AgentServer-owned Heartbeat is a background turn: its runtime events carry
telemetry metadata but no inbound delivery route. The Gateway picks one
channel instance from ``app_id``/``bot_id``; when the route is missing it falls
back to ``"default"`` and delivers to whichever instance was registered first.
Every Heartbeat push therefore has to keep the request route.
"""

from __future__ import annotations

from typing import Any

# Runtime telemetry consumed by UIs: web, tui, browser, ide and process_cli all
# render `chat.llm_call_start`/`chat.llm_call_end` as thinking indicators and
# per-call response rows. IM channels instead turn any payload carrying text
# into a user-visible message (the Feishu channel falls back to the whole
# payload in ``_extract_message_content``), so forwarding ``chat.llm_call_end``
# would deliver a second copy of the answer after the streamed card already
# showed it. The skip policy is therefore an IM deny list: an unknown channel
# keeps its telemetry rather than silently losing it, and a new UI channel
# works without touching this module.
HEARTBEAT_UI_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "chat.llm_call_start",
        "chat.llm_call_end",
        "chat.output_phase",
        "chat.usage_metadata",
        "chat.usage_summary",
        "context.usage",
    }
)
# Platform names of ``gateway.channel_manager.im_platforms``; keep in sync when
# a new IM platform is added. ``im`` is the ChannelManager fallback for a
# channel registered without a platform name.
HEARTBEAT_IM_CHANNEL_IDS: frozenset[str] = frozenset(
    {
        "dingtalk",
        "discord",
        "feishu",
        "im",
        "slack",
        "telegram",
        "wechat",
        "wecom",
        "welink",
        "whatsapp",
        "xiaoyi",
    }
)


def is_heartbeat_im_channel(channel_id: Any) -> bool:
    """Return whether this channel delivers telemetry as user-visible content.

    Identifiers carry an optional instance suffix (``feishu:cli_app_a``,
    ``feishu_enterprise:<bot_key>``), so match the platform prefix rather than
    the whole id.
    """
    base = str(channel_id or "").split(":", 1)[0].strip().lower()
    if not base:
        return False
    return any(
        base == name or base.startswith(f"{name}_")
        for name in HEARTBEAT_IM_CHANNEL_IDS
    )


def should_skip_heartbeat_push(event_type: str, channel_id: Any) -> bool:
    """Return whether an already-normalized event must not be pushed to a channel.

    Call this after terminal errors have been normalized to ``chat.error`` so a
    failing telemetry frame is still delivered as an error.
    """
    return (
        str(event_type or "") in HEARTBEAT_UI_EVENT_TYPES
        and is_heartbeat_im_channel(channel_id)
    )


def merge_heartbeat_push_metadata(
    route_metadata: dict[str, Any] | None,
    event_metadata: Any,
) -> dict[str, Any]:
    """Merge runtime event metadata under the request's delivery route.

    Route identity always wins: an event must not redirect a Heartbeat to
    another bot, chat, or user. Event-owned telemetry keys are preserved.
    """
    route = route_metadata if isinstance(route_metadata, dict) else {}
    merged = dict(event_metadata) if isinstance(event_metadata, dict) else {}
    for key, value in route.items():
        if value not in (None, ""):
            merged[key] = value
    return merged


__all__ = [
    "HEARTBEAT_IM_CHANNEL_IDS",
    "HEARTBEAT_UI_EVENT_TYPES",
    "is_heartbeat_im_channel",
    "merge_heartbeat_push_metadata",
    "should_skip_heartbeat_push",
]
