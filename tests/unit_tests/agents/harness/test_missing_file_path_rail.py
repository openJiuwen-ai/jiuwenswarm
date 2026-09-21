"""Offline integration tests: real file tools, SQLite messages and event bus."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.messager.inprocess import InProcessMessager
from openjiuwen.agent_teams.rails.team_context import inject_team_handles
from openjiuwen.agent_teams.schema.events import TeamTopic
from openjiuwen.agent_teams.tools.database import (
    DatabaseConfig,
    DatabaseType,
    TeamDatabase,
)
from openjiuwen.agent_teams.tools.message_manager import TeamMessageManager
from openjiuwen.agent_teams.tools.team import TeamBackend
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
)
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent import AgentCard, ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    OperationMode,
    SysOperationCard,
)
from openjiuwen.core.sys_operation.cwd import get_cwd, set_cwd
from openjiuwen.harness.tools import ReadFileTool
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.agents.harness.team.rails.missing_file_path_rail import (
    MissingFilePathRail,
)
from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers.member_rails import (
    MISSING_FILE_PATH,
    _build_missing_file_path_rail,
)


@pytest_asyncio.fixture
async def world(tmp_path):
    token = set_session_id("file-recovery-test")
    original_cwd = get_cwd()
    worker = tmp_path / "worker"
    worker.mkdir()
    set_cwd(str(worker))
    await Runner.start()
    op_id = "file-recovery-test"
    Runner.resource_mgr.add_sys_operation(
        SysOperationCard(
            id=op_id, mode=OperationMode.LOCAL, work_config=LocalWorkConfig()
        )
    )
    db = TeamDatabase(
        DatabaseConfig(db_type=DatabaseType.SQLITE, connection_string=":memory:")
    )
    await db.initialize()
    await db.team.create_team(
        team_name="finance", display_name="Finance", leader_member_name="coordinator"
    )
    for name in ["analyst", "coordinator"]:
        await db.member.create_member(
            member_name=name,
            team_name="finance",
            display_name=name,
            agent_card=AgentCard(name=name).model_dump_json(),
            status="busy",
        )
    bus = InProcessMessager()
    events = []

    async def received(event):
        events.append(event)

    topic = TeamTopic.MESSAGE.build("file-recovery-test", "finance")
    await bus.subscribe(topic, received)
    backend = TeamBackend("finance", "analyst", False, db, bus)
    manager = TeamMessageManager("finance", "analyst", db, bus)
    rail = MissingFilePathRail(backend=backend, message_manager=manager)
    session = Session(card=AgentCard(name="analyst"))
    yield SimpleNamespace(
        rail=rail,
        backend=backend,
        manager=manager,
        bus=bus,
        events=events,
        session=session,
        worker=worker,
        db=db,
        reader=ReadFileTool(Runner.resource_mgr.get_sys_operation(op_id)),
    )
    await bus.unsubscribe(topic)
    await db.close()
    Runner.resource_mgr.remove_sys_operation(sys_operation_id=op_id)
    await Runner.stop()
    set_cwd(original_cwd)
    reset_session_id(token)


def context(world, result, path="filing.pdf", name="read_file", extra=None):
    return AgentCallbackContext(
        agent=None,
        session=world.session,
        extra=extra if extra is not None else {},
        inputs=ToolCallInputs(
            tool_name=name, tool_args={"file_path": path}, tool_result=result
        ),
    )


@pytest.mark.asyncio
async def test_actual_missing_pdf_requests_real_leader_without_at_symbol(world):
    result = await world.reader.invoke({"file_path": "filing.pdf"})
    assert not result.success and "File not found:" in result.error
    ctx = context(world, result)
    await world.rail.after_tool_call(ctx)
    messages = await world.manager.get_messages(to_member_name="coordinator")
    assert len(messages) == len(world.events) == 1
    message = messages[0]
    assert message.from_member_name == "analyst" and not message.broadcast
    assert json.loads(message.meta)["resolved_path"] == str(world.worker / "filing.pdf")
    assert "absolute path" in message.content and "send_message" in message.content
    finish = ctx.consume_force_finish().result
    assert finish["file_path_status"] == "waiting_for_file_path"
    # A later call in the same round cannot continue with a directory scan.
    scan = context(world, None, name="list_dir", extra=ctx.extra)
    await world.rail.before_tool_call(scan)
    assert scan.has_force_finish_request


@pytest.mark.asyncio
async def test_duplicate_failure_after_rail_rebuild_does_not_send_again(world):
    result = await world.reader.invoke({"file_path": "filing.pdf"})
    await world.rail.after_tool_call(context(world, result))
    rebuilt = MissingFilePathRail(backend=world.backend, message_manager=world.manager)
    ctx = context(world, result)
    await rebuilt.after_tool_call(ctx)
    assert len(await world.manager.get_messages(to_member_name="coordinator")) == 1
    assert ctx.has_force_finish_request


@pytest.mark.asyncio
async def test_parallel_missing_reads_queue_one_request(world):
    result = await world.reader.invoke({"file_path": "filing.pdf"})
    await asyncio.gather(
        *(world.rail.after_tool_call(context(world, result)) for _ in range(3))
    )
    assert len(await world.manager.get_messages(to_member_name="coordinator")) == 1


@pytest.mark.asyncio
async def test_absolute_path_reply_can_be_read_without_clearing_other_path(
    world, tmp_path
):
    result = await world.reader.invoke({"file_path": "filing.txt"})
    await world.rail.after_tool_call(context(world, result, "filing.txt"))
    actual = tmp_path / "attachments" / "filing.txt"
    actual.parent.mkdir()
    actual.write_text("Revenue: 42\n", encoding="utf-8")
    leader = TeamMessageManager("finance", "coordinator", world.db, world.bus)
    await leader.send_message(content=str(actual), to_member_name="analyst")
    replies = await world.manager.get_messages(to_member_name="analyst")
    recovered = await world.reader.invoke({"file_path": replies[0].content})
    assert recovered.success and "Revenue: 42" in recovered.data["content"]
    ctx = context(world, recovered, str(actual))
    await world.rail.after_tool_call(ctx)
    assert not ctx.has_force_finish_request
    pending = world.session.get_state(MissingFilePathRail.STATE_KEY)
    assert [row["path"] for row in pending] == [str(world.worker / "filing.txt")]


@pytest.mark.asyncio
async def test_success_clears_only_the_matching_resolved_path(world):
    for path in ["one/filing.txt", "two/filing.txt"]:
        result = await world.reader.invoke({"file_path": path})
        await world.rail.after_tool_call(context(world, result, path))
    actual = world.worker / "one" / "filing.txt"
    actual.parent.mkdir()
    actual.write_text("Available now", encoding="utf-8")
    result = await world.reader.invoke({"file_path": str(actual)})
    ctx = context(world, result, str(actual))
    await world.rail.after_tool_call(ctx)
    assert not ctx.has_force_finish_request
    pending = world.session.get_state(MissingFilePathRail.STATE_KEY)
    assert [row["path"] for row in pending] == [
        str(world.worker / "two" / "filing.txt")
    ]


@pytest.mark.parametrize(
    "result",
    [
        ToolOutput(
            success=True,
            data={"content": "File not found: quoted text inside a document"},
        ),
        ToolOutput(success=False, error="Permission denied"),
        ToolOutput(success=False, error="Invalid PDF page range format"),
        ToolOutput(success=False, error="No /Root object! - Is this really a PDF?"),
    ],
)
@pytest.mark.asyncio
async def test_only_actual_missing_file_errors_trigger_requests(world, result):
    ctx = context(world, result)
    await world.rail.after_tool_call(ctx)
    assert not ctx.has_force_finish_request
    assert not await world.manager.get_messages(to_member_name="coordinator")


@pytest.mark.asyncio
async def test_failed_send_reports_blocker_without_false_pending_state(world):
    world.rail._messages = SimpleNamespace(send_message=AsyncMock(return_value=None))
    result = await world.reader.invoke({"file_path": "filing.pdf"})
    ctx = context(world, result)
    await world.rail.after_tool_call(ctx)
    assert ctx.consume_force_finish().result["file_path_status"] == "blocked"
    assert not world.session.get_state(MissingFilePathRail.STATE_KEY)


@pytest.mark.parametrize(
    "leader", [None, "analyst", RuntimeError("database unavailable")]
)
@pytest.mark.asyncio
async def test_missing_leader_reports_blocker_without_sending(world, leader):
    resolver = (
        AsyncMock(side_effect=leader)
        if isinstance(leader, Exception)
        else AsyncMock(return_value=leader)
    )
    with patch.object(world.backend, "resolve_leader_member_name", resolver):
        result = await world.reader.invoke({"file_path": "filing.pdf"})
        ctx = context(world, result)
        await world.rail.after_tool_call(ctx)
    assert ctx.consume_force_finish().result["file_path_status"] == "blocked"
    assert not await world.manager.get_messages(to_member_name="coordinator")
    assert not world.session.get_state(MissingFilePathRail.STATE_KEY)


@pytest.mark.asyncio
async def test_queue_exception_does_not_claim_request_was_sent(world):
    world.rail._messages = SimpleNamespace(
        send_message=AsyncMock(side_effect=RuntimeError("queue unavailable"))
    )
    result = await world.reader.invoke({"file_path": "filing.pdf"})
    ctx = context(world, result)
    await world.rail.after_tool_call(ctx)
    assert ctx.consume_force_finish().result["file_path_status"] == "blocked"
    assert not world.session.get_state(MissingFilePathRail.STATE_KEY)


@pytest.mark.parametrize(
    "name,path", [("read_pdf", "filing.pdf"), ("read_file", ""), ("read_file", None)]
)
@pytest.mark.asyncio
async def test_unrelated_tools_and_invalid_paths_are_ignored(world, name, path):
    ctx = context(
        world, ToolOutput(success=False, error="File not found: filing.pdf"), path, name
    )
    await world.rail.after_tool_call(ctx)
    assert not ctx.has_force_finish_request
    assert not await world.manager.get_messages(to_member_name="coordinator")


@pytest.mark.asyncio
async def test_json_arguments_and_result_deduplicate_with_typed_result(world):
    result = await world.reader.invoke({"file_path": "filing.pdf"})
    ctx = context(world, result.model_dump_json())
    ctx.inputs.tool_args = json.dumps({"file_path": "filing.pdf"})
    await world.rail.after_tool_call(ctx)
    await world.rail.after_tool_call(context(world, result))
    assert len(await world.manager.get_messages(to_member_name="coordinator")) == 1


@pytest.mark.asyncio
async def test_feature_is_mounted_by_member_config_and_only_for_teammates(world):
    config = {"file_path_recovery": {"enabled": True}}
    ctx = SwarmBuildContext(role="teammate", language="en", config=config)
    inject_team_handles(ctx.extras, team_backend=world.backend, messager=world.bus)
    for mode in ["team", "code.team"]:
        specs, _ = build_member_capability_specs(config, mode, "teammate")
        spec = next(spec for spec in specs if spec.type == MISSING_FILE_PATH)
        assert isinstance(
            _build_missing_file_path_rail(spec.params, ctx), MissingFilePathRail
        )
    ctx.role = "leader"
    assert _build_missing_file_path_rail({"enabled": True}, ctx) is None
    ctx.role = "teammate"
    assert _build_missing_file_path_rail({}, ctx) is None
    for enabled in [False, "true", 1, None]:
        assert _build_missing_file_path_rail({"enabled": enabled}, ctx) is None
    no_handles = SwarmBuildContext(role="teammate", language="en", config=config)
    assert _build_missing_file_path_rail({"enabled": True}, no_handles) is None


@pytest.mark.asyncio
async def test_real_agent_loop_yields_and_allows_a_fresh_round_to_retry(
    world, tmp_path
):
    agent = ReActAgent(card=AgentCard(name="analyst-loop")).configure(
        ReActAgentConfig(
            model_config_obj=ModelRequestConfig(model_name="scripted-local-test"),
            model_client_config=ModelClientConfig(
                client_provider="OpenAI",
                api_key="unused",
                api_base="http://127.0.0.1:1",
            ),
            prompt_template=[
                {"role": "system", "content": "Read the assigned filing."}
            ],
        )
    )
    agent.ability_manager.add(world.reader.card)
    Runner.resource_mgr.add_tool(world.reader)
    await agent.register_rail(world.rail)
    model = SimpleNamespace(
        invoke=AsyncMock(
            side_effect=[
                AssistantMessage(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="missing-read",
                            name="read_file",
                            type="function",
                            arguments=json.dumps({"file_path": "filing.pdf"}),
                        )
                    ],
                ),
                AssertionError(
                    "The worker must yield before another model call can search for files"
                ),
            ]
        )
    )
    with patch.object(agent, "_get_llm", return_value=model):
        result = await agent.invoke(
            {"query": "Read filing.pdf", "conversation_id": "missing-file-loop"}
        )
    assert result["file_path_status"] == "waiting_for_file_path"
    assert model.invoke.await_count == 1
    assert len(await world.manager.get_messages(to_member_name="coordinator")) == 1
    actual = tmp_path / "filing.txt"
    actual.write_text("Revenue: 42", encoding="utf-8")
    retry_model = SimpleNamespace(
        invoke=AsyncMock(
            side_effect=[
                AssistantMessage(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="retry-read",
                            name="read_file",
                            type="function",
                            arguments=json.dumps({"file_path": str(actual)}),
                        )
                    ],
                ),
                AssistantMessage(content="Revenue: 42"),
            ]
        )
    )
    with patch.object(agent, "_get_llm", return_value=retry_model):
        result = await agent.invoke(
            {"query": f"Read {actual}", "conversation_id": "missing-file-loop"}
        )
    assert retry_model.invoke.await_count == 2
    assert "Revenue: 42" in str(result)
    assert "file_path_status" not in result
    Runner.resource_mgr.remove_tool(world.reader.card.id)
