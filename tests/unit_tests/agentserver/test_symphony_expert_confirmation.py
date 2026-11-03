"""Dynamic expert confirmation uses the existing Core and desktop HITL contract."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.interrupt.handler import ToolInterruptHandler
from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPTION_KEY, INTERRUPT_AUTO_CONFIRM_KEY, RESUME_USER_INPUT_KEY,
)
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.symphony import expert_team
from jiuwenswarm.symphony.expert_confirmation import DynamicExpertConfirmationRail


async def build_pending_fixture(
    monkeypatch, candidate_count=2, selected_count=1, summaries=None, revision_summaries=None,
    sources=None, avatars=None, descriptions=None, first_no_plan=False, revision_no_plan=False,
    supports_user_interaction=True,
    first_planning_error=False,
):
    """Reusable real Core session/interrupt fixture, also usable by desktop UI QA."""
    default_summaries = {
        f"e{i}": f"分析第{i + 1}部分资料的关键事实与依据，梳理可交付结论和后续工作建议。"
        for i in range(selected_count)
    }
    summaries = default_summaries if summaries is None else summaries
    revision_summaries = summaries if revision_summaries is None else revision_summaries

    def plan_response(display_summaries):
        return {"content": json.dumps({
            "status": "ready",
            "nodes": [
                {"id": f"e{i}", "task": f"整理第{i + 1}部分资料",
                 **({"summary": display_summaries[f"e{i}"]} if f"e{i}" in display_summaries else {})}
                for i in range(selected_count)
            ],
            "edges": [],
        }, ensure_ascii=False)}

    if first_no_plan:
        first_response = {
            "content": '{"status":"no_plan"}',
        }
    elif first_planning_error:
        first_response = RuntimeError("planner unavailable")
    else:
        first_response = plan_response(summaries)
    model = SimpleNamespace(invoke=AsyncMock(side_effect=[
        first_response,
        {"content": '{"status":"no_plan"}'} if revision_no_plan else plan_response(revision_summaries),
    ]))
    agent = SimpleNamespace(
        card=AgentCard(id="confirmation-test", name="小艺Work"),
        ability_manager=SimpleNamespace(get=lambda name: object() if name == "task_tool" else None),
    )
    session = create_agent_session(session_id="confirmation-session", card=agent.card)
    session.update_state({INTERRUPT_AUTO_CONFIRM_KEY: {expert_team.PLAN_TOOL_NAME: True}})
    call = ToolCall(id="plan-call", type="function", name=expert_team.PLAN_TOOL_NAME,
                    arguments=json.dumps({"query": "请专家整理资料"}))
    ctx = AgentCallbackContext(agent=agent, session=session, config={"permissions": {"enabled": False}},
                               inputs=ToolCallInputs(tool_call=call, tool_name=call.name, tool_args=call.arguments))
    experts = {
        f"e{i}": expert_team.LocalExpert(
            f"e{i}", f"专家{i + 1}", (descriptions or {}).get(f"e{i}", "资料分析"), object(),
            source=(sources or {}).get(f"e{i}", "local"),
            avatar=(avatars or {}).get(f"e{i}", ""),
        )
        for i in range(candidate_count)
    }
    scan = AsyncMock(return_value=experts)
    bind = AsyncMock(return_value=["bound"])
    monkeypatch.setattr(expert_team, "scan_local_experts", scan)
    monkeypatch.setattr(expert_team, "_subagent_config", lambda _agent, expert, _model: expert.id)
    monkeypatch.setattr(expert_team, "apply_extension_hot", bind)
    monkeypatch.setattr(expert_team, "unapply_extension_hot", AsyncMock())
    handoff = AsyncMock(return_value=[])
    round_state = expert_team.DynamicExpertRound(
        agent, "请专家整理资料", model, session_id=session.get_session_id(), handoff_expert=handoff,
        supports_user_interaction=supports_user_interaction,
    )
    rail = DynamicExpertConfirmationRail(lambda sid: round_state if sid == round_state.session_id else None)
    tool = expert_team.ExpertPlanTool(lambda sid: round_state if sid == round_state.session_id else None)
    payload = None
    try:
        await rail.before_tool_call(ctx)
    except AbortError as error:
        handler = ToolInterruptHandler(agent)
        state, requests = handler.build_interrupt_state(
            [(error.cause, None)], [call], AssistantMessage(content="", tool_calls=[call]), 0,
        )
        session.update_state({INTERRUPTION_KEY: state})
        payload = convert_interactions_to_ask_user_question([
            SimpleNamespace(id=request_id, value=request) for request_id, request in requests
        ])
    return SimpleNamespace(agent=agent, session=session, call=call, ctx=ctx, round=round_state,
                           rail=rail, tool=tool, payload=payload, scan=scan, bind=bind, model=model, handoff=handoff)


def prepare_resume_inputs(fixture, answers, *, request_id=None):
    """Use the production adapter to validate the UI ID and map it back to Core."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    request_id = fixture.payload["request_id"] if request_id is None else request_id
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(_loop_session=fixture.session)
    adapter._dynamic_expert_rounds = {fixture.session.get_session_id(): fixture.round}
    answer = JiuWenSwarm._build_interactive_input_from_answers(
        request_id, answers, source="confirm_interrupt",
    )
    inputs = {"query": answer}
    if not adapter._prepare_confirmation_resume(
        fixture.session.get_session_id(), {"request_id": request_id}, inputs,
    ):
        return None
    return inputs


