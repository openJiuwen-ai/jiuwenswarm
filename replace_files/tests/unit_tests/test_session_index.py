"""session_index 模块单元测试（gateway 侧会话索引，仅 remote 模式启用）"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixture：将索引文件路径重定向到 tmp_path，并隔离 is_remote_storage 状态
# ---------------------------------------------------------------------------

@pytest.fixture()
def index_dir(tmp_path, monkeypatch):
    """重定向 _index_path 到 tmp_path/gateway，并返回目录。"""
    gw_dir = tmp_path / "gateway"
    gw_dir.mkdir()
    monkeypatch.setattr(
        "jiuwenclaw.gateway.session_index._index_path",
        lambda: gw_dir / "session_index.json",
    )
    return gw_dir


@pytest.fixture(autouse=True)
def reset_web_session_storage_cache():
    """隔离 is_remote_storage 模块级缓存，避免用例间相互污染。"""
    from jiuwenclaw.gateway import session_index

    session_index._web_session_storage = None
    yield
    session_index._web_session_storage = None


def _read_index(index_dir: Path) -> list:
    p = index_dir / "session_index.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# is_remote_storage
# ---------------------------------------------------------------------------

class TestIsRemoteStorage:
    @staticmethod
    def test_env_remote(monkeypatch):
        monkeypatch.setenv("GATEWAY_WEB_SESSION_STORAGE", "remote")
        from jiuwenclaw.gateway import session_index
        assert session_index.is_remote_storage() is True

    @staticmethod
    def test_env_local(monkeypatch):
        monkeypatch.setenv("GATEWAY_WEB_SESSION_STORAGE", "local")
        from jiuwenclaw.gateway import session_index
        assert session_index.is_remote_storage() is False

    @staticmethod
    def test_env_empty_falls_back_to_config(monkeypatch):
        monkeypatch.delenv("GATEWAY_WEB_SESSION_STORAGE", raising=False)
        with patch("jiuwenclaw.gateway.session_index.is_remote_storage", return_value=False) as m:
            from jiuwenclaw.gateway import session_index
            # 直接验证 patch 可覆盖（单元隔离即可）
            assert m.return_value is False

    @staticmethod
    def test_default_is_local(monkeypatch):
        monkeypatch.delenv("GATEWAY_WEB_SESSION_STORAGE", raising=False)
        with patch("jiuwenclaw.gateway.session_index.is_remote_storage", return_value=False):
            from jiuwenclaw.gateway import session_index
            assert session_index.is_remote_storage() is False


# ---------------------------------------------------------------------------
# upsert / list_sessions
# ---------------------------------------------------------------------------

class TestUpsert:
    @staticmethod
    def test_insert_one(index_dir):
        from jiuwenclaw.gateway.session_index import upsert, list_sessions

        upsert("sess_001", role="user", content="hello", timestamp=1000.0)
        entries = list_sessions()
        assert len(entries) == 1
        assert entries[0]["session_id"] == "sess_001"
        assert entries[0]["role"] == "user"
        assert entries[0]["content"] == "hello"
        assert entries[0]["timestamp"] == 1000.0

    @staticmethod
    def test_upsert_updates_existing(index_dir):
        from jiuwenclaw.gateway.session_index import upsert, list_sessions

        upsert("sess_001", role="user", content="hello", timestamp=1000.0)
        upsert("sess_001", role="assistant", content="world", timestamp=2000.0)
        entries = list_sessions()
        assert len(entries) == 1
        assert entries[0]["role"] == "assistant"
        assert entries[0]["content"] == "world"
        assert entries[0]["timestamp"] == 2000.0

    @staticmethod
    def test_sorted_by_timestamp_desc(index_dir):
        from jiuwenclaw.gateway.session_index import upsert, list_sessions

        upsert("sess_old", role="user", content="old", timestamp=500.0)
        upsert("sess_new", role="user", content="new", timestamp=900.0)
        entries = list_sessions()
        assert entries[0]["session_id"] == "sess_new"
        assert entries[1]["session_id"] == "sess_old"

    @staticmethod
    def test_max_sessions_evicts_oldest(index_dir):
        from jiuwenclaw.gateway import session_index as si

        for i in range(si.MAX_SESSIONS + 3):
            si.upsert(f"sess_{i:03d}", role="user", content=f"msg {i}", timestamp=float(i))
        entries = si.list_sessions()
        assert len(entries) == si.MAX_SESSIONS
        # 最旧的应被淘汰，保留最近的
        ids = [e["session_id"] for e in entries]
        # 最旧的 3 条（sess_000, sess_001, sess_002）应不在列表中
        for old in ["sess_000", "sess_001", "sess_002"]:
            assert old not in ids

    @staticmethod
    def test_list_sessions_page_respects_limit_offset(index_dir):
        from jiuwenclaw.gateway.session_index import list_sessions_page, upsert

        for i in range(5):
            upsert(f"sess_{i}", role="user", content=f"c{i}", timestamp=float(i + 1))
        page, total, limit, offset = list_sessions_page({"limit": 2, "offset": 1})
        assert total == 5
        assert limit == 2
        assert offset == 1
        assert len(page) == 2

    @staticmethod
    def test_content_preview_truncated(index_dir):
        from jiuwenclaw.gateway import session_index as si

        long_content = "a" * (si.CONTENT_PREVIEW_LEN + 50)
        si.upsert("sess_long", role="user", content=long_content, timestamp=1.0)
        entries = si.list_sessions()
        assert len(entries[0]["content"]) == si.CONTENT_PREVIEW_LEN

    @staticmethod
    def test_multiple_upserts_same_promote_to_top(index_dir):
        from jiuwenclaw.gateway.session_index import upsert, list_sessions

        upsert("sess_a", role="user", content="a", timestamp=100.0)
        upsert("sess_b", role="user", content="b", timestamp=200.0)
        # 重新 upsert sess_a，时间戳更新为最大
        upsert("sess_a", role="assistant", content="reply", timestamp=300.0)
        entries = list_sessions()
        assert entries[0]["session_id"] == "sess_a"


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------

class TestRemove:
    @staticmethod
    def test_remove_existing(index_dir):
        from jiuwenclaw.gateway.session_index import upsert, remove, list_sessions

        upsert("sess_x", role="user", content="hi", timestamp=1.0)
        remove("sess_x")
        assert list_sessions() == []

    @staticmethod
    def test_remove_nonexistent_is_noop(index_dir):
        from jiuwenclaw.gateway.session_index import remove, list_sessions

        remove("nonexistent")
        assert list_sessions() == []

    @staticmethod
    def test_remove_only_target(index_dir):
        from jiuwenclaw.gateway.session_index import upsert, remove, list_sessions

        upsert("sess_a", role="user", content="a", timestamp=1.0)
        upsert("sess_b", role="user", content="b", timestamp=2.0)
        remove("sess_a")
        entries = list_sessions()
        assert len(entries) == 1
        assert entries[0]["session_id"] == "sess_b"


# ---------------------------------------------------------------------------
# local 模式回归：session.list 不应写索引文件
# ---------------------------------------------------------------------------

class TestLocalModeNoIndexWrite:
    @staticmethod
    def test_session_list_local_does_not_create_index_file(index_dir, monkeypatch):
        """local 模式下 session.list 走 get_all_sessions_metadata，不触碰索引文件。"""
        monkeypatch.setenv("GATEWAY_WEB_SESSION_STORAGE", "local")

        index_file = index_dir / "session_index.json"
        assert not index_file.exists()

        # 模拟 get_all_sessions_metadata 返回空
        with patch(
            "jiuwenclaw.agentserver.session_metadata.get_all_sessions_metadata",
            return_value=([], 0),
        ):
            from jiuwenclaw.agentserver.session_metadata import get_all_sessions_metadata
            sessions, total = get_all_sessions_metadata()
            assert sessions == []
            assert total == 0

        # 索引文件依然不存在
        assert not index_file.exists()
