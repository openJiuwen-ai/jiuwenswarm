# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A user package consumes and independently intercepts all seven queries."""

import logging

from jiuwenswarm.extensions_v2.event_types.session_queries import SESSION_QUERY_EVENTS
from jiuwenswarm.extensions_v2.query_dispatch import current_query_origin

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_query_audit")


async def collect_session_parameters(ops):
    """Return all seven values as arguments for a user-owned business function."""
    return {
        "cwd": await ops.session.cwd(),
        "root": await ops.session.root(),
        "model": await ops.session.model(),
        "repo": await ops.session.repo(),
        "messages": await ops.session.messages(),
        "turns": await ops.session.turns(),
        "usage": await ops.session.usage(),
    }


class SessionQueryAudit:
    """Observe call boundaries without collecting or fabricating host context."""

    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")
        for event_type in SESSION_QUERY_EVENTS:
            registrar.register_event_handler(event_type, self.on_query, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        parameters = await collect_session_parameters(ops)
        self.on_parameters(parameters)
        return await next_handler(event)

    def on_parameters(self, parameters):
        """Use the returned arguments; the example logs only a summary."""
        summary = {key: value for key, value in parameters.items() if key != "messages"}
        summary["messageCount"] = len(parameters["messages"])
        logger.info("session query summary: %s", summary)

    async def on_query(self, event, ops, next_handler):
        origin = current_query_origin()
        logger.debug("query before: event=%s caller=%s", event.event_type,
                     None if origin is None else origin.package)
        result = await next_handler(event)
        logger.debug("query after: event=%s denied=%s", event.event_type, "deny" in result)
        return result


def create_extension():
    return SessionQueryAudit()
