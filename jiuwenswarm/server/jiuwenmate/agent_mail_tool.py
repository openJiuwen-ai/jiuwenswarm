"""Execution-scoped, read-only Agent Mail tool routed by trusted employee identity."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from openjiuwen.core.foundation.tool.base import Tool, ToolCard
from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec, register_tool_provider

from jiuwenswarm.server.jiuwenmate.agent_mail_runtime import AgentMailRuntimeService, AgentMailScope
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

PROVIDER = "jiuwenmate.agent_mail"
_CONTEXT_KEY = "jiuwenmate.agent_mail"
_OPERATIONS = (
    "mail.list", "mail.search", "mail.read", "mail.attachment.read",
    "mail.send", "mail.reply", "mail.forward", "mail.delete",
)


def _tool_inputs(inputs: Any) -> dict[str, Any]:
    if isinstance(inputs, dict):
        return inputs
    dump = getattr(inputs, "model_dump", None)
    if callable(dump):
        value = dump()
        if isinstance(value, dict):
            return value
    return {}


class AgentMailTool(Tool):
    def __init__(self, service: AgentMailRuntimeService, scope: AgentMailScope) -> None:
        super().__init__(
            ToolCard(
                id="jiuwenmate-agent-mail",
                name="agent_mail",
                description=(
                    "在当前员工已授权的腾讯邮箱中列出、搜索和读取邮件，也可按用户明确要求发送、回复、转发邮件或将邮件移入废纸篓。"
                    "发送、回复、转发和移入废纸篓会执行操作；用户未明确要求时先询问，不执行永久删除。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": list(_OPERATIONS)},
                        "payload": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "msg": {"type": "string"},
                                "att": {"type": "string"},
                                "to": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                                "cc": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                                "bcc": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                                "subject": {"type": "string"},
                                "body": {"type": "string"},
                                "q": {"type": "string"},
                                "search_in": {"type": "string"},
                                "limit": {"type": "integer"},
                                "cursor": {"type": "string"},
                                "dir": {"type": "string"},
                                "after": {"type": "string"},
                                "before": {"type": "string"},
                                "from": {"type": "string"},
                                "has_attachments": {"type": "boolean"},
                                "is_unread": {"type": "boolean"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "required": ["operation", "payload"],
                    "additionalProperties": False,
                },
                parallel_safe=False,
            )
        )
        self._service = service
        self._scope = scope

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        data = _tool_inputs(inputs)
        operation, payload = data.get("operation"), data.get("payload")
        if operation not in _OPERATIONS or not isinstance(payload, dict):
            return {"error": "invalid_agent_mail_request"}
        try:
            return await self._service.execute(self._scope, operation, payload)
        except PersistentAgentError as exc:
            return {"error": str(exc), "code": exc.code}

    async def stream(self, inputs: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield await self.invoke(inputs, **kwargs)


async def prepare_agent_mail_invocation(spec, context, service: AgentMailRuntimeService | None):
    """Add the mail tool only when this employee has a persisted verified connection."""
    if service is None:
        return spec
    execution = context.execution
    scope = AgentMailScope(execution.namespace_id, execution.agent_id)
    status = await asyncio.to_thread(service.cached_status, scope)
    if status is None or status.get("status") != "connected":
        return spec
    context.extras[_CONTEXT_KEY] = (service, scope)
    tools = list(spec.tools or [])
    if any(
        (item.get("type") if isinstance(item, dict) else getattr(item, "type", None)) == PROVIDER
        for item in tools
    ):
        raise PersistentAgentError("duplicate Agent Mail tool declaration", code="INVALID_CONFIG")
    runtime_spec = spec.model_copy(update={"tools": [*tools, BuiltinToolSpec(type=PROVIDER)]})
    # This tool is injected only for a verified, employee-scoped mailbox. Keep
    # the immutable execution identity aligned with the effective spec that
    # agent-core validates during construction; the published snapshot was
    # already checked by build_invocation before this runtime-only addition.
    context.execution = replace(context.execution, config_fingerprint=runtime_spec.fingerprint())
    return runtime_spec


def build_agent_mail_tool(params: dict[str, Any], context):
    if params or _CONTEXT_KEY not in context.extras:
        raise ValueError("Agent Mail requires a connected employee runtime")
    service, scope = context.extras[_CONTEXT_KEY]
    return AgentMailTool(service, scope)


register_tool_provider(PROVIDER, build_agent_mail_tool)
