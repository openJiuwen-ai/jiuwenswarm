"""session_metadata 模块单元测试"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixture: mock get_agent_sessions_dir 指向 tmp_path
# ---------------------------------------------------------------------------
@pytest.fixture()
def sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.session_metadata.get_agent_sessions_dir",
        lambda: d,
    )
    return d



def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ===========================================================================
# _auto_title
# ===========================================================================
class TestAutoTitle:
    @staticmethod
    def test_normal():
        from jiuwenclaw.agentserver.session_metadata import _auto_title

        assert _auto_title("hello world") == "hello world"

    @staticmethod
    def test_truncate():
        from jiuwenclaw.agentserver.session_metadata import _auto_title, _TITLE_MAX_LEN

        long_text = "a" * 100
        result = _auto_title(long_text)
        assert len(result) == _TITLE_MAX_LEN + 3  # +3 for "..."
        assert result.endswith("...")

    @staticmethod
    def test_strip_and_newline():
        from jiuwenclaw.agentserver.session_metadata import _auto_title

        assert _auto_title("  line1\nline2  ") == "line1 line2"

    @staticmethod
    def test_empty():
        from jiuwenclaw.agentserver.session_metadata import _auto_title

        assert _auto_title("") == ""
        assert _auto_title("   ") == ""


# ===========================================================================
# init_session_metadata
# ===========================================================================
class TestInitSessionMetadata:
    @staticmethod
    def test_creates_metadata_file(sessions_dir):
        from jiuwenclaw.agentserver.session_metadata import init_session_metadata

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
        assert isinstance(data["created_at"], float)
        assert isinstance(data["last_message_at"], float)

    @staticmethod
    def test_default_empty_fields(sessions_dir):
        from jiuwenclaw.agentserver.session_metadata import init_session_metadata

        init_session_metadata(session_id="sess_002")
        data = _read_json(sessions_dir / "sess_002" / "metadata.json")
        assert data["channel_id"] == ""
        assert data["user_id"] == ""
        assert data["title"] == ""
        assert data["mode"] == "unknown"


# ===========================================================================
# update_session_metadata
# ===========================================================================
class TestUpdateSessionMetadata:
    @staticmethod
    def test_update_existing(sessions_dir):
        from jiuwenclaw.agentserver.session_metadata import (
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
        assert data["channel_id"] == "feishu"
        assert data["message_count"] == 1

    @staticmethod
    def test_fallback_create_when_no_metadata(sessions_dir):
        """外部渠道隐式创建 session 时,metadata 不存在,应自动创建"""
        from jiuwenclaw.agentserver.session_metadata import (
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
        from jiuwenclaw.agentserver.session_metadata import (
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
        from jiuwenclaw.agentserver.session_metadata import (
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
        from jiuwenclaw.agentserver.session_metadata import (
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


# ===========================================================================
# get_session_metadata
# ===========================================================================
class TestGetSessionMetadata:
    @staticmethod
    def test_returns_data(sessions_dir):
        from jiuwenclaw.agentserver.session_metadata import (
            init_session_metadata,
            get_session_metadata,
        )

        init_session_metadata(session_id="sess_g1", channel_id="web")
        data = get_session_metadata("sess_g1")
        assert data["channel_id"] == "web"

    @staticmethod
    def test_returns_empty_when_missing(sessions_dir):
        from jiuwenclaw.agentserver.session_metadata import get_session_metadata

        data = get_session_metadata("nonexistent")
        assert data == {}


# ===========================================================================
# get_all_sessions_metadata
# ===========================================================================
class TestGetAllSessionsMetadata:
    @staticmethod
    def test_basic_list(sessions_dir):
        from jiuwenclaw.agentserver.session_metadata import (
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
        from jiuwenclaw.agentserver.session_metadata import (
            _write_metadata_sync,
            get_all_sessions_metadata,
        )

        now = time.time()
        _write_metadata_sync("old", {
            "session_id": "old", "last_message_at": now - 100,
            "channel_id": "", "user_id": "", "created_at": now - 100,
            "title": "", "message_count": 0,
        })
        _write_metadata_sync("new", {
            "session_id": "new", "last_message_at": now,
            "channel_id": "", "user_id": "", "created_at": now,
            "title": "", "message_count": 0,
        })

        sessions, _ = get_all_sessions_metadata()
        assert sessions[0]["session_id"] == "new"
        assert sessions[1]["session_id"] == "old"

    @staticmethod
    def test_pagination_limit(sessions_dir):
        from jiuwenclaw.agentserver.session_metadata import (
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
        from jiuwenclaw.agentserver.session_metadata import (
            _write_metadata_sync,
            get_all_sessions_metadata,
        )

        now = time.time()
        for i in range(5):
            _write_metadata_sync(f"o{i}", {
                "session_id": f"o{i}", "last_message_at": now - i,
                "channel_id": "", "user_id": "", "created_at": now - i,
                "title": "", "message_count": 0,
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
        from jiuwenclaw.agentserver.session_metadata import get_all_sessions_metadata

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
        from jiuwenclaw.agentserver.session_metadata import get_all_sessions_metadata

        sessions, total = get_all_sessions_metadata()
        assert total == 0
        assert sessions == []


# ===========================================================================
# _read_metadata 容错
# ===========================================================================
class TestReadMetadataRobustness:
    @staticmethod
    def test_corrupted_json(sessions_dir):
        from jiuwenclaw.agentserver.session_metadata import get_session_metadata

        d = sessions_dir / "sess_bad"
        d.mkdir()
        (d / "metadata.json").write_text("not valid json", encoding="utf-8")

        data = get_session_metadata("sess_bad")
        assert data == {}

    @staticmethod
    def test_non_dict_json(sessions_dir):
        from jiuwenclaw.agentserver.session_metadata import get_session_metadata

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
        from jiuwenclaw.agentserver.session_metadata import (
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
        from jiuwenclaw.agentserver.session_metadata import (
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
        from jiuwenclaw.agentserver.session_metadata import (
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
        from jiuwenclaw.agentserver.session_metadata import (
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
        from jiuwenclaw.agentserver.session_metadata import (
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
        from jiuwenclaw.agentserver.session_metadata import (
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
        from jiuwenclaw.agentserver.session_metadata import (
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
        from jiuwenclaw.agentserver.session_metadata import (
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
