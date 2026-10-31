# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Log safe primary-model facts when a native turn reports usage."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_model_info")


class SessionModelInfo:
    """User-owned behavior consuming a host-provided read-only Ops method."""

    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")
        registrar.register_event_handler("session.model", self.on_model_query, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        model_name = await ops.session.model()
        logger.info("session model: session=%s model_name=%s", ops.session_context.session_id, model_name)
        return await next_handler(event)

    async def on_model_query(self, event, ops, next_handler):
        result = await next_handler(event)
        logger.debug("session.model query completed")
        return result


def create_extension():
    return SessionModelInfo()
