"""Step-six model carriers and delivery checks through the installed SDK hooks."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from openjiuwen.core.foundation.llm import ToolCall, ToolMessage
from openjiuwen.core.foundation.tool.schema import ToolInfo
from openjiuwen.harness.tools import ToolOutput
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent, ModelCallInputs, ToolCallInputs
from openjiuwen.harness.prompts import SystemPromptBuilder, PromptSection
from openjiuwen.harness.rails import SkillUseRail

from jiuwenswarm.server.runtime.skill_turbo.accelerator_manifest import AcceleratorSpec
from jiuwenswarm.server.runtime.skill_turbo.accelerator_renderer import START, render_catalog
from jiuwenswarm.server.runtime.skill_turbo.rails.accelerator_rail import SkillAcceleratorRail
from jiuwenswarm.server.runtime.skill_turbo.rails.delivery_summary_rail import SkillTurboDeliverySummaryRail
from jiuwenswarm.agents.harness.flash.rails.flash_turbo_prompt_rail import FlashTurboPromptRail
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.server.runtime.skill_turbo import skill_turbo_tools as tools
from test_accelerator_manifest import declaration


@pytest.fixture
def disclosure(monkeypatch):
    spec = AcceleratorSpec.model_validate(declaration())
    snapshot = tools.AcceleratorCatalogSnapshot(entries={'demo-craft': 'demo', 'hidden': 'secret'},
        accelerators={'demo-craft': (spec,), 'hidden': (spec,)}, manifest_valid={'demo-craft': True, 'hidden': True})
    monkeypatch.setattr(tools, 'collect_accelerator_catalog', lambda: copy.deepcopy(snapshot))
    builder = SystemPromptBuilder(language='cn')
    builder.add_section(PromptSection(name='skills', content={'cn': 'demo'}, priority=40))
    native = SkillUseRail(skills_dir=[])
    native.get_skills_for_session = Mock(return_value=[SimpleNamespace(name='demo-craft')])
    host = SimpleNamespace(system_prompt_builder=builder, find_rails_by_type=lambda _: [native])
    card = ToolInfo(name='skill_acceleration_exec', description='generic')
    return host, native, card


@pytest.mark.parametrize('rail_type', [SkillAcceleratorRail, FlashTurboPromptRail])
async def test_model_inputs_and_directory_follow_visibility_and_flags(disclosure, rail_type):
    host, native, card = disclosure
    rail = rail_type()
    rail.init(host)
    ctx = SimpleNamespace(agent=host, session=object(), inputs=ModelCallInputs(tools=[card]))
    await rail.before_model_call(ctx)
    assert ctx.inputs.tools[0] is not card and card.description == 'generic'
    assert 'demo-craft/prep' in ctx.inputs.tools[0].description
    assert 'hidden' not in ctx.inputs.tools[0].description
    assert '无需用户明确' in ctx.inputs.tools[0].description
    assert 'FIRST' not in ctx.inputs.tools[0].description
    assert host.system_prompt_builder.get_section('skill_accelerators').priority == 41
    native.get_skills_for_session.assert_called_with(ctx.session)
    host.system_prompt_builder.remove_section('skills')
    await rail.before_model_call(ctx)
    assert host.system_prompt_builder.get_section('skill_accelerators') is None
    native.get_skills_for_session.return_value = []
    await rail.before_model_call(ctx)
    assert 'demo-craft' not in ctx.inputs.tools[0].description
    assert host.system_prompt_builder.get_section('skill_turbo_guide') is None
    rail.config['enabled'] = False
    await rail.before_model_call(ctx)
    assert 'disabled' in ctx.inputs.tools[0].description


@pytest.mark.parametrize('rail_type', [SkillAcceleratorRail, FlashTurboPromptRail])
@pytest.mark.parametrize('language', ['cn', 'en'])
async def test_auto_batch_disclosure_and_disabled_schema_share_same_policy(disclosure, rail_type, language):
    host, _, card = disclosure
    card.parameters = copy.deepcopy(tools.skill_turbo.card.input_params)
    rail = rail_type()
    rail._resolve_language = lambda: language
    rail.init(host)
    ctx = SimpleNamespace(agent=host, session=object(), inputs=ModelCallInputs(tools=[card]))
    await rail.before_model_call(ctx)
    description = ctx.inputs.tools[0].description
    assert 'auto_batch=false' in description
    assert ('省略或null' if language == 'cn' else 'Omitted/null auto_batch defaults') in description
    assert ('先做某一步' if language == 'cn' else 'do a step first') in description
    assert ('后续步骤及输入获取方式已经明确' if language == 'cn' else 'how to obtain their inputs are clear') in description
    assert 'auto_batch' in ctx.inputs.tools[0].parameters['properties']
    rail.config['batch_enabled'] = False
    ctx.inputs = ModelCallInputs(tools=[card])
    await rail.before_model_call(ctx)
    assert 'auto_batch=false' not in ctx.inputs.tools[0].description
    assert 'auto_batch' not in ctx.inputs.tools[0].parameters['properties']
    assert 'accelerators' not in ctx.inputs.tools[0].parameters['properties']
    assert 'auto_batch' in card.parameters['properties']


@pytest.mark.parametrize('rail_type', [SkillAcceleratorRail, FlashTurboPromptRail])
@pytest.mark.parametrize('language', ['cn', 'en'])
async def test_complete_ppt_batch_examples_reach_model_and_loaded_skill(disclosure, rail_type, language):
    host, _, card = disclosure
    rail = rail_type()
    rail._resolve_language = lambda: language
    rail.init(host)
    ctx = SimpleNamespace(agent=host, session=object(), inputs=ModelCallInputs(tools=[card]))
    await rail.before_model_call(ctx)
    guide = host.system_prompt_builder.get_section('skill_turbo_guide').render(language)
    description = ctx.inputs.tools[0].description

    rail._progress = AsyncMock(return_value={'progress': 'no_progress'})
    output = ToolOutput(success=True, data={'content': 'root body'})
    call = ToolCall(id='load', type='function', name='skill_tool',
                    arguments=json.dumps({'skill_name': 'demo-craft', 'relative_file_path': 'SKILL.md'}))
    loaded = SimpleNamespace(inputs=ToolCallInputs(tool_call=call, tool_name=call.name, tool_result=output,
        tool_msg=ToolMessage(content='root body', tool_call_id='load')), session=ctx.session, exception=None)
    await rail.after_tool_call(loaded)

    for carrier in (guide, description, loaded.inputs.tool_msg.content):
        examples = [json.loads(line) for line in carrier.splitlines() if line.startswith('{"query":')]
        assert [item['accelerator'] for item in examples] == ['prep']
        assert all(item['skill'] == 'pptx-craft' and item['auto_batch'] is True for item in examples)
        assert ('一次调用连续prep→outline→generate→export' if language == 'cn' else 'one call running prep→outline→generate→export') in carrier
        assert ('只有用户明确要求人工审阅' if language == 'cn' else 'ONLY when the user explicitly asks') in carrier
        assert ('检查内容准确性' if language == 'cn' else 'Routine content checks') in carrier
        assert ('同一交付物只建一个外层todo' if language == 'cn' else 'one outer todo per deliverable') in carrier
        assert ('自行拆阶段' if language == 'cn' else 'Your own stage-by-stage plan') in carrier
        assert 'auto_batch=false' in carrier and 'outline_review' in carrier and '__full__' in carrier
        assert ('执行模式：' if language == 'cn' else 'Execution modes:') in carrier
        assert ('省略params（包括空对象）' if language == 'cn' else 'omit params (including an empty object)') in carrier
        assert ('重试须保留用户限定的执行方式' if language == 'cn' else 'Preserve the user-selected execution mode on retry') in carrier
        assert 'accelerator="__full__")' in carrier

    rail.config['batch_enabled'] = False
    ctx.inputs = ModelCallInputs(tools=[card])
    await rail.before_model_call(ctx)
    assert '{"query":' not in ctx.inputs.tools[0].description
    assert ('执行模式：' if language == 'cn' else 'Execution modes:') in ctx.inputs.tools[0].description
    assert 'accelerator="__full__")' in ctx.inputs.tools[0].description


def test_catalog_budgets_order_and_review():
    specs = []
    for i in range(10):
        raw = declaration(f's{i}', i)
        raw['summary'] = 'x' * 60
        raw['hitl'] = {'kind': 'review'}
        specs.append(AcceleratorSpec.model_validate(raw))
    text = render_catalog({'demo': list(reversed(specs))})
    assert len(text.splitlines()) == 9
    assert all(len(line) <= 96 for line in text.splitlines())
    assert text.startswith('demo/s0 (0) [review]') and 'demo/s8 ' not in text


@pytest.mark.parametrize('path,success,expected', [('SKILL.md', True, True), ('ref/SKILL.md', True, False), ('SKILL.md', False, False)])
async def test_appendix_updates_both_carriers_preserves_metadata_and_is_readonly(disclosure, path, success, expected):
    host, _, _ = disclosure
    rail = SkillAcceleratorRail()
    rail.init(host)
    rail._progress = AsyncMock(return_value={'progress': 'unavailable'})
    data = {'content': 'root body', 'skill_content': 'raw root', 'skill_directory': '/skills/demo', 'layout': {'x': 1}}
    result = ToolOutput(success=success, data=copy.deepcopy(data))
    call = ToolCall(id='load', type='function', name='skill_tool', arguments=json.dumps({'skill_name': 'demo-craft', 'relative_file_path': path}))
    ctx = SimpleNamespace(inputs=ToolCallInputs(tool_call=call, tool_name='skill_tool', tool_result=result,
        tool_msg=ToolMessage(content='root body', tool_call_id='load')), session=object(), exception=None)
    await rail.after_tool_call(ctx)
    await rail.after_tool_call(ctx)
    assert (START in result.data['content']) is expected
    assert result.data['skill_content'] == 'raw root' and result.data['layout'] == {'x': 1}
    if expected:
        assert result.data['content'].count(START) == 1
        assert ctx.inputs.tool_msg.content == result.data['content']
        assert 'unavailable' in result.data['content']
        rail._progress.assert_awaited_once()
    else:
        rail._progress.assert_not_called()


async def test_installed_sdk_sends_tool_result_before_deep_summary(monkeypatch):
    manager = AgentCallbackManager('step6-' + uuid4().hex)
    events = []
    session = SimpleNamespace(write_stream=AsyncMock(side_effect=lambda output: events.append(output.type)))
    agent = SimpleNamespace(agent_callback_manager=manager)
    # Reverse registration must not override hook priorities.
    for rail in [SkillTurboDeliverySummaryRail(), JiuSwarmStreamEventRail()]:
        await manager.register_rail(rail, agent)
    monkeypatch.setattr(tools, '_ppt_delivery_summary_start', lambda: 'SUMMARY')
    tools.set_pending_ppt_delivery_summary('SUMMARY: complete')
    call = ToolCall(id='outer', type='function', name='skill_acceleration_exec', arguments='{}')
    ctx = AgentCallbackContext(agent=agent, session=session, inputs=ToolCallInputs(tool_call=call,
        tool_name=call.name, tool_args={}, tool_result={'success': True, 'result': 'ok'}))
    try:
        await ctx.fire(AgentCallbackEvent.AFTER_TOOL_CALL)
        assert events == ['tool_result', 'llm_output']
    finally:
        await manager.clear()
        tools.clear_pending_ppt_delivery_summary()


@pytest.mark.parametrize('changes', [
    {'status': 'partial'}, {'status': 'failed', 'success': False}, {'already_done': True},
    {'next_steps': ['generate']}, {'resume_remaining': ['export']}, {'delivery_confirmed': False},
])
def test_intermediate_and_historical_results_never_queue_summary(changes):
    result = {'success': True, 'mode': 'segment', 'status': 'ok', 'delivery_confirmed': True,
              'delivery_owner': 'delivery', 'skill': 'demo', 'ledger': {'task_key': 'A'}, **changes}
    holder = {'delivery': {'delivery_summary': 'SUMMARY', 'delivery_evidence': {'new_delivery': True, 'status': 'sent'}}}
    tools.set_pending_ppt_delivery_summary('stale task summary')
    tools._wrap_segment_result(result, holder)
    assert tools.take_pending_ppt_delivery_summary() == ''


def test_pending_preserves_evidence_and_persist_failure_does_not_negate_send():
    result = {'success': True, 'mode': 'segment', 'status': 'ok', 'delivery_confirmed': True,
              'delivery_owner': 'delivery', 'skill': 'demo', 'ledger': {'task_key': 'A', 'persisted': False}}
    holder = {'delivery': {'delivery_summary': 'SUMMARY', 'files': [{'path': 'a.pptx'}],
                          'delivery_evidence': {'new_delivery': True, 'status': 'sent', 'sha256': 'digest'}}}
    wrapped = tools._wrap_segment_result(result, holder)
    pending = tools.take_pending_ppt_delivery_summary()
    assert wrapped['delivery_confirmed'] and wrapped['ledger']['persisted'] is False
    assert pending['delivery_evidence']['task_key'] == 'A'
    tools.set_pending_ppt_delivery_summary(pending)
    assert tools.take_pending_ppt_delivery_summary() == pending

from segment_state_fixtures import storage
from _segment_fixture import isolated, segment

@pytest.mark.parametrize('existing', [False, True])
async def test_load_progress_uses_readonly_ledger_and_distinguishes_read_failure(disclosure, storage, tmp_path, monkeypatch, existing):
    from openjiuwen.core.single_agent import AgentCard
    from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger
    host, _, _ = disclosure
    host.card = AgentCard(id='segment-agent', name='segment-agent', description='test')
    rail = SkillAcceleratorRail()
    rail.init(host)
    session = storage.session()
    ctx = SimpleNamespace(session=session)
    monkeypatch.setattr('jiuwenswarm.agents.harness.common.rails.task_execution_rail.get_current_task_id', lambda: '')
    if existing:
        await SkillRunLedger.open(session, skill='demo-craft', workspace_base=str(tmp_path), query='original')
    writes = storage.cp.writes
    token = tools.set_current_request_metadata({'output_dir': str(tmp_path)})
    try:
        progress = await rail._progress(ctx, 'demo-craft')
        assert (isinstance(progress['progress'], dict)) is existing
        if not existing:
            assert progress['progress'] == 'no_progress'
        assert storage.cp.writes == writes
        storage.cp.fail_next_read = True
        assert (await rail._progress(ctx, 'demo-craft'))['progress'] == 'unavailable'
        assert storage.cp.writes == writes
    finally:
        tools.reset_current_request_metadata(token)


@pytest.mark.parametrize('remaining', [True, False])
async def test_flash_structured_confirmation_keeps_pending_owner_and_respects_remaining_work(remaining):
    from openjiuwen.core.foundation.llm import AssistantMessage
    from jiuwenswarm.agents.harness.flash.rails.flash_delivery_summary_rail import FlashDeliverySummaryRail
    rail = FlashDeliverySummaryRail()
    call = ToolCall(id='outer', type='function', name='skill_acceleration_exec', arguments='{"finish_request":true}')
    history = []
    async def add(message):
        history.append(message)
    ctx = AgentCallbackContext(agent=SimpleNamespace(context_engine=None), session=SimpleNamespace(write_stream=AsyncMock()),
        context=SimpleNamespace(add_messages=add), inputs=ModelCallInputs(response=AssistantMessage(content='', tool_calls=[call])))
    await rail.before_invoke(ctx)
    await rail.after_model_call(ctx)
    evidence = {'outer_tool_call_id': 'outer', 'task_key': 'A', 'receipt': {'status': 'sent'}}
    result = {'mode': 'segment', 'status': 'ok', 'success': True, 'delivery_confirmed': True,
              'delivery_evidence': evidence, 'outer_work_remaining': remaining}
    ctx.inputs = ToolCallInputs(tool_call=call, tool_name=call.name, tool_result=result)
    tools.set_pending_ppt_delivery_summary({'summary': 'summary A', 'delivery_confirmed': True, 'delivery_evidence': evidence})
    try:
        await rail.after_tool_call(ctx)
        await rail.after_react_iteration(ctx)
        assert [m.content for m in history] == ['summary A']
        assert ctx.has_force_finish_request is (not remaining)
        payload = ctx.session.write_stream.call_args.args[0].payload
        assert payload['turbo_delivery']['delivery_evidence'] == evidence
    finally:
        await rail.after_invoke(ctx)

async def test_sent_owner_survives_later_result_processing_failure(segment, monkeypatch):
    from jiuwenswarm.server.runtime.skill_turbo import segment_execution
    segment.control['root_output'] = {'__artifact__': {
        'info': {'send_file_status': 'sent'}, 'files': [{'path': 'verified.pptx'}],
        'delivery_evidence': {'status': 'sent', 'new_delivery': True}}}
    monkeypatch.setattr(segment_execution, 'finalize_segment_result', AsyncMock(side_effect=ValueError('post-send processing failed')))
    result, _ = await segment.run()
    assert result['status'] == 'failed' and result['success'] is False
    assert result['delivery_confirmed'] is True and result['delivery_owner']
    assert segment.calls['root'] == 1
    tools._wrap_segment_result(result, {})
    assert tools.take_pending_ppt_delivery_summary() == ''

@pytest.mark.parametrize('chunk_type', ['llm_output', 'tool_call', 'tool_result'])
def test_adapter_retains_delivery_correlation_on_existing_event_types(chunk_type):
    from openjiuwen.core.session.stream.base import OutputSchema
    from jiuwenswarm.server.runtime.agent_adapter.deep.stream_parse import _parse_stream_chunk
    context = {'skill': 'demo-craft', 'task_key': 'A', 'outer_tool_call_id': 'outer'}
    delivery = {'summary': 'confirmed summary', 'delivery_confirmed': True, 'delivery_evidence': context}
    payload = {'content': 'summary', 'turbo_context': context, 'turbo_delivery': delivery}
    if chunk_type == 'tool_call':
        payload['tool_call'] = {'id': 'child', 'name': 'send_file_to_user', 'arguments': '{}'}
    if chunk_type == 'tool_result':
        payload['tool_result'] = {'tool_call_id': 'child', 'tool_name': 'send_file_to_user', 'raw_output': {'success': True}}
    parsed = _parse_stream_chunk(OutputSchema(type=chunk_type, index=0, payload=payload))
    assert parsed['turbo_context'] == context
    assert parsed['turbo_delivery'] == delivery

@pytest.mark.parametrize('kind', ['abort', 'cancelled'])
async def test_delivery_summary_does_not_swallow_control_signals(monkeypatch, kind):
    import asyncio
    from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
    error = AbortError(reason='stop delivery processing') if kind == 'abort' else asyncio.CancelledError()
    monkeypatch.setattr(tools, '_ppt_delivery_summary_start', lambda: 'SUMMARY')
    tools.set_pending_ppt_delivery_summary('SUMMARY confirmed earlier')
    call = ToolCall(id='outer', type='function', name='skill_acceleration_exec', arguments='{}')
    ctx = SimpleNamespace(inputs=ToolCallInputs(tool_call=call, tool_name=call.name,
        tool_result={'success': True}), session=SimpleNamespace(write_stream=AsyncMock(side_effect=error)), exception=None)
    with pytest.raises(type(error)):
        await SkillTurboDeliverySummaryRail().after_tool_call(ctx)
    assert tools.take_pending_ppt_delivery_summary() == ''
