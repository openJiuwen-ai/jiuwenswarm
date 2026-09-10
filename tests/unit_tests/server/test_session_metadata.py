"""session_metadata 模块单元测试"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture: mock get_agent_sessions_dir 指向 tmp_path
# ---------------------------------------------------------------------------
@pytest.fixture()
def sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: d,
    )
    # 清空内存缓存，避免跨用例污染（不同用例可能复用同一 session_id）
    from jiuwenswarm.server.runtime.session.session_metadata import (
        _METADATA_CACHE,
        _METADATA_QUEUE,
        _SESSION_REBIND_GEN,
    )
    _METADATA_CACHE.clear()
    _SESSION_REBIND_GEN.clear()
    # 排空异步写队列，避免上一个用例残留的 write task 在 init 之后写盘，
    # 把 project_dir 等字段回写为旧值（mode 惰性迁移 + 额外写回放大窗口）。
    _METADATA_QUEUE.join()
    return d



def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ===========================================================================
# _auto_title
# ===========================================================================
class TestAutoTitle:
    @staticmethod
    def test_normal():
        from jiuwenswarm.server.runtime.session.session_metadata import _auto_title

        assert _auto_title("hello world") == "hello world"

    @staticmethod
    def test_truncate():
        from jiuwenswarm.server.runtime.session.session_metadata import _auto_title, _TITLE_MAX_LEN

        long_text = "a" * 100
        result = _auto_title(long_text)
        assert len(result) == _TITLE_MAX_LEN + 3  # +3 for "..."
        assert result.endswith("...")

    @staticmethod
    def test_strip_and_newline():
        from jiuwenswarm.server.runtime.session.session_metadata import _auto_title

        assert _auto_title("  line1\nline2  ") == "line1 line2"

    @staticmethod
    def test_empty():
        from jiuwenswarm.server.runtime.session.session_metadata import _auto_title

        assert _auto_title("") == ""
        assert _auto_title("   ") == ""


# ===========================================================================
# init_session_metadata
# ===========================================================================
class TestInitSessionMetadata:
    @staticmethod
    def test_creates_metadata_file(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

        init_session_metadata(
            session_id="sess_001",
            channel_id="web",
            user_id="user_1",
            title="test title",
        )
        meta_path = sessions_dir / "sess_001" / "metadata.json"
        assert meta_path.exists()

        data = _read_json(meta_path)
        assert data["session_id"] == "sess_001"
        assert data["channel_id"] == "web"
        assert data["user_id"] == "user_1"
        assert data["title"] == "test title"
        assert data["message_count"] == 0
        assert data["mode"] == "unknown"
        assert data["round_id"] == 0
        assert isinstance(data["created_at"], float)
        assert isinstance(data["last_message_at"], float)

    @staticmethod
    def test_default_empty_fields(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

        init_session_metadata(session_id="sess_002")
        data = _read_json(sessions_dir / "sess_002" / "metadata.json")
        assert data["channel_id"] == ""
        assert data["user_id"] == ""
        assert data["title"] == ""
        assert data["mode"] == "unknown"
        assert data["round_id"] == 0

    @staticmethod
    def test_init_new_fields(sessions_dir):
        """init 写入新增字段：project_dir / model / last_user_message_at / status"""
        from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

        init_session_metadata(
            session_id="sess_new",
            project_dir="E:\\myproj",
            model="glm-5",
        )
        data = _read_json(sessions_dir / "sess_new" / "metadata.json")
        assert data["project_dir"] == "E:\\myproj"
        assert data["model"] == "glm-5"
        assert data["status"] == "idle"
        assert isinstance(data["last_user_message_at"], float)
        # created_at / last_message_at / last_user_message_at 各自独立取时间戳，
        # 允许微秒级差异，仅断言三者都在创建时刻附近
        assert abs(data["last_user_message_at"] - data["created_at"]) < 1.0

    @staticmethod
    def test_init_new_fields_default_empty(sessions_dir):
        """init 不传新字段时为空默认值"""
        from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

        init_session_metadata(session_id="sess_def")
        data = _read_json(sessions_dir / "sess_def" / "metadata.json")
        assert data["project_dir"] == ""
        assert data["model"] == ""

    @staticmethod
    def test_team_binding_fields(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

        init_session_metadata(
            session_id="sess_team",
            mode="team.plan",
            team_name="custom_team",
            team_template_id="default",
        )
        data = _read_json(sessions_dir / "sess_team" / "metadata.json")

        assert data["mode"] == "team.plan"
        assert data["team_name"] == "custom_team"
        assert data["team_template_id"] == "default"
        assert "team_template_snapshot" not in data


# ===========================================================================
# update_session_metadata
# ===========================================================================
class TestUpdateSessionMetadata:
    @staticmethod
    def test_update_existing(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_u1", channel_id="web")

        update_session_metadata(
            session_id="sess_u1",
            channel_id="feishu",
            increment_message_count=True,
        )
        # 等待异步队列写入完成
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_u1" / "metadata.json")
        assert data["channel_id"] == "web"  # 首次锁定：保持 web，不被 feishu 覆写
        assert data["message_count"] == 1

    @staticmethod
    def test_channel_id_empty_string_does_not_clear_locked(sessions_dir):
        """channel_id 首次锁定：空串更新不再清空已锁定的归属通道。

        旧实现 `if channel_id is not None` 会把空串写入并清空 channel_id；
        新守卫跳过空串，保留锁定值。
        """
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_u_emp", channel_id="web")

        update_session_metadata(
            session_id="sess_u_emp",
            channel_id="",
            increment_message_count=True,
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_u_emp" / "metadata.json")
        assert data["channel_id"] == "web"  # 空串不覆盖已锁定归属通道
        assert data["message_count"] == 1

    @staticmethod
    def test_update_agent_group_binding(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
        )

        init_session_metadata(session_id="sess_agent_group", channel_id="web")
        update_session_metadata(
            session_id="sess_agent_group",
            agent_group_name="sample-expert-group",
            touch_last_message_at=False,
            sync_write=True,
        )

        data = _read_json(
            sessions_dir / "sess_agent_group" / "metadata.json"
        )
        assert data["agent_group_name"] == "sample-expert-group"

    @staticmethod
    def test_fallback_create_when_no_metadata(sessions_dir):
        """外部渠道隐式创建 session 时,metadata 不存在,应自动创建"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            update_session_metadata,
            _METADATA_QUEUE,
        )

        # 不调用 init,直接 update — 模拟外部渠道场景
        (sessions_dir / "sess_ext").mkdir()
        update_session_metadata(
            session_id="sess_ext",
            channel_id="telegram",
            user_id="tg_user",
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_ext" / "metadata.json")
        assert data["session_id"] == "sess_ext"
        assert data["channel_id"] == "telegram"
        assert data["user_id"] == "tg_user"
        assert data["message_count"] == 0

    @staticmethod
    def test_auto_title_on_first_user_message(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_at")  # title 为空

        update_session_metadata(
            session_id="sess_at",
            user_content="帮我写一个排序算法",
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_at" / "metadata.json")
        assert data["title"] == "帮我写一个排序算法"

    @staticmethod
    def test_no_overwrite_existing_title(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_nt", title="原始标题")

        update_session_metadata(
            session_id="sess_nt",
            user_content="新消息内容",
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_nt" / "metadata.json")
        assert data["title"] == "原始标题"  # 不被覆盖

    @staticmethod
    def test_increment_message_count_multiple(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_mc")
        for _ in range(3):
            update_session_metadata(
                session_id="sess_mc", increment_message_count=True
            )
            _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_mc" / "metadata.json")
        assert data["message_count"] == 3

    @staticmethod
    def test_project_dir_first_lock_not_overwritten(sessions_dir):
        """project_dir 首次锁定后，后续传入不同值不覆盖"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_pp")
        # 首次锁定
        update_session_metadata(session_id="sess_pp", project_dir="E:\\projA")
        _METADATA_QUEUE.join()
        # 二次传入不同值
        update_session_metadata(session_id="sess_pp", project_dir="E:\\projB")
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_pp" / "metadata.json")
        assert data["project_dir"] == "E:\\projA", "project_dir 锁定后不可改"

    @staticmethod
    def test_model_overwrites_each_request(sessions_dir):
        """model 覆盖式：每次请求刷新为本次模型"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_m", model="glm-5")
        update_session_metadata(session_id="sess_m", model="glm-5.2")
        _METADATA_QUEUE.join()
        update_session_metadata(session_id="sess_m", model="glm-5.3")
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_m" / "metadata.json")
        assert data["model"] == "glm-5.3", "model 应被最后一次请求覆盖"

    @staticmethod
    def test_last_user_message_at_overwrites_when_passed(sessions_dir):
        """last_user_message_at 覆盖式：传入则刷新，不传(None)则保留旧值"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_lum")
        # 传入时间戳 → 写入
        update_session_metadata(
            session_id="sess_lum",
            last_user_message_at=1000.0,
            user_content="hi",
        )
        _METADATA_QUEUE.join()
        # 不传 last_user_message_at → 保留旧值
        update_session_metadata(session_id="sess_lum")
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_lum" / "metadata.json")
        assert data["last_user_message_at"] == 1000.0, "不传时应保留上次的用户最后输入时间"

    @staticmethod
    def test_update_new_fields_fallback_create(sessions_dir):
        """update 兜底新建分支也写入新字段"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            update_session_metadata,
            _METADATA_QUEUE,
        )

        (sessions_dir / "sess_fb").mkdir()
        update_session_metadata(
            session_id="sess_fb",
            channel_id="web",
            project_dir="E:\\fb",
            model="glm-5",
            last_user_message_at=2000.0,
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_fb" / "metadata.json")
        assert data["project_dir"] == "E:\\fb"
        assert data["model"] == "glm-5"
        assert data["last_user_message_at"] == 2000.0
        assert data["status"] == "idle"

    @staticmethod
    def test_update_team_binding_fields(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_bind", mode="agent")

        update_session_metadata(
            session_id="sess_bind",
            mode="code.team",
            team_name="research_team",
            team_template_id="research",
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_bind" / "metadata.json")
        assert data["mode"] == "code.team"
        assert data["team_name"] == "research_team"
        assert data["team_template_id"] == "research"
        assert "team_template_snapshot" not in data


# ===========================================================================
# get_session_metadata
# ===========================================================================
class TestGetSessionMetadata:
    @staticmethod
    def test_returns_data(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="sess_g1", channel_id="web")
        data = get_session_metadata("sess_g1")
        assert data["channel_id"] == "web"

    @staticmethod
    def test_returns_empty_when_missing(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        data = get_session_metadata("nonexistent")
        assert data == {}

    @staticmethod
    def test_backfill_new_fields_for_legacy_session(sessions_dir):
        """存量会话（无新字段）读取时 setdefault 兜底，前端拿到稳定 schema"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _write_metadata_sync,
            get_session_metadata,
        )

        # 模拟旧版本会话：只有老字段，没有 project_dir/model/last_user_message_at/status
        _write_metadata_sync("sess_legacy", {
            "session_id": "sess_legacy",
            "channel_id": "web",
            "user_id": "",
            "created_at": 1000.0,
            "last_message_at": 1000.0,
            "title": "old",
            "message_count": 0,
            "mode": "unknown",
            "team_name": "",
            "round_id": 0,
        })
        # 清缓存确保从磁盘读
        from jiuwenswarm.server.runtime.session.session_metadata import _METADATA_CACHE
        _METADATA_CACHE.pop("sess_legacy", None)

        data = get_session_metadata("sess_legacy", cache_bust=True)
        assert data["project_dir"] == ""
        assert data["model"] == ""
        assert data["status"] == "idle"
        assert data["last_user_message_at"] == 1000.0  # 回退到 created_at


# ===========================================================================
# increment_session_round_count
# ===========================================================================
class TestIncrementSessionRoundCount:
    @staticmethod
    def test_first_increment_returns_1(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            increment_session_round_count,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_round1")
        result = increment_session_round_count("sess_round1")
        _METADATA_QUEUE.join()

        assert result == 1
        data = _read_json(sessions_dir / "sess_round1" / "metadata.json")
        assert data["round_id"] == 1

    @staticmethod
    def test_increments_sequentially(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            increment_session_round_count,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_round_seq")
        for expected in range(1, 4):
            result = increment_session_round_count("sess_round_seq")
            _METADATA_QUEUE.join()
            assert result == expected

        data = _read_json(sessions_dir / "sess_round_seq" / "metadata.json")
        assert data["round_id"] == 3

    @staticmethod
    def test_defaults_to_0_when_no_metadata(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            increment_session_round_count,
            _METADATA_QUEUE,
        )

        (sessions_dir / "sess_no_meta").mkdir()
        result = increment_session_round_count("sess_no_meta")
        _METADATA_QUEUE.join()

        assert result == 1
        data = _read_json(sessions_dir / "sess_no_meta" / "metadata.json")
        assert data["round_id"] == 1

    @staticmethod
    def test_persists_across_restarts(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            increment_session_round_count,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_persist")
        increment_session_round_count("sess_persist")
        _METADATA_QUEUE.join()

        # Simulate restart: re-import and read from disk
        from jiuwenswarm.server.runtime.session.session_metadata import (
            increment_session_round_count,
        )
        result = increment_session_round_count("sess_persist")
        _METADATA_QUEUE.join()

        assert result == 2
        data = _read_json(sessions_dir / "sess_persist" / "metadata.json")
        assert data["round_id"] == 2


# ===========================================================================
class TestGetAllSessionsMetadata:
    @staticmethod
    def test_basic_list(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            get_all_sessions_metadata,
        )

        init_session_metadata(session_id="s1", channel_id="web")
        init_session_metadata(session_id="s2", channel_id="feishu")

        sessions, total = get_all_sessions_metadata()
        assert total == 2
        assert len(sessions) == 2
        ids = {s["session_id"] for s in sessions}
        assert ids == {"s1", "s2"}

    @staticmethod
    def test_sorted_by_last_message_at(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _write_metadata_sync,
            get_all_sessions_metadata,
        )

        now = time.time()
        _write_metadata_sync("old", {
            "session_id": "old", "last_message_at": now - 100,
            "channel_id": "", "user_id": "", "created_at": now - 100,
            "title": "", "message_count": 0, "round_id": 0,
        })
        _write_metadata_sync("new", {
            "session_id": "new", "last_message_at": now,
            "channel_id": "", "user_id": "", "created_at": now,
            "title": "", "message_count": 0, "round_id": 0,
        })

        sessions, _ = get_all_sessions_metadata()
        assert sessions[0]["session_id"] == "new"
        assert sessions[1]["session_id"] == "old"

    @staticmethod
    def test_pagination_limit(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            get_all_sessions_metadata,
        )

        for i in range(5):
            init_session_metadata(session_id=f"p{i}")

        sessions, total = get_all_sessions_metadata(limit=2)
        assert total == 5
        assert len(sessions) == 2

    @staticmethod
    def test_pagination_offset(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _write_metadata_sync,
            get_all_sessions_metadata,
        )

        now = time.time()
        for i in range(5):
            _write_metadata_sync(f"o{i}", {
                "session_id": f"o{i}", "last_message_at": now - i,
                "channel_id": "", "user_id": "", "created_at": now - i,
                "title": "", "message_count": 0, "round_id": 0,
            })

        sessions, total = get_all_sessions_metadata(limit=2, offset=2)
        assert total == 5
        assert len(sessions) == 2
        # offset=2 跳过前2个,取第3和第4个(按 last_message_at 倒序)
        assert sessions[0]["session_id"] == "o2"
        assert sessions[1]["session_id"] == "o3"

    @staticmethod
    def test_fallback_for_old_sessions(sessions_dir):
        """没有 metadata.json 的旧会话应用目录时间戳构造最小信息"""
        from jiuwenswarm.server.runtime.session.session_metadata import get_all_sessions_metadata

        (sessions_dir / "legacy_sess").mkdir()
        # 不写 metadata.json

        sessions, total = get_all_sessions_metadata()
        assert total == 1
        assert sessions[0]["session_id"] == "legacy_sess"
        assert sessions[0]["title"] == ""
        assert sessions[0]["mode"] == "unknown"
        assert sessions[0]["created_at"] > 0

    @staticmethod
    def test_empty_dir(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import get_all_sessions_metadata

        sessions, total = get_all_sessions_metadata()
        assert total == 0
        assert sessions == []

    @staticmethod
    def test_excludes_heartbeat_sessions(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            get_all_sessions_metadata,
        )

        init_session_metadata(session_id="sess_a")
        init_session_metadata(session_id="heartbeat_abc123_deadbeef")
        init_session_metadata(session_id="health_check_abc123_deadbeef")
        init_session_metadata(session_id="sess_b")

        sessions, total = get_all_sessions_metadata(limit=20)
        assert total == 2
        ids = {s["session_id"] for s in sessions}
        assert ids == {"sess_a", "sess_b"}
        assert len(sessions) == 2


# ===========================================================================
# _read_metadata 容错
# ===========================================================================
class TestReadMetadataRobustness:
    @staticmethod
    def test_corrupted_json(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        d = sessions_dir / "sess_bad"
        d.mkdir()
        (d / "metadata.json").write_text("not valid json", encoding="utf-8")

        data = get_session_metadata("sess_bad")
        assert data == {}

    @staticmethod
    def test_non_dict_json(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        d = sessions_dir / "sess_list"
        d.mkdir()
        (d / "metadata.json").write_text("[1,2,3]", encoding="utf-8")

        data = get_session_metadata("sess_list")
        assert data == {}


# ===========================================================================
# channel_metadata
# ===========================================================================
class TestChannelMetadata:
    @staticmethod
    def test_first_request_metadata_stored(sessions_dir):
        """首次请求的 metadata 应写入 channel_metadata"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            update_session_metadata,
            _METADATA_QUEUE,
        )

        update_session_metadata(
            session_id="sess_meta",
            channel_id="web",
            channel_metadata={"traceparent": "00-abc-123-01", "feishu_chat_id": "oc_xxx"},
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_meta" / "metadata.json")
        assert data["channel_metadata"]["traceparent"] == "00-abc-123-01"
        assert data["channel_metadata"]["feishu_chat_id"] == "oc_xxx"

    @staticmethod
    def test_no_overwrite_existing_metadata(sessions_dir):
        """已存在的 channel_metadata 不应被覆盖"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _write_metadata_sync,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        _write_metadata_sync("sess_no", {
            "session_id": "sess_no",
            "channel_id": "web",
            "user_id": "",
            "created_at": 1000.0,
            "last_message_at": 1000.0,
            "title": "",
            "message_count": 0,
            "round_id": 0,
            "channel_metadata": {"traceparent": "original"},
        })

        update_session_metadata(
            session_id="sess_no",
            channel_metadata={"traceparent": "new_value"},
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_no" / "metadata.json")
        assert data["channel_metadata"]["traceparent"] == "original"  # 未被覆盖

    @staticmethod
    def test_empty_metadata_not_stored(sessions_dir):
        """空 metadata 不写入字段"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            update_session_metadata,
            _METADATA_QUEUE,
        )

        update_session_metadata(
            session_id="sess_empty",
            channel_id="web",
            channel_metadata=None,
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_empty" / "metadata.json")
        assert "channel_metadata" not in data

    @staticmethod
    def test_backfill_when_missing(sessions_dir):
        """首次未写入时，后续可补充"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            update_session_metadata,
            _METADATA_QUEUE,
        )

        # 首次不带 metadata
        update_session_metadata(session_id="sess_backfill", channel_id="web")
        _METADATA_QUEUE.join()

        # 二次补充 metadata
        update_session_metadata(
            session_id="sess_backfill",
            channel_metadata={"traceparent": "backfilled"},
            increment_message_count=True,
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_backfill" / "metadata.json")
        assert data["channel_metadata"]["traceparent"] == "backfilled"


# ===========================================================================
# delivery_context
# ===========================================================================
class TestDeliveryContext:
    @staticmethod
    def test_delivery_context_can_refresh_route_metadata(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _METADATA_QUEUE,
            get_session_delivery_context,
            set_session_delivery_context,
        )

        set_session_delivery_context(
            session_id="sess_delivery",
            channel_id="feishu",
            source_request_id="req-1",
            route_metadata={"feishu_chat_id": "oc_old"},
        )
        _METADATA_QUEUE.join()

        set_session_delivery_context(
            session_id="sess_delivery",
            channel_id="feishu",
            source_request_id="req-2",
            route_metadata={"feishu_chat_id": "oc_new"},
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_delivery" / "metadata.json")
        context = get_session_delivery_context("sess_delivery")

        assert data["delivery_context"]["source_request_id"] == "req-2"
        assert data["delivery_context"]["route_metadata"]["feishu_chat_id"] == "oc_new"
        assert context is not None
        assert context["channel_id"] == "feishu"
        assert context["route_metadata"]["feishu_chat_id"] == "oc_new"

    @staticmethod
    def test_delivery_context_keeps_previous_route_metadata_when_new_request_has_none(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _METADATA_QUEUE,
            get_session_delivery_context,
            set_session_delivery_context,
        )

        set_session_delivery_context(
            session_id="sess_delivery_keep",
            channel_id="wecom",
            source_request_id="req-1",
            route_metadata={"conversation_id": "conv-1"},
        )
        _METADATA_QUEUE.join()

        set_session_delivery_context(
            session_id="sess_delivery_keep",
            channel_id="wecom",
            source_request_id="req-2",
            route_metadata=None,
        )
        _METADATA_QUEUE.join()

        context = get_session_delivery_context("sess_delivery_keep")
        assert context is not None
        assert context["source_request_id"] == "req-2"
        assert context["route_metadata"]["conversation_id"] == "conv-1"

    @staticmethod
    def test_build_server_push_message_uses_saved_delivery_context(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _METADATA_QUEUE,
            build_server_push_message,
            set_session_delivery_context,
        )

        set_session_delivery_context(
            session_id="sess_push",
            channel_id="telegram",
            source_request_id="req-origin",
            route_metadata={"telegram_chat_id": "chat-1"},
        )
        _METADATA_QUEUE.join()

        push = build_server_push_message(
            session_id="sess_push",
            request_id="push-1",
            payload={"event_type": "chat.ask_user_question"},
            fallback_channel_id="web",
        )

        assert push["channel_id"] == "telegram"
        assert push["session_id"] == "sess_push"
        assert push["metadata"]["telegram_chat_id"] == "chat-1"

    @staticmethod
    def test_delivery_context_channel_dynamic_while_top_level_channel_locked(sessions_dir):
        """顶层 metadata.channel_id 首次锁定；delivery_context.channel_id 保持动态。

        顶层归属通道稳定为 web，不被 feishu server_push 覆写（否则 web 侧栏丢会话）；
        而 delivery_context.channel_id 必须仍为 feishu，供 build_server_push_message
        路由异步推送回到用户最近活动的 feishu 通道。
        """
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _METADATA_QUEUE,
            init_session_metadata,
            get_session_delivery_context,
            set_session_delivery_context,
        )

        init_session_metadata(session_id="sess_delivery_lock", channel_id="web")

        set_session_delivery_context(
            session_id="sess_delivery_lock",
            channel_id="feishu",
            source_request_id="req-1",
            route_metadata={"feishu_chat_id": "oc_1"},
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_delivery_lock" / "metadata.json")
        assert data["channel_id"] == "web", "顶层 channel_id 首次锁定，不被 server_push 覆写"
        assert (
            data["delivery_context"]["channel_id"] == "feishu"
        ), "delivery_context.channel_id 保持动态更新"

        context = get_session_delivery_context("sess_delivery_lock")
        assert context is not None
        assert context["channel_id"] == "feishu"


# ===========================================================================
# 需求验证: 会话标题稳定性
# ===========================================================================
class TestTitleStability:
    """验证两个核心需求:
    1. 首条用户消息自动生成标题，后续消息不改变
    2. 标题一旦创建就不再变化
    """

    @staticmethod
    def test_req1_first_message_sets_title_second_does_not(sessions_dir):
        """需求1: 首条消息设置标题，第二条消息不改变标题"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        # 模拟 web 前端创建会话(无标题)
        init_session_metadata(session_id="sess_req1")

        # 第一条用户消息
        update_session_metadata(
            session_id="sess_req1",
            channel_id="web",
            increment_message_count=True,
            user_content="第一条消息应该成为标题",
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_req1" / "metadata.json")
        assert data["title"] == "第一条消息应该成为标题"

        # 第一条助手回复
        update_session_metadata(
            session_id="sess_req1",
            channel_id="web",
            increment_message_count=True,
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_req1" / "metadata.json")
        assert data["title"] == "第一条消息应该成为标题", "助手回复不应覆盖标题"

        # 第二条用户消息(模拟隔1分钟后)
        update_session_metadata(
            session_id="sess_req1",
            channel_id="web",
            increment_message_count=True,
            user_content="第二条消息不应改变标题",
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_req1" / "metadata.json")
        assert data["title"] == "第一条消息应该成为标题", "第二条用户消息不应覆盖标题"
        assert data["message_count"] == 3

    @staticmethod
    def test_req1_rapid_user_then_assistant_no_race(sessions_dir):
        """需求1(竞态): 用户消息和助手消息快速连续到达时，标题不被覆盖"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_race")

        # 模拟真实场景: 用户消息和助手消息不等异步写入就连续调用
        # 不调用 _METADATA_QUEUE.join()，模拟异步写入未完成
        update_session_metadata(
            session_id="sess_race",
            channel_id="web",
            increment_message_count=True,
            user_content="用户的第一条消息",
        )
        # 助手立即回复(不等用户消息的异步写入落盘)
        update_session_metadata(
            session_id="sess_race",
            channel_id="web",
            increment_message_count=True,
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_race" / "metadata.json")
        assert data["title"] == "用户的第一条消息", \
            "竞态条件: 助手消息的异步写入不应覆盖用户消息生成的标题"
        assert data["message_count"] == 2

    @staticmethod
    def test_req2_title_immutable_after_creation(sessions_dir):
        """需求2: 标题一旦创建就不再改变，即使后续多轮对话"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_immut")

        # 第1轮
        update_session_metadata(
            session_id="sess_immut",
            increment_message_count=True,
            user_content="最初的标题",
        )
        _METADATA_QUEUE.join()
        update_session_metadata(
            session_id="sess_immut",
            increment_message_count=True,
        )
        _METADATA_QUEUE.join()

        # 第2轮
        update_session_metadata(
            session_id="sess_immut",
            increment_message_count=True,
            user_content="第二轮消息",
        )
        _METADATA_QUEUE.join()
        update_session_metadata(
            session_id="sess_immut",
            increment_message_count=True,
        )
        _METADATA_QUEUE.join()

        # 第3轮
        update_session_metadata(
            session_id="sess_immut",
            increment_message_count=True,
            user_content="第三轮消息",
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_immut" / "metadata.json")
        assert data["title"] == "最初的标题", "多轮对话后标题仍保持不变"
        assert data["message_count"] == 5

    @staticmethod
    def test_req2_explicit_empty_title_does_not_clear(sessions_dir):
        """需求2: 即使传入空字符串 title 参数，也不应清除已有标题"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            _METADATA_QUEUE,
        )

        init_session_metadata(session_id="sess_noclear", title="已有标题")

        # 模拟某处传入 title=""
        update_session_metadata(
            session_id="sess_noclear",
            title="",
        )
        _METADATA_QUEUE.join()

        data = _read_json(sessions_dir / "sess_noclear" / "metadata.json")
        assert data["title"] == "已有标题", "空字符串不应清除已有标题"


# ===========================================================================
# sync_session_request_metadata —— 请求参数 → 会话元数据校验/同步入口
# ===========================================================================
def _drain_queue():
    from jiuwenswarm.server.runtime.session.session_metadata import _METADATA_QUEUE
    _METADATA_QUEUE.join()


class TestSyncSessionRequestMetadata:
    """sync_session_request_metadata：校验请求参数 vs 磁盘 metadata，按字段语义写入。"""

    @staticmethod
    def test_persist_session_is_initialized_and_immutable(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            init_session_metadata,
            sync_session_request_metadata,
        )

        init_session_metadata(session_id="persist_locked", persist_session=True)
        sync_session_request_metadata(
            session_id="persist_locked", persist_session=False
        )
        _drain_queue()

        assert get_session_metadata("persist_locked")["persist_session"] is True

    @staticmethod
    def test_legacy_metadata_can_lock_persist_session_once(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _write_metadata_sync,
            get_session_metadata,
            sync_session_request_metadata,
        )

        _write_metadata_sync(
            "persist_legacy",
            {"session_id": "persist_legacy", "project_dir": ""},
        )
        sync_session_request_metadata(
            session_id="persist_legacy", persist_session=True
        )
        _drain_queue()
        assert get_session_metadata("persist_legacy")["persist_session"] is True

        sync_session_request_metadata(
            session_id="persist_legacy", persist_session=False
        )
        _drain_queue()
        assert get_session_metadata("persist_legacy")["persist_session"] is True

    @staticmethod
    def test_project_dir_first_lock_writes_and_returns(sessions_dir):
        """project_dir 首次锁定：磁盘为空 → 写入请求值并返回"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="s1")  # project_dir 为空
        effective = sync_session_request_metadata(
            session_id="s1", project_dir="E:\\projA"
        )
        _drain_queue()
        assert effective == "E:\\projA"
        assert get_session_metadata("s1")["project_dir"] == "E:\\projA"

    @staticmethod
    def test_project_dir_locked_ignores_inconsistent_request_value(
        sessions_dir, monkeypatch
    ):
        """已锁定 project_dir 时，请求带不同值 → 告警 + 不覆盖 + 返回锁定值"""
        import jiuwenswarm.server.runtime.session.session_metadata as sm
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="s1")
        sync_session_request_metadata(session_id="s1", project_dir="E:\\locked")
        _drain_queue()

        # 拦截 logger.warning，避免依赖 logging propagation
        warnings: list[str] = []
        original_warning = sm.logger.warning

        def _capture_warning(msg, *args, **kwargs):
            warnings.append(msg % args if args else msg)
            original_warning(msg, *args, **kwargs)

        monkeypatch.setattr(sm.logger, "warning", _capture_warning)

        effective = sync_session_request_metadata(
            session_id="s1", project_dir="E:\\other"
        )
        _drain_queue()

        assert effective == "E:\\locked", "应返回锁定值而非请求值"
        assert get_session_metadata("s1")["project_dir"] == "E:\\locked", "不应被覆盖"
        assert any("已锁定" in w for w in warnings), "应记告警"

    @staticmethod
    def test_project_dir_locked_returns_locked_value_when_request_none(sessions_dir):
        """已锁定后，请求不带 project_dir → 返回锁定值"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
        )

        init_session_metadata(session_id="s1")
        sync_session_request_metadata(session_id="s1", project_dir="E:\\locked")
        _drain_queue()

        effective = sync_session_request_metadata(session_id="s1")  # 不传 project_dir
        assert effective == "E:\\locked"

    @staticmethod
    def test_sync_empty_session_id_returns_none(sessions_dir):
        """空 session_id → 直接返回 None，不做任何操作"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            sync_session_request_metadata,
        )
        assert sync_session_request_metadata(session_id="", project_dir="E:\\x") is None
        assert sync_session_request_metadata(session_id="   ", project_dir="E:\\x") is None

    @staticmethod
    def test_sync_none_project_dir_when_unlocked(sessions_dir):
        """未锁定且请求不带 project_dir → 返回 None，不写入"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="s1")
        effective = sync_session_request_metadata(session_id="s1")
        _drain_queue()
        assert effective is None
        assert get_session_metadata("s1")["project_dir"] == ""

    @staticmethod
    def test_sync_channel_id_first_locked_not_overwritten(sessions_dir):
        """channel_id 首次锁定：web 归属的会话不被 feishu 通道消息覆写。

        联机/共享会话场景下 feishu 人机消息经 _forward_loop 转发进共享会话时，
        若 sync 无条件写 channel_id 会把归属通道从 web 翻转为 feishu，
        导致 web 侧栏按 channel_id=="web" 过滤后会话消失。
        """
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="s_lock_ch", channel_id="web")
        # feishu 通道的请求同步进来 → 不得覆盖已锁定的归属通道
        sync_session_request_metadata(session_id="s_lock_ch", channel_id="feishu")
        _drain_queue()
        assert get_session_metadata("s_lock_ch")["channel_id"] == "web"

    @staticmethod
    def test_sync_model_overwritten_each_call(sessions_dir):
        """model：显式覆盖式——仅当 explicit_model_provided=True 时才覆盖磁盘值"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="s1", model="glm-5")
        # 显式携带 model → 覆盖磁盘值
        sync_session_request_metadata(
            session_id="s1", model="glm-5.1", explicit_model_provided=True
        )
        _drain_queue()
        assert get_session_metadata("s1")["model"] == "glm-5.1"
        sync_session_request_metadata(
            session_id="s1", model="deepseek-v4", explicit_model_provided=True
        )
        _drain_queue()
        assert get_session_metadata("s1")["model"] == "deepseek-v4"

    @staticmethod
    def test_sync_model_not_overwritten_when_implicit(sessions_dir):
        """model：请求未显式携带（如只读 RPC 回退到进程 MODEL_NAME）→ 不覆盖磁盘。

        回归保护：只读 RPC 不带 model_name，不应把进程默认模型回写覆盖
        用户在该会话用 /model 切换过的模型。
        """
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="s_m", model="user-picked-model")
        # 模拟只读 RPC：传了 model（进程默认回退值）但 explicit_model_provided=False
        sync_session_request_metadata(
            session_id="s_m", model="your-model-name", explicit_model_provided=False
        )
        _drain_queue()
        assert get_session_metadata("s_m")["model"] == "user-picked-model", \
            "未显式携带 model 时不得覆盖磁盘已锁定的用户选择"

    @staticmethod
    def test_sync_model_none_keeps_existing(sessions_dir):
        """model=None 不更新（保留上次）"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="s1", model="glm-5")
        sync_session_request_metadata(session_id="s1")  # 不传 model
        _drain_queue()
        assert get_session_metadata("s1")["model"] == "glm-5"

    @staticmethod
    def test_sync_last_user_message_at_overwritten_when_provided(sessions_dir):
        """last_user_message_at：覆盖式，传入则刷新"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="s1")
        sync_session_request_metadata(session_id="s1", last_user_message_at=1000.0)
        _drain_queue()
        assert get_session_metadata("s1")["last_user_message_at"] == 1000.0
        sync_session_request_metadata(session_id="s1", last_user_message_at=2000.0)
        _drain_queue()
        assert get_session_metadata("s1")["last_user_message_at"] == 2000.0

    @staticmethod
    def test_sync_last_user_message_at_kept_when_not_provided(sessions_dir):
        """last_user_message_at：不传则保留旧值"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="s1")
        original = get_session_metadata("s1")["last_user_message_at"]
        sync_session_request_metadata(session_id="s1")  # 不传
        _drain_queue()
        assert get_session_metadata("s1")["last_user_message_at"] == original

    @staticmethod
    def test_sync_mode_overwritten(sessions_dir):
        """mode：显式覆盖式——仅当 explicit_mode_provided=True 时才覆盖磁盘值"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="s1", mode="code")
        # 显式携带 mode → 覆盖磁盘值
        sync_session_request_metadata(
            session_id="s1", mode="agent.plan", explicit_mode_provided=True
        )
        _drain_queue()
        # 读路径惰性迁移：旧 canonical agent.plan → 新 canonical agent.work.plan
        assert get_session_metadata("s1")["mode"] == "agent.work.plan"

    @staticmethod
    def test_sync_mode_not_overwritten_when_implicit(sessions_dir):
        """mode：请求未显式携带（如只读 RPC 默认推断）→ 不覆盖磁盘已锁定的 mode。

        回归保护：只读 RPC（skills.retrieval.status 等）不带 mode，不应把 team 会话
        的 mode 腐蚀成默认推断的 agent。
        """
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="s_team", mode="team")
        # 模拟只读 RPC：传了 mode（默认推断值）但 explicit_mode_provided=False
        sync_session_request_metadata(
            session_id="s_team", mode="agent.plan", explicit_mode_provided=False
        )
        _drain_queue()
        # 读路径惰性迁移：旧 canonical team → 新 canonical team.work.normal
        assert get_session_metadata("s_team")["mode"] == "team.work.normal", \
            "未显式携带 mode 时不得覆盖磁盘已锁定的 team"

    @staticmethod
    def test_sync_whitespace_mode_not_treated_as_explicit(sessions_dir):
        """回归保护：纯空白 mode 字符串不应被误判为「显式提供」。

        上游 _prepare_code_mode_chat_turn 用 isinstance(x, str) and bool(x.strip())
        判断是否显式携带 mode。若退化为裸 bool()，bool("   ") 为 True，会把空白 mode
        当显式提供 → 经 resolve 默认推断成 agent.plan → 写盘腐蚀已锁定的 team。
        本用例守存储层契约：即使调用方误传 explicit_mode_provided=False（与严格判断一致），
        磁盘 team 不得被覆盖；同时验证空白串不应让 mode 写盘。
        """
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            sync_session_request_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="s_ws", mode="team")
        # 空白 mode 不算显式携带 → 守卫生效，不写盘
        sync_session_request_metadata(
            session_id="s_ws", mode="agent.plan", explicit_mode_provided=False
        )
        _drain_queue()
        # 读路径惰性迁移：旧 canonical team → 新 canonical team.work.normal
        assert get_session_metadata("s_ws")["mode"] == "team.work.normal", \
            "空白/未显式 mode 不得覆盖磁盘已锁定的 team"

    @staticmethod
    def test_sync_creates_when_missing(sessions_dir):
        """会话元数据不存在 → 兜底新建分支补齐字段"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            sync_session_request_metadata,
            get_session_metadata,
        )

        effective = sync_session_request_metadata(
            session_id="s_new",
            channel_id="web",
            mode="code",
            model="glm-5",
            project_dir="E:\\newproj",
            last_user_message_at=1234.0,
            explicit_mode_provided=True,
            explicit_model_provided=True,
        )
        _drain_queue()
        assert effective == "E:\\newproj"
        meta = get_session_metadata("s_new")
        assert meta["project_dir"] == "E:\\newproj"
        assert meta["model"] == "glm-5"
        # 读路径惰性迁移：旧 canonical code → 新 canonical agent.code.normal
        assert meta["mode"] == "agent.code.normal"
        assert meta["last_user_message_at"] == 1234.0
        assert meta["status"] == "idle"

    @staticmethod
    def test_sync_creates_with_defaults_when_minimal(sessions_dir):
        """兜底新建：全默认参数"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            sync_session_request_metadata,
            get_session_metadata,
        )

        effective = sync_session_request_metadata(session_id="s_min")  # 全默认
        _drain_queue()
        assert effective is None  # 无 project_dir
        meta = get_session_metadata("s_min")
        assert meta["project_dir"] == ""
        assert meta["model"] == ""
        assert meta["mode"] == "unknown"
        assert meta["status"] == "idle"
        assert meta["last_user_message_at"] > 0

    @staticmethod
    def test_sync_preserves_disk_pinned_over_stale_cache(sessions_dir):
        """跨进程缓存覆盖回归：AgentServer 缓存里残留 pinned=False,
        但磁盘已被 Gateway 置顶为 pinned=True。sync 必须强制读盘,
        保留磁盘的 pinned=True/pin_order,不被本进程旧缓存覆盖。
        """
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _METADATA_CACHE,
            _write_metadata_sync,
            init_session_metadata,
            sync_session_request_metadata,
        )

        init_session_metadata(session_id="s_pin")  # 初始 pinned=False
        # 模拟 Gateway 跨进程把磁盘置顶(pinned=True, pin_order=1):
        # _write_metadata_sync 不碰缓存,正好模拟另一进程的落盘。
        _write_metadata_sync("s_pin", {
            **_read_json(sessions_dir / "s_pin" / "metadata.json"),
            "pinned": True,
            "pin_order": 1,
        })
        # 投毒 AgentServer 本进程缓存为旧值 pinned=False(上一轮聊天残留)
        _METADATA_CACHE["s_pin"] = {
            **_read_json(sessions_dir / "s_pin" / "metadata.json"),
            "pinned": False,
            "pin_order": 0,
        }

        # AgentServer 收到一次聊天请求 → 调 sync 写 model/mode 等请求级字段
        sync_session_request_metadata(
            session_id="s_pin", model="glm-5", mode="code",
            last_user_message_at=9999.0,
            explicit_mode_provided=True,
            explicit_model_provided=True,
        )
        _drain_queue()

        # 断言:磁盘仍是 Gateway 写入的置顶状态,未被旧缓存覆盖
        data = _read_json(sessions_dir / "s_pin" / "metadata.json")
        assert data["pinned"] is True, "置顶状态被 AgentServer 旧缓存回写覆盖"
        assert data["pin_order"] == 1, "pin_order 不应丢失"
        # 请求级字段仍按语义写入
        assert data["model"] == "glm-5"
        assert data["mode"] == "agent.code.normal"
        assert data["last_user_message_at"] == 9999.0

    @staticmethod
    def test_sync_new_branch_includes_pinned_fields(sessions_dir):
        """兜底新建分支写入 pinned/pin_order 默认值,磁盘 schema 齐全。"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            sync_session_request_metadata,
        )

        sync_session_request_metadata(
            session_id="s_new_pin", channel_id="web", model="glm-5",
        )
        _drain_queue()
        data = _read_json(sessions_dir / "s_new_pin" / "metadata.json")
        assert data["pinned"] is False
        assert data["pin_order"] == 0


# ===========================================================================
# _sync_chat_request_metadata —— AgentServer 进程层薄封装（模块级函数）
# ===========================================================================
@pytest.fixture()
def clean_model_env(monkeypatch):
    """默认 MODEL_NAME 不设，避免环境污染；需要时再 monkeypatch.setenv"""
    monkeypatch.delenv("MODEL_NAME", raising=False)


def _make_agent_request(params=None, metadata=None, session_id="sess_1", channel_id="web", req_method=None):
    from jiuwenswarm.common.schema.agent import AgentRequest

    return AgentRequest(
        request_id="req-1",
        channel_id=channel_id,
        session_id=session_id,
        params=params or {},
        metadata=metadata,
        req_method=req_method,
    )


class TestSyncChatRequestMetadata:
    """_sync_chat_request_metadata：从 AgentRequest 采集参数 + 委托 sync 写盘。

    覆盖 model_name 缺失回退 MODEL_NAME、无 session_id 不写盘、
    异常退化为返回请求候选值、兜底新建等场景。
    """

    @staticmethod
    def test_collects_and_persists(sessions_dir, clean_model_env):
        """正常路径：采集 model_name/project_dir/mode → 写盘 → 返回生效 project_dir

        传 req_method=CHAT_SEND 使 is_chat_turn=True，名副其实地验证 chat 轮次刷新
        last_user_message_at/last_message_at（避免之前因 req_method=None 导致
        is_chat_turn=False、时间不被刷新、断言却靠 init 时间巧合通过的误导）。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="sess_1")  # project_dir 为空
        # 记录 init 写入的时间，用于断言 _sync 确实刷新了它（而非巧合等于 init 值）
        lum_before = get_session_metadata("sess_1")["last_user_message_at"]
        req = _make_agent_request(
            params={"model_name": "glm-5", "mode": "code", "project_dir": "E:\\projA"},
            req_method=ReqMethod.CHAT_SEND,
        )
        # params 显式带了 mode → explicit_mode_provided=True；model_name 内部判断为显式
        effective = _sync_chat_request_metadata(
            req, "E:\\projA", "code", explicit_mode_provided=True
        )
        _drain_queue()

        assert effective == "E:\\projA"
        meta = get_session_metadata("sess_1")
        assert meta["model"] == "glm-5"
        # 读路径惰性迁移：旧 canonical code → 新 canonical agent.code.normal
        assert meta["mode"] == "agent.code.normal"
        assert meta["project_dir"] == "E:\\projA"
        assert meta["channel_id"] == "web"
        # last_user_message_at 被刷新为当前时刻（chat 轮次 → is_chat_turn=True → 真正写入）
        assert meta["last_user_message_at"] > lum_before, "chat 轮次应刷新 last_user_message_at"
        assert abs(meta["last_user_message_at"] - time.time()) < 5.0

    @staticmethod
    def test_project_dir_passed_through_to_sync(sessions_dir, clean_model_env):
        """project_dir 参数透传给 sync，由 sync 决定锁定/告警"""
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="sess_1")
        update_session_metadata(session_id="sess_1", project_dir="E:\\locked")
        _drain_queue()

        # 请求带不同 project_dir，但磁盘已锁定 → 返回锁定值
        req = _make_agent_request(params={"model_name": "glm-5"})
        effective = _sync_chat_request_metadata(req, "E:\\other", "code")
        _drain_queue()
        assert effective == "E:\\locked"

    @staticmethod
    def test_no_model_name_keeps_existing(sessions_dir, monkeypatch):
        """params 不带 model_name → 未显式携带，不写盘，保持磁盘原值。

        回归保护：只读 RPC 不带 model_name，不应把进程 MODEL_NAME 默认值回写覆盖
        用户在该会话用 /model 切换过的模型。model_name 未带 → explicit_model_provided=False。
        """
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            get_session_metadata,
        )

        monkeypatch.setenv("MODEL_NAME", "env-glm-5")  # 进程默认值，不应被写盘
        init_session_metadata(session_id="sess_1", model="user-picked-model")
        req = _make_agent_request(params={})  # 不带 model_name
        _sync_chat_request_metadata(req, None, "agent")
        _drain_queue()

        assert get_session_metadata("sess_1")["model"] == "user-picked-model", \
            "未显式携带 model_name 时不得用进程默认值覆盖磁盘已选模型"

    @staticmethod
    def test_empty_model_name_keeps_existing(sessions_dir, monkeypatch):
        """params.model_name 为空白 → 同未显式携带，不写盘，保持磁盘原值"""
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            get_session_metadata,
        )

        monkeypatch.setenv("MODEL_NAME", "env-glm-5")
        init_session_metadata(session_id="sess_1", model="user-picked-model")
        req = _make_agent_request(params={"model_name": "   "})
        _sync_chat_request_metadata(req, None, "agent")
        _drain_queue()

        assert get_session_metadata("sess_1")["model"] == "user-picked-model"

    @staticmethod
    def test_no_model_no_env_keeps_existing(sessions_dir, clean_model_env):
        """params 不带 model_name 且 env 也没设 → model=None → 不覆盖"""
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="sess_1", model="original-model")
        req = _make_agent_request(params={})
        _sync_chat_request_metadata(req, None, "agent")
        _drain_queue()

        assert get_session_metadata("sess_1")["model"] == "original-model"

    @staticmethod
    def test_no_session_id_returns_project_dir_without_writing(
        sessions_dir, clean_model_env
    ):
        """session_id 为空 → 返回 project_dir，不调 sync（不写盘）"""
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata

        req = _make_agent_request(
            params={"model_name": "glm-5"}, session_id=None,
        )
        result = _sync_chat_request_metadata(req, "E:\\reqproj", "code")
        assert result == "E:\\reqproj"

    @staticmethod
    def test_empty_session_id_returns_project_dir(sessions_dir, clean_model_env):
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata

        req = _make_agent_request(params={"model_name": "glm-5"}, session_id="   ")
        assert _sync_chat_request_metadata(req, "E:\\p", "code") == "E:\\p"

    @staticmethod
    def test_returns_project_dir_on_sync_failure(
        sessions_dir, clean_model_env, monkeypatch
    ):
        """sync 抛 OSError → 返回 project_dir，不抛"""
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata
        import jiuwenswarm.server.runtime.session.session_metadata as sm

        def _boom(**kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(sm, "sync_session_request_metadata", _boom)

        req = _make_agent_request(params={"model_name": "glm-5"}, session_id="sess_1")
        result = _sync_chat_request_metadata(req, "E:\\reqproj", "code")
        assert result == "E:\\reqproj", "异常时应退化为返回请求候选值"

    @staticmethod
    def test_returns_project_dir_on_value_error(
        sessions_dir, clean_model_env, monkeypatch
    ):
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata
        import jiuwenswarm.server.runtime.session.session_metadata as sm

        def _boom(**kwargs):
            raise ValueError("bad data")

        monkeypatch.setattr(sm, "sync_session_request_metadata", _boom)

        req = _make_agent_request(params={"model_name": "glm-5"}, session_id="sess_1")
        assert _sync_chat_request_metadata(req, "E:\\p", "code") == "E:\\p"

    @staticmethod
    def test_creates_metadata_when_missing(sessions_dir, clean_model_env):
        """不先 init，直接 _sync → 经 sync 兜底新建分支创建"""
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        req = _make_agent_request(
            params={"model_name": "glm-5", "mode": "code", "project_dir": "E:\\newproj"},
            session_id="s_new",
        )
        effective = _sync_chat_request_metadata(
            req, "E:\\newproj", "code", explicit_mode_provided=True
        )
        _drain_queue()

        assert effective == "E:\\newproj"
        meta = get_session_metadata("s_new")
        assert meta["model"] == "glm-5"
        # 读路径惰性迁移：旧 canonical code → 新 canonical agent.code.normal
        assert meta["mode"] == "agent.code.normal"
        assert meta["project_dir"] == "E:\\newproj"
        assert meta["status"] == "idle"

    @staticmethod
    def test_readonly_rpc_does_not_refresh_time_fields(sessions_dir, clean_model_env):
        """回归保护：只读 RPC（skills.list）不得刷新 last_user_message_at/last_message_at。

        复现用户反馈：打开两天前的历史会话、不发消息、点一下技能按钮（skills.list），
        会话排序时间被刷新成「现在」→ 旧会话被置顶。根因是只读 RPC 走到了
        _sync_chat_request_metadata，而时间字段无 chat-turn 守卫。
        修复后：只读 RPC 传 is_chat_turn=False → 时间字段不写盘。
        """
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            get_session_metadata,
        )
        from jiuwenswarm.common.schema.message import ReqMethod

        # 两天前的历史会话
        init_session_metadata(session_id="sess_old")
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _write_metadata_sync,
        )
        two_days_ago = 1000.0
        _write_metadata_sync("sess_old", {
            **get_session_metadata("sess_old"),
            "last_message_at": two_days_ago,
            "last_user_message_at": two_days_ago,
        })

        req = _make_agent_request(
            params={},  # 只读 RPC 不带 model_name/mode
            session_id="sess_old",
            req_method=ReqMethod.SKILLS_LIST,
        )
        _sync_chat_request_metadata(req, None, "agent")
        _drain_queue()

        meta = get_session_metadata("sess_old")
        assert meta["last_user_message_at"] == two_days_ago, \
            "只读 RPC 不得刷新 last_user_message_at（历史会话不应被置顶）"
        assert meta["last_message_at"] == two_days_ago, \
            "只读 RPC 不得刷新 last_message_at"

    @staticmethod
    def test_chat_turn_refreshes_time_fields(sessions_dir, clean_model_env):
        """chat 轮次（CHAT_SEND）应刷新 last_user_message_at/last_message_at。

        契约对照：与上一用例互为镜像——只有用户真正发消息才更新排序时间。
        """
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            get_session_metadata,
        )
        from jiuwenswarm.common.schema.message import ReqMethod

        init_session_metadata(session_id="sess_chat")
        before = get_session_metadata("sess_chat")
        old_lum = before["last_user_message_at"]
        old_lm = before["last_message_at"]

        req = _make_agent_request(
            params={"model_name": "glm-5", "mode": "code"},
            session_id="sess_chat",
            req_method=ReqMethod.CHAT_SEND,
        )
        _sync_chat_request_metadata(req, None, "code", explicit_mode_provided=True)
        _drain_queue()

        meta = get_session_metadata("sess_chat")
        assert meta["last_user_message_at"] > old_lum, "chat 轮次应刷新 last_user_message_at"
        assert meta["last_message_at"] > old_lm, "chat 轮次应刷新 last_message_at"

    @staticmethod
    def test_readonly_rpc_does_not_create_empty_session_dir(sessions_dir, clean_model_env):
        """回归保护：只读 RPC 对尚不存在的 session_id 不得凭空建空目录。

        复现场景：前端拿一个未持久化的临时 session_id 去查 skills.list，
        结果在 agent/sessions 下出现一个只含 metadata.json 的空会话目录。
        根因：sync 兜底新建分支无条件 mkdir。修复后：is_chat_turn=False 时
        仍走兜底新建（保留 channel_id 等字段语义），但时间字段不刷新。
        本用例聚焦「只读 RPC 不该走到 sync」——由 _is_stateless_method_request 短路保证，
        此处补一层存储层兜底契约：即便只读 RPC 误走到 sync，时间字段也不得被改。
        """
        from jiuwenswarm.server.agent_ws_server import _sync_chat_request_metadata
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )
        from jiuwenswarm.common.schema.message import ReqMethod

        # 不预先 init → 触发 sync 兜底新建分支
        req = _make_agent_request(
            params={},
            session_id="ghost_readonly",
            req_method=ReqMethod.SKILLS_LIST,
        )
        _sync_chat_request_metadata(req, None, "agent")
        _drain_queue()

        # 兜底新建会建目录（这是 sync 的既定行为，本修复不动该分支），
        # 但只读 RPC 不应刷新时间到「现在」——last_user_message_at 应等于 created_at，
        # 而非 chat 时刻。这里只断言只读语义：mode/model 不被默认推断值腐蚀。
        meta = get_session_metadata("ghost_readonly")
        assert meta["mode"] == "unknown", \
            "只读 RPC 未显式携带 mode → 不得用默认推断值腐蚀磁盘 mode"
        assert meta["model"] == "", \
            "只读 RPC 未显式携带 model → 不得用进程默认值腐蚀磁盘 model"


