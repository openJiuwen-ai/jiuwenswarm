"""Owner triage must precede both organization cards and private instance launches."""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.foundation.tool import ToolOutput

from jiuwenswarm.agents.swarm import group_route as route, group_org_bootstrap as bootstrap
from jiuwenswarm.agents.swarm import group_chat, group_execution as policy, group_turn
from jiuwenswarm.server.runtime import group_service
from jiuwenswarm.server.runtime.agent_adapter.team_helpers import _record_group_organization_plan
from tests.agents.swarm.test_group_org_bootstrap import _MemoryGroup, SESSION


@pytest.fixture
def env(monkeypatch, tmp_path):
    group = _MemoryGroup(monkeypatch)
    monkeypatch.setattr(group_service, "group_path", lambda _: tmp_path / "group.json")
    monkeypatch.setattr(route, "publish", AsyncMock())
    monkeypatch.setattr(bootstrap, "publish_pending_organization_card", AsyncMock())
    backend = SimpleNamespace(member_name="business", team_name="outer-team", conversation_mode="group_chat",
                              message_manager=SimpleNamespace(send_message=AsyncMock(return_value="wake-id")))
    context = SimpleNamespace(session_id=SESSION, member_name="business")
    yield group, backend, context
    bootstrap._member_turns.clear()
    bootstrap._open_organization_turns.clear()
    bootstrap._organization_run_attempts.clear()


async def record(key="one", body="hello", mentions=None):
    return await _record_group_organization_plan(SESSION, {
        "mentions": mentions or ["business", "insight"], "body": body, "client_message_id": key,
    })


@pytest.mark.asyncio
async def test_greeting_reply_has_no_card_plan_or_instance_and_is_idempotent(env):
    group, backend, context = env
    assert await record() == "one"
    assert not group.state.get("organization_plans")
    bootstrap.publish_pending_organization_card.assert_not_awaited()
    sender = AsyncMock(return_value=ToolOutput(success=True, data={"message": {"message_id": "public-reply"}}))
    tool = SimpleNamespace(backend=backend, context=context)
    results = await asyncio.gather(*(route.ordinary_reply(tool, {"organization_reply": "final_chat", "content": "Hello"}, "one", sender) for _ in range(2)))
    assert all(result.success for result in results)
    sender.assert_awaited_once()
    assert route.public_routes(SESSION)[0]["status"] == "completed"
    assert route.public_routes(SESSION)[0]["reply_message_id"] == "public-reply"
    result = await route.activate(SESSION, "one", backend)
    assert not result.success
    assert not group.state.get("organization_plans")
    backend.message_manager.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_send_reserves_chat_and_retries_same_content_and_id(env):
    _, backend, context = env
    await record()
    sender = AsyncMock(side_effect=[ToolOutput(success=False, error="network"), ToolOutput(success=True, data={})])
    tool = SimpleNamespace(backend=backend, context=context)
    assert not (await route.ordinary_reply(tool, {"organization_reply": "final_chat", "content": "Hello"}, "one", sender)).success
    assert route.candidate(SESSION, "one")["status"] == "chat"
    assert not (await route.activate(SESSION, "one", backend)).success
    assert (await route.ordinary_reply(tool, {"organization_reply": "final_chat", "content": "Changed retry"}, "one", sender)).success
    assert sender.await_args_list[0] == sender.await_args_list[1]


@pytest.mark.asyncio
async def test_nonowner_cannot_decide_or_publish_and_owner_run_team_releases_proxies(env, monkeypatch):
    group, backend, context = env
    await record(body="Create a report")
    other = SimpleNamespace(**{**vars(backend), "member_name": "insight"})
    result = await route.activate(SESSION, "one", other)
    assert not result.success and "Waiting for the Owner" in result.error
    sender = AsyncMock()
    result = await route.ordinary_reply(SimpleNamespace(backend=other, context=context), {"organization_reply": "final_chat", "content": "Hi"}, "one", sender)
    assert not result.success
    sender.assert_not_awaited()
    tool = group_chat.RunTeam(backend, context, {})
    launched = AsyncMock(return_value=ToolOutput(success=True, data={"accepted": True}))
    monkeypatch.setattr(tool, "_invoke_bound_organization", launched)
    token = group_turn.bind_group_turn(group_turn.GroupTurn(SESSION, "business", "one", "organization"))
    try:
        assert (await tool.invoke({"action": "update", "execution_id": "older"})).success
        assert (await tool.invoke({"action": "start"})).success
    finally:
        group_turn.reset_group_turn(token)
    assert route.candidate(SESSION, "one")["status"] == "task"
    assert len(group.state["organization_plans"]) == 1
    assert group.state["organization_plans"][0]["body"] == "Create a report"
    backend.message_manager.send_message.assert_awaited_once()
    assert backend.message_manager.send_message.call_args.kwargs["meta"]["plan_id"] == "one"
    assert launched.await_args.args == ("one",)
    # The next proxy's own tool call remains the launch entry.
    assert await route.activate(SESSION, "one", other) is None


