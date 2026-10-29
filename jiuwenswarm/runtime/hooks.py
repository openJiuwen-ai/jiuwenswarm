# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request hook port and the lazy extension bridge used by composition roots."""

from __future__ import annotations

from typing import Any, Protocol


class RequestHooks(Protocol):
    async def before_chat(self, request: Any) -> None:
        """Apply host policy to a mutable request before execution admission."""


class ExtensionRequestHooks:
    async def before_chat(self, request: Any) -> None:
        from jiuwenswarm.common.hook_event import AgentServerHookEvents
        from jiuwenswarm.common.hooks_context import AgentServerChatHookContext
        from gateway.extensions.registry import ExtensionRegistry

        params = request.params if isinstance(request.params, dict) else {}
        if not isinstance(request.params, dict):
            request.params = params
        context = AgentServerChatHookContext(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            req_method=request.req_method.value if request.req_method is not None else None,
            params=params,
        )
        await ExtensionRegistry.get_instance().trigger(
            AgentServerHookEvents.BEFORE_CHAT_REQUEST, context
        )
