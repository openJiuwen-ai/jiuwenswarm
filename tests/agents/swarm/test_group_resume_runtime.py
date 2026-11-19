"""Clarification ownership crosses the real DeepAgent/ReActAgent boundary."""

import asyncio
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.foundation.tool import ToolOutput
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.interrupt.state import ToolInterruptEntry, ToolInterruptionState
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent, rail as with_rails
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.config import DeepAgentConfig

from jiuwenswarm.agents.swarm import group_execution as policy, group_turn, group_route as route, group_chat
from jiuwenswarm.agents.swarm.group_questions import GroupAskUserRail
from jiuwenswarm.agents.harness.common.rails.ask_user_contract import build_ask_user_interactive_input
from jiuwenswarm.server.runtime.group_questions import register_question_target
from tests.agents.swarm.test_group_route import env, record, SESSION


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['invoke', 'child_task', 'stream', 'native'])
async def test_real_deep_resume_preserves_org_until_proxy_run_team(env, monkeypatch, entrypoint, caplog):
    group, backend, context = env
    await record('original', 'Original report')
    await route.set_status(SESSION, 'original', 'clarification', expected={'pending'})
    await record('later', 'Later request must stay pending')
    inner = ReActAgent(AgentCard(id=uuid4().hex, name='inner'))
    if entrypoint == 'native':
        from openjiuwen.agent_teams.harness.native_harness import NativeHarness
        from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec

        outer = NativeHarness(DeepAgentSpec(card=AgentCard(id=uuid4().hex, name='outer'),
                                           enable_sys_operation=False, completion_timeout=None))
    else:
        outer = DeepAgent(AgentCard(id=uuid4().hex, name='outer'))
    outer._react_agent = inner
    outer._deep_config = DeepAgentConfig(enable_task_loop=entrypoint == 'native')
    from openjiuwen.harness.prompts import SystemPromptBuilder

    outer.system_prompt_builder = inner.system_prompt_builder = SystemPromptBuilder()
    monkeypatch.setattr(outer, '_ensure_initialized', AsyncMock())
    monkeypatch.setattr(outer, '_sync_expert_role_attachment', AsyncMock())
    monkeypatch.setattr(outer, '_proposal_owner', lambda: None)
    monkeypatch.setattr(outer, 'save_state', Mock())
    monkeypatch.setattr(outer, 'clear_state', Mock())
    model_context = SimpleNamespace(add_messages=AsyncMock(), get_messages=lambda: [])
    monkeypatch.setattr(inner, '_init_context', AsyncMock(return_value=model_context))
    monkeypatch.setattr(inner, '_sync_prompt_attachments', AsyncMock())
    monkeypatch.setattr(inner.context_engine, 'save_contexts', AsyncMock())
    monkeypatch.setattr(policy, 'read_group', lambda _: group.state)
    monkeypatch.setattr(policy, 'get_team_backend', lambda _: backend)
    executions = SimpleNamespace(reconcile=AsyncMock(), active=lambda _: None, background_runs=lambda _: [], read=lambda: {})
    monkeypatch.setattr(policy, 'GroupExecutions', lambda _: executions)

    ask = GroupAskUserRail(context, {})
    target = register_question_target(SESSION, SESSION, 'business', outer)
    answers = [{'question': 'Market?', 'custom_input': 'China'}]
    questions = {'q': dict(native_request_id='ask-original', target=target, member_name='business',
                           execution_id=None, client_message_id='original', status='answered', answers=answers)}
    store = SimpleNamespace(read=lambda: questions, pending=lambda *_: [])
    monkeypatch.setattr('jiuwenswarm.agents.swarm.group_questions.GroupQuestions', lambda _: store)
    monkeypatch.setattr('jiuwenswarm.server.runtime.group_questions.GroupQuestions', lambda _: store)
    for rail in [ask, policy.GroupTaskPolicyRail(context)]:
        await outer._register_rail_selective(rail)

    call = ToolCall(id='ask-original', name='ask_user', type='function',
                    arguments=json.dumps({'questions': [{'question': 'Market?'}]}))
    request = InterruptRequest(interrupt_id='interrupt-original', interrupt_kind='ask_user',
                               interrupt_owner=ask.interrupt_owner)
    state = ToolInterruptionState(
        ai_message=AssistantMessage(content='', tool_calls=[call]), iteration=0,
        interrupted_tools={call.id: ToolInterruptEntry(tool_call=call, interrupt_requests={call.id: request})},
    )
    monkeypatch.setattr(inner._hitl_handler, 'load', lambda _: state)
    proxy = group_chat.RunTeam(backend, context, {})
    launched = AsyncMock(return_value=ToolOutput(success=True, data={'accepted': True}))
    monkeypatch.setattr(proxy, '_invoke_bound_organization', launched)
    monkeypatch.setattr(proxy, '_run', AsyncMock(side_effect=AssertionError('must not start a private team')))
    inner.ability_manager.add_ability(proxy.card, proxy)
    observed = []
    responses = iter([
        AssistantMessage(content='', tool_calls=[ToolCall(id='launch', name='run_team', type='function',
                          arguments=json.dumps({'action': 'start', 'run_id': 'job', 'content': 'Report'}))]),
        AssistantMessage(content='Accepted.'),
        AssistantMessage(content='Ordinary reply.'),
    ])

    @with_rails(before=AgentCallbackEvent.BEFORE_MODEL_CALL, after=AgentCallbackEvent.AFTER_MODEL_CALL)
    async def model(_agent, ctx):
        observed.append(ctx.extra.get(group_turn.TURN_KEY))
        return next(responses)

    monkeypatch.setattr(inner, '_railed_model_call', MethodType(model, inner))
    query = build_ask_user_interactive_input(call.id, answers)
    session = create_agent_session(session_id=uuid4().hex, card=inner.card)
    try:
        if entrypoint == 'native':
            completed = asyncio.Event()
            async def on_round(kind):
                if kind == 'finished':
                    completed.set()
            await outer.subscribe(on_round=on_round)
            await outer.start(session=session)
            await outer.send(query)
            await asyncio.wait_for(completed.wait(), 10)
        elif entrypoint == 'stream':
            async for _ in outer.stream({'query': query}, session):
                pass
        else:
            work = outer.invoke({'query': query}, session)
            await (asyncio.create_task(work) if entrypoint == 'child_task' else work)
        assert observed and all(turn and turn.client_message_id == 'original' for turn in observed)
        launched.assert_awaited_once_with('original')
        proxy._run.assert_not_awaited()
        assert route.candidate(SESSION, 'original')['status'] == 'task'
        assert route.candidate(SESSION, 'later')['status'] == 'pending'
        assert 'China' in group.state['organization_plans'][0]['body']
        assert group_turn.current_group_turn(SESSION, 'business') is None
        assert not inner.agent_callback_manager.has_hooks(AgentCallbackEvent.BEFORE_INVOKE)
        monkeypatch.setattr(inner._hitl_handler, 'load', lambda _: None)
        if entrypoint == 'native':
            completed.clear()
            await outer.send('Ordinary internal follow-up')
            await asyncio.wait_for(completed.wait(), 10)
        else:
            await outer.invoke({'query': 'Ordinary internal follow-up'}, session)
        assert observed[-1] is None
        assert launched.await_count == 1
        assert 'Callback execution failed' not in caplog.text
    finally:
        if entrypoint == 'native':
            await outer.stop()
        await inner.agent_callback_manager.clear()
        await outer.agent_callback_manager.clear()


