"""Private, frozen cross-group sharing receipts and revocable destination grants.

Only ``get`` and ``for_query`` return the full result. Other host operations
return receipt metadata; only the authenticated owner's list includes previews.
The host must recheck platform access and current grants before sending data.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import json
import re
import secrets
import sqlite3
from pathlib import Path

from openjiuwen.harness.persistent_agent import GroupMessageQuery

from jiuwenswarm.server.jiuwenmate.group_chat_repository import utc_time
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.search_contracts import GroupMessageSearchResult
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore


APPROVAL_TIMEOUT = timedelta(minutes=15)
MAX_PREVIEW_TEXT_BYTES = 512 * 1024
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
_MAX_SCOPE_BYTES = 64 * 1024
_DECISIONS = {"once": "approved_once", "permanent": "approved_permanent", "reject": "rejected"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_share_approvals (
    approval_id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    query_digest TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    source_groups_json TEXT NOT NULL,
    native_approval_id TEXT,
    tool_call_id TEXT,
    tool_call_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    decision TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(namespace_id, agent_id, request_id, query_digest)
);
CREATE INDEX IF NOT EXISTS group_share_approvals_owner
    ON group_share_approvals(namespace_id, config_id, created_at);
CREATE INDEX IF NOT EXISTS group_share_approvals_request
    ON group_share_approvals(namespace_id, request_id, status);
CREATE INDEX IF NOT EXISTS group_share_approvals_native
    ON group_share_approvals(native_approval_id);
CREATE INDEX IF NOT EXISTS group_share_approvals_expiry
    ON group_share_approvals(status, expires_at);
CREATE TABLE IF NOT EXISTS group_share_grants (
    grant_id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    source_group_id TEXT NOT NULL,
    source_group_name TEXT NOT NULL,
    target_json TEXT NOT NULL,
    grant_key TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS group_share_grants_active
    ON group_share_grants(grant_key) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS group_share_grants_owner
    ON group_share_grants(namespace_id, config_id, created_at);
"""


