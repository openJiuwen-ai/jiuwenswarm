# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The document's state a task pane sends with a message reaches the model once, as a read_surface_context result."""

from __future__ import annotations

from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.single_agent.rail.base import AgentRail

from jiuwenswarm.server.client_attachments import save_client_state
from jiuwenswarm.server.runtime.session.session_metadata import get_session_equipment
from tests.unit_tests.office.conftest import WORD_PACKAGE
from tests.unit_tests.office.test_task_pane_conversation import FROM_ANOTHER_CONVERSATION, SessionAdapter

READ_SECTION = ToolCall(id="section-1", type="function", name="read_doc_section", arguments='{"heading": "Terms"}')


def states_seen(model, call: int = -1) -> list[str]:
    """The ``read_surface_context`` results in the model's ``call``-th call, oldest first."""
    messages = model.seen[call][0]
    calls = {c.id for m in messages for c in getattr(m, "tool_calls", None) or () if c.name == "read_surface_context"}
    return [m.content for m in messages if getattr(m, "tool_call_id", None) in calls]


async def test_each_state_a_message_carries_reaches_the_model_once(tmp_path, monkeypatch, attach_pane) -> None:
    attach_pane("doc-a", ["read_surface_context"], client_app="office-word")

    async with SessionAdapter(tmp_path, monkeypatch, "doc-a", "office-word") as pane:
        await pane.turn(client_state="<doc_state>v1</doc_state>")
        assert states_seen(pane.model) == ["<doc_state>v1</doc_state>"]

        # Another conversation's message brings no state: the model has the one it saw.
        await pane.turn(**FROM_ANOTHER_CONVERSATION)
        assert states_seen(pane.model) == ["<doc_state>v1</doc_state>"]

        await pane.turn(client_state="<doc_state>v2</doc_state>")
        assert states_seen(pane.model) == ["<doc_state>v1</doc_state>", "<doc_state>v2</doc_state>"]


async def test_a_resumed_turn_adds_no_state(conversation, attach_pane) -> None:
    attach_pane("doc-a", ["read_surface_context", "read_doc_section"], client_app="office-word")
    save_client_state("doc-a", "<doc_state>v1</doc_state>")

    async with conversation([[READ_SECTION]], session_id="doc-a", package=WORD_PACKAGE) as pane:
        [request] = await pane.turn("summarize the terms")
        assert await pane.answer((request, {"content": "The terms."})) == []

    assert [states_seen(pane.model, call) for call in (0, 1)] == [["<doc_state>v1</doc_state>"]] * 2


def _shows_state(message) -> bool:
    calls = getattr(message, "tool_calls", None) or ()
    return (getattr(message, "tool_call_id", None) or "").startswith("client_state_") or any(
        call.id.startswith("client_state_") for call in calls)


class _Compaction(AgentRail):
    """Drops the shown state from the conversation before each model call, as a compressor may."""

    priority = 99

    async def before_model_call(self, ctx) -> None:
        ctx.context.set_messages([m for m in ctx.context.get_messages() if not _shows_state(m)])


async def test_a_state_compaction_dropped_is_not_shown_again(conversation, attach_pane) -> None:
    attach_pane("doc-a", ["read_surface_context", "read_doc_section"], client_app="office-word")
    save_client_state("doc-a", "<doc_state>v1</doc_state>")

    async with conversation([[READ_SECTION]], session_id="doc-a", package=WORD_PACKAGE) as pane:
        await pane.agent.register_rail(_Compaction())
        [request] = await pane.turn("summarize the terms")
        await pane.answer((request, {"content": "The terms."}))

    assert [states_seen(pane.model, call) for call in (0, 1)] == [["<doc_state>v1</doc_state>"], []]


async def test_a_state_over_the_limit_fails_the_message_after_its_equipment_is_saved(
        tmp_path, monkeypatch, attach_pane) -> None:
    attach_pane("doc-a", ["read_surface_context"], client_app="office-word")

    async with SessionAdapter(tmp_path, monkeypatch, "doc-a", "office-word") as pane:
        [error] = await pane.send(client_state="x" * (256 * 1024 + 1))

    assert "client_state must be text of at most 256 KiB" in error
    assert get_session_equipment("doc-a", cache_bust=True) != {}
