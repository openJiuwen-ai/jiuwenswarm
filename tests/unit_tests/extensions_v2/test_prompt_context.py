from __future__ import annotations

import pytest
from openjiuwen.core.events import OPS_BUILD_EVENT_TYPE, Event, ExtensionEventBus, HandlerLevel, SessionContext

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.extensions_v2.event_types.prompt_context import (
    INSTRUCTION_FILE_KINDS,
    PROMPT_CONTEXT_EVENT,
    InstructionFile,
    PromptContextBlock,
    PromptContextInput,
    PromptContextResult,
    register_prompt_context_events,
    valid_prompt_context_input,
    valid_prompt_context_output,
)


@pytest.mark.asyncio
async def test_prompt_context_core_returns_an_equivalent_result() -> None:
    bus = ExtensionEventBus()
    register_prompt_context_events(bus)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))

    blocks = [PromptContextBlock(name="currentDate", text="2026-11-04 10:00:00")]
    files = [InstructionFile(path="/workspace/AGENTS.md", kind="project", content="rules")]
    value = PromptContextInput(blocks=blocks, instructionFiles=files)

    result = await bus.trigger(
        Event(PROMPT_CONTEXT_EVENT, value),
        SessionContext("session-1", cwd="/workspace", metadata={"context_count": 1}),
    )

    assert result == PromptContextResult(blocks=blocks, instructionFiles=files)
    assert result is not value


def test_prompt_context_matcher_uses_the_complete_block_collection() -> None:
    value = PromptContextInput(blocks=[PromptContextBlock(name="currentDate", text="now")])

    assert match_pattern_subset(value, {}) is True
    assert match_pattern_subset(value, {"blocks": value.blocks}) is True
    assert match_pattern_subset(value, {"blocks.0.name": "currentDate"}) is False


def test_prompt_context_validators_reject_duplicate_names_and_bad_instruction_files() -> None:
    duplicate = PromptContextInput(
        blocks=[
            PromptContextBlock(name="same", text="one"),
            PromptContextBlock(name="same", text="two"),
        ]
    )
    bad_file = InstructionFile(path="/workspace/AGENTS.md", kind="unknown", content="rules")

    assert valid_prompt_context_input(duplicate) is False
    assert valid_prompt_context_input(PromptContextInput(blocks=[], instructionFiles=[bad_file])) is False
    assert valid_prompt_context_output(PromptContextResult(blocks=[])) is True
    assert INSTRUCTION_FILE_KINDS == {"managed", "user", "project", "local", "memory"}


@pytest.mark.asyncio
async def test_prompt_context_handler_can_rewrite_the_whole_collection() -> None:
    bus = ExtensionEventBus()
    register_prompt_context_events(bus)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))

    async def rewrite(event, _ops, _next_handler):
        value = event.event_input
        return PromptContextResult(
            blocks=[
                PromptContextBlock(name="extra", text="added"),
                *reversed(value.blocks),
            ],
            instructionFiles=[],
        )

    bus.register_event_handler(PROMPT_CONTEXT_EVENT, rewrite, {}, HandlerLevel.USER)
    result = await bus.trigger(
        Event(
            PROMPT_CONTEXT_EVENT,
            PromptContextInput(
                blocks=[PromptContextBlock(name="currentDate", text="now")],
            ),
        )
    )

    assert result.blocks[0] == PromptContextBlock(name="extra", text="added")
    assert result.blocks[1] == PromptContextBlock(name="currentDate", text="now")
    assert result.instructionFiles == []