async def apply_answers(fixture, answers, *, request_id=None):
    inputs = prepare_resume_inputs(fixture, answers, request_id=request_id)
    if inputs is None:
        return {"success": False, "status": "error", "reason": "expert_confirmation_expired"}
    # Actual ReAct clears its interruption state before calling the Rail.
    fixture.session.update_state({INTERRUPTION_KEY: None})
    fixture.ctx.extra[RESUME_USER_INPUT_KEY] = inputs["query"]
    await fixture.rail.before_tool_call(fixture.ctx)
    if fixture.ctx.extra.get("_skip_tool"):
        return fixture.ctx.inputs.tool_result
    return await fixture.tool.invoke({"query": "请专家整理资料"}, session=fixture.session)


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [1, 2, 3, 5, 6, 20, 21])
async def test_confirmation_card_counts_all_valid_candidates_and_preserves_core_contract(monkeypatch, candidate_count):
    fixture = await build_pending_fixture(monkeypatch, candidate_count)
    question = fixture.payload["questions"][0]
    assert fixture.payload["event_type"] == "chat.ask_user_question"
    assert fixture.payload["source"] == "confirm_interrupt"
    assert fixture.payload["request_id"].startswith("symphony-plan:")
    assert fixture.payload["request_id"] != fixture.call.id
    assert question["tool_call_id"] == fixture.call.id
    assert question["question"].startswith(
        "让「专家1」接着帮你？\n"
        "小艺Work为你找到了这位专家。你同意后，专家会接着这段对话把任务做下去，"
        "之后也会继续回复你。想换回小艺Work，随时可以在输入框选择。\n"
    )
    assert "任务目标：" not in question["question"]
    assert "专家1" in question["question"].splitlines()
    assert "分析第1部分资料" not in question["question"]
    assert "主理人" not in question["question"]
    assert "汇总" not in question["question"]
    assert "依赖" not in question["question"]
    assert [option["label"] for option in question["options"]] == [
        "交给这位专家", "还是小艺Work来做",
    ]
    assert [option["value"] for option in question["options"]] == ["approve", "decline"]
    assert all(not option["description"] for option in question["options"])
    assert ("当前可用的专家还不多，去「专家·专家团」召唤更多专家吧。" in question["question"]) is (candidate_count <= 5)
    assert f"{candidate_count} 位" not in question["question"]
    assert fixture.round.candidate_count == candidate_count
    candidates = json.loads(fixture.model.invoke.await_args_list[0].kwargs["messages"][1]["content"])["experts"]
    assert len(candidates) == candidate_count
    assert fixture.model.invoke.await_count == 1
    assert len(fixture.round.pending_plan["nodes"]) == 1
    assert question["expert_plan"] == {
        "plan_id": fixture.payload["request_id"].removeprefix("symphony-plan:"),
        "candidate_count": candidate_count,
        "display": {
            "title": "让「专家1」接着帮你？",
            "description": question["question"].splitlines()[1],
            "feedback_placeholder": "想换位专家或补充要求？告诉小艺Work…",
            **({"candidate_hint": "当前可用的专家还不多，去「专家·专家团」召唤更多专家吧。"}
               if candidate_count <= 5 else {}),
        },
        "members": [{
            "id": "e0", "name": "专家1", "source": "local",
            "summary": "资料分析",
        }],
    }
    fixture.bind.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source, description, expected", [
    ("local", "  擅长市场趋势与竞品分析。  ", "擅长市场趋势与竞品分析。"),
    ("cache", "甲" * 80, "甲" * 80),
    ("local", "甲" * 81, "甲" * 79 + "…"),
    ("cache", " \n\t ", None),
])
async def test_single_expert_description_fits_card_without_changing_execution(
    monkeypatch, source, description, expected,
):
    fixture = await build_pending_fixture(
        monkeypatch, candidate_count=3, sources={"e0": source}, descriptions={"e0": description},
    )
    question = fixture.payload["questions"][0]
    member = question["expert_plan"]["members"][0]
    assert member["source"] == source
    assert member.get("summary") == expected
    if expected is None:
        assert "summary" not in member
    assert fixture.round._selected_experts["e0"].description == description
    assert fixture.round.confirmation_display(question["expert_plan"]["plan_id"]) == question["expert_plan"]
    fixture.bind.assert_not_awaited()
    ready = await apply_answers(fixture, [{"selected_options": ["交给这位专家"]}])
    assert ready["status"] == "handed_off"
    assert ready["expert"] == {"id": "e0", "name": "专家1", "source": source}
    assert "planned_graph" not in ready and "bindings" not in ready
    fixture.handoff.assert_awaited_once()
    fixture.bind.assert_not_awaited()
    assert "summary" not in json.dumps(ready, ensure_ascii=False)
    fixture.model.invoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirmation_uses_actual_selected_package_assets(monkeypatch):
    fixture = await build_pending_fixture(
        monkeypatch, candidate_count=3, selected_count=2,
        sources={"e0": "cache", "e1": "local"},
        avatars={"e0": "avatars/cache.png", "e1": "https://example.com/local.png"},
    )
    members = fixture.payload["questions"][0]["expert_plan"]["members"]
    assert fixture.payload["questions"][0]["question"].startswith(
        "请这些专家一起帮你？\n"
        "小艺Work为你找到了几位专家。你同意后，小艺Work作为团队主理人，"
        "会协调他们按下面的分工一起完成这件事。\n"
    )
    assert fixture.payload["questions"][0]["options"][0]["label"] == "请他们一起做"
    assert [(member["id"], member["source"], member["avatar"]) for member in members] == [
        ("e0", "cache", "avatars/cache.png"),
        ("e1", "local", "https://example.com/local.png"),
    ]
    assert "当前可用的专家还不多，去「专家·专家团」召唤更多专家吧。" in fixture.payload["questions"][0]["question"]
    fixture.bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_summary_limits_and_failure_leave_original_tasks_intact(monkeypatch):
    fixture = await build_pending_fixture(
        monkeypatch, candidate_count=2, selected_count=2,
        summaries={"e0": "甲" * 80, "e1": "乙" * 81},
    )
    question = fixture.payload["questions"][0]
    assert question["expert_plan"]["members"][0]["summary"] == "甲" * 80
    assert "summary" not in question["expert_plan"]["members"][1]
    assert "专家2：整理第2部分资料" not in question["question"]
    assert fixture.round.pending_plan["nodes"]["e1"]["task"] == "整理第2部分资料"
    ready = await apply_answers(fixture, [{"selected_options": ["交给专家处理"]}])
    assert ready["planned_graph"]["graph"]["nodes"]["e1"]["task"] == "整理第2部分资料"
    assert "summary" not in json.dumps(ready, ensure_ascii=False)
    assert "甲" * 80 not in fixture.round.guidance()
    from openjiuwen.harness.tools.subagent.task_manager import TaskRequest
    tasks = await fixture.round.prepare_dispatch("task-e0", [
        TaskRequest(expert_team._subagent_type("e0"), "整理第1部分资料"),
    ], [])
    task_context = tasks[0]["task_description"]
    assert "甲" * 80 not in task_context
    assert "整理第1部分资料" in task_context
    fixture.round.rollback_dispatch("task-e0")

    missing = await build_pending_fixture(monkeypatch, selected_count=2, summaries={})
    assert missing.payload["questions"][0]["expert_plan"]["members"] == [
        {"id": "e0", "name": "专家1", "source": "local"},
        {"id": "e1", "name": "专家2", "source": "local"},
    ]
    assert "整理第1部分资料" not in missing.payload["questions"][0]["question"]
    assert missing.model.invoke.await_count == 1
    missing.bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_cancels_pending_planning_model_call(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hanging_invoke(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(expert_team, "scan_local_experts", AsyncMock(return_value={
        "e0": expert_team.LocalExpert("e0", "专家1", "资料分析", object()),
    }))
    monkeypatch.setattr(expert_team, "_subagent_config", lambda *_args: object())
    agent = SimpleNamespace(ability_manager=SimpleNamespace(get=lambda _: None))
    round_state = expert_team.DynamicExpertRound(agent, "整理资料", SimpleNamespace(invoke=hanging_invoke))
    task = asyncio.create_task(round_state.compose("整理资料"))
    await started.wait()
    await round_state.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and round_state.pending_plan is None


@pytest.mark.asyncio
async def test_confirmation_resumes_saved_plan_once_and_returns_compatible_ready(monkeypatch):
    fixture = await build_pending_fixture(monkeypatch, selected_count=2)
    original_plan = fixture.round.pending_plan
    assert fixture.round.plan is None
    assert (await fixture.tool.invoke({}, session=fixture.session))["reason"] == "expert_confirmation_required"
    gate = expert_team.DynamicExpertRail(lambda _: fixture.round)
    gate.init(fixture.agent)
    ctx = AgentCallbackContext(agent=fixture.agent, session=fixture.session, inputs=ToolCallInputs(
        tool_call=ToolCall(id="task-before-approve", name="task_tool", type="function", arguments="{}"),
        tool_name="task_tool",
        tool_args={"subagent_type": expert_team._subagent_type("e0")},
    ))
    await gate.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"]
    ready = await apply_answers(fixture, [{"selected_options": ["请他们一起做"]}])
    assert ready == {
        "success": True, "status": "ready", "planned_graph": {"graph": original_plan},
        "bindings": {key: expert_team._subagent_type(key) for key in ("e0", "e1")},
        "expert_assets": {key: {"source": "local"} for key in ("e0", "e1")},
    }
    assert fixture.round.plan is original_plan
    repeated = await apply_answers(fixture, [{"selected_options": ["确认并开始"]}])
    assert repeated["reason"] == "expert_confirmation_expired"
    fixture.scan.assert_awaited_once()
    assert fixture.model.invoke.await_count == 1
    fixture.bind.assert_awaited_once()
    assert fixture.bind.await_args.args[1].subagents == ["e0", "e1"]
    fixture.handoff.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["交给这位专家", "确认并切换专家", "召唤专家处理", "交给专家处理"])
async def test_single_expert_confirmation_hands_off_once_without_delegation(monkeypatch, label):
    fixture = await build_pending_fixture(monkeypatch, sources={"e0": "cache"}, avatars={"e0": "avatar.png"})
    selected = fixture.round._selected_experts["e0"]
    fixture.scan.return_value = {}  # Confirmation consumes the frozen selection, never rescans.
    result = await apply_answers(fixture, [{"selected_options": [label]}])
    assert result == {
        "success": True, "status": "handed_off",
        "plan_id": fixture.payload["request_id"].removeprefix("symphony-plan:"),
        "expert": {"id": "e0", "name": "专家1", "source": "cache", "avatar": "avatar.png"},
    }
    fixture.handoff.assert_awaited_once_with(fixture.round, selected)
    fixture.bind.assert_not_awaited()
    assert fixture.round.plan is None and fixture.round._bindings == {} and fixture.round._refs == []
    assert "task_tool" not in fixture.round.guidance()
    assert not fixture.ctx.has_force_finish_request
    assert (await apply_answers(fixture, [{"selected_options": [label]}]))["reason"] == "expert_confirmation_expired"
    fixture.handoff.assert_awaited_once()
    await fixture.round.close()
    expert_team.unapply_extension_hot.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_expert_handoff_failure_force_finishes_without_running_original_task(monkeypatch):
    fixture = await build_pending_fixture(monkeypatch)
    fixture.handoff.side_effect = RuntimeError("expert_metadata_write_failed")
    result = await apply_answers(fixture, [{"selected_options": ["交给这位专家"]}])
    assert result == {
        "success": False, "status": "handoff_failed", "reason": "expert_metadata_write_failed",
        "plan_id": fixture.payload["request_id"].removeprefix("symphony-plan:"),
    }
    assert fixture.ctx.has_force_finish_request
    assert fixture.ctx.consume_force_finish().result == {
        "output": "这位专家暂时没能接手，这次任务没有继续执行。请再试一次。",
        "result_type": "answer",
    }
    assert fixture.round.pending_plan is None and fixture.round._selected_experts == {}
    assert fixture.session.get_state(expert_team.CONFIRMATION_STATE_KEY) is None
    assert "重试" in fixture.round.guidance()
    fixture.bind.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("answers, expected", [
    ([{"selected_options": ["还是小艺Work来做"]}], "declined"),
    ([{"selected_options": ["不用专家，由小艺Work继续"]}], "declined"),
    ([{"selected_options": ["不用专家，继续"]}], "declined"),
    ([{"selected_options": [], "custom_input": "请把范围改成国内"}], "revised"),
    ([], "declined"),
    ([{"selected_options": ["永久记住"]}], "error"),
])
async def test_decline_revision_skip_and_permission_grants_do_not_bind(monkeypatch, answers, expected):
    fixture = await build_pending_fixture(
        monkeypatch, revision_summaries={"e0": "根据补充条件重新分析资料并确定后续工作的重点与结论。"},
    )
    old_plan_id = fixture.payload["request_id"]
    if expected == "revised":
        with pytest.raises(AbortError) as interrupted:
            await apply_answers(fixture, answers)
        new_plan_id = interrupted.value.cause.request.metadata["dynamic_expert_plan_id"]
        assert new_plan_id != old_plan_id.removeprefix("symphony-plan:")
        assert fixture.round.feedback == "请把范围改成国内"
        assert fixture.round.pending_plan is not None
        assert fixture.model.invoke.await_count == 2
        assert fixture.scan.await_count == 2
        assert fixture.round.confirmation_display(new_plan_id)["members"][0]["summary"] == "资料分析"
    else:
        result = await apply_answers(fixture, answers)
        assert result["status"] == expected
        assert fixture.ctx.has_force_finish_request is (expected == "error")
        if expected == "declined":
            assert fixture.round._closed and fixture.round.pending_plan is None
            assert fixture.round._selected_configs == {}
    fixture.bind.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["session", "call", "plan", "restart"])
