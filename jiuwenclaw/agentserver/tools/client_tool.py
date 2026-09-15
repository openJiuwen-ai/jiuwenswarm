# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Generic browser-executed Custom Tool runtime."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from jiuwenclaw.e2a.constants import (
    E2A_RESPONSE_KIND_CLIENT_TOOL_REQUEST,
    E2A_RESPONSE_STATUS_IN_PROGRESS,
    E2A_SOURCE_PROTOCOL_E2A,
    E2A_WIRE_SERVER_PUSH_KEY,
)
from jiuwenclaw.e2a.models import (
    E2A_PROTOCOL_VERSION,
    E2AProvenance,
    E2AResponse,
    IdentityOrigin,
    utc_now_iso,
)

if TYPE_CHECKING:
    from openjiuwen.core.foundation.tool import Tool

logger = logging.getLogger(__name__)
CLIENT_TOOL_TIMEOUT_SECONDS = 60.0
MAX_CUSTOM_TOOLS = 32
MAX_TOOL_SCHEMA_BYTES = 16_384
MAX_TOOL_MANIFEST_BYTES = 65_536
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def normalize_tool_definitions(value: Any) -> list[dict[str, Any]]:
    """Validate untrusted Host tool manifests and return normalized definitions."""
    if not isinstance(value, list) or not value or len(value) > MAX_CUSTOM_TOOLS:
        return []
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    manifest_bytes = 0
    for item in value:
        if not isinstance(item, dict):
            return []
        name = str(item.get("name") or "").strip()
        description = str(item.get("description") or "").strip()
        input_schema = item.get("inputSchema")
        if not TOOL_NAME_PATTERN.fullmatch(name) or name in names:
            return []
        if not description or not isinstance(input_schema, dict):
            return []
        if input_schema.get("type") != "object":
            return []
        try:
            schema_bytes = len(
                json.dumps(
                    input_schema,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError, RecursionError):
            return []
        manifest_bytes += (
            schema_bytes
            + len(name.encode("utf-8"))
            + len(description.encode("utf-8"))
        )
        if schema_bytes > MAX_TOOL_SCHEMA_BYTES or manifest_bytes > MAX_TOOL_MANIFEST_BYTES:
            return []
        names.add(name)
        normalized.append(
            {
                "name": name,
                "description": description[:2000],
                "inputSchema": input_schema,
                "readOnly": item.get("readOnly") is True,
            }
        )
    return normalized


@dataclass(slots=True)
class PendingClientToolCall:
    tool_call_id: str
    invocation_id: str
    session_id: str
    provider_id: str
    resource_id: str
    client_session_id: str
    tool_name: str
    future: asyncio.Future[dict[str, Any]]


class ClientToolManager:
    _instance: ClientToolManager | None = None

    def __new__(cls) -> ClientToolManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._pending = {}
            cls._instance._send_push_callback = None
        return cls._instance

    def __init__(self) -> None:
        self._send_push_callback: Any

    def set_send_push_callback(self, callback: Any) -> None:
        self._send_push_callback = callback

    def reset_state(self) -> None:
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        invocation_id: str,
        session_id: str,
        provider_id: str,
        resource_id: str,
        client_session_id: str,
        expected_resource_version: str | int | None,
        available_tools: set[str],
        channel_id: str,
        timeout: float = CLIENT_TOOL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        if tool_name not in available_tools:
            return {
                "success": False,
                "error": {
                    "code": "TOOL_NOT_FOUND",
                    "message": f"Host does not provide tool: {tool_name}",
                },
            }
        if not isinstance(arguments, dict):
            return {
                "success": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "arguments must be an object",
                },
            }
        if self._send_push_callback is None:
            raise RuntimeError("Client Tool send_push callback not set")

        tool_call_id = f"client_tool_{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self._pending[tool_call_id] = PendingClientToolCall(
            tool_call_id,
            invocation_id,
            session_id,
            provider_id,
            resource_id,
            client_session_id,
            tool_name,
            future,
        )
        ts = utc_now_iso()
        event = {
            "type": "agent.custom_tool_call",
            "tool_call_id": tool_call_id,
            "invocation_id": invocation_id,
            "client_session_id": client_session_id,
            "provider_id": provider_id,
            "resource_id": resource_id,
            "expected_resource_version": expected_resource_version,
            "tool_name": tool_name,
            "arguments": dict(arguments),
        }
        response = E2AResponse(
            protocol_version=E2A_PROTOCOL_VERSION,
            response_id=f"client_tool_resp_{uuid.uuid4().hex[:12]}",
            request_id=invocation_id,
            correlation_id=tool_call_id,
            sequence=0,
            is_final=False,
            status=E2A_RESPONSE_STATUS_IN_PROGRESS,
            response_kind=E2A_RESPONSE_KIND_CLIENT_TOOL_REQUEST,
            timestamp=ts,
            provenance=E2AProvenance(
                source_protocol=E2A_SOURCE_PROTOCOL_E2A,
                converter="jiuwenclaw.agentserver.tools.client_tool:invoke",
                converted_at=ts,
                details={
                    "kind": "client_tool_request",
                    "provider_id": provider_id,
                    "tool_name": tool_name,
                },
            ),
            body=event,
            session_id=session_id,
            channel=channel_id,
            identity_origin=IdentityOrigin.AGENT,
            metadata={E2A_WIRE_SERVER_PUSH_KEY: True, "client_session_id": client_session_id},
        )
        try:
            pushed = self._send_push_callback(response.to_dict())
            if inspect.isawaitable(pushed):
                await pushed
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[ClientTool] timeout tool_call_id=%s tool_name=%s", tool_call_id, tool_name)
            return {
                "success": False,
                "error": {
                    "code": "TIMEOUT",
                    "message": "Custom Tool execution timed out",
                },
            }
        finally:
            self._pending.pop(tool_call_id, None)

    def complete(self, result: dict[str, Any], *, session_id: str | None = None) -> tuple[bool, str]:
        tool_call_id = str(result.get("tool_call_id") or "").strip()
        pending = self._pending.get(tool_call_id)
        if pending is None:
            return False, "unknown_or_late_response"
        if str(result.get("invocation_id") or "") != pending.invocation_id:
            return False, "invocation_mismatch"
        if session_id is not None and str(session_id) != pending.session_id:
            return False, "session_mismatch"
        if str(result.get("client_session_id") or "") != pending.client_session_id:
            return False, "client_session_mismatch"
        if str(result.get("provider_id") or "") != pending.provider_id:
            return False, "provider_mismatch"
        if str(result.get("resource_id") or "") != pending.resource_id:
            return False, "resource_mismatch"
        if not isinstance(result.get("success"), bool):
            return False, "invalid_result"
        if result.get("data") is not None and not isinstance(result.get("data"), dict):
            return False, "invalid_result"
        if result.get("error") is not None and not isinstance(result.get("error"), dict):
            return False, "invalid_result"
        if result["success"] is False and not isinstance(result.get("error"), dict):
            return False, "invalid_result"
        version = result.get("resource_version")
        if version is not None and (
            isinstance(version, bool) or not isinstance(version, (str, int))
        ):
            return False, "invalid_result"
        if pending.future.done():
            return False, "duplicate_response"
        payload = {
            "tool_call_id": tool_call_id,
            "tool_name": pending.tool_name,
            "success": result["success"],
            "data": result.get("data"),
            "error": result.get("error"),
            "resource_version": result.get("resource_version"),
        }
        pending.future.set_result(payload)
        return True, "accepted"


