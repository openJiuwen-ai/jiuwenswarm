"""Cross-instance tests: two VibeSkillSessionStore instances sharing one DCS backend."""
from __future__ import annotations

from typing import Any

import pytest

from jiuwenclaw.channel.vibeskill_session import (
    VibeSkillSessionState,
    VibeSkillSessionStore,
)
from jiuwenclaw.channel.vibeskill_session_dcs_store import (
    VibeSkillSessionDcsConfig,
    VibeSkillSessionDcsStore,
)


class InMemoryFakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self.set_should_fail = False

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> bool:
        if self.set_should_fail:
            raise RuntimeError("simulated DCS failure")
        self._data[key] = str(value)
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        return key in self._data

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for k in keys:
            if k in self._data:
                del self._data[k]
                deleted += 1
        return deleted

    async def aclose(self) -> None:
        pass


@pytest.fixture
def shared_redis() -> InMemoryFakeRedis:
    return InMemoryFakeRedis()


def _make_dcs(shared_redis: InMemoryFakeRedis) -> VibeSkillSessionDcsStore:
    cfg = VibeSkillSessionDcsConfig(host="fake", port=6379, ttl_seconds=86400)
    dcs = VibeSkillSessionDcsStore(cfg)
    dcs._dcs._client = shared_redis
    return dcs


@pytest.mark.asyncio
async def test_gw2_resolves_session_created_on_gw1(shared_redis: InMemoryFakeRedis) -> None:
    store1 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))
    store2 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))

    session = await store1.get_or_create(session_id=None, mode="SkillCreate")
    sid = session.session_id

    resolved = await store2.resolve_session(sid)
    assert resolved is not None
    assert resolved.session_id == sid
    assert sid in store2._sessions


@pytest.mark.asyncio
async def test_gw2_observes_state_change_from_gw1(shared_redis: InMemoryFakeRedis) -> None:
    store1 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))
    store2 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))

    session = await store1.get_or_create(session_id=None, mode="SkillCreate")
    sid = session.session_id
    await store1.set_state(sid, VibeSkillSessionState.BUSY)

    assert await store2.get_state(sid) is VibeSkillSessionState.BUSY


@pytest.mark.asyncio
async def test_metadata_replicated_via_dcs(shared_redis: InMemoryFakeRedis) -> None:
    store1 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))
    store2 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))

    session = await store1.get_or_create(session_id=None, mode="SkillCreate")
    sid = session.session_id
    await store1.set_metadata(sid, {"user_id": "user_42"})

    loaded = await store2.get_session(sid)
    assert loaded is not None
    assert store2.get_user_id(sid) == "user_42"


@pytest.mark.asyncio
async def test_delete_on_gw1_visible_on_gw2(shared_redis: InMemoryFakeRedis) -> None:
    store1 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))
    store2 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))

    session = await store1.get_or_create(session_id=None, mode="SkillCreate")
    sid = session.session_id
    await store2.resolve_session(sid)
    assert sid in store2._sessions

    await store1.delete_session(sid)

    store2._sessions.clear()
    assert await store2.resolve_session(sid) is None


@pytest.mark.asyncio
async def test_dcs_save_failure_does_not_pollute_local_memory(
    shared_redis: InMemoryFakeRedis,
) -> None:
    store = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))
    shared_redis.set_should_fail = True

    with pytest.raises(RuntimeError, match="simulated DCS failure"):
        await store.get_or_create(session_id=None, mode="SkillCreate")

    assert store._sessions == {}


@pytest.mark.asyncio
async def test_store_without_dcs_behaves_as_before() -> None:
    store = VibeSkillSessionStore(dcs_store=None)

    session = await store.get_or_create(session_id=None, mode="SkillCreate")
    assert session.session_id.startswith("vibeskill_")

    await store.set_state(session.session_id, VibeSkillSessionState.BUSY)
    assert await store.get_state(session.session_id) is VibeSkillSessionState.BUSY

    deleted = await store.delete_session(session.session_id)
    assert deleted is True
    assert await store.resolve_session(session.session_id) is None


@pytest.mark.asyncio
async def test_gw2_observes_exportable_change_from_gw1(shared_redis: InMemoryFakeRedis) -> None:
    store1 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))
    store2 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))

    session = await store1.get_or_create(session_id=None, mode="SkillCreate")
    sid = session.session_id
    await store1.set_exportable(sid, True)

    loaded = await store2.get_session(sid)
    assert loaded is not None
    assert loaded.exportable is True


@pytest.mark.asyncio
async def test_list_sessions_returns_only_local_view(shared_redis: InMemoryFakeRedis) -> None:
    store1 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))
    store2 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))

    await store1.get_or_create(session_id=None, mode="SkillCreate")
    await store1.get_or_create(session_id=None, mode="SkillCreate")

    assert len(await store1.list_sessions()) == 2
    assert await store2.list_sessions() == []


@pytest.mark.asyncio
async def test_export_and_file_ready_obs_urls_replicated_via_dcs(
    shared_redis: InMemoryFakeRedis,
) -> None:
    store1 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))
    store2 = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))

    session = await store1.get_or_create(session_id=None, mode="SkillCreate")
    sid = session.session_id
    await store1.set_last_export_obs_url(sid, "https://obs/export-v1.zip")
    await store1.append_file_ready_obs_url(sid, "https://obs/file-a.png")
    await store1.append_file_ready_obs_url(sid, "https://obs/file-b.png")

    loaded = await store2.get_session(sid)
    assert loaded is not None
    assert loaded.last_export_obs_url == "https://obs/export-v1.zip"
    assert loaded.file_ready_obs_urls == [
        "https://obs/file-a.png",
        "https://obs/file-b.png",
    ]


@pytest.mark.asyncio
async def test_set_last_export_obs_url_returns_previous(shared_redis: InMemoryFakeRedis) -> None:
    store = VibeSkillSessionStore(dcs_store=_make_dcs(shared_redis))
    session = await store.get_or_create(session_id=None, mode="SkillCreate")
    sid = session.session_id

    assert await store.set_last_export_obs_url(sid, "https://obs/export-v1.zip") == ""
    assert await store.set_last_export_obs_url(sid, "https://obs/export-v2.zip") == (
        "https://obs/export-v1.zip"
    )

    loaded = await store.get_session(sid)
    assert loaded is not None
    assert loaded.last_export_obs_url == "https://obs/export-v2.zip"