async def test_stale_or_mismatched_confirmation_cannot_bind_or_replan(monkeypatch, mismatch):
    fixture = await build_pending_fixture(monkeypatch)
    if mismatch == "session":
        fixture.round.session_id = "another-session"
    elif mismatch == "call":
        fixture.call.id = "old-call"
    elif mismatch == "plan":
        metadata = fixture.session.get_state(expert_team.CONFIRMATION_STATE_KEY)
        metadata["dynamic_expert_plan_id"] = "old-plan"
        fixture.session.update_state({expert_team.CONFIRMATION_STATE_KEY: metadata})
    else:
        fixture.rail = DynamicExpertConfirmationRail(lambda _: None)
    result = await apply_answers(fixture, [{"selected_options": ["确认并开始"]}])
    assert result["reason"] == "expert_confirmation_expired"
    fixture.bind.assert_not_awaited()
    fixture.scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_zero_candidates_ask_for_no_plan_choice(monkeypatch):
    fixture = await build_pending_fixture(monkeypatch, candidate_count=0)
    assert fixture.round.candidate_count == 0
    assert fixture.payload["request_id"].startswith("symphony-no-plan:")
    assert fixture.payload["questions"][0]["expert_no_plan"] == {
        "confirmation_id": fixture.payload["request_id"].removeprefix("symphony-no-plan:"),
        "candidate_count": 0,
        "display": {
            "title": "这次没找到合适的专家",
            "description": "当前可用的专家还不多，去「专家·专家团」召唤更多专家吧。",
        },
    }
    assert (await fixture.tool.invoke({}, session=fixture.session))["reason"] == "expert_confirmation_required"
    fixture.model.invoke.assert_not_awaited()
    fixture.bind.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [0, 1, 20, 21, 100])