@pytest.mark.asyncio
async def test_concurrent_resumes_bind_each_inner_input_and_cleanup(env, monkeypatch):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs

    _, _, context = env
    outer = DeepAgent(AgentCard(id=uuid4().hex, name='owner'))
    inner = ReActAgent(AgentCard(id=uuid4().hex, name='inner'))
    outer._react_agent = inner
    target = register_question_target(SESSION, SESSION, 'business', outer)
    for key in ['one', 'two']:
        await record(key)
    questions = {key: dict(native_request_id=key, target=target, member_name='business', execution_id=None,
                          client_message_id=key, status='answered') for key in ['one', 'two']}
    monkeypatch.setattr('jiuwenswarm.agents.swarm.group_questions.GroupQuestions',
                        lambda _: SimpleNamespace(read=lambda: questions))
    rail = GroupAskUserRail(context, {})
    ready = {key: asyncio.Event() for key in questions}

    async def resume(key, other):
        query = build_ask_user_interactive_input(key, [{'question': 'Market?', 'custom_input': 'China'}])
        ctx = AgentCallbackContext(agent=outer, inputs=InvokeInputs(query=query))
        await rail.before_invoke(ctx)
        try:
            ready[key].set()
            await ready[other].wait()
            fresh = AgentCallbackContext(agent=inner, inputs=InvokeInputs(query=query))
            await fresh.fire(AgentCallbackEvent.BEFORE_INVOKE)
            assert fresh.extra[group_turn.TURN_KEY].client_message_id == key
            ordinary = AgentCallbackContext(agent=inner, inputs=InvokeInputs(query='ordinary'))
            await ordinary.fire(AgentCallbackEvent.BEFORE_INVOKE)
            assert group_turn.TURN_KEY not in ordinary.extra
        finally:
            await rail.after_invoke(ctx)

    try:
        await asyncio.gather(resume('one', 'two'), resume('two', 'one'))
        assert not inner.agent_callback_manager.has_hooks(AgentCallbackEvent.BEFORE_INVOKE)
    finally:
        await inner.agent_callback_manager.clear()
        await outer.agent_callback_manager.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
