# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""存储模块单元测试。

验证 SQLiteStore 的 record_event/get_events/create_alert/get_alerts/
record_raw_event/get_events_by_trace_id,ModuleStorageManager 目录结构,
MemoryStore 基本功能。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_ssas.core.framework.storage.memory_store import MemoryStore
from agent_ssas.core.framework.storage.module_store import ModuleStorageManager
from agent_ssas.core.framework.storage.sqlite_store import SQLiteStore


def _make_event(
    event_id: str = "test1",
    event_type: str = "invoke_start",
    event_class: str = "lifecycle",
    interaction_seq: int = 0,
    session_id: str = "s1",
    agent_id: str = "a1",
    trace_id: str = "t1",
    timestamp: float = 0.0,
    **extra,
) -> dict:
    """构造事件 dict,便于测试复用。"""
    event = {
        "event_id": event_id,
        "event_type": event_type,
        "event_class": event_class,
        "interaction_seq": interaction_seq,
        "session_id": session_id,
        "agent_id": agent_id,
        "trace_id": trace_id,
        "timestamp": timestamp,
    }
    event.update(extra)
    return event


class TestSQLiteStore:
    """SQLiteStore 读写。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_record_and_get_events(sqlite_store: SQLiteStore) -> None:
        """验证 record_event + get_events 基本读写。"""
        await sqlite_store.record_event(_make_event())
        events = await sqlite_store.get_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_id"] == "test1"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_filter_by_session_id(sqlite_store: SQLiteStore) -> None:
        """验证按 session_id 过滤事件。"""
        await sqlite_store.record_event(_make_event(session_id="s1"))
        await sqlite_store.record_event(
            _make_event(
                event_id="test2",
                event_type="invoke_end",
                session_id="s2",
                trace_id="t2",
            )
        )
        events = await sqlite_store.get_events(session_id="s1")
        assert len(events) == 1
        assert events[0]["session_id"] == "s1"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_record_event_auto_id(sqlite_store: SQLiteStore) -> None:
        """验证 event 缺少 event_id 时自动生成 UUID。"""
        event = _make_event()
        del event["event_id"]
        returned_id = await sqlite_store.record_event(event)
        assert returned_id  # 非空字符串
        events = await sqlite_store.get_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_id"] == returned_id

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_record_event_invalid_type(sqlite_store: SQLiteStore) -> None:
        """验证传入非 dict 时抛出 TypeError。"""
        with pytest.raises(TypeError):
            await sqlite_store.record_event("not a dict")  # type: ignore[arg-type]

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_record_event_empty_dict(sqlite_store: SQLiteStore) -> None:
        """验证传入空 dict 时抛出 ValueError。"""
        with pytest.raises(ValueError):
            await sqlite_store.record_event({})

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_create_and_get_alerts(sqlite_store: SQLiteStore) -> None:
        """验证 create_alert + get_alerts 基本读写。"""
        await sqlite_store.create_alert(
            {
                "alert_id": "alert-1",
                "interaction_seq": 0,
                "session_id": "s1",
                "trace_id": "t1",
                "risk_level": "high",
                "risk_type": "tool_misuse",
                "timestamp": 1715000000.0,
            }
        )
        alerts = await sqlite_store.get_alerts()
        assert len(alerts) == 1
        assert alerts[0]["alert_id"] == "alert-1"
        assert alerts[0]["risk_level"] == "high"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_get_alerts_filter_acknowledged(
        sqlite_store: SQLiteStore,
    ) -> None:
        """验证 get_alerts 按 acknowledged 过滤。"""
        await sqlite_store.create_alert(
            {"alert_id": "a1", "risk_level": "low", "acknowledged": False}
        )
        await sqlite_store.create_alert(
            {"alert_id": "a2", "risk_level": "high", "acknowledged": True}
        )
        unack = await sqlite_store.get_alerts(acknowledged=False)
        assert len(unack) == 1
        assert unack[0]["alert_id"] == "a1"
        ack = await sqlite_store.get_alerts(acknowledged=True)
        assert len(ack) == 1
        assert ack[0]["alert_id"] == "a2"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_record_raw_event(sqlite_store: SQLiteStore) -> None:
        """验证 record_raw_event 持久化原始事件。"""
        raw_event = {
            "common": {
                "source": "AgentSSASSecurityRail",
                "event_type": "tool_input",
                "event_class": "lifecycle",
                "interaction_seq": 0,
                "session_id": "s1",
                "agent_id": "a1",
                "trace_id": "t1",
                "timestamp": 1715000000.0,
            },
            "payload": {"tool_name": "bash"},
            "metadata": {},
        }
        raw_id = await sqlite_store.record_raw_event(raw_event)
        assert raw_id  # 自动生成的 raw_event_id

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_get_events_by_trace_id(sqlite_store: SQLiteStore) -> None:
        """验证按 trace_id 关联查询统一事件和原始事件。"""
        # 写入统一事件
        await sqlite_store.record_event(
            _make_event(event_id="e1", trace_id="trace-1")
        )
        # 写入原始事件
        await sqlite_store.record_raw_event(
            {
                "common": {
                    "event_type": "tool_input",
                    "event_class": "lifecycle",
                    "interaction_seq": 0,
                    "session_id": "s1",
                    "agent_id": "a1",
                    "trace_id": "trace-1",
                    "timestamp": 0.0,
                },
                "payload": {},
                "metadata": {},
            }
        )
        results = await sqlite_store.get_events_by_trace_id("trace-1")
        assert len(results) == 2
        sources = {r.get("_source") for r in results}
        assert sources == {"event", "raw_event"}

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_get_events_by_trace_id_empty_trace(
        sqlite_store: SQLiteStore,
    ) -> None:
        """验证空 trace_id 抛出 ValueError。"""
        with pytest.raises(ValueError):
            await sqlite_store.get_events_by_trace_id("")

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_get_events_invalid_limit(sqlite_store: SQLiteStore) -> None:
        """验证非正整数 limit 抛出 ValueError。"""
        with pytest.raises(ValueError):
            await sqlite_store.get_events(limit=0)
        with pytest.raises(ValueError):
            await sqlite_store.get_events(limit=-1)


class TestTimestampTextColumn:
    """timestamp_text 可读时间列(本地时区 YYYY-MM-DD HH:MM:SS.mmm)。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_event_timestamp_text_format(sqlite_store: SQLiteStore) -> None:
        """验证 events 表 timestamp_text 写入为可读格式。"""
        import re
        import time

        now = time.time()
        await sqlite_store.record_event(_make_event(event_id="t1", timestamp=now))
        row = sqlite_store._conn.execute(
            "SELECT timestamp_text FROM events WHERE event_id = 't1'"
        ).fetchone()
        assert row is not None
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", row[0]
        ), f"timestamp_text 格式不符: {row[0]!r}"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_raw_event_and_alert_timestamp_text(
        sqlite_store: SQLiteStore,
    ) -> None:
        """验证 raw_events 与 alerts 表 timestamp_text 写入。"""
        import re

        await sqlite_store.record_raw_event(
            {
                "common": {
                    "event_type": "tool_input",
                    "event_class": "lifecycle",
                    "timestamp": 1715000000.123,
                    "session_id": "s1",
                    "trace_id": "t1",
                }
            }
        )
        await sqlite_store.create_alert(
            {"alert_id": "a1", "timestamp": 1715000000.456, "risk_level": "high"}
        )
        raw_row = sqlite_store._conn.execute(
            "SELECT timestamp_text FROM raw_events LIMIT 1"
        ).fetchone()
        alert_row = sqlite_store._conn.execute(
            "SELECT timestamp_text FROM alerts WHERE alert_id = 'a1'"
        ).fetchone()
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", raw_row[0]
        )
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", alert_row[0]
        )

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_legacy_db_migration_backfills(ssas_home: Path) -> None:
        """验证旧库(无 timestamp_text 列)自动迁移并回填历史数据。"""
        import sqlite3

        db_path = ssas_home / "ssas" / "legacy_test.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 手工构造旧表结构(无 timestamp_text 列)
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                event_type TEXT,
                event_class TEXT,
                interaction_seq INTEGER,
                session_id TEXT,
                agent_id TEXT,
                trace_id TEXT,
                timestamp REAL,
                data TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO events (event_id, timestamp, data) VALUES (?, ?, ?)",
            ("old1", 1715000000.0, "{}"),
        )
        conn.execute(
            "INSERT INTO events (event_id, timestamp, data) VALUES (?, ?, ?)",
            ("old_invalid", 0.0, "{}"),
        )
        conn.commit()
        conn.close()

        # 打开 SQLiteStore 触发迁移
        store = SQLiteStore(db_path)
        columns = {
            row[1]
            for row in store._conn.execute("PRAGMA table_info(events)").fetchall()
        }
        assert "timestamp_text" in columns
        # 有效 timestamp 回填,无效(<=0)保持 NULL
        row1 = store._conn.execute(
            "SELECT timestamp_text FROM events WHERE event_id = 'old1'"
        ).fetchone()
        row2 = store._conn.execute(
            "SELECT timestamp_text FROM events WHERE event_id = 'old_invalid'"
        ).fetchone()
        assert row1[0] is not None and len(row1[0]) > 0
        assert row2[0] is None
        store._conn.close()