async def test_no_plan_choice_ignores_candidate_count_and_preserves_wire_metadata(monkeypatch, candidate_count):
    fixture = await build_pending_fixture(
        monkeypatch, candidate_count=candidate_count, first_no_plan=True,
    )
    assert fixture.round.candidate_count == candidate_count
    question = fixture.payload["questions"][0]
    assert fixture.payload["request_id"].startswith("symphony-no-plan:")
    assert question["question"] == (
        "这次没找到合适的专家\n"
        "当前可用的专家还不多，去「专家·专家团」召唤更多专家吧。"
    )
    assert [item["label"] for item in question["options"]] == ["小艺Work继续", "去召唤专家"]
    assert [item["value"] for item in question["options"]] == ["continue", "summon"]
    assert question["expert_no_plan"] == {
        "confirmation_id": fixture.payload["request_id"].removeprefix("symphony-no-plan:"),
        "candidate_count": candidate_count,
        "display": {
            "title": "这次没找到合适的专家",
            "description": "当前可用的专家还不多，去「专家·专家团」召唤更多专家吧。",
        },
    }
    assert "expert_plan" not in question
    assert fixture.session.get_state(expert_team.CONFIRMATION_STATE_KEY)["dynamic_expert_no_plan_id"] == (
        question["expert_no_plan"]["confirmation_id"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [1, 21])
@pytest.mark.parametrize("label, decision, forced", [
    ("continue", "continue", False),
    ("summon", "summon", True),
    ("小艺Work继续", "continue", False),
    ("去召唤专家", "summon", True),
])
async def test_no_plan_choice_consumed_once_and_preserves_tool_result(
    monkeypatch, candidate_count, label, decision, forced,
):
    fixture = await build_pending_fixture(
        monkeypatch, candidate_count=candidate_count, first_no_plan=True,
    )
    confirmation_id = fixture.payload["questions"][0]["expert_no_plan"]["confirmation_id"]
    result = await apply_answers(fixture, [{"selected_options": [label]}])
    assert result == {
        "success": True, "status": "no_plan", "reason": "no_suitable_plan",
        "decision": decision, "confirmation_id": confirmation_id, "candidate_count": candidate_count,
    }
    assert fixture.ctx.has_force_finish_request is forced
    if forced:
        assert fixture.ctx.consume_force_finish().result["result_type"] == "answer"
        assert "本轮已结束" in fixture.round.guidance()
    assert fixture.session.get_state(expert_team.CONFIRMATION_STATE_KEY) is None
    assert (await apply_answers(fixture, [{"selected_options": [label]}]))["reason"] == (
        "expert_confirmation_expired"
    )
    fixture.bind.assert_not_awaited()
    fixture.handoff.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [0, 1, 21])