# ===========================================================================
# session.get_metadata RPC handler —— Gateway 层只读出口
# ===========================================================================
class _FakeWebChannel:
    """最小 WebChannel 桩，记录 register_method / send_response 调用。"""

    def __init__(self):
        self.methods: dict[str, object] = {}
        self.responses: list[dict] = []

    def register_method(self, name, handler):
        self.methods[name] = handler

    def on_connect(self, handler):
        pass

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {
                "id": req_id,
                "ok": ok,
                "payload": payload,
                "error": error,
                "code": code,
            }
        )


@pytest.fixture()
def registered_channel(sessions_dir):
    """注册所有 web handler，返回 _FakeWebChannel（含 session.get_metadata）"""
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
        WebHandlersBindParams,
        _register_web_handlers,
    )

    channel = _FakeWebChannel()
    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
        )
    )
    return channel


async def _call_method(method_table, method, params):
    """调用 handler 并返回最后一个响应"""
    handler = method_table.methods[method]
    await handler(object(), "req-1", params, "sess-caller")
    return method_table.responses[-1]


class TestSessionGetMetadataHandler:
    """session.get_metadata：Phase 2 迁移后由 AgentServer SessionAdapter 处理。

    迁移说明：Web ``session.get_metadata`` 已改为 E2A 薄代理转发（决策 D2/D8），
    行为判定（完整 metadata / BAD_REQUEST / NOT_FOUND / 单会话隔离）落在
    AgentServer 侧 ``SessionAdapter``（``SESSION_GET_METADATA``），此处直接调用
    适配器验证；``test_method_registered`` 保留验证 Web handler 仍注册。
    """

    @staticmethod
    def _adapter_get_metadata(session_id: str):
        from jiuwenswarm.common.schema.agent import AgentRequest
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.server.runtime.gateway_adapter.session_adapter import (
            SessionAdapter,
        )

        return SessionAdapter().handle(
            AgentRequest(
                request_id="req-1",
                channel_id="web",
                req_method=ReqMethod.SESSION_GET_METADATA,
                params={"session_id": session_id},
            )
        )

    @staticmethod
    @pytest.mark.asyncio
    async def test_returns_metadata_for_existing_session(registered_channel, sessions_dir):
        """存在的会话返回完整 metadata（含新字段）"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _METADATA_QUEUE,
            init_session_metadata,
            update_session_metadata,
        )

        init_session_metadata(
            session_id="sess_x",
            channel_id="web",
            project_dir="E:\\myproj",
            model="glm-5",
        )
        update_session_metadata(
            session_id="sess_x",
            mode="agent.plan",
            model="glm-5",
            project_dir="E:\\myproj",
            last_user_message_at=1234.0,
        )
        _METADATA_QUEUE.join()

        resp = await TestSessionGetMetadataHandler._adapter_get_metadata("sess_x")

        assert resp.ok is True
        payload = resp.payload
        assert payload["session_id"] == "sess_x"
        # 读路径惰性迁移：旧 canonical agent.plan → 新 canonical agent.work.plan
        assert payload["mode"] == "agent.work.plan"
        assert payload["model"] == "glm-5"
        assert payload["project_dir"] == "E:\\myproj"
        assert payload["last_user_message_at"] == 1234.0
        assert payload["status"] == "idle"

    @staticmethod
    @pytest.mark.asyncio
    async def test_missing_session_id_returns_bad_request(registered_channel):
        """session_id 缺失 → BAD_REQUEST"""
        from jiuwenswarm.common.schema.agent import AgentRequest
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.server.runtime.gateway_adapter.session_adapter import (
            SessionAdapter,
        )

        resp = await SessionAdapter().handle(
            AgentRequest(
                request_id="req-1",
                channel_id="web",
                req_method=ReqMethod.SESSION_GET_METADATA,
                params={"session_id": ""},
            )
        )
        assert resp.ok is False
        assert resp.payload["code"] == "BAD_REQUEST"

        # params 不是 dict
        resp2 = await SessionAdapter().handle(
            AgentRequest(
                request_id="req-1",
                channel_id="web",
                req_method=ReqMethod.SESSION_GET_METADATA,
                params=None,
            )
        )
        assert resp2.ok is False
        assert resp2.payload["code"] == "BAD_REQUEST"

    @staticmethod
    @pytest.mark.asyncio
    async def test_nonexistent_session_returns_not_found(registered_channel):
        """不存在的会话 → NOT_FOUND"""
        resp = await TestSessionGetMetadataHandler._adapter_get_metadata("no_such_session")
        assert resp.ok is False
        assert resp.payload["code"] == "NOT_FOUND"

    @staticmethod
    @pytest.mark.asyncio
    async def test_method_registered(registered_channel):
        """handler 已注册为 session.get_metadata"""
        assert "session.get_metadata" in registered_channel.methods

    @staticmethod
    @pytest.mark.asyncio
    async def test_single_session_isolation(registered_channel, sessions_dir):
        """单会话隔离：A 会话的查询不返回 B 会话的数据"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _METADATA_QUEUE,
            init_session_metadata,
        )

        init_session_metadata(session_id="sess_A", model="modelA", project_dir="E:\\A")
        init_session_metadata(session_id="sess_B", model="modelB", project_dir="E:\\B")
        _METADATA_QUEUE.join()

        resp_a = await TestSessionGetMetadataHandler._adapter_get_metadata("sess_A")
        resp_b = await TestSessionGetMetadataHandler._adapter_get_metadata("sess_B")

        assert resp_a.payload["model"] == "modelA"
        assert resp_a.payload["project_dir"] == "E:\\A"
        assert resp_b.payload["model"] == "modelB"
        assert resp_b.payload["project_dir"] == "E:\\B"


