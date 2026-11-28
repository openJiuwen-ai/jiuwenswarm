"""显式单段必须经过真实节点，判定业务结果并交回外层。"""

import json

import pytest

from _segment_fixture import segment, storage, isolated


async def test_selected_skill_root_executes_without_planner(segment):
    value = segment.new()
    inputs = {**segment.inputs, 'run_dir': str(segment.workspace)}
    try:
        events = [event async for event in value.run_skill_stream('demo-craft', inputs, 'root-request', 'test')]
        assert segment.calls == {'root': 1, 'first': 1}
        assert (segment.workspace / 'prep.json').is_file()
        assert events
        value._planner.plan.assert_not_awaited()
    finally:
        value.release_registered_tools()


async def test_exact_segment_executes_without_planner_and_records_result(segment):
    result, events = await segment.run()
    assert result['status'] == 'ok' and result['success'] is True
    assert result['facts'] == {'prep_fact': 'ready'}
    assert result['ledger']['revision'] == 1 and result['ledger']['persisted'] is True
    assert result['delivery_confirmed'] is False
    assert segment.calls == {'root': 1, 'first': 1}
    assert list(segment.workspace.rglob('prep.json'))
    assert '__segment_handoff__' not in json.dumps([e.payload for e in events])
    assert '任务已结束' not in str(result)


@pytest.mark.parametrize('status,child,artifact,expected', [
    ('failed', 'ok', True, 'failed'), ('ok', 'failed', True, 'failed'),
    ('ok', 'partial', True, 'partial'), ('ok', 'ok', False, 'partial'),
    (None, 'ok', True, 'ok'), (None, 'ok', False, 'partial'),
])
async def test_final_status_uses_root_children_and_postconditions(segment, status, child, artifact, expected):
    segment.control.update(status=status, child_status=child, artifact=artifact)
    result, events = await segment.run()
    assert result['status'] == expected
    assert not any(e.payload and e.payload.get('event_type') == 'chat.error' for e in events)


async def test_repeated_completed_segment_skips_without_new_run(segment):
    first, _ = await segment.run()
    second, _ = await segment.run(query='changed request')
    assert second['already_done'] is True
    assert second['ledger']['task_key'] == first['ledger']['task_key']
    assert second['ledger']['revision'] == 1
    assert segment.calls == {'first': 1, 'root': 1}


async def test_unknown_business_parameter_does_not_execute_or_record(segment):
    result, _ = await segment.run(params={'force': True})
    assert result['status'] == 'invalid_args'
    assert not segment.calls


async def test_cached_declaration_cannot_authorize_disabled_skill(segment):
    value = segment.new()
    meta = segment.skill / 'turbo/meta.json'
    meta.write_text(json.dumps({'external_name': 'demo-craft', 'enable_turbo': False}), encoding='utf-8')
    result, _ = await segment.run(instance=value)
    assert result['success'] is False
    assert not segment.calls
