# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Verify command ingress and actual Core model-context messages, without LLM."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.context_engine import ContextEngine, ContextEngineConfig
from openjiuwen.core.foundation.llm import UserMessage
from openjiuwen.core.session.agent import Session

from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.event_types.server_run_command import (
    ServerCommandError,
)
from jiuwenswarm.server.runtime.commands.adapter_commands import (
    append_command_context, command_origin, run_adapter_slash_command,
)
from .test_server_run_command import _policy, host


async def _adapter():
    engine = ContextEngine(ContextEngineConfig(
        enable_tiktoken_counter=False, enable_openrouter_model_context_window_tokens=False,
    ))
    session = Session(session_id="session-a")
    session.commit = AsyncMock()
    context = await engine.create_context(session=session, history_messages=[UserMessage(content="prior")])
    instance = SimpleNamespace(react_agent=SimpleNamespace(context_engine=engine), _interaction_session=session)
    return SimpleNamespace(_instance=instance, _project_dir=None), session, context


@pytest.mark.asyncio
async def test_adapter_invokes_registered_user_chain_with_real_bound_context(host):
    adapter, session, context = await _adapter()
    adapter._project_dir = str(host.tmp)
    await _policy(host, '''assert ops.session_context.session_id == "session-a"
assert (await ops.session.cwd()) is not None
assert event.event_input.presentation.columns == 80
return ServerRunCommandResult(text="project ready", context=("hidden rules",))''')
    native = AsyncMock()
    output = await run_adapter_slash_command(adapter, "/project_status demo", "session-a", "agent", "web", native)
    assert output["output"] == "project ready" and "hidden rules" not in str(output)
    native.assert_not_awaited()
    assert [m.content for m in context.get_messages()] == ["prior", "hidden rules"]
    assert context.get_messages()[-1].role == "user"
    assert session.get_state("context")
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_next_text_replacement_keeps_native_followup_and_panel(host):
    adapter, session, context = await _adapter()
    await _policy(host, '''value = await next_handler(event)
return ServerRunCommandResult(text="checked: " + value.text)''')
    native = AsyncMock(return_value={"output": "original", "result_type": "followup",
                                    "followup_prompt": "original model task", "approval_chunks": ["kept"]})
    result = await run_adapter_slash_command(adapter, "/evolve demo", "session-a", "agent", "web", native)
    assert result == {"output": "checked: original", "result_type": "followup",
                      "followup_prompt": "original model task", "approval_chunks": ["kept"]}
    native.assert_awaited_once_with("/evolve demo", "session-a", "agent", channel_id="web")
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_next_cannot_replace_a_nontext_native_panel_payload(host):
    await _policy(host, '''await next_handler(event)
return ServerRunCommandResult(text="replacement")''')
    panel = {"output": {"panel_id": "native-panel"}, "result_type": "panel"}
    native = AsyncMock(return_value=panel)
    result = await run_adapter_slash_command(SimpleNamespace(), "/goal get", "session-a", "agent", "tui", native)
    assert result == panel
    native.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_extensions_and_ordinary_text_follow_original_native_path(monkeypatch):
    monkeypatch.setattr(assembly, "get_extension_bus", lambda: None)
    native = AsyncMock(return_value=None)
    adapter = SimpleNamespace()
    for query in ["hello", "/evolve demo", {"a2ui": "payload"}]:
        assert await run_adapter_slash_command(adapter, query, "s", "agent", "web", native) is None
    assert native.await_count == 3


@pytest.mark.asyncio
async def test_web_goal_keeps_original_text_policy(host):
    await _policy(host, 'raise AssertionError("goal text should bypass this event on web")')
    native = AsyncMock(return_value=None)
    assert await run_adapter_slash_command(SimpleNamespace(), "/goal set x", "session-a", "agent", "web", native) is None
    native.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "/project_status demo\n", " \n/project_status demo\t ",
    "/project_status\tdemo", "/project_status\n demo",
])
async def test_command_whitespace_cannot_bypass_the_user_policy(host, query):
    await _policy(host, '''assert event.event_input.command == "project_status"
assert event.event_input.args.strip() == "demo"
return ServerRunCommandResult(text="blocked by policy")''')
    native = AsyncMock(return_value=None)
    result = await run_adapter_slash_command(SimpleNamespace(), query, "session-a", "agent", "web", native)
    assert result["output"] == "blocked by policy"
    native.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_unclaimed_command_is_rejected_and_not_submitted_to_model(host):
    native = AsyncMock(return_value=None)
    with pytest.raises(ServerCommandError, match="COMMAND_UNKNOWN"):
        await run_adapter_slash_command(SimpleNamespace(), "/not_registered", "session-a", "agent", "web", native)
    native.assert_awaited_once()


@pytest.mark.asyncio
async def test_note_writer_retains_prior_messages_and_uses_live_session(monkeypatch):
    adapter, session, context = await _adapter()
    await append_command_context(adapter, "session-a", ("one", "two"))
    assert [message.content for message in context.get_messages()] == ["prior", "one", "two"]
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_context_uses_session_restore_and_same_processor_config(monkeypatch):
    adapter, session, context = await _adapter()
    engine = adapter._instance.react_agent.context_engine
    await engine.save_contexts(session)
    await engine.clear_context(session_id="session-a")
    await append_command_context(adapter, "session-a", ("restored note",))
    restored = engine.get_context(session_id="session-a")
    assert [message.content for message in restored.get_messages()] == ["prior", "restored note"]
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_command_can_create_a_real_product_session_and_context(tmp_path):
    from openjiuwen.core.single_agent import AgentCard

    engine = ContextEngine(ContextEngineConfig(
        enable_tiktoken_counter=False, enable_openrouter_model_context_window_tokens=False,
    ))
    instance = SimpleNamespace(
        react_agent=SimpleNamespace(context_engine=engine),
        card=AgentCard(id="command-fresh-context", name="Command test"),
    )
    adapter = SimpleNamespace(_instance=instance)
    session_id = "command-" + tmp_path.name
    await append_command_context(adapter, session_id, ("first hidden note",))
    context = engine.get_context(session_id=session_id)
    assert context.get_messages()[0].content == "first hidden note"
    assert context.get_session_ref().get_state("context")


@pytest.mark.asyncio
async def test_model_context_unavailable_is_reported_explicitly():
    with pytest.raises(ServerCommandError, match="COMMAND_CONTEXT_UNAVAILABLE"):
        await append_command_context(SimpleNamespace(), "session-a", ("note",))


@pytest.mark.parametrize("channel,kind", [
    ("web", "bridge"), ("sdk", "sdk"), ("tui", "unclassified"),
    ("cron", "scheduled-trigger"), ("heartbeat", "task-notification"), (None, "unclassified"),
])
def test_origin_is_host_classified_and_does_not_invent_a_human_gesture(channel, kind):
    assert command_origin(channel).kind == kind


def test_channel_origin_names_its_server():
    assert command_origin("feishu").server == "feishu"