@pytest.mark.asyncio
async def test_clarification_waits_without_task_then_activates_same_request(env):
    group, backend, _ = env
    await record()
    await route.set_status(SESSION, "one", "clarification", expected={"pending"})
    assert not group.state.get("organization_plans")
    assert route.public_routes(SESSION)[0]["status"] == "clarification"
    assert await route.activate(SESSION, "one", backend) is None
    assert group.state["organization_plans"][0]["plan_id"] == "one"


@pytest.mark.asyncio
async def test_followup_isolation_and_conflicting_duplicate_rejected(env):
    group, backend, context = env
    await record("one", "Hello")
    await record("two", "Create a report")
    await route.ordinary_reply(SimpleNamespace(backend=backend, context=context), {"organization_reply": "final_chat", "content": "Hi"}, "one",
                               AsyncMock(return_value=ToolOutput(success=True, data={})))
    assert route.candidate(SESSION, "two")["status"] == "pending"
    with pytest.raises(ValueError, match="already belongs"):
        await route.record_candidate(SESSION, mentions=["business", "insight"], body="different", plan_id="two")
    assert await route.activate(SESSION, "two", backend) is None
    assert [plan["plan_id"] for plan in group.state["organization_plans"]] == ["two"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mentions", [["writer"], ["business"], ["writer", "business"], ["business", "insight", "writer"]])
async def test_single_and_mixed_mentions_do_not_create_candidates(env, mentions):
    group, _, _ = env
    assert await record(mentions=mentions) is None
    assert not route.routes(SESSION)
    assert not group.state.get("organization_plans")


@pytest.mark.asyncio
async def test_owner_no_tool_turn_ends_candidate_without_card(env, monkeypatch):
    group, _, context = env
    await record()
    monkeypatch.setattr(policy, "read_group", lambda _: group.state)
    monkeypatch.setattr(policy, "GroupExecutions", lambda _: SimpleNamespace(active=lambda _: None))
    monkeypatch.setattr("jiuwenswarm.server.runtime.group_questions.GroupQuestions", lambda _: SimpleNamespace(pending=lambda _: []))
    ctx = SimpleNamespace(extra={group_turn.TURN_KEY: group_turn.GroupTurn(SESSION, "business", "one", "organization")})
    await policy.GroupTaskPolicyRail(context).after_invoke(ctx)
    assert route.candidate(SESSION, "one")["status"] == "failed"
    assert not group.state.get("organization_plans")


@pytest.mark.asyncio
async def test_nonowner_model_turn_stops_without_classification_call(env):
    _, _, context = env
    await record()
    context.member_name = "insight"
    ctx = SimpleNamespace(extra={group_turn.TURN_KEY: group_turn.GroupTurn(SESSION, "insight", "one", "organization")},
                          agent=SimpleNamespace(system_prompt_builder=Mock()), request_force_finish=Mock())
    await policy.GroupTaskPolicyRail(context).before_model_call(ctx)
    ctx.request_force_finish.assert_called_once()
    ctx.agent.system_prompt_builder.add_section.assert_not_called()


@pytest.mark.asyncio
async def test_framework_notice_rebinds_original_request_not_recent_message(env, monkeypatch):
    _, backend, _ = env
    await record(body="Report with confirmed budget 500万–5000万元")
    await route.activate(SESSION, "one", backend)
    await record("two", "Unrelated later request")
    from openjiuwen.agent_teams.group_chat import handler
    expand = AsyncMock(return_value="verified original notice")
    monkeypatch.setattr(handler, "context_for", expand)
    notice = SimpleNamespace(meta=json.dumps({"type": "swarm_group_route", "plan_id": "one"}),
                             to_member_name="insight", from_member_name="business", team_name="outer-team")
    trigger = SimpleNamespace(message_id="original")
    backend.db = SimpleNamespace(message=SimpleNamespace(get_message=AsyncMock(side_effect=[notice, trigger])))
    parts = ['<team-inbound from="business" message_id="wake-id">SWARM_GROUP_TASK_READY: notice</team-inbound>']
    await route.expand_route_notices(parts, backend, SESSION, "insight")
    assert len(parts) == 1 and parts[0].startswith("verified original notice")
    assert "500万–5000万元" in parts[0]
    assert "do not repeat" in parts[0]
    assert "Unrelated later request" not in parts[0]
    expand.assert_awaited_once_with(backend, "insight", trigger)


@pytest.mark.asyncio
async def test_runtime_death_and_owner_timeout_stop_pending_bubble(env, monkeypatch):
    await record()
    await route.set_status(SESSION, "one", "clarification", expected={"pending"}, owner_process=[99999999, 0])
    assert route.candidate(SESSION, "one")["status"] == "failed"
    await record("two")
    now = route.time.time()
    monkeypatch.setattr(route.time, "time", lambda: now + route.PENDING_SECONDS + 1)
    assert route.public_routes(SESSION)[1]["status"] == "failed"


class _OwnerProcess:
    pid = 12345
    created = 100.0
    identity = 42

    def create_time(self):
        return self.created

    def status(self):
        return route.psutil.STATUS_RUNNING

    def __hash__(self):
        return self.identity


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["gone", "reused", "zombie"])
async def test_confirmed_owner_loss_still_blocks_proxy_launch(env, monkeypatch, failure):
    group, backend, context = env
    process = _OwnerProcess()
    monkeypatch.setattr(route.psutil, "Process", lambda *args: process)
    await record()
    await route.set_status(SESSION, "one", "clarification", expected={"pending"}, **route.owner_process_fields())
    if failure == "gone":
        monkeypatch.setattr(route.psutil, "Process", Mock(side_effect=route.psutil.NoSuchProcess(process.pid)))
    elif failure == "reused":
        process.identity += 1
    else:
        process.status = lambda: route.psutil.STATUS_ZOMBIE
    assert route.public_routes(SESSION)[0]["status"] == "failed"
    tool = group_chat.RunTeam(backend, context, {})
    launch = AsyncMock()
    monkeypatch.setattr(tool, "_invoke_bound_organization", launch)
    token = group_turn.bind_group_turn(group_turn.GroupTurn(SESSION, "business", "one", "organization"))
    try:
        result = await tool.invoke({"action": "start"})
    finally:
        group_turn.reset_group_turn(token)
    assert not result.success
    launch.assert_not_awaited()
    assert not group.state.get("organization_plans")


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_error", [route.psutil.AccessDenied(12345), OSError("temporary process probe error")])
async def test_unobservable_owner_is_not_failed_and_is_checked_again(env, monkeypatch, probe_error):
    process = _OwnerProcess()
    monkeypatch.setattr(route.psutil, "Process", lambda *args: process)
    await record()
    await route.set_status(SESSION, "one", "clarification", expected={"pending"}, **route.owner_process_fields())
    monkeypatch.setattr(route.psutil, "Process", Mock(side_effect=probe_error))
    assert route.public_routes(SESSION)[0]["status"] == "clarification"
    assert route.routes(SESSION)["one"]["status"] == "clarification"
    monkeypatch.setattr(route.psutil, "Process", Mock(side_effect=route.psutil.NoSuchProcess(process.pid)))
    assert route.public_routes(SESSION)[0]["status"] == "failed"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS psutil clock adjustment regression")
