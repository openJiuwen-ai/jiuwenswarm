"""状态前置保存与结果保存的不同失败语义。"""

from unittest.mock import AsyncMock

import pytest

from _segment_fixture import segment, storage, isolated
from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger


@pytest.mark.parametrize('mode', ['before', 'drop', 'after', 'confirm'])
async def test_creation_without_confirmed_save_does_not_start_business(segment, mode):
    segment.storage.cp.mode = mode
    result, _ = await segment.run()
    assert result['success'] is False
    assert not segment.calls
    assert not list(segment.workspace.rglob('prep.json'))


async def test_result_save_failure_keeps_business_result_and_files(segment, monkeypatch):
    monkeypatch.setattr(SkillRunLedger, 'save', AsyncMock(return_value=False))
    result, _ = await segment.run()
    assert result['status'] == 'ok' and result['success'] is True
    assert result['ledger']['persisted'] is False
    assert {'code': 'ledger_save_failed'} in result['warnings']
    assert list(segment.workspace.rglob('prep.json'))


async def test_retry_requires_persisted_invalidation(segment, monkeypatch):
    segment.control['status'] = 'failed'
    first, _ = await segment.run()
    monkeypatch.setattr(SkillRunLedger, 'prepare_rerun', AsyncMock(return_value=False))
    segment.control['status'] = 'ok'
    result, _ = await segment.run()
    assert result['error'] == 'rerun_invalidation_not_persisted'
    assert segment.calls == {'first': 1, 'root': 1}
    assert result['ledger']['revision'] == first['ledger']['revision']