# ===========================================================================
# 惰性迁移: 读取老会话时按需推断缺失字段并写回磁盘
# 覆盖 P2：stat() OSError 不得中断读取；or 短路不得跳过合法 0.0 时间戳
# ===========================================================================
class TestLazyMigrationOnRead:
    """惰性迁移:读取老会话 metadata 时补全 project_dir/model/status/last_user_message_at,
    可推断字段(work_mode/project_id)做确定性推断并异步写盘。"""

    @staticmethod
    def test_fills_missing_constant_fields(sessions_dir):
        """缺 project_dir/model/status 的老会话读取时补常量默认值。"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            _METADATA_CACHE,
        )

        sdir = sessions_dir / "old_session"
        sdir.mkdir()
        (sdir / "metadata.json").write_text(
            json.dumps({"session_id": "old_session"}), encoding="utf-8"
        )
        _METADATA_CACHE.pop("old_session", None)

        data = get_session_metadata("old_session", cache_bust=True)
        assert data["project_dir"] == ""
        assert data["model"] == ""
        assert data["status"] == "idle"
        assert "last_user_message_at" in data

    @staticmethod
    def test_repairs_subagent_mode_leaked_into_web_parent(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _METADATA_CACHE,
            get_session_metadata,
        )

        session_dir = sessions_dir / "web_parent"
        session_dir.mkdir()
        (session_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "session_id": "web_parent",
                    "channel_id": "web",
                    "mode": "subagent",
                    "team_name": "",
                    "work_mode": "work",
                }
            ),
            encoding="utf-8",
        )
        _METADATA_CACHE.pop("web_parent", None)

        metadata = get_session_metadata(
            "web_parent",
            cache_bust=True,
            enable_writeback=False,
        )

        assert metadata["mode"] == "agent.work.normal"

    @staticmethod
    def test_keeps_standalone_subagent_channel_mode(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _METADATA_CACHE,
            get_session_metadata,
        )

        session_dir = sessions_dir / "subagent_session"
        session_dir.mkdir()
        (session_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "session_id": "subagent_session",
                    "channel_id": "subagent",
                    "mode": "subagent",
                    "team_name": "",
                    "work_mode": "work",
                }
            ),
            encoding="utf-8",
        )
        _METADATA_CACHE.pop("subagent_session", None)

        metadata = get_session_metadata(
            "subagent_session",
            cache_bust=True,
            enable_writeback=False,
        )

        assert metadata["mode"] == "subagent"

    @staticmethod
    def test_last_user_message_at_uses_last_message_at_when_present(sessions_dir):
        """有 last_message_at 时优先用它，不被 or 短路跳过 0.0。"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            _METADATA_CACHE,
        )

        sdir = sessions_dir / "s_with_lma"
        sdir.mkdir()
        (sdir / "metadata.json").write_text(
            json.dumps({"session_id": "s_with_lma", "last_message_at": 123.0}),
            encoding="utf-8",
        )
        _METADATA_CACHE.pop("s_with_lma", None)

        data = get_session_metadata("s_with_lma", cache_bust=True)
        _drain_queue()
        assert data["last_user_message_at"] == 123.0
        # 写盘后磁盘也有该字段
        assert _read_json(sdir / "metadata.json")["last_user_message_at"] == 123.0

    @staticmethod
    def test_zero_last_message_at_not_short_circuited(sessions_dir):
        """last_message_at=0.0（合法但 falsy）不得被 ``or`` 跳过回退到 stat mtime。"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            _METADATA_CACHE,
        )

        sdir = sessions_dir / "s_zero"
        sdir.mkdir()
        (sdir / "metadata.json").write_text(
            json.dumps({"session_id": "s_zero", "last_message_at": 0.0}),
            encoding="utf-8",
        )
        _METADATA_CACHE.pop("s_zero", None)

        data = get_session_metadata("s_zero", cache_bust=True)
        # 0.0 是合法值，应被采用而非回退到目录 mtime
        assert data["last_user_message_at"] == 0.0

    @staticmethod
    def test_falls_back_to_dir_mtime(sessions_dir):
        """无任何时间字段时回退到目录 mtime（OSError 时 0.0 兜底）。"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            _METADATA_CACHE,
        )

        sdir = sessions_dir / "s_no_time"
        sdir.mkdir()
        (sdir / "metadata.json").write_text(
            json.dumps({"session_id": "s_no_time"}), encoding="utf-8"
        )
        expected_mtime = sdir.stat().st_mtime
        _METADATA_CACHE.pop("s_no_time", None)

        data = get_session_metadata("s_no_time", cache_bust=True)
        _drain_queue()
        assert data["last_user_message_at"] == expected_mtime

    @staticmethod
    def test_corrupt_metadata_returns_empty_without_raising(sessions_dir):
        """metadata.json 非法 JSON 时返回空 dict,不抛异常。

        惰性迁移语义下无启动扫描,单条读取 corrupt 文件时 _read_metadata
        返回 {},_apply_metadata_defaults_with_inference 对空 dict 短路返回。
        故不会触发跨会话影响(无 "good 仍被迁移" 的交叉语义)。
        """
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            _METADATA_CACHE,
        )

        bad = sessions_dir / "s_corrupt"
        bad.mkdir()
        (bad / "metadata.json").write_text("{not json", encoding="utf-8")
        _METADATA_CACHE.pop("s_corrupt", None)

        # 不抛异常,返回空 dict
        data = get_session_metadata("s_corrupt", cache_bust=True)
        assert data == {}
        # bad 的文件原样保留(未被改写)
        assert (bad / "metadata.json").read_text(encoding="utf-8") == "{not json"

    @staticmethod
    def test_resolves_project_id_from_project_dir(sessions_dir, tmp_path, monkeypatch):
        """有 project_dir 但无 project_id 的存量会话,读取时按 path 解析到 project_id 并写盘。"""
        # 准备 project_store: 创建一个有路径的项目
        root = tmp_path / "agent"
        root.mkdir()
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.project_store.get_agent_root_dir",
            lambda: root,
        )
        from jiuwenswarm.server.runtime.session import project_store
        project_store.invalidate_cache()
        proj = project_store.create_project("P", "E:\\legacy_app")

        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            _METADATA_CACHE,
        )

        # 存量会话: 有 project_dir,无 project_id
        sdir = sessions_dir / "s_legacy"
        sdir.mkdir()
        (sdir / "metadata.json").write_text(
            json.dumps({"session_id": "s_legacy", "project_dir": "E:\\legacy_app"}),
            encoding="utf-8",
        )
        _METADATA_CACHE.pop("s_legacy", None)

        data = get_session_metadata("s_legacy", cache_bust=True)
        _drain_queue()
        assert data["project_id"] == proj.project_id
        # project_dir 保留不变
        assert data["project_dir"] == "E:\\legacy_app"
        # 写盘后磁盘也有 project_id
        assert _read_json(sdir / "metadata.json")["project_id"] == proj.project_id

    @staticmethod
    def test_unresolvable_project_dir_leaves_empty_project_id(sessions_dir, tmp_path, monkeypatch):
        """project_dir 无法匹配任何项目时,project_id 留空(归入默认项目)。"""
        root = tmp_path / "agent"
        root.mkdir()
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.project_store.get_agent_root_dir",
            lambda: root,
        )
        from jiuwenswarm.server.runtime.session import project_store
        project_store.invalidate_cache()

        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            _METADATA_CACHE,
        )

        sdir = sessions_dir / "s_orphan"
        sdir.mkdir()
        (sdir / "metadata.json").write_text(
            json.dumps({"session_id": "s_orphan", "project_dir": "E:\\gone"}),
            encoding="utf-8",
        )
        _METADATA_CACHE.pop("s_orphan", None)

        data = get_session_metadata("s_orphan", cache_bust=True)
        assert data["project_id"] == ""


