# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""小艺 GUI 自动化（xiaoyi_gui_agent）：通过 InvokeJarvisGUIAgent 与设备协同完成屏幕操作."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.gui_rpc.reverse_rpc import XIAOYI_GUI_MAX_TIMEOUT_SECONDS
from jiuwenswarm.common.invocation_context import get_current_invocation_context
from jiuwenswarm.common.reverse_rpc.errors import (
    ReverseRpcError,
    ReverseRpcTimeoutError,
    ReverseRpcTransportDisconnected,
)
from jiuwenswarm.common.utils import logger
from jiuwenswarm.server.gui_rpc.reverse_rpc import (
    get_xiaoyi_gui_reverse_rpc_client,
)
from jiuwenswarm.server.xiaoyi_invocation import get_xiaoyi_invocation_extension

from .utils import ToolInputError, format_success_response


def _get_gui_tool_async_lock(channel: Any) -> asyncio.Lock:
    gl = getattr(channel, "gui_tool_lock", None)
    if gl is not None:
        return gl
    inner = getattr(channel, "_gui_tool_lock", None)
    if inner is None:
        inner = asyncio.Lock()
        setattr(channel, "_gui_tool_lock", inner)
    return inner


def _payload_is_gui_final(payload: Dict[str, Any]) -> bool:
    """兼容设备 isFinal 为 bool / 1 / \"true\" 等."""
    v = payload.get("isFinal")
    if v is True:
        return True
    if isinstance(v, (int, float)) and int(v) == 1:
        return True
    if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes"):
        return True
    return False


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


