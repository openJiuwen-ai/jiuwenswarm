# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Durable publish drafts and single-attempt tasks. Credentials never enter this API.

The caller must derive scope from trusted workspace, publisher and Hub identity.
Recovery is explicit and must run before workers start, never on every connection.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishIdentity
from jiuwenswarm.server.runtime.marketplace.hub_publish_client import PublishRequest

_SAFE_ERRORS = frozenset(
    {
        "interrupted",
        "upload_timeout",
        "upload_failed",
        "network_error",
        "unauthorized",
        "permission_denied",
        "checksum_required",
        "checksum_mismatch",
        "invalid_file_format",
        "invalid_version",
        "invalid_plugin_structure",
        "invalid_plugin_config",
        "invalid_agent_plugin_manifest",
        "invalid_agent_plugin_capability",
        "invalid_agent_mcp",
        "invalid_manifest_json",
        "missing_persona",
        "dangerous_content",
        "invalid_visibility",
        "plugin_not_found",
        "version_conflict",
        "version_exists",
        "plugin_name_exists",
        "plugin_type_immutable",
        "file_too_large",
        "rate_limited",
        "artifact_changed",
        "invalid_response",
        "publish_failed",
        "transport_error",
        "hub_error",
        "protocol_error",
        "artifact_unavailable",
        "invalid_artifact",
        "artifact_read_failed",
        "unsupported_response_encoding",
        "upload_outcome_unknown",
        "hub_rejected",
        "hub_request_failed",
    }
)


class PublishStoreError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _request_json(request: PublishRequest) -> str:
    if not isinstance(request, PublishRequest):
        raise PublishStoreError("invalid_draft")
    identity = request.identity
    return json.dumps(
        {
            "identity": dict(
                kind=identity.kind,
                package_name=identity.package_name,
                version=identity.version,
                target_asset_id=identity.target_asset_id,
            ),
            "artifact_path": str(request.artifact_path),
            "artifact_sha256": request.artifact_sha256,
            "display_name": request.display_name,
            "description": request.description,
            "tags": list(request.tags),
            "version_desc": request.version_desc,
            "visibility": request.visibility,
            "force": request.force,
        }
    )


def _request(data: str) -> PublishRequest:
    value = json.loads(data)
    value["identity"] = PublishIdentity(**value["identity"])
    value["artifact_path"] = Path(value["artifact_path"])
    value["tags"] = tuple(value["tags"])
    return PublishRequest(**value)


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise PublishStoreError("invalid_identifier")


def _timestamp(now: float | None) -> float:
    value = time.time() if now is None else now
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PublishStoreError("invalid_timestamp")
    return value


