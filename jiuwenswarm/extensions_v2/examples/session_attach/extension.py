# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Observe committed views without starting chat or modifying the host."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_attach")


class SessionAttachExample:
    """User-owned initialization for a view entering a main Session."""

    def register(self, registrar):
        registrar.register_event_handler("session.attach", self.on_attach, {}, "user")

    async def on_attach(self, event, ops, next_handler):
        root = await ops.session.root()
        receipt = await next_handler(event)
        logger.info("session attach example: session=%s surface=%s client=%s root=%s",
                    ops.session_context.session_id, event.event_input.surface,
                    receipt.clientId, root)
        return receipt


def create_extension():
    return SessionAttachExample()