def _json(value: object, limit: int) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        size = len(encoded.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        raise PersistentAgentError("invalid group sharing JSON") from None
    if size > limit:
        raise PersistentAgentError("group sharing snapshot is too large")
    return encoded


def _name(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise PersistentAgentError("invalid group sharing display name")


def _scope_json(scope: dict) -> str:
    if not isinstance(scope, dict):
        raise PersistentAgentError("invalid group sharing scope")
    for name in ("namespace_id", "config_id", "agent_id", "request_id", "principal_ref"):
        require_identifier(name, scope.get(name))
    _name(scope.get("requester_name"))
    target = scope.get("target")
    if not isinstance(target, dict):
        raise PersistentAgentError("invalid group sharing target")
    for name in ("channel", "kind", "id"):
        require_identifier(name, target.get(name))
    if "app_id" not in target:
        raise PersistentAgentError("missing group sharing application")
    if target["app_id"] not in (None, ""):
        require_identifier("app_id", target["app_id"])
    _name(target.get("name"))
    try:
        GroupMessageQuery.model_validate(scope.get("query"))
    except ValueError:
        raise PersistentAgentError("invalid group sharing query") from None
    return _json(scope, _MAX_SCOPE_BYTES)


def _target_view(target: dict) -> dict:
    return {key: target[key] for key in ("channel", "app_id", "kind", "id", "name")}


def _grant_key(scope: dict, source_group_id: str) -> str:
    require_identifier("source_group_id", source_group_id)
    target = scope["target"]
    return _json([
        scope["namespace_id"], scope["config_id"], scope["agent_id"], source_group_id,
        target["channel"], target["app_id"], target["kind"], target["id"],
    ], _MAX_SCOPE_BYTES)


class GroupShareRepository(SQLiteStore):
    """Durable host state in the caller-supplied agents.sqlite3; opens no default DB."""

    _schema = _SCHEMA

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        with self._transaction() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(group_share_approvals)")}
            if "tool_call_ids_json" not in columns:
                conn.execute("ALTER TABLE group_share_approvals ADD COLUMN tool_call_ids_json TEXT NOT NULL DEFAULT '[]'")
                rows = conn.execute(
                    "SELECT approval_id, tool_call_id FROM group_share_approvals WHERE tool_call_id IS NOT NULL",
                ).fetchall()
                conn.executemany(
                    "UPDATE group_share_approvals SET tool_call_ids_json=? WHERE approval_id=?",
                    [(json.dumps([row["tool_call_id"]]), row["approval_id"]) for row in rows],
                )

    @staticmethod
    def _record(row: sqlite3.Row, *, private: bool = False) -> dict:
        record = {key: row[key] for key in (
            "approval_id", "namespace_id", "config_id", "agent_id", "request_id", "query_digest",
            "native_approval_id", "tool_call_id", "status", "decision", "created_at", "expires_at", "decided_at",
        )}
        record["scope"] = json.loads(row["scope_json"])
        record["source_groups"] = json.loads(row["source_groups_json"])
        record["tool_call_ids"] = json.loads(row["tool_call_ids_json"])
        if private:
            record["result"] = json.loads(row["result_json"])
        return record

    @staticmethod
    def _approval_view(row: sqlite3.Row) -> dict:
        scope = json.loads(row["scope_json"])
        sources = json.loads(row["source_groups_json"])
        frozen = json.loads(row["result_json"])
        messages = [{
            "group_id": hit["group_id"], "group_name": hit["group_name"],
            "message_id": hit["message"]["message_id"], "sender_name": hit["message"]["sender_name"],
            "text": hit["message"].get("text"),
        } for hit in frozen["hits"]]
        excerpts = []
        for message in messages[:3]:
            text = " ".join((message["text"] or "").split()) or "暂无正文"
            excerpts.append(text if len(text) <= 100 else text[:99] + "…")
        target_name = scope["target"]["name"]
        target_label = target_name if len(target_name) <= 40 else target_name[:39] + "…"
        summary = (
            f"申请分享 {len(sources)} 个来源群的 {len(messages)} 条消息至「{target_label}」。内容摘录："
            + ("；".join(excerpts) if excerpts else "暂无正文")
        )[:400]
        return {
            **{key: row[key] for key in (
                "approval_id", "request_id", "agent_id", "config_id", "status",
                "created_at", "expires_at", "decided_at",
            )},
            "can_decide": row["status"] == "pending" and row["native_approval_id"] is not None and len(sources) == 1,
            "requester_name": scope["requester_name"], "source_groups": sources,
            "target": _target_view(scope["target"]),
            "query": GroupMessageQuery.model_validate(scope["query"]).model_dump(mode="json"),
            "summary": summary,
            "messages": messages,
        }

    @staticmethod
    def _grant_view(row: sqlite3.Row) -> dict:
        return {
            **{key: row[key] for key in (
                "grant_id", "source_group_id", "source_group_name", "created_at", "status",
            )},
            "target": _target_view(json.loads(row["target_json"])),
        }

    @staticmethod
    def _owner(owner_principal: str) -> None:
        if owner_principal != "local":
            raise PersistentAgentError("group sharing requires the local owner", code="FORBIDDEN")

    @staticmethod
    def _owned(row: sqlite3.Row | None, namespace_id: str, config_id: str) -> None:
        if row is None:
            raise PersistentAgentError("group sharing record not found", code="NOT_FOUND")
        if row["namespace_id"] != namespace_id or row["config_id"] != config_id:
            raise PersistentAgentError("group sharing belongs to another colleague", code="FORBIDDEN")

    @staticmethod
    def _required(conn: sqlite3.Connection, approval_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM group_share_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise PersistentAgentError("group sharing approval not found", code="NOT_FOUND")
        return row

    @staticmethod
    def _native_request(conn: sqlite3.Connection, native_id: str, namespace_id: str, request_id: str) -> None:
        other = conn.execute(
            "SELECT 1 FROM group_share_approvals WHERE native_approval_id=? "
            "AND (namespace_id<>? OR request_id<>?) LIMIT 1", (native_id, namespace_id, request_id),
        ).fetchone()
        if other is not None:
            raise PersistentAgentError("native approval belongs to another request", code="CONFLICT")

    def create_or_get(
        self, scope: dict, query_digest: str, result: dict, source_groups: list[dict], now: datetime,
    ) -> dict | None:
        """Freeze one per-source approval snapshot; refuse scope rebinding."""
        return self.create_or_get_batch([(scope, query_digest, result, source_groups)], now=now)[0]

    def create_or_get_batch(self, snapshots: list[tuple[dict, str, dict, list[dict]]], *, now: datetime) -> list[dict]:
        """Atomically freeze all hit groups for one search so partial approval batches cannot appear."""
        stamp = utc_time(now)
        if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= 128:
            raise PersistentAgentError("invalid group sharing snapshot batch")
        prepared = []
        seen_digests = set()
        for scope, query_digest, result, source_groups in snapshots:
            scope_json = _scope_json(scope)
            if not isinstance(query_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", query_digest):
                raise PersistentAgentError("invalid group sharing query digest")
            if query_digest in seen_digests:
                raise PersistentAgentError("duplicate group sharing source snapshot")
            seen_digests.add(query_digest)
            result_json = _json(result, _MAX_SNAPSHOT_BYTES)
            try:
                frozen = GroupMessageSearchResult.model_validate(json.loads(result_json))
            except ValueError:
                raise PersistentAgentError("invalid group sharing result") from None
            for key in ("namespace_id", "agent_id", "request_id"):
                if getattr(frozen, key) != scope[key]:
                    raise PersistentAgentError("group sharing result invocation mismatch")
            if not isinstance(source_groups, list) or len(source_groups) != 1:
                raise PersistentAgentError("each group sharing approval requires exactly one source")
            group = source_groups[0]
            if not isinstance(group, dict):
                raise PersistentAgentError("invalid group sharing source group")
            group_id = require_identifier("group_id", group.get("group_id"))
            _name(group.get("name"))
            source = {"group_id": group_id, "name": group["name"]}
            if not frozen.hits or any(hit.group_id != group_id for hit in frozen.hits):
                raise PersistentAgentError("group sharing snapshot must contain hits from its source")
            if sum(len((hit.message.text or "").encode("utf-8")) for hit in frozen.hits) > MAX_PREVIEW_TEXT_BYTES:
                raise PersistentAgentError("group sharing preview is too large")
            prepared.append((scope, query_digest, scope_json, result_json, _json([source], _MAX_SCOPE_BYTES)))

        with self._transaction() as conn:
            existing_rows = []
            for scope, query_digest, scope_json, *_ in prepared:
                existing = conn.execute(
                    "SELECT * FROM group_share_approvals WHERE namespace_id=? AND agent_id=? "
                    "AND request_id=? AND query_digest=?",
                    (scope["namespace_id"], scope["agent_id"], scope["request_id"], query_digest),
                ).fetchone()
                if existing is not None and existing["scope_json"] != scope_json:
                    raise PersistentAgentError("group sharing scope cannot change", code="CONFLICT")
                existing_rows.append(existing)
            if any(row is not None for row in existing_rows):
                if not all(row is not None for row in existing_rows):
                    raise PersistentAgentError("group sharing snapshot batch is incomplete", code="CONFLICT")
                return [self._record(row, private=True) for row in existing_rows]

            approval_ids = []
            for scope, query_digest, scope_json, result_json, source_json in prepared:
                approval_id = "share_" + secrets.token_hex(16)
                conn.execute(
                    "INSERT INTO group_share_approvals "
                    "(approval_id, namespace_id, config_id, agent_id, request_id, query_digest, scope_json, "
                    "result_json, source_groups_json, status, created_at, expires_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
                    (approval_id, scope["namespace_id"], scope["config_id"], scope["agent_id"], scope["request_id"],
                     query_digest, scope_json, result_json, source_json,
                     stamp.isoformat(), (stamp + APPROVAL_TIMEOUT).isoformat()),
                )
                approval_ids.append(approval_id)
            return [self._record(self._required(conn, approval_id), private=True) for approval_id in approval_ids]

    def get(self, approval_id: str) -> dict | None:
        """Private host lookup; never forward this result to an RPC or requester."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM group_share_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return None if row is None else self._record(row, private=True)

    def for_query(self, namespace_id: str, agent_id: str, request_id: str, query_digest: str) -> dict | None:
        """Private host lookup for the exact original query fingerprint."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM group_share_approvals WHERE namespace_id=? AND agent_id=? "
                "AND request_id=? AND query_digest=?", (namespace_id, agent_id, request_id, query_digest),
            ).fetchone()
        return None if row is None else self._record(row, private=True)

    def for_batch(self, namespace_id: str, agent_id: str, request_id: str, search_digest: str) -> list[dict]:
        """Return the immutable per-source snapshots created for one search invocation."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM group_share_approvals WHERE namespace_id=? AND agent_id=? AND request_id=? "
                "ORDER BY rowid", (namespace_id, agent_id, request_id),
            ).fetchall()
        return [
            self._record(row, private=True)
            for row in rows
            if json.loads(row["scope_json"]).get("search_digest") == search_digest
        ]

    def _for_request(self, namespace_id: str, request_id: str, *, approved: bool) -> list[dict]:
        states = "('approved_once','approved_permanent')" if approved else "('pending')"
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM group_share_approvals WHERE namespace_id=? AND request_id=? "
                f"AND status IN {states} ORDER BY rowid", (namespace_id, request_id),
            ).fetchall()
        return [self._record(row) for row in rows]

    def pending_for_request(self, namespace_id: str, request_id: str) -> list[dict]:
        return self._for_request(namespace_id, request_id, approved=False)

    def approved_for_request(self, namespace_id: str, request_id: str) -> list[dict]:
        """Receipts for outbound checks; permanent receipts still require current grants."""
        return self._for_request(namespace_id, request_id, approved=True)

    def for_native(self, native_approval_id: str) -> dict | None:
        """Find any linked business receipt, including decided ones, to prevent bypasses."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM group_share_approvals WHERE native_approval_id=? ORDER BY rowid LIMIT 1",
                (native_approval_id,),
            ).fetchone()
        return None if row is None else self._record(row)

    def for_native_all(self, native_approval_id: str) -> list[dict]:
        """Return every per-source receipt linked to one paused native tool batch."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM group_share_approvals WHERE native_approval_id=? ORDER BY rowid",
                (native_approval_id,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def bind_native(self, namespace_id: str, request_id: str, native_approval_id: str) -> None:
        """Bind every currently unbound pending receipt in the original request."""
        require_identifier("native_approval_id", native_approval_id)
        with self._transaction() as conn:
            self._native_request(conn, native_approval_id, namespace_id, request_id)
            conn.execute(
                "UPDATE group_share_approvals SET native_approval_id=? WHERE namespace_id=? AND request_id=? "
                "AND status='pending' AND native_approval_id IS NULL", (native_approval_id, namespace_id, request_id),
            )

    def bind_call(self, approval_id: str, native_approval_id: str, tool_call_id: str) -> None:
        """Associate every per-source receipt for one query with the same paused tool call."""
        require_identifier("native_approval_id", native_approval_id)
        require_identifier("tool_call_id", tool_call_id)
        with self._transaction() as conn:
            row = self._required(conn, approval_id)
            self._native_request(conn, native_approval_id, row["namespace_id"], row["request_id"])
            if row["native_approval_id"] not in (None, native_approval_id):
                raise PersistentAgentError("group sharing native approval cannot be rebound", code="CONFLICT")
            batch_digest = json.loads(row["scope_json"]).get("search_digest")
            rows = conn.execute(
                "SELECT * FROM group_share_approvals WHERE namespace_id=? AND config_id=? AND agent_id=? "
                "AND request_id=? ORDER BY rowid",
                (row["namespace_id"], row["config_id"], row["agent_id"], row["request_id"]),
            ).fetchall()
            for candidate in rows:
                candidate_scope = json.loads(candidate["scope_json"])
                same_batch = candidate["approval_id"] == approval_id or (
                    batch_digest is not None and candidate_scope.get("search_digest") == batch_digest
                )
                if not same_batch:
                    continue
                if candidate["native_approval_id"] not in (None, native_approval_id):
                    raise PersistentAgentError("group sharing native approval cannot be rebound", code="CONFLICT")
                tool_call_ids = json.loads(candidate["tool_call_ids_json"])
                if tool_call_id not in tool_call_ids:
                    tool_call_ids = sorted([*tool_call_ids, tool_call_id])
                conn.execute(
                    "UPDATE group_share_approvals SET native_approval_id=?, tool_call_id=COALESCE(tool_call_id,?), "
                    "tool_call_ids_json=? WHERE approval_id=?",
                    (native_approval_id, tool_call_id, json.dumps(tool_call_ids), candidate["approval_id"]),
                )

    def list_for_owner(self, namespace_id: str, config_id: str, owner_principal: str = "local") -> dict:
        """Owner-only previews. Call expire_pending before listing to refresh timeout states."""
        self._owner(owner_principal)
        with self._transaction() as conn:
            approvals = conn.execute(
                "SELECT * FROM group_share_approvals WHERE namespace_id=? AND config_id=? ORDER BY rowid",
                (namespace_id, config_id),
            ).fetchall()
            grants = conn.execute(
                "SELECT * FROM group_share_grants WHERE namespace_id=? AND config_id=? ORDER BY rowid",
                (namespace_id, config_id),
            ).fetchall()
        visible_approvals = [row for row in approvals if len(json.loads(row["source_groups_json"])) == 1]
        return {"approvals": [self._approval_view(row) for row in visible_approvals],
                "grants": [self._grant_view(row) for row in grants]}

    def _transition(self, conn: sqlite3.Connection, row: sqlite3.Row, status: str, stamp: str, decision: str | None) -> dict:
        conn.execute(
            "UPDATE group_share_approvals SET status=?, decision=?, decided_at=? WHERE approval_id=? AND status='pending'",
            (status, decision, stamp, row["approval_id"]),
        )
        return self._record(self._required(conn, row["approval_id"]))

    @staticmethod
    def _has_grant(conn: sqlite3.Connection, scope: dict, source_group_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM group_share_grants WHERE grant_key=? AND status='active'", (_grant_key(scope, source_group_id),),
        ).fetchone() is not None

    def has_grant(self, scope: dict, source_group_id: str) -> bool:
        """Check the exact namespace/config/runtime/source/destination grant, never a receipt."""
        with closing(self._connect()) as conn:
            return self._has_grant(conn, scope, source_group_id)

    def authorize_from_grants(self, approval_id: str, *, now: datetime) -> dict:
        """Record existing current grants atomically, without creating or restoring any grant."""
        stamp = utc_time(now).isoformat()
        with self._transaction() as conn:
            row = self._required(conn, approval_id)
            if row["status"] != "pending":
                return self._record(row)
            if stamp >= row["expires_at"]:
                return self._transition(conn, row, "expired", stamp, None)
            scope = json.loads(row["scope_json"])
            sources = json.loads(row["source_groups_json"])
            if not sources or not all(self._has_grant(conn, scope, group["group_id"]) for group in sources):
                return self._record(row)
            return self._transition(conn, row, "approved_permanent", stamp, "permanent")

    def decide(
        self, approval_id: str, *, namespace_id: str, config_id: str, owner_principal: str,
        decision: str, now: datetime,
    ) -> dict:
        """Apply an owner decision once; retries never issue grants from an old receipt."""
        self._owner(owner_principal)
        if decision not in _DECISIONS:
            raise PersistentAgentError("invalid group sharing decision")
        stamp = utc_time(now).isoformat()
        with self._transaction() as conn:
            row = self._required(conn, approval_id)
            self._owned(row, namespace_id, config_id)
            if len(json.loads(row["source_groups_json"])) != 1:
                raise PersistentAgentError("group sharing approval must contain exactly one source group", code="CONFLICT")
            if row["status"] != "pending":
                if row["decision"] == decision:
                    return self._record(row)
                raise PersistentAgentError("group sharing decision conflicts with receipt", code="CONFLICT")
            if stamp >= row["expires_at"]:
                return self._transition(conn, row, "expired", stamp, None)
            if row["native_approval_id"] is None:
                raise PersistentAgentError("group sharing native approval is not bound", code="CONFLICT")
            if decision == "permanent":
                scope = json.loads(row["scope_json"])
                for group in json.loads(row["source_groups_json"]):
                    if self._has_grant(conn, scope, group["group_id"]):
                        continue
                    conn.execute(
                        "INSERT INTO group_share_grants (grant_id, namespace_id, config_id, agent_id, "
                        "source_group_id, source_group_name, target_json, grant_key, approval_id, status, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,'active',?)",
                        ("share_grant_" + secrets.token_hex(16), namespace_id, config_id, scope["agent_id"],
                         group["group_id"], group["name"], _json(_target_view(scope["target"]), _MAX_SCOPE_BYTES),
                         _grant_key(scope, group["group_id"]), approval_id, stamp),
                    )
            decided = self._transition(conn, row, _DECISIONS[decision], stamp, decision)
            return decided

    def revoke(
        self, grant_id: str, *, namespace_id: str, config_id: str, owner_principal: str, now: datetime,
    ) -> dict:
        """Revoke only this grant generation; future approvals receive a fresh grant ID."""
        self._owner(owner_principal)
        stamp = utc_time(now).isoformat()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM group_share_grants WHERE grant_id=?", (grant_id,)).fetchone()
            self._owned(row, namespace_id, config_id)
            conn.execute(
                "UPDATE group_share_grants SET status='revoked', revoked_at=? WHERE grant_id=? AND status='active'",
                (stamp, grant_id),
            )
            return self._grant_view(conn.execute("SELECT * FROM group_share_grants WHERE grant_id=?", (grant_id,)).fetchone())

    def expire_pending(self, now: datetime) -> list[dict]:
        """Expire overdue requests and invalidate legacy approvals spanning multiple source groups."""
        stamp = utc_time(now).isoformat()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM group_share_approvals WHERE status='pending' ORDER BY rowid",
            ).fetchall()
            expired = []
            for row in rows:
                legacy_multi_source = len(json.loads(row["source_groups_json"])) != 1
                if row["expires_at"] <= stamp or legacy_multi_source:
                    expired.append(self._transition(conn, row, "expired", stamp, None))
            return expired

