# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import json
import logging
import uuid
from pathlib import Path

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent, InvokeInputs
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.sys_operation import LocalWorkConfig, SysOperation, SysOperationCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.rails.task_planning_rail import TaskPlanningRail
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec, SysOperationSpec, WorkspaceSpec
from openjiuwen.harness.schema.task import TodoItem
from openjiuwen.harness.tools import TodoCreateTool
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.agents.harness.common.rails.todo_progress_rail import TodoProgressRail
from jiuwenswarm.agents.harness.common.rails.todo_progress_receipt import _HINTS, receipt_suffix
from jiuwenswarm.agents.harness.common.rails.todo_progress_snapshot import (
    TodoReceiptChange,
    completed_regressions,
)
from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
    MemberInfo, TeamWorkspaceInfo, build_member_rails,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec
from jiuwenswarm.agents.swarm.providers.code_rails import build_code_task_planning
from jiuwenswarm.agents.swarm.registry import register_swarm_providers
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def build_todo_items(*statuses):
    return [{"id": f"item{i}", "content": f"Stage {i}", "activeForm": f"Doing stage {i}",
             "description": f"Verify stage {i}", "status": status} for i, status in enumerate(statuses)]


@pytest.fixture
def product_agent(tmp_path, request):
    operation_card = SysOperationCard(
        id=f"todo-product-{uuid.uuid4().hex}",
        work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)]),
    )
    operation = SysOperation(operation_card)
    mode = request.param
    if mode == "team":
        register_swarm_providers()
        config = {"react": {"subagents": {"browser_agent": {"enabled": False}}}}
        context = SwarmBuildContext(session_id="todo-product", mode="team", role="teammate",
                                    project_dir=str(tmp_path), config=config)
        base = DeepAgentSpec(
            card=AgentCard(id=f"product-{uuid.uuid4().hex}", name="todo", description="todo integration"),
            workspace=WorkspaceSpec(root_path=str(tmp_path)), cwd=str(tmp_path),
            sys_operation=SysOperationSpec(id=operation_card.id,
                                          work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)])),
            enable_read_image_multimodal=False,
        )
        spec = build_member_deep_agent_spec(config, "team", "teammate", base)
        agent = spec.build(context=context)
        [rail] = [item for item in agent.configured_rails() if isinstance(item, TaskPlanningRail)]
    elif mode == "work":
        rail = JiuWenSwarmDeepAdapter._build_task_planning_rail()
    elif mode == "code":
        rail = JiuwenSwarmCodeAdapter._build_code_task_planning_rail()
    elif mode == "swarm":
        rail = build_code_task_planning({}, SwarmBuildContext(project_dir=str(tmp_path), config={}))
    else:
        rails = build_member_rails(MemberInfo(role="worker"),
                                   team_workspace=TeamWorkspaceInfo(root_dir=str(tmp_path), config={}))
        rail = next(item for item in rails if isinstance(item, TaskPlanningRail))
    if mode != "team":
        agent = DeepAgent(AgentCard(id=f"product-{uuid.uuid4().hex}", name="todo", description="todo integration"))
        agent.configure(DeepAgentConfig(sys_operation=operation, workspace=Workspace(root_path=str(tmp_path))))
    rail.init(agent)
    yield agent, rail, Session(session_id="todo-product")
    rail.uninit(agent)


async def execute(agent, session, name, arguments):
    call = ToolCall(id=uuid.uuid4().hex, type="function", name=name, arguments=json.dumps(arguments))
    context = AgentCallbackContext(agent=agent.react_agent, session=session)
    [(result, message)] = await agent.ability_manager.execute(context, call, session)
    return result, message, call


