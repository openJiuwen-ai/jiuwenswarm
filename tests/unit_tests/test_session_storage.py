"""session_storage 模块单元测试（SessionMap 存储抽象层）"""
from __future__ import annotations

import json

import pytest

from jiuwenswarm.gateway.routing.session_map import Session, invoke_service_id


def _sess(session_id: str, *, chat_id: str = "c", bot_id: str = "b", agent_id: str | None = None) -> Session:
    return Session(
        session_id=session_id,
        service_id=invoke_service_id(chat_id, bot_id),
        agent_id=agent_id,
    )


# ---------------------------------------------------------------------------
# Fixture：重定向存储路径到 tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture()
def storage_dir(tmp_path):
    """重定向 LocalSessionStorage 路径到 tmp_path。"""
    storage_path = tmp_path / "storage"
    storage_path.mkdir()
    return storage_path


# ---------------------------------------------------------------------------
# LocalSessionStorage load/save/get/set/remove/get_all
# ---------------------------------------------------------------------------

class TestLocalSessionStorageBasic:
    """LocalSessionStorage 基本 CRUD 测试."""

    @staticmethod
    def test_load_empty(storage_dir):
        """空文件加载应正常返回。"""
        from jiuwenswarm.gateway.routing.session_storage import LocalSessionStorage

        store_path = storage_dir / "session_map_default.json"
        store_path.write_text("{}", encoding="utf-8")

        storage = LocalSessionStorage(store_path=store_path)
        assert storage.get_all() == {}

    @staticmethod
    def test_set_and_get(storage_dir):
        """设置后应能获取。"""
        from jiuwenswarm.gateway.routing.session_storage import LocalSessionStorage

        store_path = storage_dir / "session_map_default.json"
        storage = LocalSessionStorage(store_path=store_path)

        storage.set("key1", _sess("sess_001"))
        result = storage.get("key1")
        assert result is not None
        assert result.session_id == "sess_001"

    @staticmethod
    def test_get_nonexistent(storage_dir):
        """获取不存在的 key 应返回 None。"""
        from jiuwenswarm.gateway.routing.session_storage import LocalSessionStorage

        store_path = storage_dir / "session_map_default.json"
        storage = LocalSessionStorage(store_path=store_path)

        assert storage.get("nonexistent") is None

    @staticmethod
    def test_remove(storage_dir):
        """删除后应无法获取。"""
        from jiuwenswarm.gateway.routing.session_storage import LocalSessionStorage

        store_path = storage_dir / "session_map_default.json"
        storage = LocalSessionStorage(store_path=store_path)

        storage.set("key1", _sess("sess_001"))
        storage.remove("key1")
        assert storage.get("key1") is None

    @staticmethod
    def test_get_all(storage_dir):
        """获取所有数据。"""
        from jiuwenswarm.gateway.routing.session_storage import LocalSessionStorage

        store_path = storage_dir / "session_map_default.json"
        storage = LocalSessionStorage(store_path=store_path)

        storage.set("key1", _sess("sess_001"))
        storage.set("key2", _sess("sess_002"))
        all_data = storage.get_all()
        assert len(all_data) == 2
        assert all_data["key1"].session_id == "sess_001"
        assert all_data["key2"].session_id == "sess_002"


# ---------------------------------------------------------------------------
# LocalSessionStorage load/save 持久化
# ---------------------------------------------------------------------------