def test_real_psutil_clock_adjustment_keeps_owner_identity(monkeypatch):
    from psutil import _psosx

    if not _psosx.INIT_BOOT_TIME:
        pytest.skip("OS boot-time reads are denied in this environment")
    item = dict(status="clarification", plan_id="clock", updated_at=0, **route.owner_process_fields())
    # Simulate the integer boot-time boundary crossed by a system clock update;
    # neither the actual system clock nor the running Worker is modified.
    monkeypatch.setattr(_psosx, "boot_time", lambda: _psosx.INIT_BOOT_TIME + 1)
    assert route.psutil.Process().create_time() != item["owner_process"][1]
    assert route._effective(item)["status"] == "clarification"

@pytest.mark.asyncio
async def test_reply_in_flight_does_not_overwrite_later_request(env):
    _, backend, context = env
    await record("one")
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(_inputs):
        entered.set()
        await release.wait()
        return ToolOutput(success=True, data={})

    task = asyncio.create_task(route.ordinary_reply(SimpleNamespace(backend=backend, context=context),
                                                   {"organization_reply": "final_chat", "content": "Hello"}, "one", send))
    await entered.wait()
    await record("two", "later message")
    release.set()
    await task
    assert route.candidate(SESSION, "one")["status"] == "completed"
    assert route.candidate(SESSION, "two")["status"] == "pending"


