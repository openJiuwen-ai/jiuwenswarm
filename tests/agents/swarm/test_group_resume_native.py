"""Exercise persisted answers through NativeHarness and its real task scheduler."""

import asyncio
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from openjiuwen.agent_teams.harness.native_harness import NativeHarness
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.interrupt.state import ToolInterruptEntry, ToolInterruptionState
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent, rail as with_rails
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec

from jiuwenswarm.agents.swarm import group_chat, group_execution as policy, group_org_bootstrap as bootstrap
from jiuwenswarm.agents.swarm import group_route as route, group_turn
from jiuwenswarm.agents.swarm.group_questions import GroupAskUserRail
from jiuwenswarm.server.runtime.group_questions import GroupQuestions, register_question_target
from jiuwenswarm.server.runtime import group_service
from tests.agents.swarm.test_group_org_bootstrap import _Pool
from tests.agents.swarm.test_group_route import env, record, SESSION, _OwnerProcess
from tests.agents.swarm.test_group_turn import notice


def tool_call(name, call_id, arguments):
    return ToolCall(id=call_id, name=name, type='function', arguments=json.dumps(arguments))


@pytest.mark.asyncio
@pytest.mark.parametrize('clarify_again', [False, True])
@pytest.mark.parametrize('clock_shift', [False, True])
async def test_answer_native_scheduler_and_three_proxy_tools_join_one_org(env, monkeypatch, tmp_path, clarify_again, clock_shift, caplog):
    group, owner_backend, _ = env
    members = ['business', 'insight', 'market']
    group.state.update(status='active', channel_id='web', session_id=SESSION,
                       members=[dict(kind='agent_group', id=name, member_name=name) for name in members])
    await record('original', 'Original report', members)
    process = _OwnerProcess()
    monkeypatch.setattr(route, 'psutil', SimpleNamespace(**{**vars(route.psutil), 'Process': lambda *args: process}))
    await route.set_status(SESSION, 'original', 'clarification', expected={'pending'}, **route.owner_process_fields())
    await record('later', 'Later report', members)
    history = tmp_path / 'history.jsonl'
    history.write_text(json.dumps(dict(message_id='trigger', client_message_id='original', sender='user',
                                      mentions=members, body='Original report')))
    backends = {}
    published = AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(message_id='result')))
    for name in members:
        backends[name] = SimpleNamespace(member_name=name, team_name='outer-team', conversation_mode='group_chat',
                                        message_manager=owner_backend.message_manager,
                                        group_conversation=AsyncMock(return_value=SimpleNamespace(history_path=history)),
                                        append_group_message=published)
    monkeypatch.setattr(policy, 'read_group', lambda _: group.state)
    monkeypatch.setattr(policy, 'get_team_backend', lambda ctx: backends[ctx.member_name])
    monkeypatch.setattr(policy, 'GroupExecutions', lambda _: SimpleNamespace(
        reconcile=AsyncMock(), active=lambda _: None, background_runs=lambda _: [], read=lambda: {}))
    monkeypatch.setattr(GroupQuestions, 'publish', AsyncMock())
    monkeypatch.setattr('jiuwenswarm.server.runtime.group_questions.read_group', lambda _: group.state)
    monkeypatch.setattr('jiuwenswarm.server.runtime.group_questions.group_path', group_service.group_path)

    pool = _Pool()
    runtime = SimpleNamespace(pool=pool)
    launches = []
    finish = asyncio.Event()
    collecting = asyncio.Event()

    async def ready(**kwargs):
        member = kwargs['member']['member_name']
        launches.append(member)
        team_id = bootstrap.instance_team_id(SESSION, member)
        pool.add(team_id)
        return team_id

    async def create(**kwargs):
        pool.entries[kwargs['owner_team_id']].agent.team_backend.org_task_manager = SimpleNamespace(
            organization_id=kwargs['organization_id'])

    async def invite(**kwargs):
        pool.entries[kwargs['target_team_id']].agent.team_backend.org_task_manager = SimpleNamespace(
            organization_id=kwargs['organization_id'])

    async def result(*args, **kwargs):
        collecting.set()
        await finish.wait()
        return 'Completed organization report', []

    monkeypatch.setattr(bootstrap, '_organization_runtime', lambda: (
        runtime, SimpleNamespace(release_membership=AsyncMock()), None))
    monkeypatch.setattr(bootstrap, 'ensure_member_instance', ready)
    monkeypatch.setattr(bootstrap, '_owner_creates_organization', create)
    monkeypatch.setattr(bootstrap, '_owner_invites_team', invite)
    for name in ['_record_team_run', '_set_member_instance_silence', '_block_template_expert_creation',
                 '_owner_primes_root_task', '_queue_owner_start_reminder', 'pause_organization_instances',
                 'publish_organization_card']:
        monkeypatch.setattr(bootstrap, name, AsyncMock())
    monkeypatch.setattr(bootstrap, '_await_completed_result', result)
    monkeypatch.setattr(bootstrap, '_JOIN_POLL_SECONDS', 0.001)

    hosts = {}
    seen = {name: [] for name in members}
    prompts = {name: [] for name in members}
    tool_results = {name: [] for name in members}
    launch = tool_call('run_team', 'launch', dict(action='start', run_id='job', content='Report'))
    progress = tool_call('group_send_message', 'progress', dict(content='Other teams are starting', mentions=members[1:]))
    second_question = tool_call('ask_user', 'ask-again', dict(questions=[dict(question='Budget?')]))

    async def start_proxy(name, responses):
        inner = ReActAgent(AgentCard(id=uuid4().hex, name=name + '-inner'))
        host = NativeHarness(DeepAgentSpec(card=AgentCard(id=uuid4().hex, name=name),
                                           enable_sys_operation=False, completion_timeout=None))
        host._react_agent = inner
        host.system_prompt_builder = inner.system_prompt_builder = SystemPromptBuilder()
        monkeypatch.setattr(host, '_ensure_initialized', AsyncMock())
        monkeypatch.setattr(host, 'save_state', Mock())
        monkeypatch.setattr(host, 'clear_state', Mock())
        model_context = SimpleNamespace(add_messages=AsyncMock(), get_messages=lambda: [])
        monkeypatch.setattr(inner, '_init_context', AsyncMock(return_value=model_context))
        monkeypatch.setattr(inner, '_sync_prompt_attachments', AsyncMock())
        monkeypatch.setattr(inner.context_engine, 'save_contexts', AsyncMock())
        ctx = SimpleNamespace(session_id=SESSION, member_name=name)
        ask = GroupAskUserRail(ctx, {})
        for rail in [ask, policy.GroupTaskPolicyRail(ctx)]:
            await host._register_rail_selective(rail)
        proxy = group_chat.RunTeam(backends[name], ctx, {})
        monkeypatch.setattr(proxy, '_run', AsyncMock(side_effect=AssertionError('private team fallback')))
        inner.ability_manager.add_ability(proxy.card, proxy)
        message = policy.GroupMessageTool(backends[name], ctx, 'Post group reply')
        inner.ability_manager.add_ability(message.card, message)
        replies = iter(responses)

        @with_rails(before=AgentCallbackEvent.BEFORE_MODEL_CALL, after=AgentCallbackEvent.AFTER_MODEL_CALL)
        async def model(_agent, callback):
            seen[name].append(callback.extra.get(group_turn.TURN_KEY))
            prompts[name].append(inner.system_prompt_builder.build())
            return next(replies)

        async def collect(callback):
            tool_results[name].append((callback.inputs.tool_name, callback.inputs.tool_result))

        monkeypatch.setattr(inner, '_railed_model_call', MethodType(model, inner))
        await inner.register_callback(AgentCallbackEvent.AFTER_TOOL_CALL, collect, priority=-100)
        done = asyncio.Event()

        async def on_round(kind):
            if kind == 'finished':
                done.set()

        await host.subscribe(on_round=on_round)
        session = create_agent_session(session_id=uuid4().hex, card=inner.card)
        hosts[name] = (host, inner, session, done, proxy)
        await session.pre_run()
        await host.start(session=session)
        return ask

    tasks = []
    try:
        responses = [
            AssistantMessage(content='', tool_calls=[tool_call('group_send_message', 'legacy-ack', dict(content='收到，我会先提问，回答后启动。'))]),
            AssistantMessage(content='', tool_calls=[tool_call('group_send_message', 'explicit-ack', dict(content='收到，我会先提问，回答后启动。', organization_reply='acknowledgement'))]),
            AssistantMessage(content='', tool_calls=[progress, launch]), AssistantMessage(content='Accepted.'),
        ]
        if clarify_again:
            responses.insert(2, AssistantMessage(content='', tool_calls=[second_question]))
        ask = await start_proxy('business', responses)
        host, inner, session, done, _ = hosts['business']
        target = register_question_target(SESSION, SESSION, 'business', host)
        call = tool_call('ask_user', 'ask-original', dict(questions=[dict(question='Market?')]))
        request = InterruptRequest(interrupt_id='original-question', interrupt_kind='ask_user',
                                   interrupt_owner=ask.interrupt_owner)
        inner._hitl_handler.save(ToolInterruptionState(
            ai_message=AssistantMessage(content='', tool_calls=[call]), iteration=0,
            interrupted_tools={call.id: ToolInterruptEntry(tool_call=call, interrupt_requests={call.id: request})},
        ), session)
        store = GroupQuestions(SESSION)
        await store.ask(dict(request_id=call.id, questions=[dict(question='Market?')]), target, 'business', None,
                        client_message_id='original')
        question = store.pending()[0]
        answers = [dict(question='Market?', custom_input='China')]
        if clock_shift:
            process.created += 1
        assert route.public_routes(SESSION)[0]['status'] == 'clarification'
        assert not launches
        await store.answer(question['request_id'], answers)
        await asyncio.wait_for(done.wait(), 10)
        await store.answer(question['request_id'], answers)
        if clarify_again:
            assert not launches
            assert route.candidate(SESSION, 'original')['status'] == 'clarification'
            pending = store.pending()
            assert len(pending) == 1 and store.read()[pending[0]['request_id']]['client_message_id'] == 'original'
            done.clear()
            await store.answer(pending[0]['request_id'], [dict(question='Budget?', custom_input='100')])
            await asyncio.wait_for(done.wait(), 10)
        assert launches == ['business']
        receipts = [result for tool, result in tool_results['business'] if tool == 'group_send_message']
        assert not receipts[0].success
        assert receipts[1].success and receipts[1].data['posted'] is False
        assert route.candidate(SESSION, 'original')['status'] == 'task'
        assert not published.called  # A progress broadcast must not escape.
        notifications = owner_backend.message_manager.send_message.await_args_list
        assert {call.args[1] for call in notifications} == set(members[1:])
        assert all(call.kwargs['meta']['plan_id'] == 'original' for call in notifications)
        for name in members[1:]:
            await start_proxy(name, [AssistantMessage(content='', tool_calls=[second_question]),
                                     AssistantMessage(content='', tool_calls=[launch]), AssistantMessage(content='Joined.')])
            participant, _, _, completed, _ = hosts[name]
            # Deliver the original, persisted public message after the verified route notice.
            await participant.send(notice('trigger'))
            await asyncio.wait_for(completed.wait(), 10)
            assert not store.pending()
            assert all(question['member_name'] == 'business' for question in store.read().values())
            assert 'China' in prompts[name][0] and 'do not repeat' in prompts[name][0]
            assert 'Later report' not in prompts[name][0]
            assert any(tool == 'ask_user' and 'run_team first' in str(result)
                       for tool, result in tool_results[name])
        await asyncio.wait_for(collecting.wait(), 10)
        plan = group.state['organization_plans'][0]
        rows = group_service.group_organization_teams(SESSION, plan['organization_id'])
        assert set(launches) == set(members) and len(launches) == 3
        assert {row['member_name'] for row in rows} == set(members)
        assert 'China' in plan['body']
        if clarify_again:
            assert '100' in plan['body']
        assert len(group.state['organization_plans']) == 1
        assert route.candidate(SESSION, 'later')['status'] == 'pending'
        for name in members:
            assert seen[name] and all(turn.client_message_id == 'original' for turn in seen[name])
            runs = [result for tool, result in tool_results[name] if tool == 'run_team']
            assert len(runs) == 1 and runs[0].success
            assert runs[0].data['organization_id'] == plan['organization_id']
            assert runs[0].data['plan_id'] == 'original'
            hosts[name][4]._run.assert_not_awaited()
            assert not hosts[name][1].agent_callback_manager.has_hooks(AgentCallbackEvent.BEFORE_INVOKE)
        tasks = [bootstrap._owner_runs[(SESSION, 'original')]]
        finish.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert published.await_count == 1
        assert 'Callback execution failed' not in caplog.text
        assert 'owner_turn_ended_without_reply' not in caplog.text
    finally:
        finish.set()
        for host, inner, session, _, _ in hosts.values():
            await host.stop()
            await session.post_run()
            await inner.agent_callback_manager.clear()
            await host.agent_callback_manager.clear()
        for key, task in list(bootstrap._owner_runs.items()):
            if key[0] == SESSION:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        bootstrap._accepted_plans.clear()
        bootstrap._proxy_tools.clear()
