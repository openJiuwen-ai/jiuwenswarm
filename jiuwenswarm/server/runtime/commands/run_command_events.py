# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trusted command continuations and private native receipts, outside EventBus."""

from __future__ import annotations

import asyncio
import copy
import itertools
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext

from jiuwenswarm.extensions_v2.event_types.server_run_command import (
    SERVER_RUN_COMMAND_EVENT, CommandOrigin, CommandPresentation, ServerCommandError,
    ServerRunCommandInput, ServerRunCommandResult, valid_origin, valid_presentation,
)

_REFERENCES = itertools.count(1)
_CURRENT: ContextVar[_CommandFrame | None] = ContextVar("server_run_command", default=None)


@dataclass(frozen=True, slots=True)
class NativeCommandResult:
    """The platform's private result and its public text/context projection."""

    value: Any
    text: str | None = None
    context: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandExecution:
    """A host-only outcome; native payloads never appear in the event schema."""

    output: ServerRunCommandResult
    native: Any = None
    use_native: bool = False


@dataclass(slots=True)
class _CommandFrame:
    context: SessionContext
    original: ServerRunCommandInput
    execute: Callable[[str], Awaitable[NativeCommandResult]]
    owner: asyncio.Task
    active: bool = True
    native: NativeCommandResult | None = None
    public: ServerRunCommandResult | None = None
    failure: BaseException | None = None
    executing: bool = False
    retained_context: tuple[str, ...] = ()
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def execute_once(self, args: str) -> ServerRunCommandResult:
        if self.executing:
            raise ServerCommandError("COMMAND_REENTRANT")
        async with self.lock:
            if current_command() is not self:
                raise ServerCommandError("COMMAND_UNBOUND_INVOCATION")
            if self.failure is not None:
                raise self.failure
            if self.public is None:
                try:
                    self.executing = True
                    native = await self.execute(args)
                    if not isinstance(native, NativeCommandResult) or not _valid_text_context(native.text, native.context):
                        raise ServerCommandError("COMMAND_INVALID_NATIVE_RESULT")
                    self.native = NativeCommandResult(copy.deepcopy(native.value), native.text, tuple(native.context))
                    self.public = ServerRunCommandResult(native.text, tuple(native.context) or None, next(_REFERENCES))
                    self.retained_context = tuple(native.context)
                except BaseException as failure:
                    self.failure = failure
                    raise
                finally:
                    self.executing = False
            return ServerRunCommandResult(self.public.text, self.retained_context or None, self.public.ref)

    def accept_result(self, value: object) -> bool:
        if not isinstance(value, ServerRunCommandResult) or not _valid_text_context(value.text, value.context):
            return False
        if value.ref is not None:
            if type(value.ref) is not int or self.public is None or value.ref != self.public.ref:
                return False
            if value.text is not None and value.text != self.public.text:
                return False
        if value.context is not None:
            notes = tuple(value.context)
            if notes[:len(self.retained_context)] != self.retained_context:
                return False
            self.retained_context = notes
        return True


def _valid_text_context(text: object, context: object) -> bool:
    return ((text is None or isinstance(text, str))
            and (context is None or isinstance(context, tuple)
                 and all(isinstance(note, str) and bool(note) for note in context)))


def current_command() -> _CommandFrame | None:
    frame = _CURRENT.get()
    return frame if frame is not None and frame.active and frame.owner is asyncio.current_task() else None


async def dispatch_server_run_command(
    bus: ExtensionEventBus, context: SessionContext, *, command: str, args: str,
    origin: CommandOrigin, execute: Callable[[str], Awaitable[NativeCommandResult]],
    record_context: Callable[[tuple[str, ...]], Awaitable[None]],
    presentation: CommandPresentation = CommandPresentation(),
) -> CommandExecution:
    """Run one host-selected command and consume the final hidden context once.

    The host resolves aliases and supplies its original implementation. USER
    handlers only see the public input/result; native payloads stay in this frame.
    """
    if not isinstance(command, str) or re.fullmatch(r"[A-Za-z0-9_-]+", command) is None:
        raise ServerCommandError("COMMAND_INVALID_NAME")
    if not isinstance(context.session_id, str) or not context.session_id.strip():
        raise ServerCommandError("COMMAND_UNBOUND_SESSION")
    if not isinstance(args, str) or not valid_origin(origin) or not valid_presentation(presentation):
        raise ServerCommandError("COMMAND_INVALID_INPUT")
    previous = current_command()
    if previous is not None and previous.context.session_id == context.session_id:
        raise ServerCommandError("COMMAND_REENTRANT")
    original = ServerRunCommandInput(command, args, origin, presentation)
    frame = _CommandFrame(context, original, execute, asyncio.current_task())
    token = _CURRENT.set(frame)
    try:
        output = await bus.trigger(Event(SERVER_RUN_COMMAND_EVENT, original), context)
        if not frame.accept_result(output):
            raise ServerCommandError("COMMAND_INVALID_RESULT")
        notes = frame.retained_context
        if notes:
            await record_context(notes)
        use_native = output.ref is not None
        text = frame.public.text if use_native else output.text
        final = ServerRunCommandResult(text=text, context=notes or None, ref=output.ref)
        return CommandExecution(final, copy.deepcopy(frame.native.value) if frame.native is not None else None, use_native)
    finally:
        frame.active = False
        _CURRENT.reset(token)
