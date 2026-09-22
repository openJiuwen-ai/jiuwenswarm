"""SQLite hosting.db: targets + watermarks."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from jiuwenswarm.server.im.im_hosting.gate import normalize_rule
from jiuwenswarm.server.im.im_hosting.paths import hosting_db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS im_hosting_targets (
  id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  external_id TEXT NOT NULL,
  title TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL DEFAULT 'manual',
  poll_interval_seconds INTEGER,
  fetch_count INTEGER,
  rule_override TEXT,
  hosting_since_ms INTEGER NOT NULL,
  last_poll_at_ms INTEGER,
  last_error TEXT,
  last_preview_json TEXT,
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  expert_service_id TEXT,
  expert_agent_id TEXT,
  expert_persona TEXT,
  UNIQUE(channel_id, target_kind, external_id)
);

CREATE TABLE IF NOT EXISTS im_watermarks (
  channel_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  last_processed_at_ms INTEGER NOT NULL,
  last_processed_msg_id TEXT,
  PRIMARY KEY (channel_id, scope, scope_id)
);

CREATE TABLE IF NOT EXISTS im_hosting_reply_turns (
  channel_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  source_msg_id TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  PRIMARY KEY (channel_id, scope, scope_id, source_msg_id)
);

CREATE TABLE IF NOT EXISTS im_hosting_policy (
  channel_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  updated_at_ms INTEGER NOT NULL
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_target_expert_columns(conn: sqlite3.Connection) -> None:
    cols = {str(item[1]) for item in conn.execute("PRAGMA table_info(im_hosting_targets)")}
    if "expert_service_id" not in cols:
        conn.execute("ALTER TABLE im_hosting_targets ADD COLUMN expert_service_id TEXT")
    if "expert_agent_id" not in cols:
        conn.execute("ALTER TABLE im_hosting_targets ADD COLUMN expert_agent_id TEXT")
    # 个人版 UI 叫「分身」说明，整段 markdown 存在 expert_persona。
    if "expert_persona" not in cols:
        conn.execute("ALTER TABLE im_hosting_targets ADD COLUMN expert_persona TEXT")
    conn.commit()


def _row_to_target(row: sqlite3.Row) -> dict[str, Any]:
    preview = None
    raw = row["last_preview_json"]
    if raw:
        try:
            preview = json.loads(raw)
        except json.JSONDecodeError:
            preview = None
    rule = None
    rule_raw = row["rule_override"]
    if rule_raw:
        try:
            rule = json.loads(rule_raw)
        except json.JSONDecodeError:
            rule = None
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "target_kind": row["target_kind"],
        "external_id": row["external_id"],
        "title": row["title"],
        "enabled": bool(row["enabled"]),
        "source": row["source"],
        "poll_interval_seconds": row["poll_interval_seconds"],
        "fetch_count": row["fetch_count"],
        "rule_override": rule,
        "hosting_since_ms": row["hosting_since_ms"],
        "last_poll_at_ms": row["last_poll_at_ms"],
        "last_error": row["last_error"],
        "last_preview": preview,
        "created_at_ms": row["created_at_ms"],
        "updated_at_ms": row["updated_at_ms"],
        "expert_service_id": _row_get(row, "expert_service_id"),
        "expert_agent_id": _row_get(row, "expert_agent_id"),
        "expert_persona": _row_get(row, "expert_persona"),
    }


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


class HostingStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or hosting_db_path()
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            _ensure_target_expert_columns(conn)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def list_targets(self, channel_id: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if channel_id:
                rows = conn.execute(
                    "SELECT * FROM im_hosting_targets WHERE channel_id=? ORDER BY updated_at_ms DESC",
                    (channel_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM im_hosting_targets ORDER BY updated_at_ms DESC"
                ).fetchall()
        return [_row_to_target(r) for r in rows]

    def get_target(self, target_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM im_hosting_targets WHERE id=?", (target_id,)
            ).fetchone()
        return _row_to_target(row) if row else None

    def find_target(
        self, channel_id: str, target_kind: str, external_id: str
    ) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM im_hosting_targets
                WHERE channel_id=? AND target_kind=? AND external_id=?
                """,
                (channel_id, target_kind, external_id),
            ).fetchone()
        return _row_to_target(row) if row else None

    def add_target(
        self,
        *,
        channel_id: str,
        target_kind: str,
        external_id: str,
        title: Optional[str] = None,
        source: str = "manual",
        enabled: bool = True,
        rule_override: Any = None,
        expert_service_id: Optional[str] = None,
        expert_agent_id: Optional[str] = None,
        expert_persona: Optional[str] = None,
    ) -> dict[str, Any]:
        existing = self.find_target(channel_id, target_kind, external_id)
        extra: dict[str, Any] = {}
        if rule_override:
            extra["rule_override"] = rule_override
        if expert_service_id is not None:
            extra["expert_service_id"] = expert_service_id
        if expert_agent_id is not None:
            extra["expert_agent_id"] = expert_agent_id
        if expert_persona is not None:
            extra["expert_persona"] = expert_persona
        if existing:
            if source == "manual":
                extra["enabled"] = True
                extra["source"] = "manual"
            if extra:
                patched = self.patch_target(existing["id"], extra)
                return patched or existing
            return existing
        now = _now_ms()
        target_id = str(uuid.uuid4())
        rule = normalize_rule(rule_override) if rule_override else None
        rule_json = json.dumps(rule, ensure_ascii=False) if rule is not None else None
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO im_hosting_targets (
                  id, channel_id, target_kind, external_id, title, enabled, source,
                  rule_override, hosting_since_ms, created_at_ms, updated_at_ms,
                  expert_service_id, expert_agent_id, expert_persona
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_id,
                    channel_id,
                    target_kind,
                    external_id,
                    title or "",
                    1 if enabled else 0,
                    source,
                    rule_json,
                    now,
                    now,
                    now,
                    (expert_service_id or "").strip() or "default",
                    (expert_agent_id or "").strip() or "default",
                    (expert_persona or "").strip() or None,
                ),
            )
            conn.commit()
        row = self.get_target(target_id)
        assert row is not None
        return row

    def patch_target(self, target_id: str, patch: dict[str, Any]) -> Optional[dict[str, Any]]:
        current = self.get_target(target_id)
        if current is None:
            return None
        now = _now_ms()
        enabled = current["enabled"]
        hosting_since = current["hosting_since_ms"]
        clear_watermark = False
        if "enabled" in patch:
            enabled = bool(patch["enabled"])
            if enabled and not current["enabled"]:
                hosting_since = now
                clear_watermark = True
        title = patch["title"] if "title" in patch else current["title"]
        source = patch["source"] if "source" in patch else current.get("source") or "manual"
        poll_interval = (
            patch["poll_interval_seconds"]
            if "poll_interval_seconds" in patch
            else current["poll_interval_seconds"]
        )
        fetch_count = patch["fetch_count"] if "fetch_count" in patch else current["fetch_count"]
        rule = current["rule_override"]
        if "rule_override" in patch:
            incoming = patch["rule_override"]
            if incoming is None or incoming == "":
                rule = None
            else:
                rule = normalize_rule(incoming)
        rule_json = json.dumps(rule, ensure_ascii=False) if rule is not None else None
        expert_service_id = (
            str(patch["expert_service_id"]).strip() or None
            if "expert_service_id" in patch
            else current.get("expert_service_id")
        )
        expert_agent_id = (
            str(patch["expert_agent_id"]).strip() or None
            if "expert_agent_id" in patch
            else current.get("expert_agent_id")
        )
        expert_persona = (
            str(patch["expert_persona"]).strip() or None
            if "expert_persona" in patch
            else current.get("expert_persona")
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE im_hosting_targets SET
                  title=?, enabled=?, source=?, poll_interval_seconds=?, fetch_count=?,
                  rule_override=?, hosting_since_ms=?, updated_at_ms=?,
                  expert_service_id=?, expert_agent_id=?,
                  expert_persona=?
                WHERE id=?
                """,
                (
                    title,
                    1 if enabled else 0,
                    source,
                    poll_interval,
                    fetch_count,
                    rule_json,
                    hosting_since,
                    now,
                    expert_service_id,
                    expert_agent_id,
                    expert_persona,
                    target_id,
                ),
            )
            if clear_watermark:
                conn.execute(
                    "DELETE FROM im_watermarks WHERE channel_id=? AND scope=? AND scope_id=?",
                    (current["channel_id"], current["target_kind"], current["external_id"]),
                )
            conn.commit()
        return self.get_target(target_id)

    def release_target(self, target_id: str) -> bool:
        """用户取消托管：停用但留行，避免自动发现立刻又加回来。"""
        return self.patch_target(target_id, {"enabled": False}) is not None

    def delete_target(self, target_id: str) -> bool:
        current = self.get_target(target_id)
        if current is None:
            return False
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM im_hosting_targets WHERE id=?", (target_id,))
            conn.execute(
                "DELETE FROM im_watermarks WHERE channel_id=? AND scope=? AND scope_id=?",
                (current["channel_id"], current["target_kind"], current["external_id"]),
            )
            conn.commit()
        return True

    def drop_auto_targets(self, channel_id: str, *, kinds: list[str] | tuple[str, ...]) -> int:
        """关掉全局自动托管后，清掉该通道自动进名单的会话；手选保留。"""
        wanted = {str(kind) for kind in kinds if str(kind) in {"group", "user"}}
        if not wanted:
            return 0
        dropped = 0
        for target in self.list_targets(channel_id):
            if str(target.get("source") or "") != "auto":
                continue
            if str(target.get("target_kind") or "") not in wanted:
                continue
            if self.delete_target(str(target["id"])):
                dropped += 1
        return dropped

    def record_poll(
        self,
        target_id: str,
        *,
        preview: list[dict[str, Any]] | None,
        error: Optional[str],
    ) -> None:
        now = _now_ms()
        preview_json = json.dumps(preview, ensure_ascii=False) if preview is not None else None
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE im_hosting_targets
                SET last_poll_at_ms=?, last_error=?, last_preview_json=?, updated_at_ms=?
                WHERE id=?
                """,
                (now, error, preview_json, now, target_id),
            )
            conn.commit()

    def get_watermark(
        self, channel_id: str, scope: str, scope_id: str
    ) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM im_watermarks
                WHERE channel_id=? AND scope=? AND scope_id=?
                """,
                (channel_id, scope, scope_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "channel_id": row["channel_id"],
            "scope": row["scope"],
            "scope_id": row["scope_id"],
            "last_processed_at_ms": row["last_processed_at_ms"],
            "last_processed_msg_id": row["last_processed_msg_id"],
        }

    def set_watermark(
        self,
        channel_id: str,
        scope: str,
        scope_id: str,
        *,
        last_processed_at_ms: int,
        last_processed_msg_id: Optional[str] = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO im_watermarks (
                  channel_id, scope, scope_id, last_processed_at_ms, last_processed_msg_id
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, scope, scope_id) DO UPDATE SET
                  last_processed_at_ms=excluded.last_processed_at_ms,
                  last_processed_msg_id=excluded.last_processed_msg_id
                """,
                (
                    channel_id,
                    scope,
                    scope_id,
                    last_processed_at_ms,
                    last_processed_msg_id,
                ),
            )
            conn.commit()

    def hosted_keys(self, channel_id: str) -> set[tuple[str, str]]:
        return {
            (t["target_kind"], t["external_id"])
            for t in self.list_targets(channel_id)
        }

    def claim_reply_turn(
        self,
        channel_id: str,
        scope: str,
        scope_id: str,
        source_msg_id: str,
    ) -> bool:
        """幂等：同一条源消息只代回一次。"""
        msg_id = (source_msg_id or "").strip()
        if not msg_id:
            return False
        now = _now_ms()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO im_hosting_reply_turns (
                  channel_id, scope, scope_id, source_msg_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (channel_id, scope, scope_id, msg_id, now),
            )
            conn.commit()
            return cur.rowcount > 0

    def load_policy_payloads(self) -> dict[str, dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT channel_id, payload FROM im_hosting_policy").fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                out[str(row["channel_id"])] = payload
        return out

    def save_policy_payloads(self, payloads: dict[str, dict[str, Any]]) -> None:
        now = _now_ms()
        with self._lock, self._connect() as conn:
            for channel_id, payload in payloads.items():
                conn.execute(
                    """
                    INSERT INTO im_hosting_policy (channel_id, payload, updated_at_ms)
                    VALUES (?, ?, ?)
                    ON CONFLICT(channel_id) DO UPDATE SET
                      payload=excluded.payload,
                      updated_at_ms=excluded.updated_at_ms
                    """,
                    (channel_id, json.dumps(payload, ensure_ascii=False), now),
                )
            conn.commit()
