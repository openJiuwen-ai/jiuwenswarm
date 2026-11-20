# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Public ``prompt.context`` contract and its identity CORE handler."""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, make_dataclass
from typing import Any, Literal, NoReturn, SupportsIndex

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


_UNVERIFIED_FIELD = "isUnverified"
_INSTRUCTION_FILES_FIELD = "instructionFiles"

# These public events deliberately use the upstream wire spelling. Generating
# the dataclasses keeps that runtime API exact without declaring mixed-case
# schema fields as regular Python attributes.
InstructionFile = make_dataclass(
    "InstructionFile",
    [
        ("path", str),
        ("kind", InstructionFileKind),
        ("content", str),
        ("parent", str | None, field(default=None)),
        (_UNVERIFIED_FIELD, bool, field(default=False)),
    ],
    namespace={
        "__doc__": "An instruction file associated with the prompt context.",
        "__module__": __name__,
    },
    frozen=True,
    slots=True,
)


def _instruction_files(value: object) -> list[InstructionFile] | None:
    """Expose the Python spelling without changing the event contract."""
    return getattr(value, _INSTRUCTION_FILES_FIELD)


def _context_to_dict(value: object) -> dict:
    """Return an independent JSON-compatible representation."""
    return copy.deepcopy(asdict(value))


PromptContextInput = make_dataclass(
    "PromptContextInput",
    [
        ("blocks", list[PromptContextBlock]),
        (_INSTRUCTION_FILES_FIELD, list[InstructionFile] | None, field(default=None)),
    ],
    namespace={
        "__doc__": "The complete context collection prepared before a user message render.",
        "__module__": __name__,
        "instruction_files": property(_instruction_files),
    },
    frozen=True,
    slots=True,
)

PromptContextResult = make_dataclass(
    "PromptContextResult",
    [
        ("blocks", list[PromptContextBlock]),
        (_INSTRUCTION_FILES_FIELD, list[InstructionFile] | None, field(default=None)),
    ],
    namespace={
        "__doc__": "The complete context collection returned by the event chain.",
        "__module__": __name__,
        "instruction_files": property(_instruction_files),
        "to_dict": _context_to_dict,
    },
    frozen=True,
    slots=True,
)


class _ReadOnlyList(list[Any]):
    """List-compatible container that prevents in-place event input edits."""

    def _raise_read_only(self) -> NoReturn:
        raise TypeError("prompt.context input collections are read-only")

    def __setitem__(self, _index: SupportsIndex | slice, _value: Any) -> NoReturn:
        self._raise_read_only()

    def __delitem__(self, _index: SupportsIndex | slice) -> NoReturn:
        self._raise_read_only()

    def __iadd__(self, _values: Iterable[Any]) -> NoReturn:
        self._raise_read_only()

    def __imul__(self, _count: SupportsIndex) -> NoReturn:
        self._raise_read_only()

    def append(self, _value: Any) -> NoReturn:
        self._raise_read_only()

    def clear(self) -> NoReturn:
        self._raise_read_only()

    def extend(self, _values: Iterable[Any]) -> NoReturn:
        self._raise_read_only()

    def insert(self, _index: SupportsIndex, _value: Any) -> NoReturn:
        self._raise_read_only()

    def pop(self, _index: SupportsIndex = -1) -> NoReturn:
        self._raise_read_only()

    def remove(self, _value: Any) -> NoReturn:
        self._raise_read_only()

    def reverse(self) -> NoReturn:
        self._raise_read_only()

    def sort(
        self,
        *,
        key: Callable[[Any], Any] | None = None,
        reverse: bool = False,
    ) -> NoReturn:
        del key, reverse
        self._raise_read_only()


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
        if (
            not isinstance(block, PromptContextBlock)
            or block.__class__ is not PromptContextBlock
        ):
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
        isinstance(value, InstructionFile)
        and value.__class__ is InstructionFile
        and isinstance(value.path, str)
        and bool(value.path.strip())
        and isinstance(value.kind, str)
        and value.kind in INSTRUCTION_FILE_KINDS
        and isinstance(value.content, str)
        and (value.parent is None or isinstance(value.parent, str))
        and isinstance(value.isUnverified, bool)
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