@pytest.mark.asyncio
async def test_group_message_tool_uses_consumed_request_and_server_reply_id(env):
    _, backend, context = env
    await record("one")
    await record("two")
    backend.append_group_message = AsyncMock(return_value=SimpleNamespace(
        model_dump=lambda **_: {"message": {"message_id": "reply"}}))
    tool = policy.GroupMessageTool(backend, context, "Reply")
    token = group_turn.bind_group_turn(group_turn.GroupTurn(SESSION, "business", "one", "organization"))
    try:
        result = await tool.invoke({"organization_reply": "final_chat", "content": "Hello", "client_message_id": "model-invented"})
    finally:
        group_turn.reset_group_turn(token)
    assert result.success
    assert route.candidate(SESSION, "one")["status"] == "completed"
    assert route.candidate(SESSION, "two")["status"] == "pending"
    assert backend.append_group_message.call_args.kwargs["client_message_id"].startswith("org-chat-")


@pytest.mark.asyncio
async def test_clarification_is_bound_to_request_and_its_answers_reach_plan(env, monkeypatch):
    from openjiuwen.core.foundation.llm import ToolCall
    from jiuwenswarm.agents.swarm.group_questions import GroupAskUserRail
    from jiuwenswarm.server.runtime import group_questions

    group, backend, context = env
    await record("one", "Prepare a report")
    questions = {}

    async def ask(payload, target, member, execution, *, client_message_id):
        assert execution is None
        assert client_message_id == "one"
        questions["question"] = dict(client_message_id=client_message_id, member_name=member, status="pending")

    store = SimpleNamespace(ask=ask, read=lambda: questions)
    monkeypatch.setattr(group_questions, "GroupQuestions", lambda _: store)
    monkeypatch.setattr("jiuwenswarm.agents.swarm.group_questions.GroupQuestions", lambda _: store)
    ctx = SimpleNamespace(agent=Mock(), extra={group_turn.TURN_KEY: group_turn.GroupTurn(SESSION, "business", "one", "organization")})
    rail = GroupAskUserRail(context, {})
    await rail.before_invoke(ctx)
    call = ToolCall(id="ask", type="function", name="ask_user", arguments=json.dumps({"query": "Which market?"}))
    sender = AsyncMock()
    proxy = SimpleNamespace(backend=backend, context=context)
    for payload in [{"content": "收到，马上提问"},
                    {"content": "收到，马上提问", "organization_reply": "acknowledgement"}]:
        await route.ordinary_reply(proxy, payload, "one", sender)
        assert route.candidate(SESSION, "one")["status"] == "pending"
    sender.assert_not_awaited()
    await rail.resolve_interrupt(ctx, call, None)
    assert route.candidate(SESSION, "one")["status"] == "clarification"
    assert not group.state.get("organization_plans")
    assert not (await route.activate(SESSION, "one", backend)).success
    questions["question"].update(status="answered", answers=[{"question": "Which market?", "custom_input": "China"}])
    assert await route.activate(SESSION, "one", backend) is None
    assert "China" in group.state["organization_plans"][0]["body"]