class TestLocalSessionStoragePersist:
    """LocalSessionStorage load/save 持久化测试."""

    @staticmethod
    def test_save_single_session(storage_dir):
        """set 应把 Session 对象持久化为 JSON dict。"""
        from jiuwenswarm.gateway.routing.session_storage import LocalSessionStorage

        store_path = storage_dir / "session_map_default.json"
        storage = LocalSessionStorage(store_path=store_path)

        storage.set("key1", _sess("sess_001"))
        storage.set("key2", _sess("sess_002"))

        with open(store_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["key1"]["session_id"] == "sess_001"
        assert data["key1"]["service_id"]

    @staticmethod
    def test_save_nonexistent_session(storage_dir):
        """save 不在 mapping 中的 Session 应无操作（不抛异常）。"""
        from jiuwenswarm.gateway.routing.session_storage import LocalSessionStorage

        store_path = storage_dir / "session_map_default.json"
        storage = LocalSessionStorage(store_path=store_path)

        storage.save(_sess("nonexistent_session_id"))

    @staticmethod
    def test_load_from_existing_file(storage_dir):
        """从已存在的文件加载（兼容旧版纯字符串）。"""
        from jiuwenswarm.gateway.routing.session_storage import LocalSessionStorage

        store_path = storage_dir / "session_map_default.json"
        store_path.write_text(
            json.dumps({"key1": "sess_old"}, ensure_ascii=False),
            encoding="utf-8",
        )

        storage = LocalSessionStorage(store_path=store_path)

        result = storage.get("key1")
        assert result is not None
        assert result.session_id == "sess_old"
        assert result.service_id


# ---------------------------------------------------------------------------
# claw_id 隔离
# ---------------------------------------------------------------------------

class TestClawIdIsolation:
    """不同 claw_id 应使用不同的存储路径。"""

    @staticmethod
    def test_local_storage_path(storage_dir):
        """LocalSessionStorage 使用固定路径 session_map.json。"""
        from jiuwenswarm.gateway.routing.session_storage import LocalSessionStorage

        storage = LocalSessionStorage(store_path=storage_dir / "test_map.json")
        assert "test_map.json" in str(storage.store_path)


# ---------------------------------------------------------------------------
# SessionMap 使用存储抽象
# ---------------------------------------------------------------------------

class TestSessionMapWithStorage:
    """SessionMap 通过存储抽象层操作。"""

    @staticmethod
    def test_session_map_set_and_find(storage_dir, monkeypatch):
        """set_session_id / find_session_id 应读写 Session 并持久化。"""
        import sys

        from jiuwenswarm.gateway.routing.session_map import SessionMap, SessionMapScope
        from jiuwenswarm.gateway.routing.session_storage import LocalSessionStorage

        store_path = storage_dir / "session_map_default.json"
        session_storage_mod = sys.modules["jiuwenswarm.gateway.routing.session_storage"]

        def _resolve_storage() -> session_storage_mod.SessionStorage:
            return session_storage_mod.LocalSessionStorage(store_path=store_path)

        monkeypatch.setattr(SessionMap, "_resolve_storage", staticmethod(_resolve_storage))
        monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

        sm = SessionMap(scope=SessionMapScope.PER_CHAT_BOT)
        sm.set_session_id("provider1", "chat1", "bot1", "user1", "provider1::chat1::bot1::ts1::suffix1")
        assert sm.find_session_id("provider1", "chat1", "bot1", "user1") == (
            "provider1::chat1::bot1::ts1::suffix1"
        )
        sess = sm.find_session("provider1", "chat1", "bot1", "user1")
        assert sess is not None
        assert sess.service_id == invoke_service_id("chat1", "bot1")

        storage2 = LocalSessionStorage(store_path=store_path)
        result = storage2.get("provider1::chat1::bot1")
        assert result is not None
        assert result.session_id == "provider1::chat1::bot1::ts1::suffix1"

    @staticmethod
    def test_invoke_ids_helpers():
        from jiuwenswarm.gateway.routing.session_map import (
            SessionMapScope,
            invoke_ids_from_identity,
            invoke_ids_from_session_id_string,
        )

        svc, aid = invoke_ids_from_identity("chat", "bot", "user", SessionMapScope.PER_CHAT_BOT)
        assert svc == invoke_service_id("chat", "bot")
        assert aid is None

        svc2, aid2 = invoke_ids_from_identity("chat", "bot", "user", SessionMapScope.PER_CHAT_BOT_USER)
        assert svc2 == svc
        assert aid2 == "user"

        sid = "feishu::chatX::botY::aabb::cc"
        s3, a3 = invoke_ids_from_session_id_string(sid)
        assert s3 == invoke_service_id("chatX", "botY")
        assert a3 is None


# ---------------------------------------------------------------------------
# RedisSessionStorage
# ---------------------------------------------------------------------------

class _AsyncDictRedis:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def peek(self, key: str) -> str | None:
        return self._data.get(key)

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> bool:
        self._data[key] = value
        return True

    async def delete(self, key: str) -> bool:
        return self._data.pop(key, None) is not None

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self._data.get(key) for key in keys]

    async def scan_keys(self, pattern: str) -> list[str]:
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            return [key for key in self._data if key.startswith(prefix)]
        return [key for key in self._data if key == pattern]