@pytest.mark.asyncio
@pytest.mark.parametrize("product_agent", ["work", "code", "swarm", "team", "legacy_team"], indirect=True)
async def test_product_registration_error_message_and_saved_event(product_agent):
    agent, rail, session = product_agent
    create = next(item for item in rail.tools if isinstance(item, TodoCreateTool))
    assert create.require_explicit_status and rail.clone_for_subagent().require_explicit_status
    registered = next(card for card in agent.ability_manager.list() if card.name == "todo_create")
    assert "status" in registered.input_params["properties"]["tasks"]["items"]["required"]
    definition = next(info for info in await agent.ability_manager.list_tool_info() if info.name == "todo_create")
    assert "status" in definition.parameters["properties"]["tasks"]["items"]["required"]
    assert "自动" not in definition.description and "automatically" not in definition.description
    await execute(agent, session, "todo_create", {"tasks": build_todo_items("completed", "pending")})
    path = create._get_file_path(session.get_session_id())
    original = Path(path).read_bytes()
    stream = JiuSwarmStreamEventRail()
    await agent.register_rail(stream)
    ctx = AgentCallbackContext(agent=agent, session=session,
                               inputs=InvokeInputs(query="Validate status preservation",
                                                   conversation_id=session.get_session_id()))
    await ctx.fire(AgentCallbackEvent.BEFORE_INVOKE)
    missing = build_todo_items("completed", "pending")
    del missing[1]["status"]
    result, message, _ = await execute(agent, session, "todo_create", {"tasks": missing})
    assert result is None
    for text in ("item1", "pending", "in_progress", "completed", "cancelled", "Example task:", "No changes were written"):
        assert text in message.content
    assert Path(path).read_bytes() == original

    expected = build_todo_items(*(["completed"] * 7), "in_progress", "pending")
    result, message, _ = await execute(agent, session, "todo_create", {"tasks": expected})
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    assert [item["status"] for item in saved] == [item["status"] for item in expected]
    assert "(task_id: item7)" in message.content and "(task_id: item7)" in result["message"]
    listed, _, _ = await execute(agent, session, "todo_list", {})
    fields = ("id", "content", "status", "depends_on")
    assert listed["tasks"] == [{key: item[key] for key in fields} for item in saved]

    await execute(agent, session, "todo_create", {"tasks": build_todo_items("completed", "cancelled", "pending")})
    ctx.inputs = InvokeInputs(query="Validate status preservation", result={"result_type": "answer"})
    await ctx.fire(AgentCallbackEvent.AFTER_INVOKE)
    await agent.unregister_rail(stream)
    await session.close_stream()
    events = [event async for event in session.stream_iterator()]
    updates = [event for event in events if getattr(event, "type", None) == "todo.updated"]
    rebuilt = [item["status"] for item in expected]
    assert [[item["status"] for item in event.payload["todos"]] for event in updates] == [
        ["completed", "pending"], rebuilt, rebuilt, ["completed", "pending"],
    ]
    listed, _, _ = await execute(agent, session, "todo_list", {})
    assert [item["status"] for item in listed["tasks"]] == ["completed", "cancelled", "pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize("product_agent", ["work"], indirect=True)
async def test_diagnostic_counts_and_regression_receipt(product_agent, progress_logs):
    agent, rail, session = product_agent
    progress = TodoProgressRail()
    await agent.register_rail(progress)
    context = AgentCallbackContext(agent=agent, session=session, inputs=InvokeInputs(query="Replan"))
    await context.fire(AgentCallbackEvent.BEFORE_INVOKE)
    statuses = [("completed", "pending"), ("in_progress", "completed"), ("completed", "pending")]
    for current in statuses:
        _, message, _ = await execute(agent, session, "todo_create", {"tasks": build_todo_items(*current)})
        if current == ("in_progress", "completed"):
            receipt = message.content
            assert "Recorded in progress: 'Stage 0' (task_id: item0)" in receipt
            assert "Continue task" not in receipt
            assert "已完成项退回未完成：item0" in receipt
            assert "执行这些条目前请先核实" in receipt
            assert _HINTS["cn"]["last_item"] not in receipt
    state = progress._states[session.get_session_id()]
    assert state.writes_ok == 3 and state.rail_errors == 0
    writes = [json.loads(record.getMessage().split("[TodoProgress] write ", 1)[1])
              for record in progress_logs.records if record.getMessage().startswith("[TodoProgress] write ")]
    assert [(item["completed_before"], item["completed_after"], item["completed_to_unfinished_count"])
            for item in writes] == [(0, 1, 0), (1, 1, 1), (1, 1, 1)]
    assert all("item0" not in record.getMessage() for record in progress_logs.records
               if record.getMessage().startswith("[TodoProgress]"))
    assert completed_regressions([{"id": "old", "status": "completed"}],
                                 [{"id": "new", "status": "pending"}]) == []
    assert completed_regressions(None, []) is None
    assert completed_regressions([], []) == []
    context.inputs.result = {"result_type": "answer"}
    await context.fire(AgentCallbackEvent.AFTER_INVOKE)
    await agent.unregister_rail(progress)


@pytest.fixture
def progress_logs(caplog):
    logger = logging.getLogger("jiuwenswarm")
    old_level = logger.level
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.INFO)
    yield caplog
    logger.removeHandler(caplog.handler)
    logger.setLevel(old_level)


