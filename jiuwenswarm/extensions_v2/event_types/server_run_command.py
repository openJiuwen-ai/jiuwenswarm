# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Claude command.run contracts under the Server event name."""

from __future__ import annotations

from dataclasses import dataclass

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset

SERVER_RUN_COMMAND_EVENT = "server.run_command"
ORIGIN_KINDS = frozenset({
    "composer", "bridge", "sdk", "task-notification", "scheduled-trigger",
    "peer", "peer-send-message", "projects-relay", "channel", "coordinator",
    "observer", "observer-activity", "auto-continuation", "unclassified",
    "slack-ping", "plugin",
})


@dataclass(frozen=True, slots=True)
class CommandOrigin:
    """Provenance supplied by the host; name/server belong to their variants."""

    kind: str
    name: str | None = None
    server: str | None = None


@dataclass(frozen=True, slots=True)
class CommandPresentation:
    """Host-measured presentation; an unmeasured headless run uses false/80."""

    isFullscreen: bool = False
    columns: int = 80


@dataclass(frozen=True, slots=True)
class ServerRunCommandInput:
    """Only args may be rewritten; omitted presentation inherits the host fact."""

    command: str
    args: str
    origin: CommandOrigin
    presentation: CommandPresentation | None = None

    def __post_init__(self) -> None:
        if self.presentation is None:
            from jiuwenswarm.server.runtime.commands.run_command_events import current_command

            frame = current_command()
            if frame is not None:
                object.__setattr__(self, "presentation", frame.original.presentation)


@dataclass(frozen=True, slots=True)
class ServerRunCommandResult:
    """Printed text, hidden user messages, and a host-local execution reference."""

    text: str | None = None
    context: tuple[str, ...] | None = None
    ref: int | None = None

    def __post_init__(self) -> None:
        if self.context is None:
            from jiuwenswarm.server.runtime.commands.run_command_events import current_command

            frame = current_command()
            if frame is not None and frame.retained_context:
                object.__setattr__(self, "context", frame.retained_context)


class ServerCommandError(RuntimeError):
    """Report an invalid host invocation or final command result."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def valid_origin(value: object) -> bool:
    if not isinstance(value, CommandOrigin) or not isinstance(value.kind, str) or value.kind not in ORIGIN_KINDS:
        return False
    if value.kind == "plugin":
        return isinstance(value.name, str) and bool(value.name.strip()) and value.server is None
    if value.kind == "channel":
        return isinstance(value.server, str) and bool(value.server.strip()) and value.name is None
    return value.name is None and value.server is None


def valid_presentation(value: object) -> bool:
    return (isinstance(value, CommandPresentation) and type(value.isFullscreen) is bool
            and type(value.columns) is int and value.columns > 0)


def valid_command_input(value: object) -> bool:
    from jiuwenswarm.server.runtime.commands.run_command_events import current_command

    frame = current_command()
    return bool(
        frame is not None and isinstance(value, ServerRunCommandInput)
        and value.command == frame.original.command and value.origin == frame.original.origin
        and value.presentation == frame.original.presentation
        and isinstance(value.args, str)
    )


def valid_command_output(value: object) -> bool:
    from jiuwenswarm.server.runtime.commands.run_command_events import current_command

    frame = current_command()
    return frame is not None and frame.accept_result(value)


def register_server_run_command_event(bus: ExtensionEventBus) -> None:
    from jiuwenswarm.extensions_v2.event_handlers.server_run_command import server_run_command_core

    bus.register_event_type(
        SERVER_RUN_COMMAND_EVENT, match_pattern_subset, server_run_command_core,
        input_validator=valid_command_input, output_validator=valid_command_output,
    )