@pytest.mark.asyncio
async def test_nonowner_public_post_is_blocked_after_chat_finishes(env):
    _, backend, context = env
    await record()
    await route.ordinary_reply(SimpleNamespace(backend=backend, context=context), {"organization_reply": "final_chat", "content": "Hi"}, "one",
                               AsyncMock(return_value=ToolOutput(success=True, data={})))
    backend.member_name = "insight"
    send = AsyncMock()
    result = await route.ordinary_reply(SimpleNamespace(backend=backend, context=context), {"organization_reply": "final_chat", "content": "Me too"}, "one", send)
    assert not result.success
    send.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, RuntimeError('mailbox unavailable')])
async def test_participant_notification_failure_closes_admission_without_owner_launch(env, monkeypatch, failure):
    group, backend, context = env
    await record()
    monkeypatch.setattr(bootstrap, 'publish_organization_card', AsyncMock())
    backend.message_manager.send_message = AsyncMock(return_value=None, side_effect=failure)
    tool = group_chat.RunTeam(backend, context, {})
    launched = AsyncMock()
    monkeypatch.setattr(tool, '_invoke_bound_organization', launched)
    token = group_turn.bind_group_turn(group_turn.GroupTurn(SESSION, 'business', 'one', 'organization'))
    try:
        assert not (await tool.invoke({'action': 'start'})).success
    finally:
        group_turn.reset_group_turn(token)
    launched.assert_not_awaited()
    assert route.candidate(SESSION, 'one')['status'] == 'failed'
    assert group.state['organization_plans'][0]['status'] == 'failed'
    assert bootstrap.publish_organization_card.call_args.kwargs['status'] == 'failed'

@pytest.mark.asyncio
async def test_owner_can_read_required_history_before_decision_but_cannot_create_work(env, monkeypatch):
    group, backend, context = env
    await record()
    monkeypatch.setattr(policy, 'read_group', lambda _: group.state)
    monkeypatch.setattr(policy, 'get_team_backend', lambda _: backend)
    rail = policy.GroupTaskPolicyRail(context)
    turn = group_turn.GroupTurn(SESSION, 'business', 'one', 'organization')
    for name, blocked in [('read_file', False), ('start_group_task', True), ('build_team', True)]:
        ctx = SimpleNamespace(extra={group_turn.TURN_KEY: turn}, inputs=SimpleNamespace(tool_name=name, tool_call=None))
        await rail.before_tool_call(ctx)
        assert bool(ctx.extra.get('_skip_tool')) is blocked
        await rail.on_tool_exception(ctx)
    assert route.candidate(SESSION, 'one')['status'] == 'pending'
    assert not group.state.get('organization_plans')

@pytest.mark.asyncio
async def test_retry_of_existing_organization_is_not_reclassified(env):
    group, _, _ = env
    previous = bootstrap.record_organization_plan(SESSION, mentions=['business', 'insight'], body='Task', plan_id='old')
    assert await record('old', 'Task') == 'old'
    assert not route.routes(SESSION)
    assert group.state['organization_plans'] == [previous]


@pytest.mark.asyncio
@pytest.mark.parametrize('candidate_exists', [True, False])
async def test_mailbox_owner_run_team_never_falls_back_to_old_instance(env, monkeypatch, tmp_path, candidate_exists):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, UserMessageInputs
    from tests.agents.swarm.test_group_turn import mailbox_backend, inbound, row

    group, backend, context = env
    # Reproduce a public proxy with an older completed organization instance.
    old_instance = AsyncMock(return_value={'team_id': 'old-team'})
    monkeypatch.setattr(bootstrap, 'saved_organization_instance', old_instance)
    source, _ = await mailbox_backend(tmp_path, monkeypatch, [row('a', ['business', 'insight'])])
    backend.db = source.db
    backend.message_manager.get_broadcast_read_at = source.db.message.get_broadcast_read_at
    backend.group_conversation = source.group_conversation
    backend.group_chat_spec = source.group_chat_spec
    if candidate_exists:
        await record('client-a', 'Create a report')
    monkeypatch.setattr(policy, 'read_group', lambda _: group.state)
    monkeypatch.setattr(policy, 'get_team_backend', lambda _: backend)
    rail = policy.GroupTaskPolicyRail(context)
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=[inbound('a')]))
    await rail.on_user_message(ctx)
    assert ctx.extra[group_turn.TURN_KEY].route == 'organization'
    if candidate_exists:
        assert route.routes(SESSION)['client-a']['owner_process_identity'] == str(hash(route.psutil.Process()))
    tool = group_chat.RunTeam(backend, context, {})
    launch = AsyncMock(return_value=ToolOutput(success=True, data={'accepted': True}))
    if candidate_exists:
        monkeypatch.setattr(tool, '_invoke_bound_organization', launch)
    ctx.inputs = SimpleNamespace(tool_name='run_team')
    await rail.before_tool_call(ctx)
    try:
        result = await tool.invoke({'action': 'start', 'run_id': 'new', 'content': 'Internal task wrapper'})
    finally:
        await rail.on_tool_exception(ctx)
    old_instance.assert_not_awaited()
    if candidate_exists:
        assert result.success
        launch.assert_awaited_once_with('client-a')
        assert route.candidate(SESSION, 'client-a')['status'] == 'task'
        assert len(group.state['organization_plans']) == 1
        assert group.state['organization_plans'][0]['body'] == 'Create a report'
        backend.message_manager.send_message.assert_awaited_once()
    else:
        assert not result.success
        assert 'no organization plan' in result.error
        assert not group.state.get('organization_plans')
        backend.message_manager.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_plan', [False, True])
