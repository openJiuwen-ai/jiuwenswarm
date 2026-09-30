# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS SQLite 持久化存储引擎。

SQLiteStore 是底层持久化引擎,每个实例对应一个 .db 文件,
拥有独立的数据库连接和异步锁。使用 WAL 模式以支持并发读。
data 字段以 JSON 字符串存储完整的事件/告警 dict,查询时反序列化。
时间以双列存储:timestamp 为 Unix 浮点秒(用于计算与排序),
timestamp_text 为本地时区可读格式(YYYY-MM-DD HH:MM:SS.mmm,便于直接查看)。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from agent_ssas.core.framework.utils.id_utils import new_uuid

# 允许执行 TTL 清理的表名白名单(防 SQL 注入)
_CLEANUP_TABLES: frozenset[str] = frozenset({"events", "alerts", "raw_events"})


def _format_local_timestamp(ts: float) -> str:
    """将 Unix 浮点秒格式化为本地时区可读时间字符串。

    Args:
        ts: Unix 时间戳(秒)。

    Returns:
        本地时区 "YYYY-MM-DD HH:MM:SS.mmm" 格式字符串。
        入参非法时返回空字符串。
    """
    try:
        # DTZ006: 刻意使用本地时区(便于运维直接查看),非 UTC
        dt = datetime.fromtimestamp(ts)  # noqa: DTZ006
    except (OverflowError, OSError, ValueError):
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{dt.microsecond // 1000:03d}"


class SQLiteStore:
    """SQLite 持久化存储,支持异步读写和告警订阅。

    每个 SQLiteStore 实例对应一个 .db 文件,拥有独立的连接和异步锁。
    使用 WAL 模式以支持并发读。所有公开方法均为异步,通过 asyncio.Lock
    串行化数据库操作,避免 sqlite3 在多协程并发访问时抛出异常。

    表结构设计:data 字段以 JSON 字符串存储完整的事件/告警 dict,
    查询时反序列化。这种设计使得表结构稳定,即使事件字段变化也不需要修改表结构。
    """

    def __init__(self, db_path: str | Path) -> None:
        """初始化 SQLiteStore。

        创建数据库文件(若不存在),初始化表结构和 WAL 模式。
        建立持久连接,供后续所有操作复用。

        Args:
            db_path: 数据库文件路径。传 ":memory:" 可创建内存数据库(测试用)。
        """
        self._db_path = str(db_path)
        self._lock = asyncio.Lock()
        # check_same_thread=False 允许在 asyncio.to_thread 的工作线程中使用连接,
        # 配合 _lock 串行化访问,避免 sqlite3 的线程检查异常
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表结构和 WAL 模式。

        创建 events、alerts、raw_events 三张表(若不存在),启用 WAL 模式。
        所有表均以 IF NOT EXISTS 创建,保证可重复执行。
        为 trace_id 建立索引,加速按 trace_id 关联查询。
        对旧库执行 timestamp_text 列迁移与历史数据回填。
        """
        # 内存数据库不支持 WAL,须跳过;文件数据库启用 WAL
        if self._db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            # WAL 模式下 synchronous=NORMAL 可大幅提升写入性能,
            # 仅在断电时可能丢失最后几条事务, 对安全事件审计可接受
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                event_type TEXT,
                event_class TEXT,
                interaction_seq INTEGER,
                session_id TEXT,
                agent_id TEXT,
                trace_id TEXT,
                timestamp REAL,
                timestamp_text TEXT,
                data TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id TEXT UNIQUE,
                interaction_seq INTEGER,
                session_id TEXT,
                trace_id TEXT,
                risk_level TEXT,
                risk_type TEXT,
                timestamp REAL,
                timestamp_text TEXT,
                acknowledged INTEGER DEFAULT 0,
                data TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_event_id TEXT UNIQUE,
                event_type TEXT,
                event_class TEXT,
                interaction_seq INTEGER,
                session_id TEXT,
                agent_id TEXT,
                trace_id TEXT,
                timestamp REAL,
                timestamp_text TEXT,
                data TEXT
            )
            """
        )
        # 为 trace_id 建立索引,加速按 trace_id 关联查询
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_trace_id ON events (trace_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_events_trace_id ON raw_events (trace_id)"
        )
        self._conn.commit()
        # 旧库迁移:补充 timestamp_text 列并回填历史数据
        self._migrate_timestamp_text()

    def _migrate_timestamp_text(self) -> None:
        """为旧库补充 timestamp_text 列并回填历史数据。

        对三张表逐一检查:列不存在时 ALTER TABLE 增列,随后将
        timestamp 有效(>0)且 timestamp_text 为 NULL 的历史行回填为
        本地时区可读格式。timestamp 无效的行保持 NULL。
        """
        for table in ("events", "alerts", "raw_events"):
            cursor = self._conn.execute(f"PRAGMA table_info({table})")
            columns = {row[1] for row in cursor.fetchall()}
            if "timestamp_text" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN timestamp_text TEXT"
                )
                self._conn.commit()
            # 回填历史数据(timestamp 有效但 timestamp_text 为空)
            rows = self._conn.execute(
                f"SELECT id, timestamp FROM {table} "
                "WHERE timestamp_text IS NULL AND timestamp > 0"
            ).fetchall()
            if rows:
                updates = [
                    (_format_local_timestamp(ts), row_id)
                    for row_id, ts in rows
                ]
                self._conn.executemany(
                    f"UPDATE {table} SET timestamp_text = ? WHERE id = ?",
                    updates,
                )
                self._conn.commit()

    async def record_event(self, event: dict) -> str:
        """异步写入一条事件记录,返回 event_id。

        将 event dict 的关键字段提取到独立列,完整 dict 以 JSON 字符串
        存入 data 字段。若 event 缺少 event_id,则自动生成 UUID。

        Args:
            event: 事件 dict,应包含 event_id、event_type、event_class、
                interaction_seq、session_id、agent_id、trace_id、timestamp 等字段。

        Returns:
            事件唯一标识 event_id。

        Raises:
            TypeError: event 不是 dict 类型时。
            ValueError: event 为空 dict 时。
        """
        if not isinstance(event, dict):
            raise TypeError(f"event 必须为 dict,实际类型: {type(event).__name__}")
        if not event:
            raise ValueError("event 不能为空 dict")

        # 复制一份避免修改入参
        record = dict(event)
        # 缺失 event_id 时自动生成
        event_id = record.get("event_id")
        if not event_id:
            event_id = new_uuid()
            record["event_id"] = event_id

        data_json = json.dumps(record, ensure_ascii=False, default=str)

        async with self._lock:
            await asyncio.to_thread(self._insert_event, record, data_json)
        return event_id

    def _insert_event(self, record: dict, data_json: str) -> None:
        """同步写入事件记录(在锁保护下执行)。"""
        timestamp = record.get("timestamp", 0.0)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO events
                (event_id, event_type, event_class, interaction_seq,
                 session_id, agent_id, trace_id, timestamp, timestamp_text, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("event_id", ""),
                record.get("event_type", ""),
                record.get("event_class", ""),
                record.get("interaction_seq", -1),
                record.get("session_id", ""),
                record.get("agent_id", ""),
                record.get("trace_id", ""),
                timestamp,
                _format_local_timestamp(timestamp) if timestamp else "",
                data_json,
            ),
        )
        self._conn.commit()

    async def record_raw_event(self, raw_event: dict) -> str:
        """异步持久化 raw_event(原始事件),返回 raw_event_id。

        raw_event 为三层结构(common / payload / metadata)。
        从 common 层提取关联 ID 字段存入独立列,完整 raw_event dict 以
        JSON 字符串存入 data 字段,用于审计溯源。若 raw_event 缺少
        raw_event_id,则自动生成 UUID。

        Args:
            raw_event: 原始事件 dict,包含 common、payload、metadata 三层。

        Returns:
            原始事件唯一标识 raw_event_id。

        Raises:
            TypeError: raw_event 不是 dict 类型时。
            ValueError: raw_event 为空 dict 时。
        """
        if not isinstance(raw_event, dict):
            raise TypeError(
                f"raw_event 必须为 dict,实际类型: {type(raw_event).__name__}"
            )
        if not raw_event:
            raise ValueError("raw_event 不能为空 dict")

        record = dict(raw_event)
        # raw_event_id 优先取顶层字段,其次取 common.raw_event_id,否则生成
        raw_event_id = record.get("raw_event_id")
        if not raw_event_id:
            common = record.get("common")
            if isinstance(common, dict):
                raw_event_id = common.get("raw_event_id")
        if not raw_event_id:
            raw_event_id = new_uuid()
            record["raw_event_id"] = raw_event_id

        # 从 common 层提取关联字段
        common = record.get("common")
        if not isinstance(common, dict):
            common = {}

        data_json = json.dumps(record, ensure_ascii=False, default=str)

        async with self._lock:
            await asyncio.to_thread(self._insert_raw_event, record, common, data_json)
        return raw_event_id

    def _insert_raw_event(self, record: dict, common: dict, data_json: str) -> None:
        """同步写入 raw_event 记录(在锁保护下执行)。"""
        timestamp = common.get("timestamp", 0.0)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO raw_events
                (raw_event_id, event_type, event_class, interaction_seq,
                 session_id, agent_id, trace_id, timestamp, timestamp_text, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("raw_event_id", ""),
                common.get("event_type", ""),
                common.get("event_class", ""),
                common.get("interaction_seq", -1),
                common.get("session_id", ""),
                common.get("agent_id", ""),
                common.get("trace_id", ""),
                timestamp,
                _format_local_timestamp(timestamp) if timestamp else "",
                data_json,
            ),
        )
        self._conn.commit()

    async def create_alert(self, alert: dict) -> str:
        """异步写入一条告警记录,返回 alert_id。

        将 alert dict 的关键字段提取到独立列,完整 dict 以 JSON 字符串
        存入 data 字段。若 alert 缺少 alert_id,则自动生成 UUID。

        Args:
            alert: 告警 dict,应包含 alert_id、interaction_seq、session_id、
                trace_id、risk_level、risk_type、timestamp 等字段。

        Returns:
            告警唯一标识 alert_id。

        Raises:
            TypeError: alert 不是 dict 类型时。
            ValueError: alert 为空 dict 时。
        """
        if not isinstance(alert, dict):
            raise TypeError(f"alert 必须为 dict,实际类型: {type(alert).__name__}")
        if not alert:
            raise ValueError("alert 不能为空 dict")

        record = dict(alert)
        alert_id = record.get("alert_id")
        if not alert_id:
            alert_id = new_uuid()
            record["alert_id"] = alert_id

        data_json = json.dumps(record, ensure_ascii=False, default=str)

        async with self._lock:
            await asyncio.to_thread(self._insert_alert, record, data_json)
        return alert_id

    def _insert_alert(self, record: dict, data_json: str) -> None:
        """同步写入告警记录(在锁保护下执行)。"""
        # acknowledged 字段以整数存储(0/1)
        acknowledged = 1 if record.get("acknowledged") else 0
        timestamp = record.get("timestamp", 0.0)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO alerts
                (alert_id, interaction_seq, session_id, trace_id,
                 risk_level, risk_type, timestamp, timestamp_text,
                 acknowledged, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("alert_id", ""),
                record.get("interaction_seq", -1),
                record.get("session_id", ""),
                record.get("trace_id", ""),
                record.get("risk_level", ""),
                record.get("risk_type", ""),
                timestamp,
                _format_local_timestamp(timestamp) if timestamp else "",
                acknowledged,
                data_json,
            ),
        )
        self._conn.commit()

    async def get_events(self, session_id: str = "", limit: int = 100) -> list[dict]:
        """查询事件记录,支持按 session_id 过滤。

        Args:
            session_id: 会话 ID,为空字符串时不过滤,返回所有事件。
            limit: 返回记录数上限,必须为正整数。

        Returns:
            事件 dict 列表,按 id 倒序(最新优先)排列。
            返回的 dict 为 data 字段反序列化后的完整事件数据。

        Raises:
            ValueError: limit 不是正整数时。
        """
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"limit 必须为正整数,实际值: {limit!r}")

        async with self._lock:
            return await asyncio.to_thread(self._query_events, session_id, limit)

    def _query_events(self, session_id: str, limit: int) -> list[dict]:
        """同步查询事件记录(在锁保护下执行)。"""
        if session_id:
            cursor = self._conn.execute(
                """
                SELECT data FROM events
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            )
        else:
            cursor = self._conn.execute(
                """
                SELECT data FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        rows = cursor.fetchall()
        results: list[dict] = []
        for row in rows:
            try:
                results.append(json.loads(row[0]))
            except (json.JSONDecodeError, TypeError):
                # 跳过无法反序列化的异常数据,保证查询不中断
                continue
        return results

    async def get_events_by_trace_id(self, trace_id: str) -> list[dict]:
        """按 trace_id 查询事件记录,用于呈现模块关联查询。

        从 events 表和 raw_events 表查询同一 trace 下的所有事件,
        合并后按 id 升序(最早优先)返回。此方法支持呈现模块基于
        trace_id 关联构建完整的 OCSF 格式报告。

        Args:
            trace_id: 追踪 ID,不能为空字符串。

        Returns:
            事件 dict 列表,按 id 升序排列。包含统一事件和原始事件,
            每条记录带有 "_source" 字段标识来源("event" 或 "raw_event")。

        Raises:
            ValueError: trace_id 为空字符串时。
        """
        if not trace_id:
            raise ValueError("trace_id 不能为空字符串")

        async with self._lock:
            return await asyncio.to_thread(self._query_events_by_trace_id, trace_id)

    def _query_events_by_trace_id(self, trace_id: str) -> list[dict]:
        """同步按 trace_id 查询事件记录(在锁保护下执行)。"""
        # 查询统一事件表
        cursor = self._conn.execute(
            """
            SELECT id, data FROM events
            WHERE trace_id = ?
            ORDER BY id ASC
            """,
            (trace_id,),
        )
        event_rows = cursor.fetchall()

        # 查询原始事件表
        cursor = self._conn.execute(
            """
            SELECT id, data FROM raw_events
            WHERE trace_id = ?
            ORDER BY id ASC
            """,
            (trace_id,),
        )
        raw_event_rows = cursor.fetchall()

        results: list[dict] = []
        for _id, data_json in event_rows:
            try:
                record = json.loads(data_json)
                record["_source"] = "event"
                results.append(record)
            except (json.JSONDecodeError, TypeError):
                continue
        for _id, data_json in raw_event_rows:
            try:
                record = json.loads(data_json)
                record["_source"] = "raw_event"
                results.append(record)
            except (json.JSONDecodeError, TypeError):
                continue
        return results

    async def get_alerts(
        self, acknowledged: bool | None = None, limit: int = 50
    ) -> list[dict]:
        """查询告警记录,支持按确认状态过滤。

        Args:
            acknowledged: 确认状态过滤。None 表示不过滤,True 表示已确认,
                False 表示未确认。
            limit: 返回记录数上限,必须为正整数。

        Returns:
            告警 dict 列表,按 id 倒序(最新优先)排列。
            返回的 dict 为 data 字段反序列化后的完整告警数据,
            其中 acknowledged 字段已转换为布尔值。

        Raises:
            ValueError: limit 不是正整数时。
        """
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"limit 必须为正整数,实际值: {limit!r}")

        async with self._lock:
            return await asyncio.to_thread(self._query_alerts, acknowledged, limit)

    def _query_alerts(self, acknowledged: bool | None, limit: int) -> list[dict]:
        """同步查询告警记录(在锁保护下执行)。"""
        if acknowledged is None:
            cursor = self._conn.execute(
                """
                SELECT data FROM alerts
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        else:
            cursor = self._conn.execute(
                """
                SELECT data FROM alerts
                WHERE acknowledged = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (1 if acknowledged else 0, limit),
            )
        rows = cursor.fetchall()
        results: list[dict] = []
        for row in rows:
            try:
                record = json.loads(row[0])
                # 将 acknowledged 字段转换为布尔值
                if "acknowledged" in record:
                    record["acknowledged"] = bool(record["acknowledged"])
                results.append(record)
            except (json.JSONDecodeError, TypeError):
                continue
        return results

    async def cleanup_expired(
        self, table: str, ttl_days: int, now: float | None = None
    ) -> int:
        """清理表中超过 TTL 的过期数据,返回删除的行数。

        按 timestamp 列(Unix 秒)与 cutoff = now - ttl_days * 86400 比较,
        删除 timestamp 小于 cutoff 的行。table 限定白名单
        (events/alerts/raw_events)防注入;ttl_days <= 0 视为禁用清理。

        Args:
            table: 表名,必须为 events / alerts / raw_events 之一。
            ttl_days: 保留天数。<= 0 时不执行清理(禁用语义)。
            now: 当前时间戳(Unix 秒)。None 时取 time.time()。

        Returns:
            删除的行数。表名非法或 TTL 禁用时返回 0。

        Raises:
            ValueError: table 不在白名单中时。
        """
        if table not in _CLEANUP_TABLES:
            raise ValueError(
                f"table 必须为 events/alerts/raw_events 之一,实际值: {table!r}"
            )
        if ttl_days <= 0:
            return 0

        current = now if now is not None else time.time()
        cutoff = current - ttl_days * 86400

        async with self._lock:
            return await asyncio.to_thread(
                self._delete_expired, table, cutoff
            )

    def _delete_expired(self, table: str, cutoff: float) -> int:
        """同步删除过期行(在锁保护下执行)。"""
        cursor = self._conn.execute(
            f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,)
        )
        self._conn.commit()
        return max(cursor.rowcount, 0)

    async def close(self) -> None:
        """关闭数据库连接。

        关闭持久连接,释放资源。供调用方在优雅关闭时调用。
        关闭后不应再调用其他方法。
        """
        async with self._lock:
            await asyncio.to_thread(self._close_conn)

    def _close_conn(self) -> None:
        """同步关闭数据库连接(在锁保护下执行)。"""
        try:
            self._conn.close()
        except Exception:
            # 关闭异常忽略,避免影响关闭流程
            pass