class TestCleanupExpired:
    """TTL 过期数据清理(cleanup_expired)。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_cleanup_events_by_ttl(sqlite_store: SQLiteStore) -> None:
        """验证 events 表按 TTL 清理过期数据,未过期数据保留。"""
        import time

        now = time.time()
        # 一条 31 天前(过期,event_ttl=30),一条 1 天前(未过期)
        await sqlite_store.record_event(
            _make_event(event_id="old", timestamp=now - 31 * 86400)
        )
        await sqlite_store.record_event(
            _make_event(event_id="new", timestamp=now - 1 * 86400)
        )
        deleted = await sqlite_store.cleanup_expired("events", 30, now=now)
        assert deleted == 1
        remaining = await sqlite_store.get_events(limit=10)
        assert len(remaining) == 1
        assert remaining[0]["event_id"] == "new"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_cleanup_alerts_by_ttl(sqlite_store: SQLiteStore) -> None:
        """验证 alerts 表按 90 天 TTL 清理。"""
        import time

        now = time.time()
        await sqlite_store.create_alert(
            {"alert_id": "old_alert", "timestamp": now - 91 * 86400}
        )
        await sqlite_store.create_alert(
            {"alert_id": "new_alert", "timestamp": now - 89 * 86400}
        )
        deleted = await sqlite_store.cleanup_expired("alerts", 90, now=now)
        assert deleted == 1
        alerts = await sqlite_store.get_alerts()
        assert len(alerts) == 1
        assert alerts[0]["alert_id"] == "new_alert"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_cleanup_ttl_zero_disabled(sqlite_store: SQLiteStore) -> None:
        """验证 ttl_days <= 0 时禁用清理(不删除任何数据)。"""
        import time

        now = time.time()
        await sqlite_store.record_event(
            _make_event(event_id="old", timestamp=now - 365 * 86400)
        )
        deleted = await sqlite_store.cleanup_expired("events", 0, now=now)
        assert deleted == 0
        events = await sqlite_store.get_events()
        assert len(events) == 1

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_cleanup_invalid_table_raises(sqlite_store: SQLiteStore) -> None:
        """验证非法表名抛出 ValueError(白名单防注入)。"""
        with pytest.raises(ValueError):
            await sqlite_store.cleanup_expired("sessions; DROP TABLE events", 30)

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_module_storage_cleanup(
        ssas_home: Path,
    ) -> None:
        """验证 ModuleStorageManager.cleanup 按 TTL 清理过程/结果库。"""
        import time

        now = time.time()
        manager = ModuleStorageManager("ttl_test_module", ssas_home)
        # process.db 写入 31 天前数据(超过 event_ttl=30 应清理)
        await manager.process_store.record_event(
            {"event_id": "p_old", "timestamp": now - 31 * 86400}
        )
        # result.db 写入 91 天前数据(超过 alert_ttl=90 应清理)
        await manager.result_store.record_event(
            {"event_id": "r_old", "timestamp": now - 91 * 86400}
        )
        # result.db 写入 1 天前数据(应保留)
        await manager.result_store.record_event(
            {"event_id": "r_new", "timestamp": now - 86400}
        )

        # cleanup 内部使用 time.time(),预置数据足够旧,无需 mock
        await manager.cleanup(30, 90)

        process_events = await manager.process_store.get_events(limit=10)
        result_events = await manager.result_store.get_events(limit=10)
        assert process_events == []
        assert len(result_events) == 1
        assert result_events[0]["event_id"] == "r_new"
        await manager.close()


class TestModuleStorageManager:
    """ModuleStorageManager 创建和目录结构。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_creates_directory_structure(ssas_home: Path) -> None:
        """验证创建模块目录结构和 process.db/result.db/config 目录。"""
        manager = ModuleStorageManager("test_module", ssas_home)
        module_dir = ssas_home / "ssas" / "modules" / "test_module"
        assert module_dir.exists()
        assert (module_dir / "process.db").exists()
        assert (module_dir / "result.db").exists()
        assert (module_dir / "config").is_dir()
        assert manager.process_store is not None
        assert manager.result_store is not None
        assert manager.config_dir == module_dir / "config"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_empty_module_name_raises(ssas_home: Path) -> None:
        """验证空 module_name 抛出 ValueError。"""
        with pytest.raises(ValueError):
            ModuleStorageManager("", ssas_home)

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_process_and_result_store_independent(
        ssas_home: Path,
    ) -> None:
        """验证过程库和结果库相互独立。"""
        manager = ModuleStorageManager("test_module", ssas_home)
        await manager.process_store.record_event(
            {"event_id": "p1", "event_type": "tool_input"}
        )
        await manager.result_store.record_event(
            {"event_id": "r1", "event_type": "alert"}
        )
        process_events = await manager.process_store.get_events(limit=10)
        result_events = await manager.result_store.get_events(limit=10)
        assert len(process_events) == 1
        assert process_events[0]["event_id"] == "p1"
        assert len(result_events) == 1
        assert result_events[0]["event_id"] == "r1"
        await manager.close()