@pytest.mark.parametrize('owner_probe', ['unchanged', 'clock_shift', 'access_denied'])
async def test_fresh_clarification_invoke_restores_exact_org_before_run_team(env, monkeypatch, missing_plan, owner_probe):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs, ToolCallInputs
    from jiuwenswarm.agents.swarm.group_questions import GroupAskUserRail
    from jiuwenswarm.agents.harness.common.rails.ask_user_contract import build_ask_user_interactive_input
    from jiuwenswarm.server.runtime.group_questions import register_question_target

    group, backend, context = env
    process = _OwnerProcess()
    monkeypatch.setattr(route.psutil, 'Process', lambda *args: process)
    await record('one', 'Original report')
    await route.set_status(SESSION, 'one', 'clarification', expected={'pending'}, **route.owner_process_fields())
    if owner_probe == 'clock_shift':
        process.created += 1
    elif owner_probe == 'access_denied':
        def probe(pid=None):
            if pid is not None:
                raise route.psutil.AccessDenied(pid)
            return process
        monkeypatch.setattr(route.psutil, 'Process', probe)
    await record('two', 'Later report')
    owner = Mock()
    target = register_question_target(SESSION, SESSION, 'business', owner)
    questions = {'q': dict(native_request_id='ask-one', target=target, member_name='business',
                           execution_id=None, client_message_id='one', status='answered',
                           answers=[{'question': 'Market?', 'custom_input': 'China'}])}
    store = SimpleNamespace(read=lambda: questions)
    monkeypatch.setattr('jiuwenswarm.agents.swarm.group_questions.GroupQuestions', lambda _: store)
    monkeypatch.setattr('jiuwenswarm.server.runtime.group_questions.GroupQuestions', lambda _: store)
    monkeypatch.setattr(policy, 'read_group', lambda _: group.state)
    monkeypatch.setattr(policy, 'get_team_backend', lambda _: backend)
    if missing_plan:
        items = route.routes(SESSION)
        items.pop('one')
        route._save(SESSION, items)
    query = build_ask_user_interactive_input('ask-one', questions['q']['answers'])
    # Core creates a new invoke context on HITL resume and does not call on_user_message.
    ctx = AgentCallbackContext(agent=owner, inputs=InvokeInputs(query=query))
    await GroupAskUserRail(context, {}).before_invoke(ctx)
    assert ctx.extra[group_turn.TURN_KEY].route == ('unknown' if missing_plan else 'organization')
    ctx.inputs = ToolCallInputs(tool_name='run_team', tool_args={}, tool_call=None)
    rail = policy.GroupTaskPolicyRail(context)
    tool = group_chat.RunTeam(backend, context, {})
    launch = AsyncMock(return_value=ToolOutput(success=True, data={'accepted': True}))
    monkeypatch.setattr(tool, '_invoke_bound_organization', launch)
    # Restoring the answer must not create the organization or launch instances.
    launch.assert_not_awaited()
    assert not group.state.get('organization_plans')
    await rail.before_tool_call(ctx)
    try:
        output = await tool.invoke({'action': 'start', 'run_id': 'job', 'task': 'Report'})
    finally:
        rail._reset_group_turn(ctx)
    assert output.success is not missing_plan
    if missing_plan:
        launch.assert_not_awaited()
    else:
        launch.assert_awaited_once_with('one')
        assert route.candidate(SESSION, 'one')['status'] == 'task'
        assert 'China' in group.state['organization_plans'][0]['body']
    assert route.candidate(SESSION, 'two')['status'] == 'pending'


