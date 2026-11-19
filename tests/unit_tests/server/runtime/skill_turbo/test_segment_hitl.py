"""真实ask_user中断、原卡恢复与定义漂移；存储和业务文件临时隔离。"""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from _segment_fixture import segment, storage, isolated
from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager
from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger


async def pause(segment):
    try:
        result, _ = await segment.run(params={'pause': True})
    except AbortError:
        pass
    else:
        pytest.fail(f'expected native pause, received {result}')
    checkpoint = await ResumeContextManager(segment.storage.session()).load()
    assert checkpoint and checkpoint['segment']['accelerator'] == 'prep'
    assert checkpoint['pending_tool_call_id'].startswith('skill_turbo-tc-ask_user-')
    return checkpoint


async def test_resume_reuses_task_inputs_and_completed_children(segment):
    checkpoint = await pause(segment)
    result, events = await segment.run(params={'pause': True}, resume_ctx=checkpoint,
                                        answers=[{'question': '确认继续？', 'selected_options': ['继续']}])
    assert result['status'] == 'ok'
    assert segment.calls == {'first': 1, 'root': 1}
    assert result['ledger']['task_key'] == checkpoint['segment']['task_key']
    assert await ResumeContextManager(segment.storage.session()).load() is None
    assert not any(e.payload and e.payload.get('event_type') == 'chat.error' for e in events)


@pytest.mark.parametrize('mutation', ['comment', 'description', 'add', 'rename', 'delete', 'version'])
async def test_execution_definition_drift_refuses_resume_without_nodes(segment, mutation):
    checkpoint = await pause(segment)
    if mutation == 'comment':
        segment.code.write_text(segment.code.read_text(encoding='utf-8') + '\n# changed\n', encoding='utf-8')
    elif mutation == 'description':
        segment.raw['accelerators'][0]['summary'] = '新介绍'
    elif mutation == 'version':
        segment.raw['manifest_version'] = 2
    elif mutation == 'add':
        (segment.code.parent / 'shared.py').write_text('VALUE = 1\n', encoding='utf-8')
    elif mutation == 'rename':
        segment.code.rename(segment.code.with_name('renamed.py'))
    else:
        segment.code.unlink()
    if mutation in {'description', 'version'}:
        (segment.skill / 'turbo/accelerators.json').write_text(json.dumps(segment.raw), encoding='utf-8')
    result, _ = await segment.run(params={'pause': True}, resume_ctx=checkpoint, answers=[{}])
    assert result['status'] == 'failed' and result['success'] is False
    assert segment.calls == {'first': 1}
    assert await ResumeContextManager(segment.storage.session()).load() is None


async def test_no_checkpoint_answers_never_fresh_execute(segment):
    result, _ = await segment.run(answers=[{}])
    assert result['error'] == 'resume_context_unavailable'
    assert not segment.calls


async def test_can_pause_false_has_no_resume_or_pause_frames(segment):
    segment.raw['accelerators'][0]['hitl'] = {'can_pause': False}
    (segment.skill / 'turbo/accelerators.json').write_text(json.dumps(segment.raw), encoding='utf-8')
    result, events = await segment.run(params={'pause': True})
    assert result['status'] == 'failed'
    assert result['error'] == '该段不支持人工中断'
    assert segment.calls == {'first': 1}
    assert await ResumeContextManager(segment.storage.session()).load() is None
    assert not any(e.payload and e.payload.get('event_type') in {
        '__interaction__', 'chat.ask_user_question', 'chat.invocation_paused', 'chat.error',
    } for e in events)


@pytest.mark.parametrize('can_pause', [True, False])
async def test_cancel_always_propagates_without_run(segment, can_pause):
    segment.raw['accelerators'][0]['hitl'] = {'can_pause': can_pause}
    (segment.skill / 'turbo/accelerators.json').write_text(json.dumps(segment.raw), encoding='utf-8')
    segment.control['exception'] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await segment.run()
    ledger = await SkillRunLedger.current(segment.storage.session(), skill='demo-craft',
                                         workspace_base=str(segment.workspace))
    assert ledger.data['runs'] == []