class TestMemoryStore:
    """MemoryStore 基本功能。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_record_and_get_events(memory_store: MemoryStore) -> None:
        """验证 record_event + get_events 基本读写。"""
        await memory_store.record_event(_make_event())
        events = await memory_store.get_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_id"] == "test1"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_filter_by_session_id(memory_store: MemoryStore) -> None:
        """验证按 session_id 过滤。"""
        await memory_store.record_event(_make_event(session_id="s1"))
        await memory_store.record_event(
            _make_event(event_id="e2", session_id="s2")
        )
        events = await memory_store.get_events(session_id="s1")
        assert len(events) == 1
        assert events[0]["session_id"] == "s1"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_create_and_get_alerts(memory_store: MemoryStore) -> None:
        """验证 create_alert + get_alerts 基本读写。"""
        await memory_store.create_alert(
            {"alert_id": "a1", "risk_level": "high"}
        )
        alerts = await memory_store.get_alerts()
        assert len(alerts) == 1
        assert alerts[0]["alert_id"] == "a1"

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_record_raw_event_and_get_by_trace(
        memory_store: MemoryStore,
    ) -> None:
        """验证 record_raw_event + get_events_by_trace_id。"""
        await memory_store.record_event(
            _make_event(event_id="e1", trace_id="trace-1")
        )
        await memory_store.record_raw_event(
            {
                "common": {
                    "event_type": "tool_input",
                    "interaction_seq": 0,
                    "session_id": "s1",
                    "trace_id": "trace-1",
                    "timestamp": 0.0,
                },
                "payload": {},
                "metadata": {},
            }
        )
        results = await memory_store.get_events_by_trace_id("trace-1")
        assert len(results) == 2
