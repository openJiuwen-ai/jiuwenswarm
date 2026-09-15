"""StreamEventRail tool-context repair: serialised interrupt envelopes.

Scenario:
1. User asks the parent to delegate.
2. Parent assistant emits a tool_call (id "tc1", name "task").
3. The delegation tool returns an interrupt *envelope* dict (not an
   exception). The dict {"result_type": "interrupt", "state": [...],
   "interrupt_ids": [...]} is serialized into a ToolMessage.
4. User answers; the sub-agent resumes, finishes, and the real result is
   appended as another ToolMessage for the same tool_call_id.

_fix_incomplete_tool_context runs before every model call and dedupes
ToolMessages per tool_call_id first-wins, exempting "interrupt
placeholders". If the serialized envelope is not classified as a
placeholder, the envelope wins, the real answer is dropped, and the
model re-asks the question inside the envelope's state.

The envelope reaches the ToolMessage in two serialisation shapes:
- Python repr via ability_manager str(result) — single quotes, nested
  OutputSchema(...) reprs (the production path for delegation tools).
- JSON via json.dumps paths — double quotes.
"""

import json
from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)


class _ModelContext:
    def __init__(self, messages):
        self.messages = list(messages)

    def get_messages(self):
        return list(self.messages)

    def pop_messages(self, size):
        popped = self.messages[:size]
        self.messages = self.messages[size:]
        return popped

    async def add_messages(self, message):
        self.messages.append(message)


def _rail() -> JiuSwarmStreamEventRail:
    rail = JiuSwarmStreamEventRail.__new__(JiuSwarmStreamEventRail)
    rail._deep_agent = None
    return rail


def _assistant_delegate() -> AssistantMessage:
    return AssistantMessage(
        content="",
        tool_calls=[
            {"id": "tc1", "type": "tool_call", "name": "task", "arguments": "{}"}
        ],
    )


def _envelope_tool_message_repr(tool_call_id: str = "tc1") -> ToolMessage:
    """Production shape: ability_manager str()-serialises the raw envelope dict.

    Matches ToolInterruptHandler.build_interrupt_result output rendered by
    str(): single quotes, nested OutputSchema/InteractionOutput reprs.
    """
    content = (
        "{'result_type': 'interrupt', 'state': [OutputSchema(type='interaction', "
        "index=0, payload=InteractionOutput(id='inner-1', "
        "value={'tool_name': 'ask_user', 'message': 'Which naming style do you "
        "want?'}))], 'interrupt_ids': ['inner-1']}"
    )
    return ToolMessage(content=content, tool_call_id=tool_call_id)


def _envelope_tool_message_json(tool_call_id: str = "tc1") -> ToolMessage:
    """JSON shape: json.dumps serialisation of the same envelope."""
    envelope = {
        "result_type": "interrupt",
        "state": [
            {
                "type": "interaction",
                "payload": {
                    "id": "inner-1",
                    "value": {
                        "tool_name": "ask_user",
                        "message": "Which naming style do you want?",
                    },
                },
            }
        ],
        "interrupt_ids": ["inner-1"],
    }
    return ToolMessage(content=json.dumps(envelope), tool_call_id=tool_call_id)


def _ctx(messages):
    return SimpleNamespace(
        context=_ModelContext(messages),
        inputs=SimpleNamespace(tools=[]),
        session=None,
        extra={},
    )


async def _run_repair(rail, envelope_message):
    messages = [
        UserMessage(content="delegate please"),
        _assistant_delegate(),
        envelope_message,
        ToolMessage(content="Final naming: snake_case everywhere.", tool_call_id="tc1"),
    ]
    ctx = _ctx(messages)
    await rail._fix_incomplete_tool_context(ctx)
    return [m.content for m in ctx.context.messages if isinstance(m, ToolMessage)]


@pytest.mark.asyncio
async def test_repr_envelope_then_real_result_keeps_real_result():
    """Production path: str()-serialised envelope must not beat the real answer."""
    tool_texts = await _run_repair(_rail(), _envelope_tool_message_repr())
    assert len(tool_texts) == 1, f"expected exactly one ToolMessage, got {tool_texts}"
    assert "snake_case" in tool_texts[0], (
        "real sub-agent answer was discarded; the interrupt envelope survived: "
        f"{tool_texts[0]}"
    )


@pytest.mark.asyncio
async def test_json_envelope_then_real_result_keeps_real_result():
    """JSON-serialised envelope variant gets the same treatment."""
    tool_texts = await _run_repair(_rail(), _envelope_tool_message_json())
    assert len(tool_texts) == 1, f"expected exactly one ToolMessage, got {tool_texts}"
    assert "snake_case" in tool_texts[0], (
        "real sub-agent answer was discarded; the interrupt envelope survived: "
        f"{tool_texts[0]}"
    )


@pytest.mark.asyncio
async def test_placeholder_text_then_real_result_keeps_real_result():
    """Control case: literal placeholder text is handled fine."""
    rail = _rail()
    messages = [
        UserMessage(content="delegate please"),
        _assistant_delegate(),
        ToolMessage(
            content=(
                "[Tool interrupted] Tool task was interrupted by the user "
                "and has no result."
            ),
            tool_call_id="tc1",
        ),
        ToolMessage(content="Final naming: snake_case everywhere.", tool_call_id="tc1"),
    ]
    ctx = _ctx(messages)
    await rail._fix_incomplete_tool_context(ctx)

    tool_texts = [
        m.content for m in ctx.context.messages if isinstance(m, ToolMessage)
    ]
    assert len(tool_texts) == 1
    assert "snake_case" in tool_texts[0]


@pytest.mark.asyncio
async def test_normal_tool_result_with_result_type_text_is_not_misclassified():
    """A real tool result that merely mentions the key names must survive.

    The envelope detection is content-based, so guard against false
    positives: a legitimate answer discussing interrupt envelopes in prose
    (or a non-interrupt result_type) must NOT be dropped as a placeholder.
    """
    rail = _rail()
    messages = [
        UserMessage(content="delegate please"),
        _assistant_delegate(),
        ToolMessage(
            content=(
                "The sub-agent reported: result_type was 'answer'. "
                "It mentions 'interrupt_ids' only in documentation prose."
            ),
            tool_call_id="tc1",
        ),
    ]
    ctx = _ctx(messages)
    await rail._fix_incomplete_tool_context(ctx)

    tool_texts = [
        m.content for m in ctx.context.messages if isinstance(m, ToolMessage)
    ]
    assert len(tool_texts) == 1
    assert "sub-agent reported" in tool_texts[0]


def test_envelope_detection_unit():
    detect = JiuSwarmStreamEventRail._is_serialised_interrupt_envelope
    assert detect(str({'result_type': 'interrupt', 'state': [], 'interrupt_ids': []}))
    assert detect(json.dumps({'result_type': 'interrupt', 'state': [], 'interrupt_ids': []}))
    assert not detect("ordinary tool output")
    assert not detect(str({'result_type': 'answer', 'output': 'done'}))
    assert not detect("")