def _safe_error(value: dict | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {
        "code",
        "http_status",
        "outcome_unknown",
        "retry_after",
    }:
        raise PublishStoreError("invalid_record")
    if not isinstance(value.get("code"), str) or value["code"] not in _SAFE_ERRORS:
        raise PublishStoreError("invalid_record")
    for name in ("http_status", "retry_after"):
        number = value.get(name)
        if number is None:
            continue
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise PublishStoreError("invalid_record")
    if "outcome_unknown" in value and not isinstance(value["outcome_unknown"], bool):
        raise PublishStoreError("invalid_record")
    return json.dumps(value)


def _safe_result(value: dict | None, draft: sqlite3.Row) -> str:
    keys = {
        "asset_id",
        "kind",
        "package_name",
        "version",
        "publish_result",
        "visibility",
        "deduplicated",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise PublishStoreError("invalid_record")
    if (
        not isinstance(value["asset_id"], str)
        or not value["asset_id"]
        or len(value["asset_id"]) > 512
    ):
        raise PublishStoreError("invalid_record")
    for key in ("kind", "package_name", "version"):
        if value[key] != draft[key]:
            raise PublishStoreError("invalid_record")
    if draft["target_asset_id"] and value["asset_id"] != draft["target_asset_id"]:
        raise PublishStoreError("invalid_record")
    unconfirmed = value["visibility"] is None and value["publish_result"] in (
        "pending_moderation",
        "publish_success",
        "published",
        "publish_failed",
    )
    if (
        not unconfirmed
        and value["visibility"] != _request(draft["request_json"]).visibility
    ):
        raise PublishStoreError("invalid_record")
    state = value["publish_result"]
    if state is not None and (
        not isinstance(state, str) or len(state.encode("utf-8")) > 4096
    ):
        raise PublishStoreError("invalid_record")
    if not isinstance(value["deduplicated"], bool):
        raise PublishStoreError("invalid_record")
    return json.dumps(value)


class PublishStore:
    def __init__(self, path: Path, *, max_active_per_scope: int = 32):
        if type(max_active_per_scope) is not int or max_active_per_scope < 1:
            raise ValueError("max_active_per_scope must be positive")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._max_active = max_active_per_scope
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            path, timeout=30, isolation_level=None, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS publish_drafts (
                draft_id TEXT PRIMARY KEY, scope TEXT NOT NULL, local_id TEXT NOT NULL,
                kind TEXT NOT NULL, package_name TEXT NOT NULL, version TEXT NOT NULL,
                target_asset_id TEXT, target_key TEXT NOT NULL, request_json TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publish_operations (
                operation_id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                draft_id TEXT NOT NULL UNIQUE REFERENCES publish_drafts(draft_id),
                kind TEXT NOT NULL, target_key TEXT NOT NULL, version TEXT NOT NULL,
                execution_status TEXT NOT NULL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, result_json TEXT, error_json TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS publish_active_target
                ON publish_operations(scope, kind, target_key, version)
                WHERE execution_status IN ('queued', 'uploading', 'unknown');
            CREATE TABLE IF NOT EXISTS publish_requests (
                scope TEXT NOT NULL, request_id TEXT NOT NULL,
                operation_id TEXT NOT NULL REFERENCES publish_operations(operation_id),
                PRIMARY KEY(scope, request_id)
            );
            CREATE INDEX IF NOT EXISTS publish_draft_resource
                ON publish_drafts(scope, kind, local_id);
        """)

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    def save_draft(
        self,
        scope: str,
        local_id: str,
        request: PublishRequest,
        *,
        expires_at: float,
        now: float | None = None,
    ) -> str:
        _identifier(scope)
        _identifier(local_id)
        stamp = _timestamp(now)
        if _timestamp(expires_at) <= stamp:
            raise PublishStoreError("draft_expired")
        serialized = _request_json(request)
        identity = request.identity
        draft_id = uuid.uuid4().hex
        target_key = (
            ("id:" + identity.target_asset_id)
            if identity.target_asset_id
            else ("name:" + identity.package_name)
        )
        with self._transaction():
            self._db.execute(
                "INSERT INTO publish_drafts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    draft_id,
                    scope,
                    local_id,
                    identity.kind,
                    identity.package_name,
                    identity.version,
                    identity.target_asset_id,
                    target_key,
                    serialized,
                    stamp,
                    expires_at,
                ),
            )
        return draft_id

    def _draft(self, scope: str, draft_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM publish_drafts WHERE scope=? AND draft_id=?",
            (scope, draft_id),
        ).fetchone()
        if row is None:
            raise PublishStoreError("not_found")
        return row

    def get_draft(
        self, scope: str, draft_id: str, *, now: float | None = None
    ) -> PublishRequest:
        with self._lock:
            row = self._draft(scope, draft_id)
            if row["expires_at"] <= _timestamp(now):
                raise PublishStoreError("draft_expired")
            return _request(row["request_json"])

    def commit_draft(
        self, scope: str, draft_id: str, request_id: str, *, now: float | None = None
    ) -> tuple[str, bool]:
        _identifier(request_id)
        stamp = _timestamp(now)
        with self._transaction():
            draft = self._draft(scope, draft_id)
            alias = self._db.execute(
                """SELECT o.operation_id, o.draft_id FROM publish_requests r
                JOIN publish_operations o USING(operation_id) WHERE r.scope=? AND r.request_id=?""",
                (scope, request_id),
            ).fetchone()
            if alias is not None:
                if alias["draft_id"] != draft_id:
                    raise PublishStoreError("request_conflict")
                return alias["operation_id"], False
            existing = self._db.execute(
                "SELECT operation_id FROM publish_operations WHERE scope=? AND draft_id=?",
                (scope, draft_id),
            ).fetchone()
            if existing is not None:
                operation_id = existing["operation_id"]
                self._db.execute(
                    "INSERT INTO publish_requests VALUES (?,?,?)",
                    (scope, request_id, operation_id),
                )
                return operation_id, False
            if draft["expires_at"] <= stamp:
                raise PublishStoreError("draft_expired")
            conflict = self._db.execute(
                """SELECT 1 FROM publish_operations WHERE scope=? AND kind=?
                AND target_key=? AND version=? AND execution_status IN ('queued','uploading','unknown')""",
                (scope, draft["kind"], draft["target_key"], draft["version"]),
            ).fetchone()
            if conflict:
                raise PublishStoreError("target_conflict")
            active = self._db.execute(
                "SELECT COUNT(*) FROM publish_operations WHERE scope=? AND execution_status IN ('queued','uploading')",
                (scope,),
            ).fetchone()[0]
            if active >= self._max_active:
                raise PublishStoreError("queue_full")
            operation_id = uuid.uuid4().hex
            self._db.execute(
                "INSERT INTO publish_operations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    scope,
                    draft_id,
                    draft["kind"],
                    draft["target_key"],
                    draft["version"],
                    "queued",
                    stamp,
                    stamp,
                    None,
                    None,
                ),
            )
            self._db.execute(
                "INSERT INTO publish_requests VALUES (?,?,?)",
                (scope, request_id, operation_id),
            )
            return operation_id, True

    def _operation(self, scope: str, operation_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM publish_operations WHERE scope=? AND operation_id=?",
            (scope, operation_id),
        ).fetchone()
        if row is None:
            raise PublishStoreError("not_found")
        return row

    def operation_request(self, scope: str, operation_id: str) -> PublishRequest:
        """Read an owned committed snapshot for a worker, independent of draft TTL."""
        with self._lock:
            operation = self._operation(scope, operation_id)
            return _request(self._draft(scope, operation["draft_id"])["request_json"])

    def start(self, scope: str, operation_id: str) -> bool:
        with self._transaction():
            self._operation(scope, operation_id)
            cursor = self._db.execute(
                "UPDATE publish_operations SET execution_status='uploading', updated_at=? WHERE "
                "scope=? AND operation_id=? AND execution_status='queued'",
                (time.time(), scope, operation_id),
            )
            return cursor.rowcount == 1

    def finish(
        self,
        scope: str,
        operation_id: str,
        *,
        execution_status: str,
        result: dict | None = None,
        error: dict | None = None,
    ) -> None:
        if execution_status not in ("completed", "failed", "unknown"):
            raise PublishStoreError("invalid_transition")
        with self._transaction():
            op = self._operation(scope, operation_id)
            if op["execution_status"] not in ("queued", "uploading"):
                raise PublishStoreError("invalid_transition")
            if execution_status == "completed":
                if op["execution_status"] != "uploading" or error is not None:
                    raise PublishStoreError("invalid_transition")
                result_json = _safe_result(result, self._draft(scope, op["draft_id"]))
            else:
                if result is not None or error is None:
                    raise PublishStoreError("invalid_record")
                result_json = None
            error_json = _safe_error(error)
            self._db.execute(
                """UPDATE publish_operations SET execution_status=?, result_json=?,
                error_json=?, updated_at=? WHERE scope=? AND operation_id=?""",
                (
                    execution_status,
                    result_json,
                    error_json,
                    time.time(),
                    scope,
                    operation_id,
                ),
            )

    def _record(self, op: sqlite3.Row) -> dict:
        draft = self._draft(op["scope"], op["draft_id"])
        operation_fields = (
            "operation_id", "draft_id", "execution_status", "created_at", "updated_at"
        )
        draft_fields = ("kind", "local_id", "package_name", "version", "target_asset_id")
        result = {key: op[key] for key in operation_fields}
        result.update({key: draft[key] for key in draft_fields})
        result["result"] = (
            json.loads(op["result_json"]) if op["result_json"] is not None else None
        )
        result["error"] = (
            json.loads(op["error_json"]) if op["error_json"] is not None else None
        )
        return result

    def status(self, scope: str, operation_id: str) -> dict:
        with self._lock:
            return self._record(self._operation(scope, operation_id))

    def records(self, scope: str, kind: str, local_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """SELECT o.* FROM publish_operations o JOIN publish_drafts d USING(draft_id)
                WHERE o.scope=? AND d.kind=? AND d.local_id=? ORDER BY o.created_at DESC, o.rowid DESC""",
                (scope, kind, local_id),
            ).fetchall()
            return [self._record(row) for row in rows]

    def local_status(self, kind: str, local_id: str) -> str:
        """Workspace asset summary only; never expose account-scoped records or IDs."""
        with self._lock:
            rows = self._db.execute(
                """SELECT o.execution_status, o.result_json FROM publish_operations o
                JOIN publish_drafts d USING(draft_id)
                WHERE d.kind=? AND d.local_id=? ORDER BY o.created_at DESC, o.rowid DESC""",
                (kind, local_id),
            ).fetchall()
        completed = [
            json.loads(row["result_json"])
            for row in rows
            if row["execution_status"] == "completed" and row["result_json"]
        ]
        states = [item.get("publish_result") for item in completed]
        if any(state in ("published", "publish_success") for state in states):
            return "published"
        if "pending_moderation" in states:
            return "pending"
        all_completed = bool(rows) and all(
            row["execution_status"] == "completed" for row in rows
        )
        if all_completed and states and all(state == "publish_failed" for state in states):
            return "unpublished"
        return "unknown"

    def recover_interrupted(self) -> int:
        with self._transaction():
            count = 0
            for before, after in (("queued", "failed"), ("uploading", "unknown")):
                error = json.dumps(
                    {"code": "interrupted", "outcome_unknown": after == "unknown"}
                )
                count += self._db.execute(
                    "UPDATE publish_operations SET execution_status=?,error_json=?,updated_at=? WHERE "
                    "execution_status=?",
                    (after, error, time.time(), before),
                ).rowcount
            return count

    def cleanup_artifacts(
        self, *, now: float | None = None
    ) -> tuple[set[Path], set[Path]]:
        """Expire unsubmitted drafts; retain operation history without its ZIPs."""
        stamp = _timestamp(now)
        keep, remove = set(), set()
        with self._transaction():
            rows = self._db.execute(
                """SELECT d.*, o.execution_status FROM publish_drafts d
                LEFT JOIN publish_operations o USING(draft_id)"""
            ).fetchall()
            for row in rows:
                path = _request(row["request_json"]).artifact_path
                state = row["execution_status"]
                if state is None and row["expires_at"] <= stamp:
                    self._db.execute(
                        "DELETE FROM publish_drafts WHERE draft_id=?",
                        (row["draft_id"],),
                    )
                    remove.add(path)
                elif state in ("completed", "failed", "unknown"):
                    remove.add(path)
                else:
                    keep.add(path)
        return keep, remove - keep

    def pending_draft_count(self) -> int:
        with self._lock:
            return self._db.execute(
                """SELECT COUNT(*) FROM publish_drafts d LEFT JOIN publish_operations o
                USING(draft_id) WHERE o.operation_id IS NULL OR o.execution_status IN ('queued','uploading')"""
            ).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