# ===========================================================================
# set_session_pinned —— 跨进程同步落盘(sync_write)回归
# ===========================================================================
class TestSetSessionPinnedSyncWrite:
    """set_session_pinned 的所有写入必须 sync_write=True:返回前已落盘。

    场景:Gateway 置顶后立即返回成功,AgentServer(只读磁盘)若在异步写入
    落盘前读盘会拿到旧值。本测试不等 _METADATA_QUEUE.join(),立即读盘,
    断言置顶/取消置顶状态已同步落盘。
    """

    @staticmethod
    def test_pin_lands_on_disk_before_return(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            set_session_pinned,
        )

        init_session_metadata(session_id="s_pw")  # pinned=False

        result = set_session_pinned("s_pw", True)
        # 不调 _drain_queue():验证返回前已落盘
        assert result is not None
        assert result[0] is True  # pinned=True

        data = _read_json(sessions_dir / "s_pw" / "metadata.json")
        assert data["pinned"] is True, "置顶未在返回前同步落盘"
        assert data["pin_order"] >= 1

    @staticmethod
    def test_unpin_lands_on_disk_before_return(sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            set_session_pinned,
        )

        init_session_metadata(session_id="s_unpw")
        set_session_pinned("s_unpw", True)  # 先置顶(同步落盘)

        result = set_session_pinned("s_unpw", False)  # 再取消置顶
        # 不调 _drain_queue():取消置顶也必须落盘(重编号不再写非置顶会话)
        assert result is not None
        assert result[0] is False

        data = _read_json(sessions_dir / "s_unpw" / "metadata.json")
        assert data["pinned"] is False, "取消置顶未在返回前同步落盘"
        assert data["pin_order"] == 0