def get_client_tool_manager() -> ClientToolManager:
    return ClientToolManager()


def get_client_tool(
    *,
    session_id: str,
    request_id: str,
    channel_id: str,
    context: dict[str, Any],
) -> "Tool":
    from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

    provider_id = str(context["provider_id"])
    resource = context["resource"]
    resource_id = str(resource["id"])
    client_session_id = str(context["client_session_id"])
    definitions = normalize_tool_definitions(context.get("tools"))
    definitions_by_name = {item["name"]: item for item in definitions}
    available_tools = set(definitions_by_name)
    runtime_version = [resource.get("version")]

    async def custom_tool(
        tool_name: str,
        arguments: dict[str, Any],
        expected_resource_version: str | int | None = None,
    ) -> dict[str, Any]:
        if tool_name not in available_tools:
            return {
                "success": False,
                "error": {
                    "code": "TOOL_NOT_FOUND",
                    "message": f"Host does not provide tool: {tool_name}",
                },
            }
        version = (
            runtime_version[0]
            if expected_resource_version is None
            else expected_resource_version
        )
        result = await get_client_tool_manager().invoke(
            tool_name=tool_name,
            arguments=arguments,
            invocation_id=request_id,
            session_id=session_id,
            provider_id=provider_id,
            resource_id=resource_id,
            client_session_id=client_session_id,
            expected_resource_version=version,
            available_tools=available_tools,
            channel_id=channel_id,
        )
        if result.get("resource_version") is not None:
            runtime_version[0] = result["resource_version"]
        return result

    tool_summary = "; ".join(f"{item['name']}: {item['description']}" for item in definitions)
    card = ToolCard(
        name="custom_tool",
        description=f"调用当前客户端 Host Adapter 注册的工具。可用工具：{tool_summary}",
        input_params={
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "enum": sorted(available_tools)},
                "arguments": {"type": "object", "description": "参数必须符合所选工具的 inputSchema"},
                "expected_resource_version": {
                    "description": "写操作基于的资源版本",
                    "oneOf": [{"type": "string"}, {"type": "integer"}],
                },
            },
            "required": ["tool_name", "arguments"],
            "allOf": [
                {
                    "if": {
                        "properties": {"tool_name": {"const": item["name"]}}
                    },
                    "then": {
                        "properties": {"arguments": item["inputSchema"]}
                    },
                }
                for item in definitions
            ],
        },
    )
    return LocalFunction(card=card, func=custom_tool)
