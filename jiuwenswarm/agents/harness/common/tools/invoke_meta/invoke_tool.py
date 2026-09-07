# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unified invoke meta-tool: cloud PluginSkillExec (mcp/run) or remote Agent Runtime."""

from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator, Dict

import anyio
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
    invoke_timeout_s_description,
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
_INVOKE_TIMEOUT_MIN_S = 1.0
_INVOKE_TIMEOUT_MAX_S = 3600.0
_DEFAULT_INVOKE_TIMEOUT_S = 300.0
_BATCH_IMAGE_SECONDS = 60.0
_PLUGIN_SKIP_KEYS = frozenset(
    {
        _BUNDLE_NAME_KEY,
        "functionName",
        "funcName",
        "skillName",
        "turnContinue",
        "eventContexts",
        "progressToken",
        "contexts",
        "wait",
        "timeout_s",
    }
)


def _parse_invoke_inputs(inputs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    params = inputs.get("arguments", inputs.get("params", {}))
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("arguments 必须是对象")
    params = dict(params)

    func_name = str(inputs.get("functionName") or inputs.get("funcName") or "").strip()
    return func_name, params


def _coerce_positive_timeout(raw: Any) -> float | None:
    """Return a positive finite timeout, or None when the value is not usable."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return None
    return value


def _extract_explicit_timeout_s(
    inputs: dict[str, Any], params: dict[str, Any]
) -> float | None:
    """Top-level timeout_s wins; otherwise arguments.timeout_s if it is a positive number."""
    explicit = _coerce_positive_timeout(inputs.get("timeout_s"))
    if explicit is not None:
        return explicit
    return _coerce_positive_timeout(params.get("timeout_s"))


def _clamp_timeout_s(value: float) -> float:
    return max(_INVOKE_TIMEOUT_MIN_S, min(_INVOKE_TIMEOUT_MAX_S, float(value)))


def _parse_positive_int(raw: Any) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float) and raw.is_integer() and raw > 0:
        return int(raw)
    if isinstance(raw, str) and raw.strip().isdigit():
        parsed = int(raw.strip())
        return parsed if parsed > 0 else None
    return None


def _music_default_timeout_s() -> float:
    return _clamp_timeout_s(float(os.getenv("MUSIC_WS_TIMEOUT", "600") or "600"))


def _resolve_invoke_timeout(
    func_name: str,
    params: dict[str, Any],
    explicit: float | None,
) -> tuple[float, bool, bool]:
    """Return (timeout_s, default_wall, is_explicit).

    default_wall is True only for the leftover 300s branch (Client timeout stays None).
    Explicit 300 still counts as explicit and is passed to CloudPluginClient.
    """
    if explicit is not None:
        return _clamp_timeout_s(explicit), False, True
    if func_name == _MUSIC_FUNC:
        return _music_default_timeout_s(), False, False
    image_count = _parse_positive_int(params.get("max_images"))
    if image_count is not None and image_count > 1:
        return _clamp_timeout_s(_BATCH_IMAGE_SECONDS * image_count), False, False
    return _DEFAULT_INVOKE_TIMEOUT_S, True, False


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
    explicit_timeout: float | None = None,
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

    arguments = {k: v for k, v in params.items() if k not in _PLUGIN_SKIP_KEYS}
    arguments["bundleName"] = spec.plugin_id
    arguments["functionName"] = spec.tool_name
    for key in ("skillName", "turnContinue", "eventContexts", "progressToken", "contexts"):
        if key in params:
            arguments[key] = params[key]

    context = build_cloud_plugin_context(session_id=session_id)
    timeout, default_wall, is_explicit = _resolve_invoke_timeout(
        spec.tool_name, params, explicit_timeout
    )
    client_timeout = None if default_wall else timeout
    client = CloudPluginClient(
        base_url=base_url, session_id=session_id, timeout=client_timeout
    )
    logger.info(
        "[InvokeTool] [session=%s] plugin via mcp/run pluginId=%s toolName=%s url=%s "
        "max_images=%s timeout=%s explicit=%s",
        session_id or "",
        spec.plugin_id,
        spec.tool_name,
        base_url,
        params.get("max_images"),
        timeout,
        is_explicit,
    )
    try:
        with anyio.fail_after(timeout):
            return await client.invoke(spec, arguments=arguments, context=context)
    except TimeoutError:
        seconds = int(timeout) if timeout == int(timeout) else timeout
        return {
            "success": False,
            "error": f"{spec.tool_name} timed out after {seconds}s",
            "pluginId": spec.plugin_id,
            "toolName": spec.tool_name,
        }


async def _dispatch_invoke(
    func_name: str,
    params: dict[str, Any],
    *,
    session_id: str | None,
    explicit_timeout: float | None = None,
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
    return await _invoke_cloud_plugin(
        built,
        params,
        session_id=session_id,
        explicit_timeout=explicit_timeout,
    )


def _build_invoke_tool_card() -> ToolCard:
    """Build invoke ToolCard; zone sentence comes from AGENT_RUNTIME_MCP_RUN."""
    card = ToolCard(
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
                "timeout_s": {
                    "type": "number",
                    "description": invoke_timeout_s_description(),
                },
            },
            "required": ["functionName", "arguments"],
        },
    )
    # AbilityManager defaults to 300s when resilience is absent; group-image /
    # music / explicit timeout_s can exceed that. Real deadline is fail_after
    # inside InvokeTool (capped at 3600).
    card.properties["resilience"] = {"timeout_s": None}
    return card


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

        explicit_timeout = _extract_explicit_timeout_s(merged, params)
        logger.info(
            "[InvokeTool] functionName=%s sessionId=%s",
            func_name,
            session_id or "",
        )
        return await _dispatch_invoke(
            func_name,
            params,
            session_id=session_id,
            explicit_timeout=explicit_timeout,
            **kwargs,
        )

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> AsyncIterator[Any]:
        yield "Stream is not supported for this tool."
        raise build_error(StatusCode.TOOL_STREAM_NOT_SUPPORTED, card=self._card)


__all__ = ["InvokeTool"]