@pytest.mark.asyncio
async def test_admission_gate_is_plan_scoped_and_releases_after_run_team(env, monkeypatch):
    group, backend, context = env
    await record()
    assert route.admission_block_reason(SESSION, 'one', 'business', 'ask_user') is None
    await route.activate(SESSION, 'one', backend)
    await record('two', 'Other request')
    assert route.admission_block_reason(SESSION, 'two', 'insight', 'ask_user') is None
    assert route.admission_block_reason(SESSION, 'one', 'writer', 'ask_user') is None
    monkeypatch.setattr(policy, 'read_group', lambda _: group.state)
    monkeypatch.setattr(policy, 'get_team_backend', lambda _: backend)
    context.member_name = 'insight'
    rail = policy.GroupTaskPolicyRail(context)
    turn = group_turn.GroupTurn(SESSION, 'insight', 'one', 'organization')
    for name, blocked in [('ask_user', True), ('start_group_task', True), ('build_team', True),
                          ('read_file', False), ('run_team', False)]:
        ctx = SimpleNamespace(extra={group_turn.TURN_KEY: turn}, inputs=SimpleNamespace(tool_name=name, tool_call=None))
        await rail.before_tool_call(ctx)
        assert bool(ctx.extra.get('_skip_tool')) is blocked
        await rail.on_tool_exception(ctx)
    bootstrap.claim_organization_plan(SESSION, 'one', 'insight')
    try:
        assert route.admission_block_reason(SESSION, 'one', 'insight', 'ask_user') is None
    finally:
        bootstrap._accepted_plans.discard((SESSION, 'one', 'insight'))
    group.state['organization_plans'][0]['status'] = 'failed'
    assert route.admission_context(SESSION, 'one', 'insight') == ''