class TestSetSessionPinnedQueuedWriteRace:
    """运行中已有旧异步 metadata 写入时,置顶状态不能被旧快照回滚。"""

    @staticmethod
    def test_old_async_write_does_not_overwrite_pin_after_return(sessions_dir, monkeypatch):
        from jiuwenswarm.server.runtime.session import session_metadata as sm

        original_write = sm._write_metadata_sync
        old_write_started = threading.Event()
        release_old_write = threading.Event()

        def _delayed_write(session_id, metadata, options=None):
            if session_id == "s_async_pin" and metadata.get("model") == "old-queued-write":
                old_write_started.set()
                assert release_old_write.wait(5), "old queued write was not released"
            return original_write(session_id, metadata, options)

        monkeypatch.setattr(sm, "_write_metadata_sync", _delayed_write)

        sm.init_session_metadata(session_id="s_async_pin")
        sm.update_session_metadata(session_id="s_async_pin", model="old-queued-write")
        assert old_write_started.wait(5), "old queued write did not start"

        result = sm.set_session_pinned("s_async_pin", True)
        assert result == (True, 1)
        assert _read_json(sessions_dir / "s_async_pin" / "metadata.json")["pinned"] is True

        release_old_write.set()
        _drain_queue()

        data = _read_json(sessions_dir / "s_async_pin" / "metadata.json")
        assert data["model"] == "old-queued-write"
        assert data["pinned"] is True
        assert data["pin_order"] == 1