async def test_no_plan_without_interaction_keeps_original_chat_flow(monkeypatch, candidate_count):
    fixture = await build_pending_fixture(
        monkeypatch, candidate_count=candidate_count, first_no_plan=True,
        supports_user_interaction=False,
    )
    assert fixture.payload is None
    assert await fixture.tool.invoke({}, session=fixture.session) == {
        "success": True, "status": "no_plan",
        "reason": "no_available_experts" if candidate_count == 0 else "no_suitable_plan",
    }


@pytest.mark.asyncio
async def test_planning_error_does_not_open_no_plan_choice(monkeypatch):
    fixture = await build_pending_fixture(
        monkeypatch, candidate_count=1, first_planning_error=True,
    )
    assert fixture.payload is None
    assert (await fixture.tool.invoke({}, session=fixture.session))["reason"] == "expert_planning_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [0, 1, 20, 21, 100])
async def test_revision_to_no_plan_asks_again_and_old_answer_expires(monkeypatch, candidate_count):
    fixture = await build_pending_fixture(
        monkeypatch, candidate_count=max(candidate_count, 1), revision_no_plan=True,
    )
    old_id = fixture.payload["request_id"]
    if candidate_count == 0:
        fixture.scan.return_value = {}
    with pytest.raises(AbortError) as interrupted:
        await apply_answers(fixture, [{"selected_options": [], "custom_input": "只看本地专家"}])
    request = interrupted.value.cause.request
    assert "dynamic_expert_no_plan_id" in request.metadata
    assert request.metadata["expert_no_plan"]["candidate_count"] == candidate_count
    handler = ToolInterruptHandler(fixture.agent)
    state, requests = handler.build_interrupt_state(
        [(interrupted.value.cause, None)], [fixture.call],
        AssistantMessage(content="", tool_calls=[fixture.call]), 0,
    )
    fixture.session.update_state({INTERRUPTION_KEY: state})
    fixture.payload = convert_interactions_to_ask_user_question([
        SimpleNamespace(id=request_id, value=value) for request_id, value in requests
    ])
    assert fixture.payload["request_id"].startswith("symphony-no-plan:")
    assert fixture.payload["request_id"] != old_id
    assert prepare_resume_inputs(fixture, [{"selected_options": ["去召唤专家"]}], request_id=old_id) is None
    result = await apply_answers(fixture, [{"selected_options": ["去召唤专家"]}])
    assert result["decision"] == "summon"
    assert fixture.ctx.has_force_finish_request


