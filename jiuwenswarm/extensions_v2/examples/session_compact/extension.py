# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Keep project constraints in summaries; optionally compact on an idle attach."""

from dataclasses import replace
import logging

from openjiuwen.core.context_engine import CompactionError
from openjiuwen.core.events import Event

from jiuwenswarm.extensions_v2.event_types.session_compact import SessionCompactSkipped

logger = logging.getLogger(__name__)


class ProjectCompaction:
    """A user-owned policy, without model collection, platform edits or UI calls."""

    def __init__(self, *, compact_on_attach: bool = False) -> None:
        self.compact_on_attach = compact_on_attach
        self._requested = set()

    def register(self, registrar) -> None:
        registrar.register_event_handler("session.compact", self.on_compact, {}, "user")
        if self.compact_on_attach:
            registrar.register_event_handler("session.attach", self.on_attach, {}, "user")

    async def on_compact(self, event, ops, next_handler):
        value = event.event_input
        instructions = "\n".join(filter(None, [
            value.instructions,
            "Preserve user constraints, changed files, verification results and pending work.",
        ]))
        return await next_handler(Event(event.event_type, replace(value, instructions=instructions)))

    async def on_attach(self, event, ops, next_handler):
        sid = ops.session_context.session_id
        if sid not in self._requested:
            self._requested.add(sid)
            try:
                result = await ops.session.compact(instructions="Keep the project's pending work.")
            except CompactionError as error:
                self._requested.discard(sid)
                if error.code not in ("COMPACT_BUSY", "COMPACT_SESSION_UNAVAILABLE", "COMPACT_NOT_INTEGRATED"):
                    raise
                logger.info("Compaction not ready: session=%s code=%s", sid, error.code)
            else:
                if isinstance(result, SessionCompactSkipped):
                    logger.info("Compaction skipped: session=%s reason=%s", sid, result.skip)
                else:
                    logger.info("Compaction completed: session=%s messages=%s before=%s after=%s",
                                sid, len(result.messages), result.tokens_before, result.tokens_after)
        return await next_handler(event)


def create_extension():
    return ProjectCompaction()
