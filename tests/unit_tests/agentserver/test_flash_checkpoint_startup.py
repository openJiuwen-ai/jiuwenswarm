import asyncio
from contextvars import ContextVar
from threading import Event
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_flash


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ready", "config_error", "checkpoint_error", "cancel"])
async def test_create_instance_waits_for_checkpoint_and_cleans_up(monkeypatch, outcome):
    adapter = object.__new__(interface_flash.JiuwenSwarmFlashAdapter)
    adapter._checkpointer = None
    loop = asyncio.get_running_loop()
    tenant = ContextVar("checkpoint_test_tenant", default=None)
    token = tenant.set("tenant-a")
    checkpoint_started = Event()
    config_resolved = asyncio.Event()
    release = asyncio.Event()
    checkpoint_finished = asyncio.Event()
    source = {"react": {"max_iterations": 7}}

    async def checkpoint():
        assert tenant.get() == "tenant-a"
        checkpoint_started.set()
        try:
            await release.wait()
            if outcome == "checkpoint_error":
                raise RuntimeError("checkpoint failed")
        finally:
            checkpoint_finished.set()

    def resolve(config):
        assert config is source
        assert tenant.get() == "tenant-a"
        assert checkpoint_started.wait(5) and not checkpoint_finished.is_set()
        loop.call_soon_threadsafe(config_resolved.set)
        if outcome == "config_error":
            raise ValueError("invalid config")
        return config

    class AssemblyReached(Exception):
        pass

    def bind_request():
        assert checkpoint_finished.is_set()
        raise AssemblyReached

    setup = AsyncMock(side_effect=checkpoint)
    monkeypatch.setattr(adapter, "set_checkpoint", setup)
    monkeypatch.setattr(interface_flash, "_resolve_instance_config_base", resolve)
    monkeypatch.setattr(adapter, "_bind_request_env_overlay", bind_request)
    build = asyncio.create_task(adapter.create_instance(mode="flash", config_base=source))
    try:
        await asyncio.wait_for(config_resolved.wait(), 5)
        if outcome in ("ready", "checkpoint_error"):
            assert not build.done()
            release.set()
        elif outcome == "cancel":
            build.cancel()
        expected = {
            "ready": AssemblyReached,
            "config_error": ValueError,
            "checkpoint_error": RuntimeError,
            "cancel": asyncio.CancelledError,
        }
        with pytest.raises(expected[outcome]):
            await asyncio.wait_for(build, 5)
        setup.assert_awaited_once_with()
        assert checkpoint_finished.is_set()
        assert source == {"react": {"max_iterations": 7}}
    finally:
        release.set()
        build.cancel()
        await asyncio.gather(build, return_exceptions=True)
        tenant.reset(token)


@pytest.mark.asyncio
async def test_flash_checkpoint_sqlite_round_trip_and_inherited_handle(tmp_path, monkeypatch):
    from openjiuwen.core.session.config.base import Config
    from openjiuwen.core.session.internal.agent import AgentSession
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    monkeypatch.setattr(interface_deep, "is_enterprise", lambda: False)
    adapter = object.__new__(interface_flash.JiuwenSwarmFlashAdapter)
    adapter._user_workspace_dir = tmp_path
    adapter._checkpoint_init_lock = asyncio.Lock()
    adapter._checkpointer = None
    config = await adapter._prepare_instance_config_and_checkpoint({"react": {"max_iterations": 7}})
    assert config["react"]["max_iterations"] == 7
    checkpoint = adapter._checkpointer
    try:
        writer = AgentSession(session_id="flash-checkpoint", config=Config())
        writer.agent_id = Mock(return_value="agent-a")
        writer.state().update({"saved_value": "before-restart"})
        await checkpoint.post_agent_execute(writer)
        assert (tmp_path / ".checkpoint" / "checkpoint.db").is_file()

        child = object.__new__(interface_flash.JiuwenSwarmFlashAdapter)
        child.copy_tenant_env_bindings_from(adapter)
        await child._prepare_instance_config_and_checkpoint({"react": {}})
        assert child._checkpointer is checkpoint
        reader = AgentSession(session_id="flash-checkpoint", config=Config())
        reader.agent_id = writer.agent_id
        await child._checkpointer.pre_agent_execute(reader, None)
        assert reader.state().get("saved_value") == "before-restart"
    finally:
        await checkpoint._kv_store.engine.dispose()