class _TestRedisSessionStorage:
    """Reuse RedisSessionStorage with injected fake Redis."""

    def __init__(self, redis_client: _AsyncDictRedis, claw_id: str) -> None:
        from jiuwenswarm.gateway.routing.session_storage import RedisSessionStorage

        self._inner = RedisSessionStorage()
        self._inner._redis = redis_client
        self._inner._claw_id = claw_id
        self._inner._mapping = {}

    async def seed_session(self, identity_key: str, raw_session: dict) -> None:
        await self._inner._redis.set(  # type: ignore[union-attr]
            self._inner._redis_key(identity_key),
            json.dumps(raw_session),
        )

    def has_identity_key(self, identity_key: str) -> bool:
        return bool(
            self._inner._redis
            and self._inner._redis.peek(self._inner._redis_key(identity_key))
        )

    def get(self, identity_key: str):
        return self._inner.get(identity_key)

    def get_all(self):
        return self._inner.get_all()

    def load(self) -> None:
        self._inner.load()

    def set(self, identity_key: str, session: Session) -> None:
        self._inner.set(identity_key, session)


@pytest.mark.asyncio
async def test_redis_session_storage_fetches_existing_session_without_local_cache() -> None:
    test_storage = _TestRedisSessionStorage(_AsyncDictRedis(), "gateway-1")

    identity_key = "feishu::chat-1::bot-1"
    raw_session = {
        "session_id": "feishu::chat-1::bot-1::abc123::def456",
        "service_id": invoke_service_id("chat-1", "bot-1"),
        "agent_id": None,
    }
    await test_storage.seed_session(identity_key, raw_session)

    session = test_storage.get(identity_key)

    assert session is not None
    assert session.session_id == raw_session["session_id"]
    assert test_storage.get_all()[identity_key].session_id == raw_session["session_id"]


def test_session_map_reload_with_redis_storage() -> None:
    """Injected RedisSessionStorage: reload refreshes in-memory cache from Redis."""
    from jiuwenswarm.gateway.routing.session_map import SessionMap, SessionMapScope

    shared_redis = _AsyncDictRedis()
    storage_a = _TestRedisSessionStorage(shared_redis, "gateway-1")
    storage_b = _TestRedisSessionStorage(shared_redis, "gateway-1")

    map_a = SessionMap(scope=SessionMapScope.PER_CHAT_BOT)
    map_a._storage = storage_a  # type: ignore[assignment]
    sid = "feishu::chat-1::bot-1::abc123::def456"
    map_a.set_session_id("feishu", "chat-1", "bot-1", "user-1", sid)

    assert storage_a.has_identity_key("feishu::chat-1::bot-1")

    map_b = SessionMap(scope=SessionMapScope.PER_CHAT_BOT)
    map_b._storage = storage_b  # type: ignore[assignment]
    map_b.reload()
    assert map_b.find_session_id("feishu", "chat-1", "bot-1", "user-1") == sid
    assert "feishu::chat-1::bot-1" in map_b._storage.get_all()
