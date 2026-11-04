# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Issue only a host-owned handle within a trusted authorization call."""

from jiuwenswarm.extensions_v2.session_authorization.dispatch import authorize_core
from openjiuwen.core.events import HandlerLevel


async def session_authorize_core(event, ops, next_handler):
    del next_handler
    return authorize_core(event, ops)


def register_authorization_lifecycle_handlers(bus, service) -> None:
    """Observe actual reload publication without trusting a returned report."""
    if service is None:
        return

    async def after_reload(event, ops, next_handler):
        try:
            return await next_handler(event)
        finally:
            # Failed reload keeps its old generation live; it is not revoked.
            service.prune()

    bus.register_event_handler("package.reload", after_reload, {}, HandlerLevel.BUILTIN)