@tool(
    name="xiaoyi_gui_agent",
    description=(
        "Automates various tasks in phone apps by simulating human interactions on the phone screen "
        "(taps, swipes, text input, page navigation, etc.).\n\n"
        "This tool operates like a real user on the phone, so it can complete many tasks that cannot be "
        "done through internet APIs, for example:\n"
        "- The task requires actually operating a phone app UI\n"
        "- The data exists only inside an app\n"
        "- The data cannot be obtained through internet APIs\n"
        "- User actions need to be performed (check-ins, following, purchasing, etc.)\n"
        "- Content needs to be posted or sent within an app\n"
        "- App or phone settings need to be modified\n\n"
        "Notes:\n"
        "- The operation timeout is 3 minutes (180 seconds)\n"
        "- This tool takes a long time to run; do not call it repeatedly\n"
        "- While this tool is running, do not make any other tool calls; you must wait until this tool "
        "returns a result or times out before doing anything else. Whether it is a new text reply or the "
        "next tool call, you must strictly wait while this tool is running\n"
        "- If it times out or fails, retry at most once\n"
        "- If the user instruction involves reading/writing notes or viewing calendar events, do not put "
        "such operations in the query parameter; use the preset note-related tools and calendar-related "
        "tools to complete those operations\n\n"
        "Parameter query: the natural-language operation instruction and the expected result."
    ),
)
async def xiaoyi_gui_agent(query: str) -> Dict[str, Any]:
    """执行 GUI Agent 指令."""
    if not query or not isinstance(query, str) or not query.strip():
        raise ToolInputError("Missing valid parameter query (non-empty string)")

    query = query.strip()
    invocation = get_current_invocation_context()
    if invocation is None:
        logger.error(
            "[INVOCATION_CTX] MISSING phase=TOOL_READ capability=gui query_len=%s",
            len(query),
        )
        raise RuntimeError(
            "GUI Agent call failed [INVALID_CONTEXT]: the current invocation context is unavailable"
        )
    logger.info(
        "[INVOCATION_CTX] TOOL_READ capability=gui invocation_id=%s "
        "request_id=%s session_id=%s channel_id=%s asyncio_task_id=%s",
        invocation.invocation_id,
        invocation.request_id,
        invocation.session_id,
        invocation.channel_id,
        id(asyncio.current_task()) if asyncio.current_task() else None,
    )
    if str(invocation.channel_id or "").strip().lower() != "xiaoyi":
        raise RuntimeError(
            "GUI Agent call failed [INVALID_CONTEXT]: the current invocation is not on the Xiaoyi channel"
        )
    xiaoyi = get_xiaoyi_invocation_extension(invocation)
    xiaoyi_session_id = _first_text(
        xiaoyi.root_session_id if xiaoyi else None,
        xiaoyi.params_session_id if xiaoyi else None,
        invocation.chat_id,
    )
    xiaoyi_task_id = _first_text(xiaoyi.task_id if xiaoyi else None)
    xiaoyi_message_id = _first_text(xiaoyi.message_id if xiaoyi else None)
    missing = [
        name
        for name, value in (
            ("xiaoyi_session_id", xiaoyi_session_id),
            ("xiaoyi_task_id", xiaoyi_task_id),
            ("xiaoyi_message_id", xiaoyi_message_id),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "GUI Agent call failed [INVALID_CONTEXT]: the current Xiaoyi invocation is missing "
            + ", ".join(missing)
        )
    logger.info(
        "[GUI_RPC_TRACE] phase=TOOL_CALL_BEGIN source_request_id=%s "
        "jiuwen_session_id=%s channel_id=%s query_len=%s",
        invocation.request_id,
        invocation.session_id,
        invocation.channel_id,
        len(query),
    )
    try:
        response = await get_xiaoyi_gui_reverse_rpc_client().call(
            query=query,
            source_request_id=invocation.request_id,
            jiuwen_session_id=invocation.session_id,
            xiaoyi_session_id=xiaoyi_session_id,
            xiaoyi_task_id=xiaoyi_task_id,
            xiaoyi_message_id=xiaoyi_message_id,
            device_id=_first_text(xiaoyi.device_id if xiaoyi else None),
            execution_id=invocation.invocation_id,
            app_id=_first_text(xiaoyi.app_id if xiaoyi else None),
            binding_id=_first_text(xiaoyi.binding_id if xiaoyi else None),
            timeout=XIAOYI_GUI_MAX_TIMEOUT_SECONDS,
        )
    except ReverseRpcTransportDisconnected as exc:
        logger.error(
            "[GUI_RPC_TRACE] phase=TOOL_CALL_FAILED source_request_id=%s "
            "error_code=TRANSPORT_DISCONNECTED error_type=%s",
            invocation.request_id,
            type(exc).__name__,
        )
        raise RuntimeError(
            "GUI Agent call failed [TRANSPORT_DISCONNECTED]: the Gateway connection has been disconnected"
        ) from exc
    except (ReverseRpcTimeoutError, asyncio.TimeoutError) as exc:
        logger.error(
            "[GUI_RPC_TRACE] phase=TOOL_CALL_FAILED source_request_id=%s "
            "error_code=GUI_TIMEOUT error_type=%s",
            invocation.request_id,
            type(exc).__name__,
        )
        raise RuntimeError(
            "GUI Agent call failed [GUI_TIMEOUT]: the Xiaoyi GUI Agent operation timed out (3 minutes)"
        ) from exc
    except ReverseRpcError as exc:
        error_code = str(getattr(exc, "code", None) or "REVERSE_RPC_ERROR")
        logger.error(
            "[GUI_RPC_TRACE] phase=TOOL_CALL_FAILED source_request_id=%s "
            "error_code=%s error_type=%s",
            invocation.request_id,
            error_code,
            type(exc).__name__,
        )
        raise RuntimeError(
            f"GUI Agent call failed [{error_code}]: {exc}"
        ) from exc
    except Exception as exc:
        logger.exception(
            "[GUI_RPC_TRACE] phase=TOOL_CALL_FAILED source_request_id=%s "
            "error_code=INTERNAL_ERROR error_type=%s",
            invocation.request_id,
            type(exc).__name__,
        )
        raise RuntimeError(
            f"GUI Agent call failed [INTERNAL_ERROR]: {exc}"
        ) from exc

    if not response.success:
        error_code = response.error_code or "INTERNAL_ERROR"
        error_message = response.error_message or "GUI RPC execution failed"
        logger.error(
            "[GUI_RPC_TRACE] phase=TOOL_RESPONSE_FAILED source_request_id=%s "
            "rpc_id=%s error_code=%s",
            invocation.request_id,
            response.rpc_id,
            error_code,
        )
        raise RuntimeError(
            f"GUI Agent call failed [{error_code}]: {error_message}"
        )
    text = response.result or ""
    logger.info(
        "[GUI_RPC_TRACE] phase=TOOL_CALL_COMPLETED source_request_id=%s "
        "rpc_id=%s result_len=%s",
        invocation.request_id,
        response.rpc_id,
        len(text),
    )
    logger.info(
        "[GUI_AGENT_DIAG] phase=TOOL_RESULT_RETURNED "
        "source_request_id=%s rpc_id=%s result=%r",
        invocation.request_id,
        response.rpc_id,
        text,
    )
    return format_success_response(
        {"success": True, "result": text},
        "GUI operation completed",
    )
