"""Execution media permissions apply independently of channel attachments."""
import asyncio
import pytest
from jiuwenswarm.server.jiuwenmate.media_runtime import ExecutionMediaScope, run_media_stage

@pytest.mark.asyncio
async def test_current_output_allowed_but_other_session_and_private_cache_denied(tmp_path):
    session=tmp_path/'office'/'sessions'/'a'; output=session/'output';output.mkdir(parents=True)
    ours=output/'proof.png'
    foreign=tmp_path/'office'/'sessions'/'b'/'output'/'other.png';foreign.parent.mkdir(parents=True);foreign.write_bytes(b'image')
    private=tmp_path/'cache'/'original.png';private.parent.mkdir();private.write_bytes(b'image')
    scope=ExecutionMediaScope(session)
    ours.write_bytes(b'image')
    assert await scope.resolve(str(ours)) == str(ours.resolve())
    for path in [foreign,private]:
        with pytest.raises(ValueError):await scope.resolve(str(path))
    with pytest.raises(ValueError):await scope.resolve('https://example.com/private.png')

@pytest.mark.asyncio
async def test_same_session_outputs_remain_readable_across_executions(tmp_path):
    output=tmp_path/'output';output.mkdir();old=output/'old.png';old.write_bytes(b'old')
    scope=ExecutionMediaScope(tmp_path)
    assert await scope.resolve(str(old)) == str(old.resolve())
    old.write_bytes(b'updated')
    assert await scope.resolve(str(old)) == str(old.resolve())

@pytest.mark.asyncio
async def test_handle_abort_cancels_and_joins_media_preparation():
    from jiuwenswarm.server.jiuwenmate.execution_driver import ExecutionHandle
    started=asyncio.Event();cleaned=asyncio.Event()
    async def abort(context,reason):pass
    handle=ExecutionHandle(abort)
    async def download():
        try:started.set();await asyncio.Event().wait()
        finally:cleaned.set()
    task=asyncio.create_task(run_media_stage(download(),handle))
    await started.wait();await handle.abort('cancel')
    with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,1)
    assert cleaned.is_set()

@pytest.mark.parametrize('enabled',[True,False])
def test_vision_uses_saved_catalogue_instead_of_stale_environment(monkeypatch,enabled):
    from jiuwenswarm.common.multimodal_store import MultimodalStore
    from jiuwenswarm.server.jiuwenmate.vision_runtime import vision_config_from_env
    state={'tools':{'vision':{'enabled':enabled,'model_id':'model'}},'models':[
        {'model_id':'model','connection':{'api_key':'saved-key','api_base':'https://example.invalid/v1','model_name':'saved-model'}}]}
    monkeypatch.setattr(MultimodalStore,'load_catalogue_locked',staticmethod(lambda path:({},state)))
    monkeypatch.setenv('VISION_ENABLED','true');monkeypatch.setenv('VISION_API_KEY','stale-key')
    monkeypatch.setenv('VISION_MODEL','stale-model');monkeypatch.setenv('VISION_BASE_URL','https://stale.invalid')
    config=vision_config_from_env()
    if enabled:assert (config.model,config.api_key,config.base_url)==('saved-model','saved-key','https://example.invalid/v1')
    else:assert config is None

@pytest.mark.asyncio
async def test_same_session_output_survives_permission_resume(tmp_path):
    output = tmp_path / 'output'
    output.mkdir()
    image = output / 'proof.png'
    image.write_bytes(b'image')
    resumed = ExecutionMediaScope(tmp_path)
    assert await resumed.resolve('output/proof.png') == str(image)

@pytest.mark.asyncio
async def test_channel_attachment_directory_is_explicit_and_does_not_grant_other_messages(tmp_path):
    session = tmp_path / 'sessions' / 'a'
    session.mkdir(parents=True)
    incoming = tmp_path / 'incoming' / 'current-message'
    incoming.mkdir(parents=True)
    image = incoming / 'photo.png'
    image.write_bytes(b'image')
    other = tmp_path / 'incoming' / 'other-message'
    other.mkdir()
    (other / 'photo.png').write_bytes(b'image')
    scope = ExecutionMediaScope(session, inbound_directory=incoming)
    assert await scope.resolve(str(image)) == str(image)
    assert await scope.resolve('incoming/current-message/photo.png') == str(image)
    with pytest.raises(ValueError):
        await scope.resolve(str(other / 'photo.png'))

@pytest.mark.asyncio
async def test_source_revocation_also_blocks_generated_output(tmp_path):
    image = tmp_path / 'output' / 'proof.png'
    image.parent.mkdir()
    image.write_bytes(b'image')
    scope = ExecutionMediaScope(tmp_path)
    async def revoked():
        raise ValueError('原消息已撤回')
    scope.source_guard = revoked
    with pytest.raises(ValueError, match='原消息已撤回'):
        await scope.resolve(str(image))

@pytest.mark.asyncio
async def test_office_relative_current_output_and_symlink_escape(tmp_path):
    session = tmp_path / 'sessions' / 'ours'
    output = session / 'output'
    output.mkdir(parents=True)
    image = output / 'proof.png'
    image.write_bytes(b'image')
    foreign = tmp_path / 'foreign.png'
    foreign.write_bytes(b'private')
    (output / 'link.png').symlink_to(foreign)
    scope = ExecutionMediaScope(session)
    assert await scope.resolve('sessions/ours/output/proof.png') == str(image)
    with pytest.raises(ValueError):
        await scope.resolve('output/link.png')

@pytest.mark.asyncio
async def test_channel_directory_cannot_escape_through_parent_symlink(tmp_path):
    session = tmp_path / 'office' / 'sessions' / 'a'
    session.mkdir(parents=True)
    foreign = tmp_path / 'private' / 'message'
    foreign.mkdir(parents=True)
    (foreign / 'photo.png').write_bytes(b'private')
    (tmp_path / 'office' / 'incoming').symlink_to(foreign.parent, target_is_directory=True)
    scope = ExecutionMediaScope(session, inbound_directory=tmp_path / 'office' / 'incoming' / 'message')
    with pytest.raises(ValueError):
        await scope.resolve('incoming/message/photo.png')

@pytest.mark.asyncio
async def test_output_directory_symlink_does_not_expand_access_to_session_drafts(tmp_path):
    draft = tmp_path / 'draft.png'
    draft.write_bytes(b'private')
    (tmp_path / 'output').symlink_to(tmp_path, target_is_directory=True)
    scope = ExecutionMediaScope(tmp_path)
    with pytest.raises(ValueError):
        await scope.resolve('output/draft.png')


def test_broken_saved_catalogue_does_not_fall_back_to_legacy_credentials(monkeypatch):
    from jiuwenswarm.common.multimodal_models import MultimodalError
    from jiuwenswarm.common.multimodal_store import MultimodalStore
    from jiuwenswarm.server.jiuwenmate.vision_runtime import vision_config_from_env

    def broken(path):
        raise MultimodalError('MM_CONFIG_READ_FAILED')

    monkeypatch.setattr(MultimodalStore, 'load_catalogue_locked', staticmethod(broken))
    monkeypatch.setenv('VISION_ENABLED', 'true')
    monkeypatch.setenv('VISION_API_KEY', 'legacy-key')
    monkeypatch.setenv('VISION_MODEL', 'legacy-model')
    monkeypatch.setenv('VISION_BASE_URL', 'https://legacy.invalid')
    with pytest.raises(MultimodalError):
        vision_config_from_env()