# ===========================================================================
# Session identity preservation under concurrent writes (regression tests)
#
# Background: _write_metadata_sync replaces the whole file; it does not merge
# fields. The previous _read_metadata returned an empty file as a valid {}
# (`read_text() or '{}'`) and did not hold _FILE_LOCK. Writers used write_text,
# which truncates before writing, so a concurrent reader could land in the
# truncation window, read "" -> get {} -> read-modify-write the empty dict back
# -> session_id / title / created_at permanently erased. The session then loses
# its ID in the list, cannot be opened, and session.pin fails with
# "session_id is required".
# ===========================================================================
class TestIdentityPreservation:
    @staticmethod
    def _seed(sessions_dir: Path, sid: str = "sess_identity") -> Path:
        d = sessions_dir / sid
        d.mkdir(parents=True, exist_ok=True)
        meta = {
            "session_id": sid,
            "channel_id": "web",
            "created_at": 1700000000.0,
            "title": "title must be preserved",
            "team_name": f"jiuwen_team_{sid}",
            "message_count": 3,
            "mode": "team",
        }
        (d / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return d / "metadata.json"

    @staticmethod
    def test_empty_file_is_read_failure_not_empty_dict(sessions_dir, monkeypatch):
        """An empty file must be treated as a read failure and logged, not silently
        accepted as valid empty metadata.

        Note: both implementations return {}, so the return value alone cannot
        tell them apart. The real difference is whether the failed read is
        recorded at all -- that silence is why this bug was so hard to trace.
        """
        import jiuwenswarm.server.runtime.session.session_metadata as sm

        fpath = TestIdentityPreservation._seed(sessions_dir)
        fpath.write_text("", encoding="utf-8")
        sm._METADATA_CACHE.clear()

        warnings: list[str] = []
        monkeypatch.setattr(
            sm.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg) % a if a else str(msg))
        )

        assert sm._read_metadata("sess_identity", cache_bust=True) == {}
        assert warnings, "empty file silently returned as a valid {} with no warning"

    @staticmethod
    def test_write_without_session_id_does_not_erase_identity(sessions_dir):
        """When the payload lacks session_id, identity fields on disk must survive."""
        import jiuwenswarm.server.runtime.session.session_metadata as sm

        fpath = TestIdentityPreservation._seed(sessions_dir)
        # Simulate a caller doing read-modify-write after getting an empty
        # dict (real case: persist_workflow_runs reads empty, then writes back
        # a payload carrying only workflow_runs).
        sm._write_metadata_sync("sess_identity", {"workflow_runs": {"wf_1": {}}})

        data = _read_json(fpath)
        assert data["session_id"] == "sess_identity"
        assert data["title"] == "title must be preserved"
        assert data["created_at"] == 1700000000.0
        assert data["team_name"] == "jiuwen_team_sess_identity"
        # The new field is still written
        assert data["workflow_runs"] == {"wf_1": {}}

    @staticmethod
    def test_write_is_atomic_no_empty_window(sessions_dir):
        """A concurrent reader must never observe an empty file mid-write (atomic replace)."""
        import jiuwenswarm.server.runtime.session.session_metadata as sm

        fpath = TestIdentityPreservation._seed(sessions_dir)
        big = {
            "session_id": "sess_identity",
            "title": "title must be preserved",
            "created_at": 1700000000.0,
            "blob": "x" * 200_000,
        }
        empties: list[str] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    raw = fpath.read_text(encoding="utf-8")
                except (FileNotFoundError, PermissionError):
                    empties.append("missing")
                    continue
                if not raw.strip():
                    empties.append("empty")

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        for i in range(60):
            big["message_count"] = i
            sm._write_metadata_sync("sess_identity", big)
        stop.set()
        t.join(timeout=3)

        assert empties == [], f"observed {len(empties)} empty/missing reads: write is not atomic"

    @staticmethod
    def test_concurrent_persist_keeps_identity(sessions_dir):
        """A normal write racing a new-field-only write must not erase identity."""
        import jiuwenswarm.server.runtime.session.session_metadata as sm

        fpath = TestIdentityPreservation._seed(sessions_dir)
        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                i += 1
                meta = sm._read_metadata("sess_identity", cache_bust=True)
                if meta.get("session_id"):
                    meta["message_count"] = i
                    sm._write_metadata_sync("sess_identity", meta)

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        for i in range(200):
            sm._write_metadata_sync("sess_identity", {"workflow_runs": {"n": i}})
        stop.set()
        t.join(timeout=3)

        data = _read_json(fpath)
        for key in ("session_id", "title", "created_at", "team_name"):
            assert key in data, f"identity field {key} was lost after concurrent writes"


