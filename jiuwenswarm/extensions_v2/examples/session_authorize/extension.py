# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Observe authorization availability without collecting or sending secrets."""

import logging

logger = logging.getLogger(__name__)


class AuthorizationProbe:
    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {})
        registrar.register_event_handler("session.authorize", self.on_authorize, {})

    async def on_usage(self, event, ops, next_handler):
        value = await ops.session.authorize()
        logger.info("Session authorization available=%s kind=%s", value is not None,
                    None if value is None else value["kind"])
        return await next_handler(event)

    async def on_authorize(self, event, ops, next_handler):
        return await next_handler(event)


def create_extension():
    return AuthorizationProbe()