@pytest.mark.asyncio
async def test_join_wait_does_not_fail_ready_member_while_other_proxy_is_busy(env, monkeypatch):
    monkeypatch.setattr(bootstrap, 'MEMBER_JOIN_TIMEOUT_SECONDS', 0.03)
    monkeypatch.setattr(bootstrap, '_JOIN_POLL_SECONDS', 0.005)
    bound = asyncio.Event()

    async def live(*args):
        return SimpleNamespace(org_task_manager=SimpleNamespace(organization_id='test-org')) if bound.is_set() else None

    monkeypatch.setattr(bootstrap, '_live_backend', live)
    monkeypatch.setattr(bootstrap, '_proxy_turn_active', AsyncMock(side_effect=lambda sid, name: name == 'insight'))
    waiter = asyncio.create_task(bootstrap._wait_for_members(
        SimpleNamespace(), session_id=SESSION, organization_id='test-org', members=['business', 'insight']))
    try:
        await asyncio.sleep(0.09)
        assert not waiter.done()
        bound.set()
        assert await asyncio.wait_for(waiter, 1) == []
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_each_proxy_gets_idle_grace_after_its_own_turn_ends(env, monkeypatch):
    monkeypatch.setattr(bootstrap, 'MEMBER_JOIN_TIMEOUT_SECONDS', 0.06)
    monkeypatch.setattr(bootstrap, '_JOIN_POLL_SECONDS', 0.005)
    active = True
    monkeypatch.setattr(bootstrap, '_live_backend', AsyncMock(return_value=None))
    monkeypatch.setattr(bootstrap, '_proxy_turn_active', AsyncMock(side_effect=lambda sid, name: active and name == 'insight'))
    waiter = asyncio.create_task(bootstrap._wait_for_members(
        SimpleNamespace(), session_id=SESSION, organization_id='test-org', members=['business', 'insight']))
    try:
        await asyncio.sleep(0.09)
        active = False
        await asyncio.sleep(0.02)
        assert not waiter.done()
        assert await asyncio.wait_for(waiter, 1) == ['business', 'insight']
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['question', 'queued', 'joining'])
async def test_explicit_join_waits_are_scoped_to_original_plan(env, monkeypatch, kind):
    from jiuwenswarm.server.runtime.group_questions import GroupQuestions

    group, backend, _ = env
    await record()
    await route.activate(SESSION, 'one', backend)
    plan = group.state['organization_plans'][0]
    questions = {'q': dict(status='pending', client_message_id='other', member_name='insight', execution_id=None)}
    monkeypatch.setattr(GroupQuestions, 'read', lambda _: questions)
    pending = asyncio.get_running_loop().create_future()
    mapping = bootstrap._queued_plans if kind == 'queued' else bootstrap._member_runs
    monkeypatch.setattr(bootstrap, 'MEMBER_JOIN_TIMEOUT_SECONDS', 0.03)
    monkeypatch.setattr(bootstrap, '_JOIN_POLL_SECONDS', 0.005)
    monkeypatch.setattr(bootstrap, '_live_backend', AsyncMock(return_value=None))
    monkeypatch.setattr(bootstrap, '_proxy_turn_active', AsyncMock(return_value=False))
    key = (SESSION, 'one', 'insight')
    foreign = (SESSION, 'other', 'insight')
    mapping[foreign] = pending
    waiter = None
    try:
        assert bootstrap._pending_join_members(SESSION, plan['organization_id']) == set()
        if kind == 'question':
            questions['q']['client_message_id'] = 'one'
        else:
            mapping[key] = pending
        assert bootstrap._pending_join_members(SESSION, plan['organization_id']) == {'insight'}
        waiter = asyncio.create_task(bootstrap._wait_for_members(
            SimpleNamespace(), session_id=SESSION, organization_id=plan['organization_id'], members=['insight']))
        await asyncio.sleep(0.09)
        assert not waiter.done()
        questions['q']['status'] = 'answered'
        pending.set_result(None)
        assert bootstrap._pending_join_members(SESSION, plan['organization_id']) == set()
        await asyncio.sleep(0.01)
        assert not waiter.done()
        assert await asyncio.wait_for(waiter, 1) == ['insight']
    finally:
        mapping.pop(key, None)
        mapping.pop(foreign, None)
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('purpose', [None, 'acknowledgement', 'invalid'])
async def test_receipt_never_finishes_candidate_or_posts_and_can_start_task(env, purpose):
    group, backend, context = env
    await record(body='Ask first, then prepare the report')
    backend.append_group_message = AsyncMock()
    tool = policy.GroupMessageTool(backend, context, 'Reply')
    token = group_turn.bind_group_turn(group_turn.GroupTurn(SESSION, 'business', 'one', 'organization'))
    try:
        payload = {'content': '收到，先提问，回答后启动三个专家团。'}
        if purpose is not None:
            payload['organization_reply'] = purpose
        result = await tool.invoke(payload)
        assert result.success is (purpose == 'acknowledgement')
        assert route.candidate(SESSION, 'one')['status'] == 'pending'
        backend.append_group_message.assert_not_awaited()
        assert not group.state.get('organization_plans')
        assert await route.activate(SESSION, 'one', backend) is None
        assert route.candidate(SESSION, 'one')['status'] == 'task'
    finally:
        group_turn.reset_group_turn(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('mentions', [['writer'], ['business'], ['writer', 'business']])
async def test_direct_messages_need_no_organization_reply_declaration(env, mentions):
    _, backend, context = env
    await record(mentions=mentions)
    backend.member_name = context.member_name = mentions[0]
    backend.append_group_message = AsyncMock(return_value=SimpleNamespace(
        model_dump=lambda **_: {'message': {'message_id': 'direct-reply'}}))
    tool = policy.GroupMessageTool(backend, context, 'Reply')
    token = group_turn.bind_group_turn(group_turn.GroupTurn(SESSION, mentions[0], 'one', 'direct'))
    try:
        result = await tool.invoke({'content': '收到', 'client_message_id': 'direct-reply'})
        assert result.success
        backend.append_group_message.assert_awaited_once()
        assert not route.routes(SESSION)
    finally:
        group_turn.reset_group_turn(token)
