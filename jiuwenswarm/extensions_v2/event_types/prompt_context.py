# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Public ``prompt.context`` contract and its identity CORE handler."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from typing import Any, Literal, NoReturn

from openjiuwen.core.events import Event, EventHandlerOps, EventNext, ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset

PROMPT_CONTEXT_EVENT = "prompt.context"
INSTRUCTION_FILE_KINDS = {"managed", "user", "project", "local", "memory"}
InstructionFileKind = Literal["managed", "user", "project", "local", "memory"]


@dataclass(frozen=True, slots=True)
class PromptContextBlock:
    """One ordered, named text block in the current user-message context."""

    name: str
    text: str


@dataclass(frozen=True, slots=True)
class InstructionFile:
    """An instruction file associated with the prompt context."""

    path: str
    kind: InstructionFileKind
    content: str
    parent: str | None = None
    isUnverified: bool = False


@dataclass(frozen=True, slots=True)
class PromptContextInput:
    """The complete context collection prepared before a user message render."""

    blocks: list[PromptContextBlock]
    instructionFiles: list[InstructionFile] | None = None

    @property
    def instruction_files(self) -> list[InstructionFile] | None:
        """Expose the Python spelling without changing the event contract."""
        return self.instructionFiles


@dataclass(frozen=True, slots=True)
class PromptContextResult:
    """The complete context collection returned by the event chain."""

    blocks: list[PromptContextBlock]
    instructionFiles: list[InstructionFile] | None = None

    @property
    def instruction_files(self) -> list[InstructionFile] | None:
        """Expose the Python spelling without changing the event contract."""
        return self.instructionFiles

    def to_dict(self) -> dict:
        """Return an independent JSON-compatible representation."""
        return copy.deepcopy(asdict(self))


class _ReadOnlyList(list[Any]):
    """List-compatible container that prevents in-place event input edits."""

    @staticmethod
    def _reject_mutation(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise TypeError("prompt.context input collections are read-only")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    __iadd__ = _reject_mutation
    __imul__ = _reject_mutation
    append = _reject_mutation
    clear = _reject_mutation
    extend = _reject_mutation
    insert = _reject_mutation
    pop = _reject_mutation
    remove = _reject_mutation
    reverse = _reject_mutation
    sort = _reject_mutation


def freeze_prompt_context_input(value: PromptContextInput) -> PromptContextInput:
    """Return an input snapshot whose collections cannot be changed in place."""
    return PromptContextInput(
        blocks=_ReadOnlyList(value.blocks),
        instructionFiles=(
            None
            if value.instructionFiles is None
            else _ReadOnlyList(value.instructionFiles)
        ),
    )


async def prompt_context_core(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> PromptContextResult:
    """Return the input unchanged; context policy belongs to extensions."""
    del ops, next_handler
    context = event.event_input
    if not isinstance(context, PromptContextInput):
        raise TypeError(f"{PROMPT_CONTEXT_EVENT} requires PromptContextInput")
    return PromptContextResult(
        blocks=list(context.blocks),
        instructionFiles=(
            None if context.instructionFiles is None else list(context.instructionFiles)
        ),
    )


def valid_prompt_context_input(value: object) -> bool:
    """Validate the complete host-provided context collection."""
    return isinstance(value, PromptContextInput) and _valid_context_fields(
        value.blocks, value.instructionFiles
    )


def valid_prompt_context_output(value: object) -> bool:
    """Validate the complete result returned by a context handler."""
    return isinstance(value, PromptContextResult) and _valid_context_fields(
        value.blocks, value.instructionFiles
    )


def _valid_context_fields(
    blocks: object,
    instruction_files: object,
) -> bool:
    if not isinstance(blocks, list):
        return False
    names: set[str] = set()
    for block in blocks:
        if type(block) is not PromptContextBlock:
            return False
        if not isinstance(block.name, str) or not block.name.strip():
            return False
        if not isinstance(block.text, str) or block.name in names:
            return False
        names.add(block.name)

    if instruction_files is None:
        return True
    if not isinstance(instruction_files, list):
        return False
    return all(_valid_instruction_file(item) for item in instruction_files)


def _valid_instruction_file(value: object) -> bool:
    return (
        type(value) is InstructionFile
        and isinstance(value.path, str)
        and bool(value.path.strip())
        and isinstance(value.kind, str)
        and value.kind in INSTRUCTION_FILE_KINDS
        and isinstance(value.content, str)
        and (value.parent is None or isinstance(value.parent, str))
        and type(value.isUnverified) is bool
    )


def register_prompt_context_events(bus: ExtensionEventBus) -> None:
    """Register the host event type and its identity CORE handler."""
    bus.register_event_type(
        PROMPT_CONTEXT_EVENT,
        match_pattern_subset,
        prompt_context_core,
        input_validator=valid_prompt_context_input,
        output_validator=valid_prompt_context_output,
    )
