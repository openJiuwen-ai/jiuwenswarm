# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Remote agent execution via Agent Runtime (AGENT_RUNTIME_BASEURL)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from jiuwenswarm.agents.harness.common.tools.invoke_meta.agent_runtime_client import (
    AgentRuntimeClient,
)
from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
    missing_agent_baseurl_error,
    resolve_agent_runtime_baseurl,
)

logger = logging.getLogger(__name__)


def validate_agent_payload(inputs: Any) -> dict[str, Any]:
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be a dict")
    agent_id = str(inputs.get("agentId") or inputs.get("agent_id") or "").strip()
    query = str(inputs.get("query") or "").strip()
    if not agent_id:
        raise ValueError("agentId is required and cannot be empty")
    if not query:
        raise ValueError("query is required and cannot be empty")
    payload: dict[str, Any] = {"agentId": agent_id, "query": query}
    files_info = inputs.get("filesInfo")
    if files_info is not None:
        if not isinstance(files_info, list):
            raise ValueError("filesInfo must be a list")
        payload["filesInfo"] = files_info
    return payload


def _extract_context_text(frame: Optional[dict[str, Any]]) -> str:
    """从最后一帧 directives 中取 DisplayStreamingText.contextText。"""
    if not isinstance(frame, dict):
        return ""
    directives = frame.get("directives")
    if not isinstance(directives, list):
        return ""
    for directive in directives:
        if not isinstance(directive, dict):
            continue
        header = directive.get("header")
        if not isinstance(header, dict) or header.get("name") != "DisplayStreamingText":
            continue
        payload = directive.get("payload")
        if not isinstance(payload, dict):
            continue
        context_text = payload.get("contextText")
        if isinstance(context_text, str):
            return context_text
    return ""


async def invoke_remote_agent(inputs: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Validate payload and call Agent Runtime ``/agent/run``."""
    _ = kwargs
    try:
        payload = validate_agent_payload(inputs)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    base_url = resolve_agent_runtime_baseurl()
    if not base_url:
        return missing_agent_baseurl_error()

    client = AgentRuntimeClient(base_url)
    logger.info(
        "[agent_as_a_tool] invoking remote agent agentId=%s",
        payload.get("agentId"),
    )
    last_frame: Optional[dict[str, Any]] = None
    try:
        async for frame in client.run_agent_stream(payload):
            if isinstance(frame, dict):
                last_frame = frame
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agent_as_a_tool] runtime agent failed: %s", exc)
        return {"success": False, "error": f"AgentRuntime call failed: {exc}"}

    return {"result": _extract_context_text(last_frame), "success": True}


__all__ = ["invoke_remote_agent", "validate_agent_payload"]