@pytest.mark.asyncio
async def test_invalid_no_plan_answer_cannot_silently_choose_an_option(monkeypatch):
    fixture = await build_pending_fixture(monkeypatch, candidate_count=1, first_no_plan=True)
    result = await apply_answers(fixture, [{"selected_options": []}])
    assert result["reason"] == "expert_confirmation_expired"
    assert fixture.ctx.has_force_finish_request
    assert "decision" not in fixture.round.response


@pytest.mark.asyncio
async def test_summon_choice_emits_successful_tool_result_before_turn_finish(monkeypatch):
    fixture = await build_pending_fixture(monkeypatch, candidate_count=1, first_no_plan=True)
    result = await apply_answers(fixture, [{"selected_options": ["去召唤专家"]}])
    emitted = AsyncMock()
    monkeypatch.setattr(fixture.session, "write_stream", emitted)
    await JiuSwarmStreamEventRail().after_tool_call(fixture.ctx)
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(emitted.await_args_list[0].args[0])
    assert parsed["event_type"] == "chat.tool_result"
    assert parsed["raw_output"] == result
    assert parsed["success"] is True
    assert parsed.get("is_error") is not True
    assert fixture.ctx.has_force_finish_request


@pytest.mark.asyncio
async def test_stopping_pending_round_discards_templates_and_confirmation_state(monkeypatch):
    fixture = await build_pending_fixture(monkeypatch)
    await fixture.round.close()
    assert fixture.session.get_state(expert_team.CONFIRMATION_STATE_KEY) is None
    assert fixture.round.pending_plan is None and fixture.round._selected_configs == {}
    result = await apply_answers(fixture, [{"selected_options": ["确认并开始"]}])
    assert result["reason"] == "expert_confirmation_expired"
    fixture.bind.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("source", "cancel_claimed"), [
    ("permission_interrupt", False), ("confirm_interrupt", False),
    ("ask_user_interrupt", False), ("permission_interrupt", True),
])
async def test_raw_core_approval_cannot_bypass_plan_admission(monkeypatch, source, cancel_claimed):
    fixture = await build_pending_fixture(monkeypatch)
    if cancel_claimed:
        admitted = prepare_resume_inputs(fixture, [{"selected_options": ["取消"]}])
        assert admitted is not None
        cancel_nonce = admitted["query"].user_inputs[fixture.call.id][expert_team.CONFIRMATION_NONCE_KEY]
    label = "本次允许"
    raw_answer = JiuWenSwarm._build_interactive_input_from_answers(
        fixture.call.id, [{"selected_options": [label]}], source=source,
    )
    if source != "ask_user_interrupt":
        assert raw_answer.user_inputs[fixture.call.id]["approved"] is True
    fixture.session.update_state({INTERRUPTION_KEY: None})
    fixture.ctx.extra[RESUME_USER_INPUT_KEY] = raw_answer
    await fixture.rail.before_tool_call(fixture.ctx)
    fixture.bind.assert_not_awaited()
    assert fixture.ctx.inputs.tool_result["reason"] == "expert_confirmation_expired"
    assert fixture.round.pending_plan is not None
    if cancel_claimed:
        assert fixture.round._confirmation_nonce == cancel_nonce
    await fixture.round.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unmatched", ["current_token", "stale_token", "invalid_saved_plan"])
