# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Execute the host-bound slash command at most once."""

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.event_types.server_run_command import (
    ServerCommandError, ServerRunCommandResult, valid_command_input,
)
from jiuwenswarm.server.runtime.commands.run_command_events import current_command


async def server_run_command_core(
    event: Event, ops: EventHandlerOps | None, next_handler: EventNext,
) -> ServerRunCommandResult:
    del next_handler
    frame = current_command()
    if frame is None or ops is None or ops.session_context != frame.context or not valid_command_input(event.event_input):
        raise ServerCommandError("COMMAND_UNBOUND_INVOCATION")
    return await frame.execute_once(event.event_input.args)