@pytest.mark.parametrize("language", ["cn", "en"])
def test_create_receipt_avoids_duplicate_next_step_and_caps_regressions(language):
    pending = [TodoItem(id="next", content="Next", status="pending")]
    assert receipt_suffix("todo_create", pending, noop=False, language=language) == ""
    closed = [TodoItem(id="closed", content="Closed", status="cancelled")]
    assert receipt_suffix("todo_create", closed, noop=False, language=language) == ""
    active = [TodoItem(id="active", content="Active", status="in_progress")]
    text = receipt_suffix(
        "todo_create",
        active,
        noop=False,
        language=language,
        change=TodoReceiptChange(
            regressed_ids=tuple(f"original-{i}-" + "x" * 90 for i in range(30)),
        ),
    )
    assert len(text) <= 600
    assert "original-0" in text and ("未列出" if language == "cn" else "not shown") in text
    text = receipt_suffix(
        "todo_create",
        active,
        noop=False,
        language=language,
        change=TodoReceiptChange(regressed_ids=("x" * 2000,)),
    )
    assert len(text) <= 600 and "xxxx" not in text
    assert ("1 项未列出" if language == "cn" else "(1 more items not shown)") in text


@pytest.mark.parametrize("language", ["cn", "en"])
@pytest.mark.parametrize("active_regressed", [True, False])
def test_create_receipt_combines_regression_and_missing_within_cap(language, active_regressed):
    todos = [TodoItem(id="active", content="Active", status="in_progress"),
             TodoItem(id="next", content="Next", status="pending")]
    regressed = (["active"] if active_regressed else []) + [f"original-{i}-" + "x" * 90 for i in range(30)]
    missing = [(f"dropped-{i}-" + "y" * 90, "Dropped work") for i in range(30)]
    text = receipt_suffix(
        "todo_create",
        todos,
        noop=False,
        language=language,
        change=TodoReceiptChange(regressed_ids=tuple(regressed), missing_items=tuple(missing)),
    )
    assert len(text) <= 600
    regressed_head = "已完成项退回未完成：" if language == "cn" else "Completed items became unfinished: "
    missing_head = "重建后的清单缺少原已完成项：" if language == "cn" else "Completed items missing from the rebuilt list: "
    assert text.index(missing_head) < text.index(regressed_head)  # 遗漏提示排在最前
    assert text.count("未列出" if language == "cn" else "not shown") == 2
    # 有遗漏时不给继续执行类提示；只有退回、退回的不是进行中项且条目能全部列出时保留
    assert _HINTS[language]["advance"] not in text
    short_regressed = (["active"] if active_regressed else []) + ["original-0", "original-1"]
    regressed_only = receipt_suffix(
        "todo_create",
        todos,
        noop=False,
        language=language,
        change=TodoReceiptChange(regressed_ids=tuple(short_regressed)),
    )
    assert (_HINTS[language]["advance"] in regressed_only) == (not active_regressed)
    assert receipt_suffix(
        "todo_modify",
        todos,
        noop=False,
        language=language,
        change=TodoReceiptChange(missing_items=tuple(missing)),
    ).count(missing_head) == 0


@pytest.mark.parametrize("with_coding_memory", [False, True])
def test_code_subagent_requires_explicit_status(with_coding_memory):
    from unittest.mock import MagicMock

    from openjiuwen.harness.rails import SysOperationRail

    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.providers.code_rails import CODING_MEMORY_EXTRAS_KEY
    from jiuwenswarm.agents.swarm.providers.code_subagents import _PARENT_MODEL_EXTRAS_KEY, build_code_agent

    memory_rail = MagicMock()
    ctx = SwarmBuildContext(mode="code.team", role="leader")
    ctx.extras[_PARENT_MODEL_EXTRAS_KEY] = MagicMock()
    if with_coding_memory:
        ctx.extras[CODING_MEMORY_EXTRAS_KEY] = memory_rail
    spec = build_code_agent({}, ctx)
    planning = [rail for rail in spec.rails if isinstance(rail, TaskPlanningRail)]
    assert len(planning) == 1 and planning[0].require_explicit_status
    # 子 agent 实例化时 rails 会经 clone_for_subagent 复制，配置须保留
    assert planning[0].clone_for_subagent().require_explicit_status
    assert any(isinstance(rail, SysOperationRail) for rail in spec.rails)
    assert (memory_rail in spec.rails) == with_coding_memory


def test_code_mode_configured_code_agent_requires_explicit_status(monkeypatch):
    from unittest.mock import MagicMock

    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter

    monkeypatch.setattr(JiuwenSwarmCodeAdapter, "_browser_runtime_enabled", staticmethod(lambda: False))
    adapter = JiuwenSwarmCodeAdapter()
    subagents, _ = adapter._build_configured_subagents(
        MagicMock(), {"subagents": {"code_agent": {"enabled": True}}}, {},
    )
    [code_spec] = [spec for spec in subagents if spec.agent_card.name == "code_agent"]
    planning = [rail for rail in code_spec.rails if isinstance(rail, TaskPlanningRail)]
    assert len(planning) == 1 and planning[0].require_explicit_status
