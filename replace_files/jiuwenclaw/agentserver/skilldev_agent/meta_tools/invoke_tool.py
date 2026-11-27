# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unified invoke meta-tool: routes to agent or external function call by functionName."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.foundation.tool import Tool, ToolCard

from jiuwenclaw.agentserver.skilldev_agent.meta_tools.agent_as_a_tool import AgentAsATool
from jiuwenclaw.agentserver.skilldev_agent.meta_tools.cloud_plugin_client import CloudPluginClient
from jiuwenclaw.agentserver.skilldev_agent.meta_tools.external_tool_registry import (
    ExternalToolSpec,
    load_external_tools,
)
from jiuwenclaw.agentserver.skilldev_agent.meta_tools.function_call_tool import (
    _function_call_tool_impl,
)
from jiuwenclaw.agentserver.skilldev_agent.meta_tools.llm_call_client import call_llm
from jiuwenclaw.agentserver.skilldev_agent.meta_tools.skilldev_tool_context import (
    load_agent_output_schema,
    load_tool_output_schema,
    resolve_session_id,
)
from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
    get_effective_request_workspace_dir,
)
from jiuwenclaw.sandbox.sandbox_routing_settings import sandbox_routing_enabled

logger = logging.getLogger(__name__)

_AGENT_FUNC_NAME = "agent_as_a_tool"
_BUNDLE_NAME_KEY = "bundleName"