async def test_unmatched_resume_only_replays_valid_plan_for_other_tools(monkeypatch, unmatched):
    fixture = await build_pending_fixture(monkeypatch)
    request_id = fixture.payload["request_id"]
    if unmatched == "stale_token":
        request_id = "symphony-plan:expired"
    elif unmatched == "invalid_saved_plan":
        request_id = "other-tool-call"
        metadata = fixture.session.get_state(expert_team.CONFIRMATION_STATE_KEY)
        metadata["dynamic_expert_plan_id"] = "expired"
        fixture.session.update_state({expert_team.CONFIRMATION_STATE_KEY: metadata})
    fixture.ctx.extra[RESUME_USER_INPUT_KEY] = JiuWenSwarm._build_interactive_input_from_answers(
        request_id, [{"selected_options": ["本次允许"]}], source="permission_interrupt",
    )
    fixture.session.update_state({INTERRUPTION_KEY: None})
    await fixture.rail.before_tool_call(fixture.ctx)
    assert fixture.ctx.inputs.tool_result["reason"] == "expert_confirmation_expired"
    assert fixture.ctx.has_force_finish_request
    fixture.scan.assert_awaited_once()
    assert fixture.model.invoke.await_count == 1
    fixture.bind.assert_not_awaited()
    await fixture.round.close()


@pytest.mark.asyncio
async def test_binding_requires_admitted_confirmation_credential(monkeypatch):
    fixture = await build_pending_fixture(monkeypatch)
    result = await fixture.round.confirm_and_bind(
        fixture.session.get_session_id(), fixture.call.id, fixture.round._confirmation,
        confirmation_nonce="",
    )
    assert result["reason"] == "expert_confirmation_expired"
    fixture.bind.assert_not_awaited()
    await fixture.round.close()


