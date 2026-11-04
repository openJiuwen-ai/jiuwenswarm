# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Register the no-argument authorization capability during ops.build."""

from openjiuwen.core.events import EventHandlerOps, EventHandlerOpsBuilder

from jiuwenswarm.extensions_v2.event_types.session_authorize import SessionAuthorization


def add_session_authorize_method(builder: EventHandlerOpsBuilder, service) -> None:
    """Extend the session namespace created by the existing session BUILTIN."""
    async def authorize(ops: EventHandlerOps) -> SessionAuthorization | None:
        if service is None:
            return None
        return await service.authorize(ops)

    builder.add_method("session", "authorize", authorize)
