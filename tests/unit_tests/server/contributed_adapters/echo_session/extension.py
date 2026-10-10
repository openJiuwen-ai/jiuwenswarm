# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Stub package contributing gateway adapters, in place of a real package.

Shaped like what a package shipping an adapter does: subclass
``GatewayAdapter``, declare the methods it answers, declare which of them it
takes from the host, then let the host mount it.

``EchoSessionAdapter`` takes ``session.list`` and nothing else, although
``SessionAdapter`` answers nine methods. ``replaces`` is the package's half.
The operator's half is ``adapters.session.list`` under
``extensions.allow_overrides``. Without it the registration is refused.

``EchoPingAdapter`` answers a method the product does not ship, so it takes
nothing from the host and needs no permission. Nothing dispatches it either,
because dispatch reads a ``ReqMethod`` member. See the ``contributed`` module
of ``gateway_adapter`` for that ceiling.
"""

from __future__ import annotations

from typing import Any, ClassVar

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter
from jiuwenswarm.server.runtime.gateway_adapter.contributed import (
    register_gateway_adapter,
)

SOURCE = "echo-session 0.0.1"
PING_METHOD = "echo.ping"


class _EchoAdapter(GatewayAdapter):
    """Records the requests it answered instead of doing the work."""

    source: ClassVar[str] = SOURCE

    def __init__(self) -> None:
        self.handled: list[str] = []

    async def handle(self, request: AgentRequest) -> AgentResponse:
        self.handled.append(request.request_id)
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"source": SOURCE},
        )


class EchoSessionAdapter(_EchoAdapter):
    """Takes ``session.list`` from the built-in adapter that answers it."""

    methods: ClassVar[frozenset[str]] = frozenset({ReqMethod.SESSION_LIST.value})
    replaces: ClassVar[frozenset[str]] = frozenset({ReqMethod.SESSION_LIST.value})


class EchoPingAdapter(_EchoAdapter):
    """Answers a method no built-in adapter and no legacy handler answers."""

    methods: ClassVar[frozenset[str]] = frozenset({PING_METHOD})


async def register_extensions(registry: Any) -> None:
    register_gateway_adapter(EchoPingAdapter())
    register_gateway_adapter(EchoSessionAdapter())