@pytest.mark.asyncio
async def test_partial_binding_failure_uses_pinned_core_atomic_rollback(monkeypatch):
    from openjiuwen.harness import extension_binder
    from openjiuwen.harness.resources.extension_resolver import ResourceKind, ResourceRef

    fixture = await build_pending_fixture(monkeypatch, selected_count=2)
    ref = ResourceRef(kind=ResourceKind.SUBAGENT, identity="e0")
    def bind_one(_agent, config):
        if config == "e1":
            raise RuntimeError("second expert failed")
        return ref
    rollback = AsyncMock()
    monkeypatch.setattr(extension_binder, "_bind_subagent", bind_one)
    monkeypatch.setattr(extension_binder, "_unbind", rollback)
    monkeypatch.setattr(expert_team, "apply_extension_hot", extension_binder.apply_extension_hot)
    result = await apply_answers(fixture, [{"selected_options": ["确认并开始"]}])
    assert result["reason"] == "expert_binding_failed"
    rollback.assert_awaited_once_with(fixture.agent, ref)
    assert fixture.round.plan is None and fixture.round.pending_plan is None
    assert fixture.round._refs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value, expected", [
    ("approve", "ready"), ("交给这位专家", "ready"), ("请他们一起做", "ready"),
    ("确认并切换专家", "ready"), ("decline", "declined"), ("还是小艺Work来做", "declined"),
])
async def test_stable_and_legacy_plan_options_resume_the_same_confirmation(monkeypatch, value, expected):
    fixture = await build_pending_fixture(monkeypatch, selected_count=2)
    result = await apply_answers(fixture, [{"selected_options": [value]}])
    assert result["status"] == expected
    assert fixture.bind.await_count == (1 if expected == "ready" else 0)
    await fixture.round.close()


@pytest.mark.asyncio
async def test_display_metadata_matches_plain_text_and_stays_out_of_execution(monkeypatch):
    fixture = await build_pending_fixture(monkeypatch, selected_count=2)
    question = fixture.payload["questions"][0]
    display = question["expert_plan"]["display"]
    assert display == {
        "title": "请这些专家一起帮你？",
        "description": question["question"].splitlines()[1],
        "feedback_placeholder": "想调整人选或分工？告诉小艺Work…",
        "candidate_hint": "当前可用的专家还不多，去「专家·专家团」召唤更多专家吧。",
    }
    ready = await apply_answers(fixture, [{"selected_options": ["approve"]}])
    assert "display" not in ready and "feedback_placeholder" not in json.dumps(ready, ensure_ascii=False)
    await fixture.round.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["session", "call", "confirmation", "restart"])
async def test_no_plan_confirmation_rejects_stale_identity(monkeypatch, mismatch):
    fixture = await build_pending_fixture(monkeypatch, candidate_count=1, first_no_plan=True)
    if mismatch == "session":
        fixture.round.session_id = "another-session"
    elif mismatch == "call":
        fixture.call.id = "old-call"
    elif mismatch == "confirmation":
        metadata = fixture.session.get_state(expert_team.CONFIRMATION_STATE_KEY)
        fixture.session.update_state({expert_team.CONFIRMATION_STATE_KEY: {
            **metadata, "dynamic_expert_no_plan_id": "expired",
        }})
    else:
        fixture.rail = DynamicExpertConfirmationRail(lambda _: None)
    result = await apply_answers(fixture, [{"selected_options": ["continue"]}])
    assert result["reason"] == "expert_confirmation_expired"
    assert "decision" not in fixture.round.response
    fixture.scan.assert_awaited_once()
    fixture.bind.assert_not_awaited()
    fixture.handoff.assert_not_awaited()
    await fixture.round.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("no_plan", [False, True])
async def test_confirmation_registration_keeps_identity_and_consumed_nonce(monkeypatch, no_plan):
    fixture = await build_pending_fixture(monkeypatch, candidate_count=1, first_no_plan=no_plan)
    register = fixture.round.request_no_plan_confirmation if no_plan else fixture.round.request_confirmation
    metadata = dict(fixture.round._confirmation)
    assert register(fixture.round.session_id, fixture.call.id, session=fixture.session) == metadata
    assert register("another-session", fixture.call.id, session=fixture.session) is None
    assert register(fixture.round.session_id, "another-call", session=fixture.session) is None
    nonce = fixture.round.claim_confirmation_resume(fixture.round.session_id, fixture.call.id, metadata)
    assert nonce
    assert register(fixture.round.session_id, fixture.call.id, session=fixture.session) == metadata
    assert fixture.round._confirmation_nonce == nonce
    assert fixture.round.claim_confirmation_resume(fixture.round.session_id, fixture.call.id, metadata) is None
    await fixture.round.close()
