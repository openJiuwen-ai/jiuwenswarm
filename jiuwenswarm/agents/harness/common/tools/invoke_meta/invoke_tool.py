# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unified invoke meta-tool: cloud PluginSkillExec (mcp/run) or remote Agent Runtime."""

from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator, Dict

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.foundation.tool import Tool, ToolCard

from jiuwenswarm.agents.harness.common.tools.invoke_meta.agent_as_a_tool import (
    invoke_remote_agent,
)
from jiuwenswarm.agents.harness.common.tools.invoke_meta.external_tool_registry import (
    ExternalToolSpec,
    load_external_tools,
)
from jiuwenswarm.agents.harness.common.tools.invoke_meta.plugin_skill_catalog import (
    invoke_arguments_description,
    invoke_function_name_description,
    invoke_tool_description,
)
from jiuwenswarm.agents.harness.common.tools.invoke_meta.schema_context import (
    resolve_session_id,
)
from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
    build_cloud_plugin_context,
    is_desktop_plugin_ws_proxy,
    missing_desktop_proxy_error,
    missing_plugin_url_error,
    missing_plugin_ws_token_error,
    resolve_plugin_runtime_url,
    resolve_plugin_ws_token,
)
from jiuwenswarm.agents.harness.common.tools.invoke_meta.workspace_context import (
    get_effective_request_workspace_dir,
)

logger = logging.getLogger(__name__)

_AGENT_FUNC_NAME = "agent_as_a_tool"
_PLUGIN_SKILL_EXEC = "PluginSkillExecTool"
_BUNDLE_NAME_KEY = "bundleName"
_DEVICE_UNSUPPORTED_MSG = "当前不支持pluginType为Device的端插件调用，请到真机进行测试"
_MUSIC_FUNC = "musicGeneration"