async def test_outer_failure_unmounts_inner_binding(env, monkeypatch, failure):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs

    _, _, context = env
    await record('one')
    outer = DeepAgent(AgentCard(id=uuid4().hex, name='owner'))
    inner = ReActAgent(AgentCard(id=uuid4().hex, name='inner'))
    outer._react_agent = inner
    target = register_question_target(SESSION, SESSION, 'business', outer)
    question = dict(native_request_id='ask', target=target, member_name='business', execution_id=None,
                    client_message_id='one', status='answered')
    monkeypatch.setattr('jiuwenswarm.agents.swarm.group_questions.GroupQuestions',
                        lambda _: SimpleNamespace(read=lambda: {'q': question}))
    await outer._register_rail_selective(GroupAskUserRail(context, {}))
    query = build_ask_user_interactive_input('ask', [{'question': 'Market?', 'custom_input': 'China'}])
    ctx = AgentCallbackContext(agent=outer, inputs=InvokeInputs(query=query))
    try:
        with pytest.raises(failure):
            async with ctx.lifecycle(AgentCallbackEvent.BEFORE_INVOKE, AgentCallbackEvent.AFTER_INVOKE):
                fresh = AgentCallbackContext(agent=inner, inputs=InvokeInputs(query=query))
                await fresh.fire(AgentCallbackEvent.BEFORE_INVOKE)
                assert fresh.extra[group_turn.TURN_KEY].client_message_id == 'one'
                raise failure()
        assert not inner.agent_callback_manager.has_hooks(AgentCallbackEvent.BEFORE_INVOKE)
    finally:
        await inner.agent_callback_manager.clear()
        await outer.agent_callback_manager.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['missing_question', 'missing_plan', 'ambiguous', 'unanswered', 'corrupt'])
async def test_unverified_org_resume_blocks_all_tool_side_effects(env, monkeypatch, invalid):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs, ToolCallInputs
    from jiuwenswarm.server.runtime.group_questions import ORGANIZATION_RESUME_KEY

    _, backend, context = env
    await record('one')
    outer = DeepAgent(AgentCard(id=uuid4().hex, name='owner'))
    inner = ReActAgent(AgentCard(id=uuid4().hex, name='inner'))
    outer._react_agent = inner
    target = register_question_target(SESSION, SESSION, 'business', outer)
    question = dict(native_request_id='ask', target=target, member_name='business', execution_id=None,
                    client_message_id='one', status='answered')
    questions = {'q': question}
    if invalid == 'missing_question':
        questions.clear()
    elif invalid == 'missing_plan':
        route._save(SESSION, {})
    elif invalid == 'ambiguous':
        questions['other'] = {**question, 'client_message_id': 'other'}
    elif invalid == 'unanswered':
        question['status'] = 'pending'
    read = Mock(return_value=questions, side_effect=ValueError('corrupt question store') if invalid == 'corrupt' else None)
    monkeypatch.setattr('jiuwenswarm.agents.swarm.group_questions.GroupQuestions', lambda _: SimpleNamespace(read=read))
    rail = GroupAskUserRail(context, {})
    query = build_ask_user_interactive_input('ask', [{'question': 'Market?', 'custom_input': 'China'}])
    query.user_inputs['ask'][ORGANIZATION_RESUME_KEY] = 'one'
    outer_ctx = AgentCallbackContext(agent=outer, inputs=InvokeInputs(query=query))
    try:
        await rail.before_invoke(outer_ctx)
        ctx = AgentCallbackContext(agent=inner, inputs=InvokeInputs(query=query))
        await ctx.fire(AgentCallbackEvent.BEFORE_INVOKE)
        assert ctx.extra[group_turn.TURN_KEY].route == 'unknown'
        assert ctx.consume_force_finish().result['result_type'] == 'error'
        monkeypatch.setattr(policy, 'read_group', lambda _: {})
        task_policy = policy.GroupTaskPolicyRail(context)
        for tool_name in ['run_team', 'group_send_message', 'start_group_task', 'send_file', 'ask_user']:
            ctx.inputs = ToolCallInputs(tool_name=tool_name, tool_args={}, tool_call=None)
            ctx.extra.pop('_skip_tool', None)
            await task_policy.before_tool_call(ctx)
            try:
                assert ctx.extra['_skip_tool']
                assert ctx.inputs.tool_result['error']
            finally:
                task_policy._reset_group_turn(ctx)
        token = group_turn.bind_group_turn(ctx.extra[group_turn.TURN_KEY])
        try:
            response = await policy.GroupMessageTool(backend, context, 'Reply').invoke({'content': 'Progress'})
            assert not response.success
        finally:
            group_turn.reset_group_turn(token)
    finally:
        await rail.after_invoke(outer_ctx)
        await inner.agent_callback_manager.clear()
        await outer.agent_callback_manager.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['single_expert', 'single_team', 'mixed', 'private_team'])
