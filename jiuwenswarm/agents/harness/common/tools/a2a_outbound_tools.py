"""Narrow Agent tools that proxy A2A outbound work to the Gateway manager."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Protocol

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.gateway.a2a_manager.outbound import (
    A2AOutboundErrorCode,
    safe_error_summary,
)
from jiuwenswarm.gateway.a2a_manager.tool_rpc import (
    A2A_TOOL_CANCEL_CALL as _RPC_CANCEL,
    A2A_TOOL_DISPATCH_TASK as _RPC_DISPATCH,
    A2A_TOOL_FIND_AGENTS as _RPC_FIND,
    A2A_TOOL_GET_DISPATCH as _RPC_GET,
)

from .acp_output_tools import get_acp_output_manager


class A2AOutboundToolBackend(Protocol):
    @property
    def ready(self) -> bool:
        raise NotImplementedError

    async def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        session_id: str,
        channel_id: str,
    ) -> dict[str, Any]:
        raise NotImplementedError


class GatewayA2AOutboundToolBackend:
    """Use the established AgentServer-to-Gateway reverse RPC connection."""

    @property
    def ready(self) -> bool:
        from jiuwenswarm.server.transports.push_registry import get_push_registry

        return bool(
            callable(getattr(get_acp_output_manager(), "_send_push_callback", None))
            and get_push_registry().reverse_rpc_ready()
        )

    async def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        session_id: str,
        channel_id: str,
    ) -> dict[str, Any]:
        if not self.ready:
            return _error(A2AOutboundErrorCode.MANAGER_UNAVAILABLE)
        try:
            response = await get_acp_output_manager().send_jsonrpc_request(
                method,
                params,
                session_id=session_id,
                channel_id=channel_id,
                # The Gateway Manager owns operation budgets. In particular,
                # sync_wait_seconds must not be preempted by a transport timer.
                timeout=None,
                log_params=False,
                cancel_method=_RPC_CANCEL,
            )
        except (asyncio.TimeoutError, RuntimeError):
            return _error(A2AOutboundErrorCode.MANAGER_UNAVAILABLE)
        result = response.get("result")
        if isinstance(result, dict):
            return result
        error = response.get("error")
        if isinstance(error, dict):
            code = str(error.get("data", {}).get("code") or "")
            return _error(code or A2AOutboundErrorCode.MANAGER_UNAVAILABLE)
        return _error(A2AOutboundErrorCode.MANAGER_UNAVAILABLE)


def _error(code: A2AOutboundErrorCode | str) -> dict[str, Any]:
    value = code.value if isinstance(code, A2AOutboundErrorCode) else str(code)
    return {
        "ok": False,
        "error_code": value,
        "error_summary": safe_error_summary(value),
    }


def _runtime_route() -> tuple[str, str]:
    # Lazy import avoids coupling this reusable toolkit module to adapter startup.
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        get_runtime_tool_channel_id,
        get_runtime_tool_session_id,
    )

    return (
        str(get_runtime_tool_session_id() or "").strip(),
        str(get_runtime_tool_channel_id() or "default").strip() or "default",
    )


def _runtime_resource_id() -> str:
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        get_runtime_tool_resource_id,
    )

    return str(get_runtime_tool_resource_id() or "").strip()


class A2AOutboundToolkit:
    def __init__(
        self,
        backend: A2AOutboundToolBackend,
        *,
        runtime_route: Callable[[], tuple[str, str]] = _runtime_route,
        runtime_resource_id: Callable[[], str] = _runtime_resource_id,
    ) -> None:
        self._backend = backend
        self._runtime_route = runtime_route
        self._runtime_resource_id = runtime_resource_id

    async def find_agents(
        self,
        query: str = "",
        required_skills: list[str] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        return await self._call(
            _RPC_FIND,
            {"query": query, "required_skills": required_skills or [], "limit": limit},
        )

    async def dispatch_task(
        self,
        agent_id: str,
        task: str,
        mode: str = "sync",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reason = ""
        if isinstance(context, dict):
            reason = str(context.get("reason") or "")[:1000]
        return await self._call(
            _RPC_DISPATCH,
            {"agent_id": agent_id, "task": task, "mode": mode, "reason": reason},
        )

    async def get_dispatch(self, dispatch_id: str) -> dict[str, Any]:
        return await self._call(_RPC_GET, {"dispatch_id": dispatch_id})

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        session_id, channel_id = self._runtime_route()
        if not session_id:
            # No remote Agent has been contacted at this point. Report the
            # local routing failure instead of mislabeling it as a rejection.
            return _error(A2AOutboundErrorCode.MANAGER_UNAVAILABLE)
        return await self._backend.call(
            method,
            {**params, "resource_id": self._runtime_resource_id()},
            session_id=session_id,
            channel_id=channel_id,
        )

    def get_tools(self) -> list[Tool]:
        def make_tool(
            name: str, description: str, input_params: dict[str, Any], func
        ) -> Tool:
            return LocalFunction(
                card=ToolCard(
                    id=name,
                    name=name,
                    description=description,
                    input_params=input_params,
                ),
                func=func,
            )

        return [
            make_tool(
                "a2a_find_agents",
                "List registered and currently callable external A2A Agents. On the first "
                "call, omit query or pass an empty query to inspect the catalog. query only "
                "ranks candidates and never hides callable Agents; required_skills is the "
                "strict exact-match filter. Never invent an agent_id or remote URL.",
                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Optional task keywords for ranking. Use an empty "
                                "string to list the callable catalog."
                            ),
                            "default": "",
                        },
                        "required_skills": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional exact skill IDs, names, or tags; all values must match.",
                            "default": [],
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "additionalProperties": False,
                },
                self.find_agents,
            ),
            make_tool(
                "a2a_dispatch_task",
                "Dispatch a text task to a registered agent_id. sync waits for a final "
                "reply; async returns only after the remote Agent provides a queryable task ID. "
                "URLs, headers, credentials, and custom timeouts are intentionally unsupported.",
                {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "task": {"type": "string"},
                        "mode": {"type": "string", "enum": ["sync", "async"]},
                        "context": {
                            "type": "object",
                            "properties": {"reason": {"type": "string"}},
                            "additionalProperties": False,
                        },
                    },
                    "required": ["agent_id", "task", "mode"],
                    "additionalProperties": False,
                },
                self.dispatch_task,
            ),
            make_tool(
                "a2a_get_dispatch",
                "Query a prior outbound request by its local dispatch_id. Do not pass a "
                "remote task ID or endpoint.",
                {
                    "type": "object",
                    "properties": {"dispatch_id": {"type": "string"}},
                    "required": ["dispatch_id"],
                    "additionalProperties": False,
                },
                self.get_dispatch,
            ),
        ]


__all__ = [
    "A2AOutboundToolBackend",
    "A2AOutboundToolkit",
    "GatewayA2AOutboundToolBackend",
    "_RPC_DISPATCH",
    "_RPC_FIND",
    "_RPC_GET",
]
