"""Unit tests for VibeSkillSessionDcsStore (Redis I/O layer)."""
from __future__ import annotations

import time
from typing import Any

import pytest

from jiuwenclaw.channel.vibeskill_session import (
    VibeSkillSession,
    VibeSkillSessionState,
)
from jiuwenclaw.channel.vibeskill_session_dcs_store import (
    VibeSkillSessionDcsConfig,
    VibeSkillSessionDcsStore,
)


class InMemoryFakeRedis:
    """Minimal in-memory fake for RedisCluster methods used by the store."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._ttl: dict[str, int] = {}
        self.closed = False
        self.fail_next_set = False
        self.fail_next_get = False
        self.fail_next_delete = False
        self.fail_next_expire = False

    async def get(self, key: str) -> str | None:
        if self.fail_next_get:
            self.fail_next_get = False
            raise RuntimeError("simulated GET failure")
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> bool:
        if self.fail_next_set:
            self.fail_next_set = False
            raise RuntimeError("simulated SET failure")
        self._data[key] = str(value)
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        if self.fail_next_expire:
            self.fail_next_expire = False
            raise RuntimeError("simulated EXPIRE failure")
        if key in self._data:
            self._ttl[key] = seconds
            return True
        return False

    async def delete(self, *keys: str) -> int:
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise RuntimeError("simulated DEL failure")
        deleted = 0
        for k in keys:
            if k in self._data:
                del self._data[k]
                self._ttl.pop(k, None)
                deleted += 1
        return deleted

    async def aclose(self) -> None:
        self.closed = True

    def get_sync(self, key: str) -> str | None:
        return self._data.get(key)

    def ttl_sync(self, key: str) -> int | None:
        return self._ttl.get(key)


def _make_store(ttl: int = 86400) -> tuple[VibeSkillSessionDcsStore, InMemoryFakeRedis]:
    cfg = VibeSkillSessionDcsConfig(host="fake", port=6379, password=None, ttl_seconds=ttl)
    store = VibeSkillSessionDcsStore(cfg)
    fake = InMemoryFakeRedis()
    store._dcs._client = fake
    return store, fake


def _make_session(
    *,
    session_id: str = "vibeskill_a",
    state: VibeSkillSessionState = VibeSkillSessionState.IDLE,
    mode: str = "SkillCreate",
    metadata: dict[str, Any] | None = None,
) -> VibeSkillSession:
    now = time.time()
    return VibeSkillSession(
        session_id=session_id,
        state=state,
        created_at=now,
        updated_at=now,
        metadata=dict(metadata or {"user_id": "u1"}),
        mode=mode,
    )


@pytest.mark.asyncio
async def test_save_session_writes_main_key_with_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_DCS_TTL_SECONDS", "60")
    store, fake = _make_store()
    session = _make_session(session_id="vibeskill_a")

    await store.save_session(session)

    raw = fake.get_sync("jiuwen:vibeskillSession:vibeskill_a")
    assert raw is not None
    assert '"session_id": "vibeskill_a"' in raw
    assert fake.ttl_sync("jiuwen:vibeskillSession:vibeskill_a") == 60


@pytest.mark.asyncio
async def test_save_session_default_no_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_DCS_TTL_SECONDS", raising=False)
    store, fake = _make_store()
    await store.save_session(_make_session(session_id="vibeskill_a"))

    assert fake.ttl_sync("jiuwen:vibeskillSession:vibeskill_a") is None


@pytest.mark.asyncio
async def test_load_session_roundtrip() -> None:
    store, _ = _make_store()
    saved = _make_session(
        session_id="vibeskill_a",
        state=VibeSkillSessionState.BUSY,
        mode="Standard",
        metadata={"user_id": "u9"},
    )
    saved.exportable = True

    await store.save_session(saved)
    loaded = await store.load_session("vibeskill_a")

    assert loaded is not None
    assert loaded.session_id == "vibeskill_a"
    assert loaded.state is VibeSkillSessionState.BUSY
    assert loaded.mode == "Standard"
    assert loaded.metadata == {"user_id": "u9"}
    assert loaded.exportable is True


@pytest.mark.asyncio
async def test_load_session_missing_exportable_defaults_false() -> None:
    store, fake = _make_store()
    fake._data["jiuwen:vibeskillSession:vibeskill_a"] = (
        '{"session_id":"vibeskill_a","state":"completed","mode":"SkillCreate",'
        '"created_at":1.0,"updated_at":1.0,"metadata":{}}'
    )

    loaded = await store.load_session("vibeskill_a")
    assert loaded is not None
    assert loaded.exportable is False
    assert loaded.last_export_obs_url == ""
    assert loaded.file_ready_obs_urls == []


@pytest.mark.asyncio
async def test_load_session_obs_urls_roundtrip() -> None:
    store, _ = _make_store()
    saved = _make_session(session_id="vibeskill_a")
    saved.last_export_obs_url = "https://obs/export.zip"
    saved.file_ready_obs_urls = [
        "https://obs/file-a.png",
        "https://obs/file-b.png",
    ]

    await store.save_session(saved)
    loaded = await store.load_session("vibeskill_a")

    assert loaded is not None
    assert loaded.last_export_obs_url == "https://obs/export.zip"
    assert loaded.file_ready_obs_urls == [
        "https://obs/file-a.png",
        "https://obs/file-b.png",
    ]


@pytest.mark.asyncio
async def test_load_session_missing_returns_none() -> None:
    store, _ = _make_store()
    assert await store.load_session("nope") is None


@pytest.mark.asyncio
async def test_load_session_corrupt_json_returns_none() -> None:
    store, fake = _make_store()
    fake._data["jiuwen:vibeskillSession:vibeskill_a"] = "not-json{"

    assert await store.load_session("vibeskill_a") is None


@pytest.mark.asyncio
async def test_load_session_legacy_internal_id_field() -> None:
    store, fake = _make_store()
    fake._data["jiuwen:vibeskillSession:vibeskill_a"] = (
        '{"internal_id":"vibeskill_a","state":"busy","mode":"SkillCreate",'
        '"created_at":1.0,"updated_at":1.0,"metadata":{}}'
    )

    loaded = await store.load_session("vibeskill_a")
    assert loaded is not None
    assert loaded.session_id == "vibeskill_a"
    assert loaded.state is VibeSkillSessionState.BUSY


@pytest.mark.asyncio
async def test_delete_session_removes_key() -> None:
    store, fake = _make_store()
    await store.save_session(_make_session(session_id="vibeskill_a"))

    await store.delete_session("vibeskill_a")

    assert fake.get_sync("jiuwen:vibeskillSession:vibeskill_a") is None


@pytest.mark.asyncio
async def test_save_session_propagates_set_failure() -> None:
    store, fake = _make_store()
    fake.fail_next_set = True

    with pytest.raises(RuntimeError, match="simulated SET failure"):
        await store.save_session(_make_session(session_id="vibeskill_a"))


@pytest.mark.asyncio
async def test_close_calls_aclose() -> None:
    store, fake = _make_store()
    await store.save_session(_make_session(session_id="vibeskill_a"))

    await store.close()

    assert fake.closed is True
    assert store._dcs._client is None


@pytest.mark.asyncio
async def test_from_env_returns_none_without_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_DCS_HOST", raising=False)
    assert VibeSkillSessionDcsStore.from_env() is None


@pytest.mark.asyncio
async def test_from_env_constructs_when_host_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_DCS_HOST", "dcs.example.com")
    monkeypatch.setenv("SANDBOX_DCS_PORT", "1234")
    monkeypatch.setenv("SANDBOX_DCS_TTL_SECONDS", "60")

    store = VibeSkillSessionDcsStore.from_env()
    assert store is not None
    assert store.host == "dcs.example.com"
    assert store._config.port == 1234
    assert store._ttl_seconds == 60
