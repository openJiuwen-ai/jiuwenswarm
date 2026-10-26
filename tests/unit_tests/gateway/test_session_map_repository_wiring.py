# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SessionMap 企业版走 SessionMapRepository / 个人版走 JSON。"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.routing.session_map import SessionMap, SessionMapScope
from gateway.routing.session_map_access import (
    clear_session_map_repository,
    set_session_map_repository,
)
from gateway.routing.session_map_repository import (
    Session,
    SessionMapRepository,
)


class _DictPersistentStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def get(self, store_name: str, identity: dict):
        key = str(identity.get("identity_key") or "")
        return self.rows.get(key)

    async def list(self, store_name: str):
        return list(self.rows.values())

    async def create(self, store_name: str, record: dict):
        key = str(record.get("identity_key") or "")
        self.rows[key] = dict(record)
        return self.rows[key]

    async def update(self, store_name: str, identity: dict, updates: dict):
        key = str(identity.get("identity_key") or "")
        if key not in self.rows:
            return None
        self.rows[key].update(updates)
        return self.rows[key]

    async def delete(self, store_name: str, identity: dict) -> bool:
        key = str(identity.get("identity_key") or "")
        return self.rows.pop(key, None) is not None


@pytest.fixture(autouse=True)
def _clear_repo():
    clear_session_map_repository()
    yield
    clear_session_map_repository()


def test_session_map_uses_repository_when_injected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gateway.routing.session_map.get_checkpoint_dir",
        lambda: tmp_path,
    )
    store = _DictPersistentStore()
    set_session_map_repository(SessionMapRepository(store))

    sm = SessionMap(scope=SessionMapScope.PER_CHAT_BOT)
    assert sm.find_session_id("feishu", "chat-1", "bot-1", "user-1") is None

    sm.set_session_id("feishu", "chat-1", "bot-1", "user-1", "sess-db-1")
    assert "feishu::chat-1::bot-1" in store.rows
    assert store.rows["feishu::chat-1::bot-1"]["session_id"] == "sess-db-1"
    assert not (tmp_path / "session_map.json").exists()

    # 新实例：缓存空，应从 Repository / DB 读回
    sm2 = SessionMap(scope=SessionMapScope.PER_CHAT_BOT)
    assert sm2.find_session_id("feishu", "chat-1", "bot-1", "user-1") == "sess-db-1"


def test_session_map_falls_back_to_json_without_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "gateway.routing.session_map.get_checkpoint_dir",
        lambda: tmp_path,
    )
    sm = SessionMap(scope=SessionMapScope.PER_CHAT_BOT)
    sm.set_session_id("feishu", "chat-2", "bot-2", "user-2", "sess-file-1")
    path = tmp_path / "session_map.json"
    assert path.exists()
    assert sm.find_session_id("feishu", "chat-2", "bot-2", "user-2") == "sess-file-1"

    sm2 = SessionMap(scope=SessionMapScope.PER_CHAT_BOT)
    assert sm2.find_session_id("feishu", "chat-2", "bot-2", "user-2") == "sess-file-1"


def test_session_map_repository_roundtrip() -> None:
    store = _DictPersistentStore()
    repo = SessionMapRepository(store)

    async def _run():
        await repo.upsert(
            "feishu::c::b",
            Session(session_id="s1", service_id="s1", agent_id=None),
        )
        got = await repo.get("feishu::c::b")
        assert got is not None
        assert got.session_id == "s1"

    import asyncio

    asyncio.run(_run())
