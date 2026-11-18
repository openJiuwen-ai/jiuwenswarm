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


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_send', [False, True])
async def test_answers_are_visible_before_native_resume_and_rollback_on_failure(service, push, fail_send):
    group = await create(service)
    gid = group['group_id']
    owner = QuestionOwner()
    target = register_question_target(gid, gid, 'advisor', owner)
    store = GroupQuestions(gid)
    await store.ask({'request_id': 'native', 'questions': [{'question': 'Market?'}]}, target,
                    'advisor', None, client_message_id='original-org')
    key = next(iter(store.read()))
    answers = [{'question': 'Market?', 'custom_input': 'China'}]

    async def resume(query):
        record = store.read()[key]
        assert record['status'] == 'answered' and record['answers'] == answers
        assert record['client_message_id'] == 'original-org'
        if fail_send:
            raise RuntimeError('queue unavailable')

    owner.send.side_effect = resume
    if fail_send:
        with pytest.raises(RuntimeError, match='queue unavailable'):
            await store.answer(key, answers)
        assert store.read()[key]['status'] == 'pending'
        assert 'answers' not in store.read()[key]
    else:
        await store.answer(key, answers)
        await store.answer(key, answers)
        owner.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['agent_template', 'agent_group', 'mixed', 'private'])
async def test_direct_clarification_does_not_acquire_organization_binding(service, push, kind):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs
    from jiuwenswarm.agents.swarm.group_turn import TURN_KEY
    from jiuwenswarm.agents.harness.common.rails.ask_user_contract import build_ask_user_interactive_input

    members = [{'kind': 'agent_template' if kind == 'agent_template' else 'agent_group',
                'id': 'one', 'member_name': 'advisor'}]
    if kind == 'mixed':
        members.append({'kind': 'agent_template', 'id': 'two', 'member_name': 'writer'})
    group = await create(service, members=members)
    gid = group['group_id']
    sid = 'private-session' if kind == 'private' else gid
    owner = QuestionOwner()
    rail = GroupAskUserRail(SimpleNamespace(session_id=sid, member_name='advisor'), {'group_id': gid})
    target = register_question_target(gid, sid, 'advisor', owner)
    store = GroupQuestions(gid)
    await store.ask({'request_id': 'native', 'questions': [{'question': 'Market?'}]}, target, 'advisor', None)
    query = build_ask_user_interactive_input('native', [{'question': 'Market?', 'custom_input': 'China'}])
    ctx = AgentCallbackContext(agent=owner, inputs=InvokeInputs(query=query))
    await rail.before_invoke(ctx)
    assert TURN_KEY not in ctx.extra


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['native_id', 'target', 'member', 'private_execution'])
async def test_resume_does_not_restore_another_questions_org(service, push, mismatch):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs
    from jiuwenswarm.agents.swarm.group_turn import TURN_KEY
    from jiuwenswarm.agents.harness.common.rails.ask_user_contract import build_ask_user_interactive_input

    group = await create(service)
    gid = group['group_id']
    owner = QuestionOwner()
    target = register_question_target(gid, gid, 'advisor', owner)
    store = GroupQuestions(gid)
    await store.ask({'request_id': 'native', 'questions': [{'question': 'Market?'}]},
                    'other-target' if mismatch == 'target' else target,
                    'other-member' if mismatch == 'member' else 'advisor',
                    'private-execution' if mismatch == 'private_execution' else None,
                    client_message_id='other-org')
    query = build_ask_user_interactive_input('unrelated' if mismatch == 'native_id' else 'native',
                                             [{'question': 'Market?', 'custom_input': 'China'}])
    ctx = AgentCallbackContext(agent=owner, inputs=InvokeInputs(query=query))
    rail = GroupAskUserRail(SimpleNamespace(session_id=gid, member_name='advisor'), {})
    await rail.before_invoke(ctx)
    assert TURN_KEY not in ctx.extra
