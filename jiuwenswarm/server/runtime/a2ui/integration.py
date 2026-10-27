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

# relay-claw 走 officeclaw channel，a2ui 对其原生生效（与 web 同等）。
_A2UI_CHANNEL_IDS = frozenset({"web", "officeclaw"})

# a2ui 流式 probe（pending render chunk）机制的适用渠道：目前仅 web 前端。
# officeclaw (relay-claw) 用 parseA2UIContent 从累积文本提取 <a2ui-json> 块，
# probe 插入的 <a2ui-json>\n 会与 agent 原始块重复、污染 content 致前端提取失败。
# 与 _A2UI_CHANNEL_IDS（能原生渲染 a2ui 的渠道）有意双轨：新增渠道时
# 「能渲染」不自动等于「需要 probe」，两侧必须各自显式声明，防语义搭便车。
_A2UI_PROBE_CHANNEL_IDS = frozenset({"web"})


def is_a2ui_channel(channel: str | None) -> bool:
    """Return whether the channel can natively run A2UI."""
    return str(channel or "").strip().lower() in _A2UI_CHANNEL_IDS


def is_a2ui_probe_channel(channel: str | None) -> bool:
    """Return whether the channel consumes A2UI streaming probe (pending render) chunks."""
    return str(channel or "").strip().lower() in _A2UI_PROBE_CHANNEL_IDS


# 仅这些渠道走服务端 A2UI repair/retry/重发（finalize_assistant_response_if_a2ui）。
# officeclaw (relay-claw) 故意排除，完全跳过服务端 finalize（含校验）：
# 1. 服务端 repair 后整段 chat.final 重发会与已累积 delta 产生重复块；
# 2. 该重发帧落在 wire 终结块之后被 relay-claw 消费方 break 丢弃；
# 3. 破损 a2ui 内容原样透传，由前端做文本降级显示（本仓库 a2uiContent.ts
#    extractTextFromMalformedA2UI :173-211 正则文本打捞 + invalidA2UIFallback
#    :162 固定错误文案降级——是降级显示而非 JSON 修复）。
# 完全跳过 = 不 repair/不校验/不重发，从根上消除重发帧时序丢失与重复块。
_A2UI_FINALIZE_REPAIR_CHANNEL_IDS = frozenset({"web"})


def should_finalize_repair(channel: str | None) -> bool:
    """Return whether the channel runs server-side A2UI repair/retry/resend."""
    return str(channel or "").strip().lower() in _A2UI_FINALIZE_REPAIR_CHANNEL_IDS


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
    client_event 形态但被渠道/开关门控挡下时留 warning：此时 dict 会走普通
    prompt 路径被 json.dumps 进提示词（模型可解读但非成品 prompt），日志便于定位
    前后端开关不一致（如 env 单侧强开、YAML truthy 变体、关开关瞬间的竞态）。
    """
    if not isinstance(content, dict) or content.get("type") != "a2ui.client_event":
        return None

    if not is_a2ui_channel(channel):
        logger.warning(
            "A2UI client event received on non-a2ui channel %r; "
            "falling back to raw-dict prompt (channel gate)",
            channel,
        )
        return None

    a2ui_config = _get_runtime_a2ui_config()
    if not a2ui_config.enabled:
        logger.warning(
            "A2UI client event received while a2ui disabled; "
            "falling back to raw-dict prompt (feature gate)",
        )
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

    # officeclaw (relay-claw) 完全跳过服务端 finalize（含校验）：不 repair、不校验、
    # 不重发。破损 a2ui 内容原样透传，由前端做文本降级显示（本仓库 a2uiContent.ts
    # extractTextFromMalformedA2UI :173-211 正则文本打捞 + invalidA2UIFallback :162
    # 固定错误文案降级——是降级显示而非 JSON 修复）。
    if not should_finalize_repair(channel):
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
