"""Group ask-user ownership, browser recovery and native interrupt resumption."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult

from jiuwenswarm.agents.swarm.group_questions import GroupAskUserRail
from jiuwenswarm.server.runtime.group_questions import GroupQuestions, register_question_target
from jiuwenswarm.server.runtime.group_execution import GroupExecutions
from jiuwenswarm.server.runtime import group_service as gs
from tests.unit_tests.server.test_group_service import service, create
from tests.unit_tests.agentserver.test_report_issue import issue_api  # noqa: F401


class QuestionOwner:
    def __init__(self):
        self.send = AsyncMock()
        self.send_input = AsyncMock(side_effect=RuntimeError("interaction_terminated"))


@pytest.mark.asyncio
async def test_restored_question_binds_owner_before_another_invoke(service, push, monkeypatch):
    from jiuwenswarm.server.runtime import group_questions as questions

    group = await create(service)
    context = SimpleNamespace(session_id=group["group_id"], member_name="advisor", language="cn")
    owner = QuestionOwner()
    target = register_question_target(group["group_id"], group["group_id"], "advisor", owner)
    store = GroupQuestions(group["group_id"])
    await store.ask({"request_id": "restored", "questions": [{"question": "Budget?"}]}, target, "advisor", None)
    monkeypatch.delitem(questions._targets, target)
    restored = QuestionOwner()
    restored.ability_manager = Mock()
    rail = GroupAskUserRail(context, {})
    rail.init(restored)
    pending = store.pending()
    assert len(pending) == 1
    await store.answer(pending[0]["request_id"], [{"question": "Budget?", "custom_input": "2000"}])
    restored.send.assert_awaited_once()
    owner.send.assert_not_awaited()


@pytest.fixture
def push(monkeypatch):
    from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(WebSocketGatewayPushTransport, "send_push", sender)
    return sender


@pytest.mark.asyncio
async def test_native_ask_rail_publishes_and_answer_resumes_exact_owner(service, push):
    group = await create(service)
    owner = QuestionOwner()
    context = SimpleNamespace(session_id=group["group_id"], member_name="advisor", language="cn")
    rail = GroupAskUserRail(context, {})
    ctx = SimpleNamespace(agent=owner)
    await rail.before_invoke(ctx)
    call = ToolCall(id="native-one", type="function", name="ask_user", arguments=json.dumps({
        "questions": [{"question": "预算是多少？", "header": "预算", "options": [{"label": "经济"}, {"label": "舒适"}]}],
    }))
    decision = await rail.resolve_interrupt(ctx, call, None)
    assert isinstance(decision, InterruptResult)
    pending = service.get(group["group_id"], user_id="owner", channel_id="web")["pending_questions"]
    assert len(pending) == 1
    question = pending[0]
    assert question["request_id"] != call.id
    assert "target" not in question
    payload = push.await_args.args[0]["payload"]
    assert payload["event_type"] == "chat.ask_user_question"
    assert payload["group_id"] == group["group_id"]
    answers = [{"question": "预算是多少？", "selected_options": ["舒适"], "custom_input": "每人 2000 元"}]
    store = GroupQuestions(group["group_id"])
    result = await store.answer(question["request_id"], answers)
    assert result["accepted"]
    assert owner.send.await_count == 1
    owner.send_input.assert_not_awaited()
    native = owner.send.await_args.args[0]
    assert native.user_inputs[call.id]["answers"]["预算是多少？"] == ["舒适", "每人 2000 元"]
    assert store.pending() == []
    assert (await store.answer(question["request_id"], answers))["accepted"]
    assert owner.send.await_count == 1
    with pytest.raises(gs.GroupError, match="another answer"):
        await store.answer(question["request_id"], [{"question": "预算是多少？", "selected_options": ["经济"]}])


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow_once", "reject"])
@pytest.mark.parametrize("role, member_name", [("leader", "host"), ("teammate", "advisor")])
async def test_group_member_issue_review_resumes_its_own_draft(
    service, push, issue_api, monkeypatch, decision, role, member_name,
):
    from openjiuwen.agent_teams.rails.team_context import TeamHandleKey
    from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
    from openjiuwen.core.single_agent.interrupt.response import ToolCallInterruptRequest, new_interrupt_id
    from openjiuwen.core.single_agent.interrupt.state import InterruptAnswer, RESUME_INTERRUPT_ANSWERS_KEY
    from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec
    from jiuwenswarm.agents.swarm import registry
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.group_execution import GroupTaskPolicyRail, install_execution_policy

    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    registry.register_swarm_providers()
    group = await create(service, members=[{"kind": "agent_template", "id": "one", "member_name": "advisor"}])
    group_id = group["group_id"]
    backend = SimpleNamespace(conversation_mode="group_chat", team_name=group["team_name"])
    context = SwarmBuildContext(
        session_id=group_id, channel_id="web", member_name=member_name, role=role, language="en",
        extras={TeamHandleKey.TEAM_BACKEND: backend},
    )
    spec = DeepAgentSpec()
    install_execution_policy(spec, include_start_task=role == "teammate")
    toolkit, = [tool for tool in spec.tools if tool.type == registry.REPORT_ISSUE]
    lookup, report = toolkit.build(language="en", context=context)
    template = await lookup.invoke({})
    args = {
        "template_id": template["template_id"],
        "title": "Model selection freezes",
        "fields": {
            "description": "The model selector freezes.",
            "reproduction_steps": "Select a model.",
            "expected_behavior": "The chat responds.",
            "actual_behavior": "The chat freezes.",
            "environment": "Windows, Python 3.13",
            "checklist": [template["checklist_options"][0]["label"]],
        },
    }
    owner = QuestionOwner()
    tool_call = ToolCall(id="native-report", type="function", name="report_issue", arguments=json.dumps(args))
    ctx = SimpleNamespace(agent=owner, inputs=SimpleNamespace(tool_call=tool_call), extra={})
    with pytest.raises(ToolInterruptException) as paused:
        await report.invoke(args, _tool_callback_context=ctx)
    native = paused.value.request
    native.interrupt_id = new_interrupt_id()
    ctx.inputs.result = {
        "result_type": "interrupt",
        "state": [{"id": tool_call.id, "value": ToolCallInterruptRequest.from_tool_call(native, tool_call)}],
    }
    await GroupTaskPolicyRail(context).after_invoke(ctx)
    assert not issue_api.posts
    pending, = service.get(group_id, user_id="owner", channel_id="web")["pending_questions"]
    assert pending["member_name"] == member_name
    assert pending["interrupt_id"] == native.interrupt_id
    assert pending["source"] == "confirm_interrupt"
    preview = pending["questions"][0]["issue_report"]
    assert push.await_args.args[0]["payload"]["questions"][0]["issue_report"] == preview

    answers = [{"question": pending["questions"][0]["question"], "selected_options": [decision]}]
    store = GroupQuestions(group_id)
    with pytest.raises(gs.GroupError, match="source"):
        await store.answer(pending["request_id"], answers)
    await store.answer(pending["request_id"], answers, source=pending["source"])
    await store.answer(pending["request_id"], answers, source=pending["source"])
    owner.send.assert_awaited_once()
    query = owner.send.await_args.args[0]
    assert set(query.interrupt_answers) == {native.interrupt_id}
    ctx.extra[RESUME_INTERRUPT_ANSWERS_KEY] = {tool_call.id: {native.interrupt_owner: InterruptAnswer(
        owner=native.interrupt_owner, kind=native.interrupt_kind, request=native,
        value=query.interrupt_answers[native.interrupt_id],
    )}}
    outcome = await report.invoke(args, _tool_callback_context=ctx)
    if decision == "reject":
        assert outcome["status"] == "cancelled"
        assert not issue_api.posts
    else:
        assert outcome["status"] == "created"
        post, = issue_api.posts
        data = json.loads(post.content)
        assert data["title"] == preview["title"]
        assert data["body"] == preview["body"]
        assert data["labels"] == ",".join(preview["labels"])


@pytest.mark.asyncio
async def test_same_native_id_from_concurrent_members_does_not_cross_resume(service, push):
    group = await create(service)
    owners = [QuestionOwner(), QuestionOwner()]
    store = GroupQuestions(group["group_id"])
    for index, owner in enumerate(owners):
        target = register_question_target(group["group_id"], f"private-{index}", "team_leader", owner)
        await store.ask({"request_id": "call-1", "questions": [{"question": "Continue?", "options": []}]},
                        target, f"member-{index}", None)
    pending = store.pending()
    assert len(pending) == 2
    assert pending[0]["request_id"] != pending[1]["request_id"]
    await store.answer(pending[1]["request_id"], [{"question": "Continue?", "selected_options": [], "custom_input": "Yes"}])
    owners[0].send.assert_not_awaited()
    owners[1].send.assert_awaited_once()
    assert len(store.pending()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ended", "missing_owner", "wrong_question", "empty", "resume_error"])
async def test_invalid_or_stale_question_does_not_lose_pending_state(service, push, case):
    group = await create(service, members=[{"kind": "agent_template", "id": "one", "member_name": "advisor"}])
    owner = QuestionOwner()
    store = GroupQuestions(group["group_id"])
    target = register_question_target(group["group_id"], "private", "leader", owner)
    execution = (await GroupExecutions(group["group_id"]).create(["advisor"], "Work", "question", defer_card=True))[0]
    await store.ask({"request_id": "native", "questions": [{"question": "Q?", "options": []}]},
                    "missing" if case == "missing_owner" else target, "advisor", execution["execution_id"])
    question = store.public(next(iter(store.read().values())))
    answers = [{"question": "Q?", "selected_options": [], "custom_input": "Yes"}]
    if case == "ended":
        await GroupExecutions(group["group_id"]).update(execution["execution_id"], status="cancelled")
    elif case == "wrong_question":
        answers[0]["question"] = "Other?"
    elif case == "empty":
        answers[0]["custom_input"] = ""
    elif case == "resume_error":
        owner.send.side_effect = RuntimeError("unavailable")
    with pytest.raises((gs.GroupError, RuntimeError)):
        await store.answer(question["request_id"], answers)
    assert store.read()[question["request_id"]]["status"] == "pending"
    if case != "resume_error":
        owner.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_question_prevents_missing_result_interruption(service, push):
    from jiuwenswarm.agents.swarm.group_execution import GroupTaskPolicyRail

    group = await create(service, members=[{"kind": "agent_template", "id": "one", "member_name": "advisor"}])
    record = (await GroupExecutions(group["group_id"]).create(["advisor"], "Work", "pending", defer_card=True))[0]
    owner = QuestionOwner()
    target = register_question_target(group["group_id"], group["group_id"], "advisor", owner)
    await GroupQuestions(group["group_id"]).ask({"request_id": "native", "questions": [{"question": "Q?"}]},
                                             target, "advisor", record["execution_id"])
    await GroupTaskPolicyRail(SimpleNamespace(session_id=group["group_id"], member_name="advisor")).after_invoke(None)
    assert GroupExecutions(group["group_id"]).get(record["execution_id"])["status"] == "queued"


@pytest.mark.asyncio
async def test_plain_question_uses_existing_normalization_and_failed_push_is_recoverable(service, push):
    group = await create(service)
    owner = QuestionOwner()
    context = SimpleNamespace(session_id=group["group_id"], member_name="advisor", language="cn")
    rail = GroupAskUserRail(context, {})
    ctx = SimpleNamespace(agent=owner)
    await rail.before_invoke(ctx)
    push.side_effect = OSError("offline")
    call = ToolCall(id="plain", type="function", name="ask_user", arguments=json.dumps({"query": "你从哪里出发？"}))
    assert isinstance(await rail.resolve_interrupt(ctx, call, None), InterruptResult)
    pending = GroupQuestions(group["group_id"]).pending()
    assert len(pending) == 1 and pending[0]["questions"][0]["question"] == "你从哪里出发？"
    push.side_effect = None
    answers = [{"question": "你从哪里出发？", "selected_options": [], "custom_input": "上海"}]
    await GroupQuestions(group["group_id"]).answer(pending[0]["request_id"], answers)
    native = owner.send.await_args.args[0]
    resolved = await rail.resolve_interrupt(ctx, call, native.user_inputs[call.id])
    assert not isinstance(resolved, InterruptResult)
    assert "上海" in str(resolved.tool_result)


@pytest.mark.asyncio
async def test_ended_question_owner_is_not_restored_as_an_unanswerable_card(service, push):
    import gc

    group = await create(service)
    owner = QuestionOwner()
    target = register_question_target(group["group_id"], "private", "leader", owner)
    store = GroupQuestions(group["group_id"])
    await store.ask({"request_id": "lost-owner", "questions": [{"question": "Q?"}]}, target, "advisor", None)
    assert len(store.pending()) == 1
    del owner
    gc.collect()
    assert store.pending() == []
    assert len(store.read()) == 1
