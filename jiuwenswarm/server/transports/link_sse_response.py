# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Close an interrupted SSE producer deterministically in enforced link mode."""

import logging

import anyio
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger(__name__)


class LinkEventSourceResponse(EventSourceResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Cancellation may occur in send(), outside the producer's next().
            # Cancelling the task alone then leaves its generator suspended at
            # yield. Do not depend on Python GC to run subscription cleanup.
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                with anyio.move_on_after(2, shield=True) as cleanup:
                    await close()
                if cleanup.cancel_called:
                    logger.warning("link SSE producer cleanup exceeded deadline")
