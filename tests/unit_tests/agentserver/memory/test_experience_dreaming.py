import asyncio
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server import experience_dreaming


@pytest.mark.asyncio
async def test_first_registration_uses_eight_hours_and_checks_live_swarm_activity(monkeypatch):
    busy = True
    registered = asyncio.Event()

    async def ensure(*, interval, ready):
        assert interval == 8 * 60 * 60
        registered.set()
        return "experience-timer"

    provider = SimpleNamespace(
        is_initialized=True,
        ensure_experience_dreaming=AsyncMock(side_effect=ensure),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(
        experience_dreaming, "build_experience_dreaming_provider", lambda: (provider, {}),
    )
    service = experience_dreaming.AgentMemoryExperienceDreamingService(lambda: busy)
    service.start()
    try:
        await asyncio.wait_for(registered.wait(), timeout=2)
        ready = provider.ensure_experience_dreaming.await_args.kwargs["ready"]
        assert not await asyncio.to_thread(asyncio.run, ready())
        busy = False
        assert await asyncio.to_thread(asyncio.run, ready())
    finally:
        await service.stop()
    assert not await ready()
    provider.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_sdk_restores_the_original_due_time(tmp_path):
    adapter = pytest.importorskip("jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider")
    if not callable(getattr(adapter.AgentMemoryMemoryProvider, "ensure_experience_dreaming", None)):
        pytest.skip("Installed agent-memory SDK does not support experience Dreaming")
    config = {"memory": {"builtin": {"provider": "jiuwen", "jiuwen": {
        "mode": "sdk",
        "space": "test-experience",
        "experience_space": "own",
        "sdk": {
            "embedder": {"default": {"target": "hashing"}},
            "markdown_store": {"default": {"target": "local", "params": {"root": str(tmp_path)}}},
        },
    }}}}
    database = tmp_path / "memory" / "experience" / "experience-dreaming.db"

    async def ready():
        return False

    async def register():
        built = experience_dreaming.build_experience_dreaming_provider(config)
        assert built is not None
        provider, scope = built
        try:
            await provider.initialize(
                user_id=scope["user"], scope_id=scope["org"], agent_id=scope["agent"],
                register_experience_jobs=False,
            )
            job_id = await provider.ensure_experience_dreaming(interval=8 * 60 * 60, ready=ready)
        finally:
            await provider.shutdown()
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute("SELECT value FROM kv WHERE key LIKE 'dreaming:v2:%'").fetchall()
        assert len(rows) == 1
        entry = json.loads(rows[0][0])
        assert entry["job_id"] == job_id
        return entry

    original = await register()
    restored = await register()
    assert original["interval"] == restored["interval"] == 8 * 60 * 60
    assert original["next_due_at"] == restored["next_due_at"]
    assert restored["generation"] > original["generation"]


@pytest.mark.asyncio
async def test_stop_cancels_a_stalled_dreaming_registration(monkeypatch):
    registering, cleaned_up = asyncio.Event(), asyncio.Event()

    async def ensure(**_kwargs):
        registering.set()
        await asyncio.Event().wait()

    async def shutdown():
        cleaned_up.set()

    provider = SimpleNamespace(
        is_initialized=True,
        ensure_experience_dreaming=ensure,
        shutdown=shutdown,
    )
    monkeypatch.setattr(experience_dreaming, "_STOP_TIMEOUT", 0.05)
    monkeypatch.setattr(experience_dreaming, "build_experience_dreaming_provider", lambda: (provider, {}))
    service = experience_dreaming.AgentMemoryExperienceDreamingService(lambda: False)
    service.start()
    await asyncio.wait_for(registering.wait(), timeout=2)
    await asyncio.wait_for(service.stop(), timeout=2)
    await asyncio.wait_for(cleaned_up.wait(), timeout=2)
