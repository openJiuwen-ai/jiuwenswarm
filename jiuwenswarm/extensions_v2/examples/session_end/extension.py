# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep user-owned session state and release it when that Runtime run ends."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_end")


class SessionEndExample:
    """A user's Session resources, independent of platform history ownership."""

    def __init__(self):
        self.cache = {}

    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")
        registrar.register_event_handler("session.end", self.on_end, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        self.cache[ops.session_context.session_id] = {"project_root": await ops.session.root()}
        return await next_handler(event)

    async def on_end(self, event, ops, next_handler):
        session_id = ops.session_context.session_id
        released = self.cache.pop(session_id, None) is not None
        logger.info("session end example: session=%s reason=%s resume=%s released=%s",
                    session_id, event.event_input.reason, event.event_input.resume.id, released)
        return await next_handler(event)


def create_extension():
    return SessionEndExample()