def test_remove_team_mode_session_dirs_at_startup_keeps_stable_team_sessions(
    sessions_dir,
):
    from jiuwenswarm.server.runtime.session.session_metadata import (
        _write_metadata_sync,
        remove_team_mode_session_dirs_at_startup,
    )

    _write_metadata_sync("stable_team", {"session_id": "stable_team", "mode": "team"})
    _write_metadata_sync(
        "stable_plan",
        {"session_id": "stable_plan", "mode": "team.plan"},
    )
    _write_metadata_sync(
        "stable_code",
        {"session_id": "stable_code", "mode": "code.team"},
    )
    _write_metadata_sync(
        "temp_team",
        {
            "session_id": "temp_team",
            "mode": "team.plan",
            "temporary_team_session": True,
        },
    )
    _write_metadata_sync(
        "agent_temp",
        {
            "session_id": "agent_temp",
            "mode": "agent",
            "temporary_team_session": True,
        },
    )

    remove_team_mode_session_dirs_at_startup()

    assert (sessions_dir / "stable_team").exists()
    assert (sessions_dir / "stable_plan").exists()
    assert (sessions_dir / "stable_code").exists()
    assert not (sessions_dir / "temp_team").exists()
    assert (sessions_dir / "agent_temp").exists()


# ===========================================================================
# rebind_session_project：并发场景（陈旧异步快照 vs 重绑）
# ===========================================================================
class TestRebindSessionProjectConcurrency:
    """覆盖 P1 关键场景：rebind 的 sync 写入与队列中陈旧异步快照的竞态。

    rebind_session_project 用 sync_write=True 立即落盘，但此前已入队、尚未被
    worker 处理的陈旧快照（如刚发送的消息/改名）仍会在 worker 写回时覆盖
    rebind 的 project_id/project_dir/work_mode/channel_metadata。版本检查机制
    （_bump_rebind_gen + worker preserve_rebound_fields）应阻止这种覆盖。
    """

    @staticmethod
    def _init_session_with_project(sessions_dir, sid, project_id, project_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            init_session_metadata,
            update_session_metadata,
        )

        init_session_metadata(
            session_id=sid,
            channel_id="tui",
            mode="code.normal",
            project_dir=project_dir,
            project_id=project_id,
            work_mode="code",
        )
        update_session_metadata(
            session_id=sid,
            project_dir=project_dir,
            project_id=project_id,
            work_mode="code",
            touch_last_message_at=False,
            sync_write=True,
        )
        _drain_queue()

    @staticmethod
    def test_rebind_overwrites_locked_project_fields(sessions_dir):
        """rebind 强制覆盖已锁定的 project 字段（基础语义）。"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            rebind_session_project,
        )

        sid = "rebind_basic"
        TestRebindSessionProjectConcurrency._init_session_with_project(
            sessions_dir, sid, "proj_old", "/old/dir"
        )
        before = get_session_metadata(sid, cache_bust=True)
        assert before["project_id"] == "proj_old"
        assert before["project_dir"] == "/old/dir"

        updated = rebind_session_project(
            session_id=sid,
            project_id="proj_new",
            project_dir="/new/dir",
            work_mode="code",
        )
        assert updated is not None
        _drain_queue()
        after = get_session_metadata(sid, cache_bust=True)
        assert after["project_id"] == "proj_new"
        assert after["project_dir"] == "/new/dir"
        assert after["work_mode"] == "code"

    @staticmethod
    def test_rebind_syncs_channel_metadata(sessions_dir):
        """rebind 同步 channel_metadata.project_dir/cwd（/resume current-dir 过滤读这里）。"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            rebind_session_project,
            update_session_metadata,
        )

        sid = "rebind_chmeta"
        TestRebindSessionProjectConcurrency._init_session_with_project(
            sessions_dir, sid, "proj_old", "/old/dir"
        )
        # 模拟历史 chat 落盘的 channel_metadata（resolve_tui_session_project_path 读这个顶层字段）
        update_session_metadata(
            session_id=sid,
            channel_metadata={"project_dir": "/old/dir", "cwd": "/old/dir"},
            touch_last_message_at=False,
            sync_write=True,
        )
        _drain_queue()
        before = get_session_metadata(sid, cache_bust=True)
        assert before.get("channel_metadata", {}).get("project_dir") == "/old/dir"

        rebind_session_project(
            session_id=sid,
            project_id="proj_new",
            project_dir="/new/dir",
            work_mode="code",
        )
        _drain_queue()
        after = get_session_metadata(sid, cache_bust=True)
        ch = after.get("channel_metadata", {})
        assert ch.get("project_dir") == "/new/dir"
        assert ch.get("cwd") == "/new/dir"

    @staticmethod
    def test_stale_queued_snapshot_does_not_overwrite_rebind(sessions_dir):
        """关键回归：入队早于 rebind 的陈旧异步快照, worker 写回时不得覆盖 rebind。

        直接模拟 worker 处理"gen 不匹配"的陈旧快照（worker 的核心行为是调用
        ``_write_metadata_sync(_MetadataWriteOptions(preserve_rebound_fields=True))``，
        确保磁盘 project 字段保留 rebind 的新值, 而非 project 字段（如 message_count）
        仍按陈旧快照更新。确定性复现：worker 线程为模块级单例, 时序不可控,
        故此处直接调用 _write_metadata_sync 模拟 worker 的"重绑后处理陈旧快照"。
        """
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _MetadataWriteOptions,
            _write_metadata_sync,
            get_session_metadata,
            rebind_session_project,
        )

        sid = "rebind_race"
        TestRebindSessionProjectConcurrency._init_session_with_project(
            sessions_dir, sid, "proj_old", "/old/dir"
        )
        # 1. 构造陈旧快照：旧 project 字段 + 改了 message_count（模拟 chat 落盘）
        stale_snapshot = get_session_metadata(sid, cache_bust=True)
        stale_snapshot["message_count"] = stale_snapshot.get("message_count", 0) + 1
        stale_snapshot["project_id"] = "proj_old"
        stale_snapshot["project_dir"] = "/old/dir"
        stale_snapshot["work_mode"] = "code"
        stale_snapshot["channel_metadata"] = {
            "project_dir": "/old/dir",
            "cwd": "/old/dir",
        }
        # 2. rebind：bump gen + sync 落盘新值
        rebind_session_project(
            session_id=sid,
            project_id="proj_new",
            project_dir="/new/dir",
            work_mode="code",
        )
        # 3. 模拟 worker 处理陈旧快照（gen_at_enqueue < 当前 gen → preserve_rebound=True）
        #    若无版本检查（preserve_rebound_fields=False）, 此调用会覆盖 rebind。
        _write_metadata_sync(
            sid,
            stale_snapshot,
            _MetadataWriteOptions(preserve_rebound_fields=True),
        )
        # 4. 断言磁盘 project 字段仍为 rebind 的新值
        after = get_session_metadata(sid, cache_bust=True)
        assert after["project_id"] == "proj_new", (
            "陈旧异步快照覆盖了 rebind 的 project_id"
        )
        assert after["project_dir"] == "/new/dir", (
            "陈旧异步快照覆盖了 rebind 的 project_dir"
        )
        assert after["work_mode"] == "code"
        ch = after.get("channel_metadata", {})
        assert ch.get("project_dir") == "/new/dir", (
            "陈旧异步快照覆盖了 rebind 的 channel_metadata.project_dir"
        )
        assert ch.get("cwd") == "/new/dir"
        # 陈旧快照的非 project 字段（message_count）应被保留
        assert after["message_count"] == 1

    @staticmethod
    def test_stale_snapshot_without_preserve_overwrites_rebind(sessions_dir):
        """对照实验：同一陈旧快照, 若 worker 不做版本检查（preserve_rebound_fields=False）
        会覆盖 rebind —— 反向证明上一用例的断言确由版本检查保护, 而非巧合。
        """
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _MetadataWriteOptions,
            _write_metadata_sync,
            get_session_metadata,
            rebind_session_project,
        )

        sid = "rebind_no_preserve"
        TestRebindSessionProjectConcurrency._init_session_with_project(
            sessions_dir, sid, "proj_old", "/old/dir"
        )
        rebind_session_project(
            session_id=sid,
            project_id="proj_new",
            project_dir="/new/dir",
            work_mode="code",
        )
        stale_snapshot = {
            "session_id": sid,
            "project_id": "proj_old",
            "project_dir": "/old/dir",
            "work_mode": "code",
            "channel_metadata": {"project_dir": "/old/dir", "cwd": "/old/dir"},
            "message_count": 9,
        }
        # 不做版本检查 → 陈旧快照直接覆盖 rebind
        _write_metadata_sync(
            sid,
            stale_snapshot,
            _MetadataWriteOptions(preserve_rebound_fields=False),
        )
        after = get_session_metadata(sid, cache_bust=True)
        assert after["project_id"] == "proj_old"
        assert after["project_dir"] == "/old/dir"

    @staticmethod
    def test_enqueue_captures_rebind_gen_for_staleness_detection(sessions_dir, monkeypatch):
        """_enqueue_write 入队时捕获当前 rebind_gen; rebind 后该值 < 当前 gen,
        worker 据此判定为陈旧。用 monkeypatch 拦截 put_nowait 避免与 worker 竞态。"""
        from jiuwenswarm.server.runtime.session import session_metadata as sm
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _bump_rebind_gen,
            _get_rebind_gen,
            init_session_metadata,
            update_session_metadata,
        )

        sid = "rebind_gen_capture"
        init_session_metadata(
            session_id=sid, channel_id="tui", mode="code.normal",
            project_dir="/old/dir", project_id="proj_old", work_mode="code",
        )
        update_session_metadata(
            session_id=sid, project_dir="/old/dir", project_id="proj_old",
            work_mode="code", touch_last_message_at=False, sync_write=True,
        )
        _drain_queue()
        assert _get_rebind_gen(sid) == 0

        # 拦截 put_nowait: 不真正入队（worker 不会消费）, 仅记录入队项
        captured: list[tuple] = []
        original_put = sm._METADATA_QUEUE.put_nowait

        def capture_put(item):
            captured.append(item)

        monkeypatch.setattr(sm._METADATA_QUEUE, "put_nowait", capture_put)
        # 入队一个异步写（rebind 之前）: 应捕获 gen=0
        update_session_metadata(
            session_id=sid, touch_last_message_at=False,
        )
        monkeypatch.setattr(sm._METADATA_QUEUE, "put_nowait", original_put)
        assert len(captured) == 1
        item_sid, _meta, options = captured[0]
        assert item_sid == sid
        assert options.rebind_gen_at_enqueue == 0
        assert options.merge_fields is None

        # rebind: bump gen
        _bump_rebind_gen(sid)
        assert _get_rebind_gen(sid) == 1
        # worker 处理时: gen_at_enqueue(0) < 当前 gen(1) → 判定为陈旧
        assert options.rebind_gen_at_enqueue < _get_rebind_gen(sid)

    @staticmethod
    def test_rebind_gen_helpers_isolated():
        """_get/_bump_rebind_gen 基础语义: 初始 0, 自增, 会话隔离。"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _REBIND_GEN_LOCK,
            _SESSION_REBIND_GEN,
            _bump_rebind_gen,
            _get_rebind_gen,
        )

        sid_a, sid_b = "gen_iso_a", "gen_iso_b"
        try:
            assert _get_rebind_gen(sid_a) == 0
            assert _get_rebind_gen(sid_b) == 0
            _bump_rebind_gen(sid_a)
            assert _get_rebind_gen(sid_a) == 1
            assert _get_rebind_gen(sid_b) == 0  # 隔离
            _bump_rebind_gen(sid_a)
            _bump_rebind_gen(sid_b)
            assert _get_rebind_gen(sid_a) == 2
            assert _get_rebind_gen(sid_b) == 1
        finally:
            with _REBIND_GEN_LOCK:
                _SESSION_REBIND_GEN.pop(sid_a, None)
                _SESSION_REBIND_GEN.pop(sid_b, None)

    @staticmethod
    def test_post_rebind_queued_snapshot_uses_new_values(sessions_dir):
        """rebind 之后入队的快照读到了新 project 值, 正常写回不触发版本合并。"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            rebind_session_project,
            update_session_metadata,
        )

        sid = "rebind_then_chat"
        TestRebindSessionProjectConcurrency._init_session_with_project(
            sessions_dir, sid, "proj_old", "/old/dir"
        )
        rebind_session_project(
            session_id=sid,
            project_id="proj_new",
            project_dir="/new/dir",
            work_mode="code",
        )
        _drain_queue()
        # rebind 之后的"chat"走 update_session_metadata: 读到新值（first-lock 不覆盖）
        update_session_metadata(
            session_id=sid,
            project_dir="/new/dir",
            project_id="proj_new",
            work_mode="code",
            touch_last_message_at=False,
        )
        _drain_queue()
        after = get_session_metadata(sid, cache_bust=True)
        assert after["project_id"] == "proj_new"
        assert after["project_dir"] == "/new/dir"

    @staticmethod
    def test_sync_write_rebind_during_write_window_preserves_rebind(sessions_dir, monkeypatch):
        """P2/P3 回归: sync_write 路径在"gen 捕获后、写盘前"窗口内发生 rebind 时不覆盖。

        set_session_pinned 等 sync_write=True 调用方经 _enqueue_write 走 sync_write
        分支。P2 之前该分支不做 rebind 版本检查; P3 之前 worker 的 gen 比较在
        _FILE_LOCK 外执行。二者现已统一: _enqueue_write 捕获 gen_at_enqueue 后,
        _write_metadata_sync 持锁重比 gen 发现陈旧即从磁盘保留 rebind 字段。

        确定性复现: 用 monkeypatch 拦截 _write_metadata_sync, 在真正落盘前注入一次
        rebind (bump gen + 落盘新 project), 模拟"gen 捕获(0)→写盘窗口内 rebind(gen→1)
        →陈旧快照写回"的竞态。修复后陈旧快照的 project 字段被磁盘当前值保留,
        非 project 字段(如 message_count)仍按快照更新。
        """
        from jiuwenswarm.server.runtime.session import session_metadata as sm
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _bump_rebind_gen,
            _enqueue_write,
            _get_rebind_gen,
            get_session_metadata,
        )

        sid = "p2_sync_race"
        TestRebindSessionProjectConcurrency._init_session_with_project(
            sessions_dir, sid, "proj_old", "/old/dir"
        )
        # 陈旧快照: 旧 project 字段 + message_count+1 (模拟 set_session_pinned 的
        # read-before-rebind: 读盘时 project 仍是 proj_old, 改了 pinned/message_count)
        stale = get_session_metadata(sid, cache_bust=True)
        # get_session_metadata 的 mode 惰性迁移会触发一次异步写回 (message_count=0)。
        # 若不排空, 该异步写可能在后续 sync_write 之后才落盘, 用 message_count=0
        # 覆盖 sync_write 写入的 message_count=1, 导致断言 flaky。
        _drain_queue()
        stale["message_count"] = stale.get("message_count", 0) + 1
        stale["project_id"] = "proj_old"
        stale["project_dir"] = "/old/dir"
        stale["work_mode"] = "code"
        stale["channel_metadata"] = {"project_dir": "/old/dir", "cwd": "/old/dir"}
        assert _get_rebind_gen(sid) == 0

        original_write = sm._write_metadata_sync
        rebind_injected: list[bool] = []

        def _inject_rebound_during_write(session_id, metadata, options=None):
            # 仅对本次 sync_write 的陈旧快照注入一次 rebind (gen 0→1, 落盘 proj_new)。
            # 用 original_write 直接写, 不走 _enqueue_write, 避免递归回到本拦截。
            if session_id == sid and not rebind_injected:
                rebind_injected.append(True)
                _bump_rebind_gen(session_id)
                rebound = dict(metadata)
                rebound["project_id"] = "proj_new"
                rebound["project_dir"] = "/new/dir"
                rebound["work_mode"] = "code"
                rebound["channel_metadata"] = {
                    "project_dir": "/new/dir", "cwd": "/new/dir",
                }
                original_write(session_id, rebound)
            # 继续处理本来的陈旧快照: rebind_gen_at_enqueue 应为 0 (早于注入的 rebind),
            # _write_metadata_sync 持锁后重比 0 < 1 → 从磁盘保留 rebind 字段。
            return original_write(session_id, metadata, options)

        monkeypatch.setattr(sm, "_write_metadata_sync", _inject_rebound_during_write)
        _enqueue_write(sid, stale, sync_write=True, preserve_pin_fields=False)
        # 注意: 不能在此调用 monkeypatch.undo() —— pytest 中 sessions_dir fixture 与本
        # 测试共享同一 function-scoped monkeypatch 实例, undo() 会连 fixture 的
        # get_agent_sessions_dir patch 一并撤销, 使后续 cache_bust=True 读盘落到真实
        # ~/.jiuwenswarm 目录(无此会话), 得到空 dict 导致断言 KeyError。由 pytest 在
        # 测试结束时统一恢复全部 patch。

        assert _get_rebind_gen(sid) == 1
        after = get_session_metadata(sid, cache_bust=True)
        # rebind 的 proj_new 必须保留, 陈旧快照不得覆盖
        assert after["project_id"] == "proj_new", (
            "sync_write 陈旧快照在写盘窗口内覆盖了 rebind 的 project_id"
        )
        assert after["project_dir"] == "/new/dir"
        ch = after.get("channel_metadata", {})
        assert ch.get("project_dir") == "/new/dir"
        assert ch.get("cwd") == "/new/dir"
        # 陈旧快照的非 project 字段 (message_count) 应被保留
        assert after["message_count"] == 1
