# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interrupt helpers for DeepAgent.

Provides utilities for converting interrupt payloads to frontend format
and building permission rails.
"""
from __future__ import annotations

from typing import Any

from jiuwenclaw.agentserver.permissions.core import get_permission_engine
from jiuwenclaw.utils import logger


def build_permission_rail(
    config: dict[str, Any],
    llm: Any = None,
    model_name: str | None = None,
) -> Any | None:
    """Build PermissionInterruptRail for tool permission checks.

    Args:
        config: Agent config dict containing permissions section
        llm: LLM instance for risk assessment
        model_name: Model name for risk assessment

    Returns:
        PermissionInterruptRail instance or None if disabled
    """
    from jiuwenclaw.agentserver.deep_agent.rails.permission_rail import PermissionInterruptRail

    permission_config = config.get("permissions", {})
    logger.info(
        "[InterruptHelpers] build_permission_rail called: enabled=%s",
        permission_config.get("enabled", False)
    )

    if not permission_config.get("enabled", False):
        logger.info("[InterruptHelpers] Permission system is disabled, returning None")
        return None

    logger.info(
        "[InterruptHelpers] Building PermissionInterruptRail intercept=all llm=%s model_name=%s",
        llm is not None,
        model_name,
    )
    try:
        permission_rail = PermissionInterruptRail(
            config=permission_config,
            engine=get_permission_engine(),
            llm=llm,
            model_name=model_name,
        )
        logger.info(
            "[InterruptHelpers] PermissionInterruptRail created successfully intercept=all"
        )
    except Exception as exc:
        logger.warning("[InterruptHelpers] PermissionInterruptRail create failed: %s", exc)
        permission_rail = None
    return permission_rail


def build_ask_user_rail() -> Any | None:
    """Build AskUserRail for user input requests.

    Returns:
        AskUserRail instance or None if creation failed
    """
    from openjiuwen.harness.rails.interrupt.ask_user_rail import AskUserRail

    try:
        ask_user_rail = AskUserRail()
        logger.info("[InterruptHelpers] AskUserRail created successfully")
    except Exception as exc:
        logger.warning("[InterruptHelpers] AskUserRail create failed: %s", exc)
        ask_user_rail = None
    return ask_user_rail


def convert_interactions_to_ask_user_question(state_outputs: list) -> dict | None:
    """Convert __interaction__ list to frontend chat.ask_user_question format.

    Args:
        state_outputs: List of OutputSchema(type=__interaction__, payload=InteractionOutput)
                      Note: In streaming mode, this list contains only one element per chunk

    Returns:
        Frontend expected chat.ask_user_question format dict
    """
    if not state_outputs:
        return None

    payload = state_outputs[0].payload if hasattr(state_outputs[0], 'payload') else state_outputs[0]
    question_data = extract_question_from_interaction(payload)
    if not question_data:
        return None

    request_id = getattr(payload, 'id', '') if hasattr(payload, 'id') else payload.get('id', '')

    return {
        "event_type": "chat.ask_user_question",
        "request_id": request_id,
        "questions": [question_data],
        "source": "permission_interrupt",
    }


def extract_question_from_interaction(payload: Any) -> dict | None:
    """Extract question info from a single interaction payload.

    Args:
        payload: InteractionOutput instance or dict

    Returns:
        Question format dict for frontend
    """
    if payload is None:
        return None

    tool_name = ""
    message = ""

    if hasattr(payload, 'value'):
        value_obj = payload.value
        message = getattr(value_obj, 'message', '') or getattr(value_obj, 'question', '')
        tool_name = getattr(value_obj, 'tool_name', '')
    elif isinstance(payload, dict):
        value_obj = payload.get('value', {})
        if isinstance(value_obj, dict):
            message = value_obj.get('message', '') or value_obj.get('question', '')
            tool_name = value_obj.get('tool_name', '')
        else:
            message = payload.get('message', '') or payload.get('question', '')
    else:
        return None

    return {
        "question": message or f"工具 `{tool_name}` 需要授权才能执行",
        "header": f"权限审批: {tool_name}" if tool_name else "权限审批",
        "options": [
            {"label": "本次允许", "description": "仅本次授权执行"},
            {"label": "总是允许", "description": "记住该规则，以后自动放行"},
            {"label": "拒绝", "description": "拒绝执行此工具"},
        ],
        "multi_select": False,
    }
