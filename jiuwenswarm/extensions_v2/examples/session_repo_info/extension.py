# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Log repository identity when a native turn reports usage."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_repo_info")


class SessionRepoInfo:
    """User-owned behavior consuming the host's read-only Git capability."""

    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")
        registrar.register_event_handler("session.repo", self.on_repo_query, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        info = await ops.session.repo()
        logger.info("session repo: session=%s info=%s", ops.session_context.session_id, info)
        return await next_handler(event)

    async def on_repo_query(self, event, ops, next_handler):
        result = await next_handler(event)
        logger.debug("session.repo query completed")
        return result


def create_extension():
    return SessionRepoInfo()
