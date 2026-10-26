# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Integration helpers for wiring A2UI into jiuwenswarm host modules.

This module owns A2UI-specific branching that would otherwise spread across
AgentServer, Gateway, and Web config handlers. Host modules should call these
small helpers and keep their original control flow generic.
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.server.runtime.a2ui.config import get_a2ui_config, get_current_a2ui_config

logger = logging.getLogger(__name__)

_WEB_CONFIG_KEY_MAP: dict[str, str] = {
    "a2ui_enabled": "enabled",
}

_A2UI_CONFIG_DEFAULT_PAYLOAD: dict[str, str] = {
    "a2ui_enabled": "false",
}

_A2UI_CHANNEL_ID = "web"


def is_a2ui_channel(channel: str | None) -> bool:
    """Return whether the channel can natively run A2UI."""
    return str(channel or "").strip().lower() == _A2UI_CHANNEL_ID


def _to_bool(value: Any) -> bool:
    """Normalize Web config boolean values to Python bools."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def _bool_text(value: bool) -> str:
    """Render boolean config values in the string format used by Web config."""
    return "true" if value else "false"


def _get_runtime_a2ui_config():
    """Read runtime A2UI config while preserving env-only fallback for tests."""
    try:
        return get_current_a2ui_config()
    except ModuleNotFoundError as exc:
        if exc.name != "yaml":
            raise
        return get_a2ui_config({})


def _build_a2ui_client_event_prompt(content: dict[str, Any], channel: str, language: str) -> str:
    """Delegate client-event prompt construction to the A2UI runtime package."""
    from jiuwenswarm.server.runtime.a2ui.runtime.prompt import build_a2ui_client_event_prompt

    return build_a2ui_client_event_prompt(content, channel=channel, language=language)


def build_user_prompt_if_a2ui_event(
    content: object,
    *,
    channel: str,
    language: str,
) -> str | None:
    """Build a model prompt for A2UI client events when the feature is enabled.

    Returns ``None`` when the payload is not an A2UI client event or when A2UI is
    disabled, allowing the normal user prompt builder to continue unchanged.
    """
    if not is_a2ui_channel(channel):
        return None

    a2ui_config = _get_runtime_a2ui_config()
    if not a2ui_config.enabled:
        return None

    if not isinstance(content, dict) or content.get("type") != "a2ui.client_event":
        return None

    return _build_a2ui_client_event_prompt(content, channel, language)


async def finalize_assistant_response_if_a2ui(
    content: str,
    *,
    channel: str | None = _A2UI_CHANNEL_ID,
    user_query: Any,
    request_id: str,
    repair_call: Any,
    retry_without_a2ui_call: Any = None,
) -> str:
    """Validate/repair assistant A2UI content while keeping host modules generic."""
    if not is_a2ui_channel(channel):
        return content

    try:
        a2ui_config = _get_runtime_a2ui_config()
    except Exception:
        logger.debug("A2UI response finalization skipped: config lookup failed", exc_info=True)
        return content

    from jiuwenswarm.server.runtime.a2ui.runtime.response_finalization import finalize_a2ui_assistant_content

    return await finalize_a2ui_assistant_content(
        content,
        user_query=user_query,
        request_id=request_id,
        repair_call=repair_call if callable(repair_call) else None,
        a2ui_enabled=a2ui_config.enabled,
        retry_without_a2ui_call=retry_without_a2ui_call if callable(retry_without_a2ui_call) else None,
    )


def apply_non_web_text_fallback_to_payload(
    payload: dict[str, object],
    *,
    channel_id: str,
) -> dict[str, object]:
    """Retain the legacy gateway hook while keeping A2UI Web-only.

    Web payloads keep raw A2UI blocks for the frontend renderer. Non-Web
    channels bypass A2UI entirely, including the previous text fallback path.
    """
    return payload


def get_a2ui_config_payload(raw_config: dict[str, object]) -> dict[str, str]:
    """Return user-facing Web config payload fields for the A2UI section."""
    config = get_a2ui_config(raw_config)
    return {
        "a2ui_enabled": _bool_text(config.enabled),
    }


def get_default_a2ui_config_payload() -> dict[str, str]:
    """Return fallback Web config fields when config loading fails."""
    return dict(_A2UI_CONFIG_DEFAULT_PAYLOAD)


def validate_a2ui_config_update(
    param_key: str,
    value: object,
) -> tuple[bool, dict[str, object], str]:
    """Validate and map one Web A2UI config update to config.yaml keys."""
    config_key = _WEB_CONFIG_KEY_MAP.get(param_key)
    if config_key is None:
        return False, {}, f"Unknown A2UI config key: {param_key}"

    return True, {config_key: _to_bool(value)}, ""


__all__ = [
    "apply_non_web_text_fallback_to_payload",
    "build_user_prompt_if_a2ui_event",
    "finalize_assistant_response_if_a2ui",
    "get_a2ui_config_payload",
    "get_default_a2ui_config_payload",
    "is_a2ui_channel",
    "validate_a2ui_config_update",
]
