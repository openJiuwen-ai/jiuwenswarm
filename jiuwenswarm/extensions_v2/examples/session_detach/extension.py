# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Own resources by association identity, allowing independent view cleanup."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_detach")


class SessionDetachExample:
    """Keep only this package's per-association presentation resources."""

    def __init__(self):
        self._resources = {}

    def register(self, registrar):
        registrar.register_event_handler("session.attach", self.on_attach, {}, "user")
        registrar.register_event_handler("session.detach", self.on_detach, {}, "user")

    async def on_attach(self, event, ops, next_handler):
        receipt = await next_handler(event)
        self._resources[receipt.attachment_id] = {"session_id": ops.session_context.session_id}
        return receipt

    async def on_detach(self, event, ops, next_handler):
        value = event.event_input
        self._resources.pop(value.attachment_id, None)
        receipt = await next_handler(event)
        logger.info("session detach example: session=%s surface=%s attachment=%s reason=%s accepted=%s",
                    ops.session_context.session_id, value.surface_id, value.attachment_id, value.reason, receipt.accepted)
        return receipt


def create_extension():
    return SessionDetachExample()
