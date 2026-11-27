# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Meta-tool: invoke user-uploaded external tools via the action executor API."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator, Dict

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenclaw.agentserver.skilldev_agent.meta_tools.action_executor import (
    call_action_executor_api,
    format_call_result_for_model,
)
from jiuwenclaw.agentserver.skilldev_agent.meta_tools.external_tool_registry import (
    ExternalToolSpec,
    load_external_tools,
    resolve_available_tools_dir,
)
from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
    get_effective_request_workspace_dir,
)
from jiuwenclaw.agentserver.skilldev_agent.meta_tools.skilldev_tool_context import (
    load_tool_output_schema,
    resolve_session_id,
)

from jiuwenclaw.agentserver.skilldev_agent.meta_tools.llm_call_client import call_llm

logger = logging.getLogger(__name__)

_DEVICE_PLUGIN_UNSUPPORTED_MSG = (
    "当前不支持pluginType为Device的端插件调用，请到真机进行测试"
)


def _resolve_workspace_dir() -> str | None:
    return get_effective_request_workspace_dir()


def _coerce_arguments(raw: Any) -> dict[str, Any]:
    if raw is None:
        raise ValueError("arguments 为必填参数，请传入对象（无参时传 {}）")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("arguments 必须是 JSON 对象")
        return parsed
    raise ValueError("arguments 必须是对象或 JSON 对象字符串")


def _validate_required_arguments(spec: ExternalToolSpec, arguments: dict[str, Any]) -> str | None:
    required = spec.parameters.get("required")
    if not isinstance(required, list):
        return None
    missing = [str(k) for k in required if str(k) not in arguments]
    if missing:
        return f"缺少必填参数: {', '.join(missing)}"
    return None


def _parse_tool_call_inputs(inputs: dict[str, Any]) -> tuple[str, str, Any]:
    """从工具入参 dict 解析 pluginId / toolName / arguments（兼容 snake_case）。"""
    plugin_id = str(inputs.get("pluginId") or inputs.get("plugin_id") or "").strip()
    tool_name = str(inputs.get("toolName") or inputs.get("tool_name") or "").strip()
    if "arguments" not in inputs:
        raise ValueError("arguments 为必填参数，请传入对象（无参时传 {}）")
    return plugin_id, tool_name, inputs["arguments"]


async def _function_call_tool_impl(**inputs: Any) -> dict[str, Any]:
    """Invoke an uploaded external tool identified by pluginId + toolName + arguments."""
    try:
        plugin_id, tool_name, raw_arguments = _parse_tool_call_inputs(inputs)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    if not plugin_id:
        return {"success": False, "error": "pluginId 为必填参数"}
    if not tool_name:
        return {"success": False, "error": "toolName 为必填参数"}

    workspace = _resolve_workspace_dir()
    if not workspace:
        return {
            "success": False,
            "error": "无法解析当前任务工作区，请确保在 SkillDev 会话中调用本工具",
        }

    tools_dir = resolve_available_tools_dir(workspace)
    registry = load_external_tools(workspace)

    spec = registry.get((plugin_id, tool_name))
    if spec is None:
        available = [f"{pid}/{name}" for pid, name in sorted(registry.keys())]
        return {
            "success": False,
            "error": (
                f"未找到外部工具 pluginId={plugin_id!r} toolName={tool_name!r}。"
                f"可用工具: {', '.join(available) or '（无）'}"
            ),
            "tools_dir": str(tools_dir),
        }

    if spec.plugin_type == "Device":
        return {
            "success": False,
            "error": _DEVICE_PLUGIN_UNSUPPORTED_MSG,
            "summary": _DEVICE_PLUGIN_UNSUPPORTED_MSG,
            "pluginId": plugin_id,
            "toolName": tool_name,
            "pluginType": spec.plugin_type,
        }

    try:
        call_args = _coerce_arguments(raw_arguments)
    except Exception as exc:
        return {"success": False, "error": f"arguments 解析失败: {exc}"}

    arg_err = _validate_required_arguments(spec, call_args)
    if arg_err:
        return {
            "success": False,
            "error": arg_err,
            "pluginId": plugin_id,
            "toolName": tool_name,
        }

    logger.info(
        "[function_call_tool] pluginId=%s toolName=%s protocol=%s arg_keys=%s",
        plugin_id,
        tool_name,
        spec.protocol,
        list(call_args.keys()),
    )
    result = call_action_executor_api(
        spec.plugin_id,
        spec.tool_name,
        call_args,
        protocol=spec.protocol,
    )
    return {
        "pluginId": plugin_id,
        "toolName": tool_name,
        "summary": format_call_result_for_model(result),
        **result,
    }


_FUNCTION_CALL_TOOL_CARD = ToolCard(
    id="jiuwenclaw_function_call_tool",
    name="function_call_tool",
    description=(
        "调用 Skill 所依赖的外部插件工具。"
        "每个工具由 pluginId 与 toolName 唯一确定；"
        "定义见 skill/references/tools/ 下的 "
        "<pluginId>__<toolName>.json（与 SKILL.md metadata.tools 条目一一对应）。"
        "调用时 pluginId、toolName、arguments 均为必填；无业务参数时 arguments 传 {}。"
        "SkillDev 开发试调阶段，若尚未复制到 skill/references/tools/，"
        "可先查阅 resources/available-tools/tool_usage.json 与同目录下的工具 JSON。"
    ),
    input_params={
        "type": "object",
        "properties": {
            "pluginId": {
                "type": "string",
                "description": "外部工具插件 ID（与上传定义一致，必填）",
            },
            "toolName": {
                "type": "string",
                "description": "外部工具名称（与上传定义一致，必填）",
            },
            "arguments": {
                "type": "object",
                "description": "调用参数键值对（必填；无参时传空对象 {}）",
            },
        },
        "required": ["pluginId", "toolName", "arguments"],
    },
)


class FunctionCallTool(Tool):
    """Harness Tool wrapper for external function calls."""

    def __init__(self, card: ToolCard | None = None) -> None:
        super().__init__(card or _FUNCTION_CALL_TOOL_CARD)

    async def invoke(self, inputs: Dict[str, Any], **kwargs) -> Any:
        logger.info("Function call tool invoke inputs: %s", inputs)
        merged = {**inputs, **kwargs}
        if os.getenv("SANDBOX_ENABLE", "").strip().lower() == "true":
            session_id = resolve_session_id(kwargs)
            output_schema: Any = None
            if session_id:
                plugin_id = str(
                    merged.get("pluginId") or merged.get("plugin_id") or ""
                ).strip()
                tool_name = str(
                    merged.get("toolName") or merged.get("tool_name") or ""
                ).strip()
                if plugin_id and tool_name:
                    output_schema = load_tool_output_schema(
                        session_id,
                        plugin_id,
                        tool_name,
                        log_prefix="FunctionCallTool",
                    )
            inputs["query"] = _coerce_arguments(merged.get("arguments"))
            inputs["outputSchema"] = output_schema
            result = await call_llm(inputs)
            return {
                "result": result.content,
            }
        return await _function_call_tool_impl(**merged)

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> AsyncIterator[Any]:
        yield "Stream is not supported for this tool."
        raise build_error(StatusCode.TOOL_STREAM_NOT_SUPPORTED, card=self._card)


def get_function_call_tool() -> Tool:
    return LocalFunction(card=_FUNCTION_CALL_TOOL_CARD, func=_function_call_tool_impl)