def _parse_invoke_inputs(inputs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    params = inputs.get("arguments", inputs.get("params", {}))
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("arguments 必须是对象")
    params = dict(params)

    func_name = str(inputs.get("functionName") or inputs.get("funcName") or "").strip()
    return func_name, params


def _normalize_plugin_skill_call(
    func_name: str, params: dict[str, Any]
) -> tuple[str, dict[str, Any], bool]:
    """Map invoke(PluginSkillExecTool, {functionName, bundleName, ...}).

    Returns (resolved_function_name, params, via_plugin_skill_exec).
    """
    if func_name != _PLUGIN_SKILL_EXEC:
        return func_name, params, False
    nested_name = str(params.get("functionName") or params.get("funcName") or "").strip()
    if not nested_name:
        raise ValueError(
            "functionName=PluginSkillExecTool 时，arguments.functionName 为必填"
            "（真实云端能力名，如 seedreamLite4Skill / seedanceMiniTask / musicGeneration）"
        )
    return nested_name, dict(params), True


def _resolve_plugin_id(func_name: str, params: dict[str, Any]) -> str:
    plugin_id = str(params.get(_BUNDLE_NAME_KEY) or "").strip()
    if plugin_id:
        return plugin_id

    workspace = get_effective_request_workspace_dir()
    if not workspace:
        return ""

    registry = load_external_tools(workspace)
    matches = sorted(pid for (pid, tool_name) in registry.keys() if tool_name == func_name)
    if len(matches) == 1:
        return matches[0]
    return ""


def _resolve_tool_spec(func_name: str, params: dict[str, Any]) -> ExternalToolSpec | None:
    workspace = get_effective_request_workspace_dir()
    if not workspace:
        return None
    registry = load_external_tools(workspace)
    plugin_id = _resolve_plugin_id(func_name, params)
    if not plugin_id:
        matches = [(pid, tid) for (pid, tid) in registry.keys() if tid == func_name]
        if len(matches) == 1:
            plugin_id = matches[0][0]
        else:
            return None
    return registry.get((plugin_id, func_name))


def _build_plugin_spec(func_name: str, params: dict[str, Any]) -> ExternalToolSpec | dict[str, Any]:
    """Resolve ExternalToolSpec from registry or synthesize from bundleName."""
    plugin_id = _resolve_plugin_id(func_name, params)
    if not plugin_id:
        return {
            "success": False,
            "error": "无法解析 bundleName/pluginId，请在 arguments 中提供 bundleName",
            "toolName": func_name,
        }

    spec = _resolve_tool_spec(func_name, params)
    if spec is not None:
        if spec.plugin_type == "Device":
            return {
                "success": False,
                "error": _DEVICE_UNSUPPORTED_MSG,
                "summary": _DEVICE_UNSUPPORTED_MSG,
                "pluginId": spec.plugin_id,
                "toolName": spec.tool_name,
                "pluginType": spec.plugin_type,
            }
        return spec

    return ExternalToolSpec(
        plugin_id=plugin_id,
        tool_name=func_name.strip(),
        description="",
        protocol="WS",
        plugin_type="Cloud",
    )


async def _invoke_cloud_plugin(
    spec: ExternalToolSpec,
    params: dict[str, Any],
    *,
    session_id: str | None,
) -> Any:
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client import (
        CloudPluginClient,
    )

    base_url = resolve_plugin_runtime_url()
    if not base_url:
        return missing_plugin_url_error(plugin_id=spec.plugin_id, tool_name=spec.tool_name)
    if not is_desktop_plugin_ws_proxy(base_url):
        return missing_desktop_proxy_error(plugin_id=spec.plugin_id, tool_name=spec.tool_name)
    if not resolve_plugin_ws_token():
        return missing_plugin_ws_token_error(plugin_id=spec.plugin_id, tool_name=spec.tool_name)

    skip = {
        _BUNDLE_NAME_KEY,
        "functionName",
        "funcName",
        "skillName",
        "turnContinue",
        "eventContexts",
        "progressToken",
        "contexts",
        "wait",
    }
    arguments = {k: v for k, v in params.items() if k not in skip}
    arguments["bundleName"] = spec.plugin_id
    arguments["functionName"] = spec.tool_name
    for key in ("skillName", "turnContinue", "eventContexts", "progressToken", "contexts"):
        if key in params:
            arguments[key] = params[key]

    context = build_cloud_plugin_context(session_id=session_id)
    ws_timeout = _plugin_ws_timeout(spec.tool_name)
    client = CloudPluginClient(
        base_url=base_url, session_id=session_id, timeout=ws_timeout
    )
    logger.info(
        "[InvokeTool] [session=%s] plugin via mcp/run pluginId=%s toolName=%s url=%s",
        session_id or "",
        spec.plugin_id,
        spec.tool_name,
        base_url,
    )
    return await client.invoke(spec, arguments=arguments, context=context)


def _plugin_ws_timeout(tool_name: str) -> float | None:
    """musicGeneration waits up to 10 minutes; others keep CloudPluginClient default."""
    if tool_name != _MUSIC_FUNC:
        return None
    return float(os.getenv("MUSIC_WS_TIMEOUT", "600") or "600")


async def _dispatch_invoke(
    func_name: str,
    params: dict[str, Any],
    *,
    session_id: str | None,
    **kwargs: Any,
) -> Any:
    if func_name == _AGENT_FUNC_NAME:
        return await invoke_remote_agent(params, **kwargs)

    try:
        func_name, params, _via_plugin_skill = _normalize_plugin_skill_call(func_name, params)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    if not str(params.get(_BUNDLE_NAME_KEY) or "").strip() and not _resolve_plugin_id(func_name, params):
        return {
            "success": False,
            "error": "无法解析 bundleName/pluginId，请在 arguments 中提供 bundleName",
            "toolName": func_name,
        }

    built = _build_plugin_spec(func_name, params)
    if isinstance(built, dict):
        return built
    return await _invoke_cloud_plugin(built, params, session_id=session_id)


def _build_invoke_tool_card() -> ToolCard:
    """Build invoke ToolCard; zone sentence comes from AGENT_RUNTIME_MCP_RUN."""
    return ToolCard(
        id="jiuwenswarm_invoke_tool",
        name="invoke",
        description=invoke_tool_description(),
        input_params={
            "type": "object",
            "properties": {
                "functionName": {
                    "type": "string",
                    "description": invoke_function_name_description(),
                },
                "arguments": {
                    "type": "object",
                    "description": invoke_arguments_description(),
                },
            },
            "required": ["functionName", "arguments"],
        },
    )


class InvokeTool(Tool):
    """Routes invoke to cloud PluginSkillExec (mcp/run) or remote Agent."""

    def __init__(self, card: ToolCard | None = None) -> None:
        super().__init__(card or _build_invoke_tool_card())

    async def invoke(self, inputs: Dict[str, Any], **kwargs) -> Any:
        merged = {**inputs, **kwargs}
        session_id = resolve_session_id(kwargs) or resolve_session_id(merged)
        try:
            func_name, params = _parse_invoke_inputs(merged)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        if not func_name:
            return {"success": False, "error": "functionName 为必填参数"}

        logger.info(
            "[InvokeTool] functionName=%s sessionId=%s",
            func_name,
            session_id or "",
        )
        return await _dispatch_invoke(
            func_name,
            params,
            session_id=session_id,
            **kwargs,
        )

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> AsyncIterator[Any]:
        yield "Stream is not supported for this tool."
        raise build_error(StatusCode.TOOL_STREAM_NOT_SUPPORTED, card=self._card)


__all__ = ["InvokeTool"]
