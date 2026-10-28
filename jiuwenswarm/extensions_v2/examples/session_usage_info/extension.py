# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read platform facts from a user-owned usage-event Handler."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_usage_info")


class SessionUsageInfo:
    """Consume read-only Ops without changing the platform Adapter."""

    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        turns = await ops.session.turns()
        usage = await ops.session.usage(breakdown=True)
        logger.info(
            "session usage: session=%s turns=%s usage=%s",
            ops.session_context.session_id, turns, usage,
        )
        return await next_handler(event)


def create_extension():
    return SessionUsageInfo()