def _parse_invoke_inputs(inputs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    func_name = str(inputs.get("functionName", inputs.get("funcName", ""))).strip()
    params = inputs.get("arguments", inputs.get("params", {}))
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("arguments 必须是对象")
    return func_name, dict(params)


def _resolve_plugin_id(func_name: str, params: dict[str, Any]) -> str:
    plugin_id = str(params.get(_BUNDLE_NAME_KEY) or "").strip()
    if plugin_id:
        return plugin_id

    workspace = get_effective_request_workspace_dir()
    if not workspace:
        return ""

    registry = load_external_tools(workspace)
    matches = sorted(
        pid for (pid, tool_name) in registry.keys() if tool_name == func_name
    )
    if len(matches) == 1:
        return matches[0]
    return ""


def _resolve_tool_spec(
        func_name: str, params: dict[str, Any]
) -> ExternalToolSpec | None:
    """根据 func_name 从工作区加载工具定义，返回 ExternalToolSpec 或 None。"""
    workspace = get_effective_request_workspace_dir()
    if not workspace:
        return None
    registry = load_external_tools(workspace)
    plugin_id = _resolve_plugin_id(func_name, params)
    if not plugin_id:
        # 尝试通过 tool_name 查找唯一匹配
        matches = [(pid, tid) for (pid, tid) in registry.keys() if tid == func_name]
        if len(matches) == 1:
            plugin_id = matches[0][0]
        else:
            return None
    return registry.get((plugin_id, func_name))


def _build_function_call_inputs(func_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Map invoke fields to function_call_tool: bundleName→pluginId, functionName→toolName."""
    return {
        "pluginId": _resolve_plugin_id(func_name, params),
        "toolName": func_name.strip(),
        "arguments": {
            k: v for k, v in params.items() if k != _BUNDLE_NAME_KEY
        },
    }


def _build_call_llm_params(func_name: str, params: dict[str, Any]) -> Any:
    """Build call_llm params from InvokeTool input.arguments."""
    if func_name == _AGENT_FUNC_NAME:
        return params.get("query", "")
    return {k: v for k, v in params.items() if k != _BUNDLE_NAME_KEY}


def _load_output_schema(
        func_name: str,
        params: dict[str, Any],
        session_id: str | None,
) -> Any | None:
    if not session_id:
        return None
    if func_name == _AGENT_FUNC_NAME:
        agent_id = str(params.get("agentId") or params.get("agent_id") or "").strip()
        if not agent_id:
            return None
        return load_agent_output_schema(
            session_id,
            agent_id,
            log_prefix="InvokeTool",
        )

    plugin_id = _resolve_plugin_id(func_name, params)
    if not plugin_id:
        return None
    return load_tool_output_schema(
        session_id,
        plugin_id,
        func_name,
        log_prefix="InvokeTool",
    )


async def _invoke_cloud_plugin(
        spec: ExternalToolSpec,
        params: dict[str, Any],
        *,
        session_id: str | None
) -> Any:
    """调用云插件（pluginType == "Cloud"）通过 WebSocket 接口。"""
    import os

    base_url = os.environ.get("AGENT_RUNTIME_MCP_RUN", "")

    if not base_url:
        return {
            "success": False,
            "error": "缺少环境变量 AGENT_RUNTIME_MCP_RUN，无法调用云插件",
            "pluginId": spec.plugin_id,
            "toolName": spec.tool_name,
            "pluginType": spec.plugin_type
        }

    client = CloudPluginClient(
        base_url=base_url,
        session_id=session_id
    )

    arguments = {k: v for k, v in params.items() if k != _BUNDLE_NAME_KEY}
    logger.info(
        "[InvokeTool] [session=%s] invoke CloudPlugin pluginId=%s toolName=%s",
        session_id or "",
        spec.plugin_id,
        spec.tool_name
    )

    return await client.invoke(
        spec,
        arguments=arguments
    )


async def _invoke_sandbox(
        func_name: str,
        params: dict[str, Any],
        *,
        session_id: str | None,
) -> Any:
    # 云 Agent 调用
    if func_name == _AGENT_FUNC_NAME:
        spec = ExternalToolSpec(
            plugin_id="xiaoyi",
            tool_name="agent_as_a_tool",
            description="",
            protocol="WS",
            plugin_type="Agent"
        )
        agent_id = str(params.get("agentId") or params.get("agent_id") or "").strip()
        if not agent_id:
            logger.warning(
                "[sessionId=%s] [InvokeTool] agentId is empty, agent call failed! functionName:%s,Params:%s",
                session_id,
                func_name,
                params
            )
            return {
                "success": False,
                "error": "agentId is empty, please check agentId had write to the correct file"
            }
        return await _invoke_cloud_plugin(
            spec, params, session_id=session_id
        )

    spec = _resolve_tool_spec(func_name, params)
    # 云插件调用
    if spec:
        if spec.plugin_type in {"Cloud", "MCP"}:
            return await _invoke_cloud_plugin(
                spec, params, session_id=session_id
            )

    output_schema = _load_output_schema(func_name, params, session_id)
    if not output_schema:
        return {
            "success": False,
            "error": "outputSchema is None, please check outputSchema had write to the correct file"
        }
    llm_params = _build_call_llm_params(func_name, params)
    logger.info(
        "Load output schema success, functionName:%s,llmParams:%s, outputSchema:%s",
        func_name,
        llm_params,
        output_schema
    )
    result = await call_llm(
        func_name=func_name,
        params=llm_params,
        output_schema=output_schema,
        session_id=session_id,
    )
    logger.info("End to call llm, result:%s", result)
    return result


async def _invoke_agent(params: dict[str, Any], **kwargs: Any) -> Any:
    tool = AgentAsATool()
    return await tool.invoke(params, **kwargs)


async def _invoke_function(
        func_name: str,
        params: dict[str, Any],
        *,
        session_id: str | None,
        **kwargs: Any,
) -> Any:
    # bundleName → pluginId，functionName → toolName，其余 arguments 字段保持不变
    plugin_id = _resolve_plugin_id(func_name, params)
    tool_name = func_name.strip()
    arguments = {k: v for k, v in params.items() if k != _BUNDLE_NAME_KEY}
    function_inputs = {
        "pluginId": plugin_id,
        "toolName": tool_name,
        "arguments": arguments,
    }
    output_schema = None
    if session_id:
        plugin_id = function_inputs.get("pluginId", "")
        tool_name = function_inputs.get("toolName", "")
        if plugin_id and tool_name:
            output_schema = load_tool_output_schema(
                session_id,
                str(plugin_id),
                str(tool_name),
                log_prefix="InvokeTool",
            )
    logger.info(
        "[InvokeTool] sessionId=%s functionName=%s outputSchema=%s",
        session_id or "",
        func_name,
        "set" if output_schema is not None else "missing",
    )
    result = await _function_call_tool_impl(**function_inputs)
    if output_schema is not None and isinstance(result, dict):
        result.setdefault("outputSchema", output_schema)
    return result


async def _dispatch_invoke(
        func_name: str,
        params: dict[str, Any],
        *,
        session_id: str | None,
        **kwargs: Any,
) -> Any:
    if sandbox_routing_enabled():
        logger.info("Sandbox routing enabled, functionName=%s, arguments=%s", func_name, params)
        return await _invoke_sandbox(func_name, params, session_id=session_id)

    if func_name == _AGENT_FUNC_NAME:
        return await _invoke_agent(params, **kwargs)

    return await _invoke_function(
        func_name,
        params,
        session_id=session_id,
        **kwargs,
    )


async def _invoke_tool_impl(**inputs: Any) -> Any:
    try:
        func_name, params = _parse_invoke_inputs(inputs)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    if not func_name:
        return {"success": False, "error": "functionName 为必填参数"}

    session_id = resolve_session_id(inputs)
    logger.info(
        "[invoke_tool] functionName=%s sessionId=%s sandbox=%s",
        func_name,
        session_id or "",
        sandbox_routing_enabled(),
    )
    return await _dispatch_invoke(
        func_name,
        params,
        session_id=session_id,
        **inputs,
    )


_INVOKE_TOOL_CARD = ToolCard(
    id="jiuwenclaw_invoke_tool",
    name="invoke",
    description=(
        "统一调用外部插件工具或 Agent 服务。"
        "functionName 为 agent_as_a_tool 时，arguments 需包含 agentId、query 及可选 filesInfo；"
        "否则 functionName 为 toolName，arguments 含 bundleName 及工具自定义参数。"
    ),
    input_params={
        "type": "object",
        "properties": {
            "functionName": {
                "type": "string",
                "description": (
                    "工具或 Agent 标识；调用 Agent 时固定为 agent_as_a_tool，"
                    "调用外部插件时为 toolName"
                ),
            },
            "arguments": {
                "type": "object",
                "description": "调用参数对象",
            },
        },
        "required": ["functionName", "arguments"],
    },
)


class InvokeTool(Tool):
    """Routes invoke requests to agent or function-call backends."""

    def __init__(self, card: ToolCard | None = None) -> None:
        super().__init__(card or _INVOKE_TOOL_CARD)

    async def invoke(self, inputs: Dict[str, Any], **kwargs) -> Any:
        merged = {**inputs, **kwargs}
        session_id = resolve_session_id(kwargs)
        logger.debug(
            "[InvokeTool] inputs=%s sessionId=%s",
            merged,
            session_id or ""
        )
        try:
            func_name, params = _parse_invoke_inputs(merged)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        if not func_name:
            return {"success": False, "error": "functionName 为必填参数"}

        logger.info(
            "[InvokeTool] functionName=%s sessionId=%s sandbox=%s",
            func_name,
            session_id or "",
            sandbox_routing_enabled(),
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


def get_invoke_tool() -> InvokeTool:
    return InvokeTool()


__all__ = [
    "InvokeTool",
    "get_invoke_tool",
]