async def test_new_call_cannot_overwrite_pending_segment(segment):
    checkpoint = await pause(segment)
    result, _ = await segment.run()
    assert result['status'] == 'busy'
    assert segment.calls == {'first': 1}
    assert await ResumeContextManager(segment.storage.session()).load() == checkpoint


async def test_corrupt_control_cannot_resume(segment):
    checkpoint = await pause(segment)
    bad = copy.deepcopy(checkpoint)
    bad['segment']['task_key'] = 'different'
    result, _ = await segment.run(params={'pause': True}, resume_ctx=bad, answers=[{}])
    assert result['success'] is False
    assert not segment.calls.get('root')


async def test_legacy_save_explicitly_removes_segment_and_inflight_merge(segment):
    checkpoint = await pause(segment)
    manager = ResumeContextManager(segment.storage.session())
    await manager.mark_in_flight(checkpoint)
    await manager.clear_in_flight()
    actual = await ResumeContextManager(segment.storage.session()).load()
    assert actual.get('resume_in_flight') is None
    await manager.save(plan_code='legacy', inputs={'query': 'legacy'}, pending_tool_call_id='legacy-tcid')
    actual = await ResumeContextManager(segment.storage.session()).load()
    assert actual.get('segment') is None


@pytest.mark.parametrize('field', ['task_states', 'pending_tool_call_id', 'inputs'])
async def test_corrupted_persisted_replay_state_is_rejected_and_cleared(segment, field):
    from jiuwenswarm.server.runtime.skill_turbo.resume_context import isolated_skill_state, SKILL_TURBO_RESUME_CTX_KEY

    checkpoint = await pause(segment)
    if field == 'task_states':
        checkpoint[field][0]['status'] = 'pending'
    elif field == 'inputs':
        checkpoint[field]['run_dir'] = str(segment.workspace)
    else:
        checkpoint[field] = 'unrelated-tcid'
    async with isolated_skill_state(segment.storage.session(), write=True) as state:
        state.replace(SKILL_TURBO_RESUME_CTX_KEY, checkpoint)
    result, _ = await segment.run(params={'pause': True}, resume_ctx=checkpoint, answers=[{}])
    assert result['status'] == 'failed'
    assert segment.calls == {'first': 1}
    assert await ResumeContextManager(segment.storage.session()).load() is None


async def test_resume_cannot_inherit_bucket_with_wrong_hash(segment):
    from jiuwenswarm.server.runtime.skill_turbo.node_artifact_store import save_node_artifacts

    checkpoint = await pause(segment)
    await save_node_artifacts(segment.storage.session(), skill='demo-craft', nodes={'first': {'status': 'completed'}},
                              namespace=checkpoint['segment']['namespace'], plan_code_hash='wrong')
    result, _ = await segment.run(params={'pause': True}, resume_ctx=checkpoint, answers=[{}])
    assert result['status'] == 'failed'
    assert segment.calls == {'first': 1}
    assert await ResumeContextManager(segment.storage.session()).load() is None


async def test_cancel_after_pause_keeps_files_without_completion_run(segment):
    checkpoint = await pause(segment)
    work = Path(checkpoint['inputs']['run_dir']) / 'keep.md'
    work.write_text('保留', encoding='utf-8')
    segment.control['exception'] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await segment.run(params={'pause': True}, resume_ctx=checkpoint,
                          answers=[{'question': '确认继续？', 'selected_options': ['继续']}])
    assert await ResumeContextManager(segment.storage.session()).load() is None
    assert work.read_text(encoding='utf-8') == '保留'
    ledger = await SkillRunLedger.current(segment.storage.session(), skill='demo-craft', workspace_base=str(segment.workspace))
    assert ledger.data['runs'] == []
