# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prepare activation-owned state once, then use it on normal notifications."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_start")


class SessionStartExample:
    """User state belongs to this activation, not to a global session counter."""

    def __init__(self):
        self.start_count = 0
        self.workspace = None

    def register(self, registrar):
        registrar.register_event_handler("session.start", self.on_start, {}, "user")
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")

    async def on_start(self, event, ops, next_handler):
        self.start_count += 1
        self.workspace = {
            "cwd": await ops.session.cwd(), "root": await ops.session.root(),
            "model": await ops.session.model(),
        }
        logger.info("session start example: package=%s generation=%s count=%s session=%s workspace=%s",
                    ops.package_metadata.package_id, event.event_input.generation,
                    self.start_count, ops.session_context.session_id, self.workspace)
        return await next_handler(event)

    async def on_usage(self, event, ops, next_handler):
        logger.info("session start example usage: session=%s ready=%s count=%s",
                    ops.session_context.session_id, self.workspace is not None, self.start_count)
        return await next_handler(event)


def create_extension():
    return SessionStartExample()