async def test_inner_resume_keeps_nonorganization_questions_unbound(env, monkeypatch, kind):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs

    group, _, context = env
    await record('unrelated-organization')
    outer = DeepAgent(AgentCard(id=uuid4().hex, name='owner'))
    inner = ReActAgent(AgentCard(id=uuid4().hex, name='inner'))
    outer._react_agent = inner
    mentions = {'single_expert': ['writer'], 'single_team': ['business'],
                'mixed': ['business', 'writer'], 'private_team': ['business']}[kind]
    assert await record('direct', mentions=mentions) is None
    if kind == 'single_expert':
        context = SimpleNamespace(session_id=SESSION, member_name='writer')
    if kind == 'private_team':
        context = SimpleNamespace(session_id='private-session', member_name='business')
    target = register_question_target(SESSION, context.session_id, context.member_name, outer)
    question = dict(native_request_id='ask', target=target, member_name=context.member_name,
                    execution_id='private' if kind == 'private_team' else None, status='answered')
    monkeypatch.setattr('jiuwenswarm.agents.swarm.group_questions.GroupQuestions',
                        lambda _: SimpleNamespace(read=lambda: {'q': question}))
    rail = GroupAskUserRail(context, {'group_id': SESSION})
    query = build_ask_user_interactive_input('ask', [{'question': 'Market?', 'custom_input': 'China'}])
    ctx = AgentCallbackContext(agent=outer, inputs=InvokeInputs(query=query))
    try:
        await rail.before_invoke(ctx)
        await rail.before_invoke(ctx)  # Re-entering setup must not leave a second hook.
        resumed = AgentCallbackContext(agent=inner, inputs=InvokeInputs(query=query))
        await resumed.fire(AgentCallbackEvent.BEFORE_INVOKE)
        assert group_turn.TURN_KEY not in resumed.extra
        assert not resumed.has_force_finish_request
        assert route.candidate(SESSION, 'unrelated-organization')['status'] == 'pending'
        assert not group.state.get('organization_plans')
    finally:
        await rail.after_invoke(ctx)
        assert not inner.agent_callback_manager.has_hooks(AgentCallbackEvent.BEFORE_INVOKE)
        await inner.agent_callback_manager.clear()
        await outer.agent_callback_manager.clear()


@pytest.mark.asyncio
async def test_binding_install_failure_aborts_instead_of_running_unbound(env, monkeypatch):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs

    _, _, context = env
    outer = DeepAgent(AgentCard(id=uuid4().hex, name='owner'))
    inner = ReActAgent(AgentCard(id=uuid4().hex, name='inner'))
    outer._react_agent = inner
    await outer._register_rail_selective(GroupAskUserRail(context, {}))
    monkeypatch.setattr(inner, 'register_rail', AsyncMock(side_effect=RuntimeError('registration failed')))
    ctx = AgentCallbackContext(agent=outer, inputs=InvokeInputs(query='ordinary'))
    try:
        with pytest.raises(RuntimeError, match='registration failed'):
            async with ctx.lifecycle(AgentCallbackEvent.BEFORE_INVOKE, AgentCallbackEvent.AFTER_INVOKE):
                pytest.fail('the unbound invocation must never execute')
        assert not inner.agent_callback_manager.has_hooks(AgentCallbackEvent.BEFORE_INVOKE)
    finally:
        await inner.agent_callback_manager.clear()
        await outer.agent_callback_manager.clear()
