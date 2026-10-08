"""Append-only, hash-chained evidence for eternal conversations."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def jsonable(value: Any) -> Any:
    """Convert framework objects without interpreting or summarizing them."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value):
        return jsonable(dataclasses.asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return jsonable(model_dump(mode="json"))
        except TypeError:
            return jsonable(model_dump())
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        public = {key: item for key, item in data.items() if not key.startswith("_")}
        if public:
            return jsonable(public)
    return str(value)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(jsonable(value), ensure_ascii=False, indent=2, sort_keys=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


_AGENT_HISTORY_INLINE_LIMIT = 256 * 1024
_RAW_HISTORY_INLINE_LIMIT = 256 * 1024

# Adapter recreation can briefly construct more than one EvidenceWriter for
# the same durable Session. An instance-local asyncio.Lock cannot serialize
# those writers, so cursor allocation is also protected by feature path.
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}


def _path_lock(root: Path) -> threading.Lock:
    key = str(root.resolve())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.Lock())


def _encoded_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _blob_reference(root: Path, value: Any, *, inline_limit: int) -> Any:
    """Store one large value by digest without changing its JSON semantics."""
    encoded = _encoded_json(value)
    if len(encoded) <= inline_limit:
        return value
    digest = hashlib.sha256(encoded).hexdigest()
    relative = Path("blobs") / f"{digest}.json"
    blob_path = root / relative
    if not blob_path.exists():
        write_json_atomic(blob_path, value)
    return {
        "$evidence_blob": relative.as_posix(),
        "bytes": len(encoded),
        "encoding": "json/utf-8",
        "sha256": digest,
    }


def resolve_evidence_blobs(value: Any, root: Path) -> Any:
    """Resolve and verify content-addressed evidence references recursively."""
    if isinstance(value, dict) and "$evidence_blob" in value:
        relative = Path(str(value["$evidence_blob"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("evidence blob path escapes its history root")
        resolved = read_json(root / relative)
        encoded = _encoded_json(resolved)
        if len(encoded) != int(value.get("bytes") or -1):
            raise RuntimeError("evidence blob byte count mismatch")
        if hashlib.sha256(encoded).hexdigest() != str(value.get("sha256") or ""):
            raise RuntimeError("evidence blob digest mismatch")
        return resolved
    if isinstance(value, dict):
        return {key: resolve_evidence_blobs(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_evidence_blobs(item, root) for item in value]
    return value


def _search_view_event(event: dict[str, Any], logical_payload: Any) -> dict[str, Any]:
    """Create the non-semantic, grep-friendly projection of one Raw event."""
    search_payload = logical_payload
    if isinstance(logical_payload, dict):
        search_payload = dict(logical_payload)
        if event.get("type") == "model-visible-envelope":
            search_payload.pop("messages", None)
            search_payload.pop("tools", None)
        elif event.get("type") == "context-replaced":
            replaced = search_payload.pop("replaced_messages", None)
            search_payload["replaced_message_count"] = (
                len(replaced) if isinstance(replaced, list) else 0
            )
    return {
        "cursor": event.get("cursor"),
        "type": event.get("type"),
        "session_id": event.get("session_id"),
        "task_id": event.get("task_id"),
        "created_at": event.get("created_at"),
        "hash": event.get("hash"),
        "payload": search_payload,
    }


def _append_agent_history(root: Path, role: str, payload: Any) -> None:
    """Append a complete, reconstructable background-Agent history record.

    Extractor inputs can legitimately contain a large frozen evidence batch.
    Repeating that batch inline for every retry makes the audit log grow
    quadratically.  Large top-level values are therefore stored once as
    content-addressed JSON blobs.  This is structural deduplication only: the
    history record carries the digest, byte count and relative path needed to
    reconstruct the exact model input without semantic summarisation.
    """
    record = jsonable(payload)
    if not isinstance(record, dict):
        record = {"value": record}
    history_root = root / "agent-history" / role
    compact: dict[str, Any] = {}
    for key, value in record.items():
        compact[key] = _blob_reference(
            history_root,
            value,
            inline_limit=_AGENT_HISTORY_INLINE_LIMIT,
        )
    append_jsonl(
        history_root / "conversation.jsonl",
        {"created_at": utc_now(), **compact},
    )


class EvidenceWriter:
    """The sole cursor authority for one session-scoped feature directory."""

    def __init__(self, root: Path, session_id: str) -> None:
        self.root = root
        self.session_id = session_id
        self.raw_path = root / "raw-history" / "events.jsonl"
        self.state_path = root / "state" / "evidence.json"
        self._lock = asyncio.Lock()
        state = read_json(self.state_path, {}) or {}
        self._cursor, self._last_hash = self._recover_durable_tail(state)

    def _recover_durable_tail(self, state: dict[str, Any]) -> tuple[int, str]:
        """Validate the WAL and recover a state/mirror tail after a crash.

        Raw History is authoritative.  It is written before both the foreground
        mirror and the small cursor state file, so either derived file may lag
        by one or more complete JSONL records when a process is terminated.
        Semantic repair is deliberately forbidden here: only byte-equivalent
        records from the already durable Raw History are replayed.
        """
        events: list[dict[str, Any]] = []
        if self.raw_path.exists():
            with self.raw_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"corrupt Raw History JSON at line {line_number}"
                        ) from exc
                    expected_cursor = len(events) + 1
                    if int(event.get("cursor") or 0) != expected_cursor:
                        raise RuntimeError(
                            f"non-monotonic Raw History cursor at line {line_number}"
                        )
                    if str(event.get("session_id") or "") != self.session_id:
                        raise RuntimeError(
                            f"Raw History session mismatch at line {line_number}"
                        )
                    previous_hash = events[-1]["hash"] if events else None
                    if event.get("previous_hash") != previous_hash:
                        raise RuntimeError(
                            f"broken Raw History hash chain at line {line_number}"
                        )
                    claimed_hash = str(event.get("hash") or "")
                    digest_value = dict(event)
                    digest_value.pop("hash", None)
                    digest_source = json.dumps(
                        digest_value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    actual_hash = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
                    if claimed_hash != actual_hash:
                        raise RuntimeError(
                            f"invalid Raw History hash at line {line_number}"
                        )
                    events.append(event)

        durable_cursor = len(events)
        durable_hash = str(events[-1]["hash"]) if events else ""
        state_cursor = int(state.get("cursor") or 0)
        state_hash = str(state.get("last_hash") or "")
        if state_cursor > durable_cursor:
            raise RuntimeError("evidence cursor state is ahead of Raw History")
        if state_cursor:
            expected_state_hash = str(events[state_cursor - 1]["hash"])
            if state_hash != expected_state_hash:
                raise RuntimeError("evidence cursor state disagrees with Raw History")

        mirror_path = self.root / "agent-history" / "foreground" / "conversation.jsonl"
        mirrored: list[dict[str, Any]] = []
        if mirror_path.exists():
            with mirror_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        mirrored.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"corrupt foreground history JSON at line {line_number}"
                        ) from exc
        if len(mirrored) > len(events) or mirrored != events[: len(mirrored)]:
            raise RuntimeError("foreground history is not a prefix of Raw History")
        for event in events[len(mirrored):]:
            append_jsonl(mirror_path, event)

        search_path = self.root / "raw-history" / "search.jsonl"
        searchable: list[dict[str, Any]] = []
        if search_path.exists():
            with search_path.open("r", encoding="utf-8") as handle:
                searchable = [json.loads(line) for line in handle if line.strip()]
        if len(searchable) > len(events):
            raise RuntimeError("Raw History search view is ahead of canonical evidence")
        for index, row in enumerate(searchable):
            event = events[index]
            if row.get("cursor") != event.get("cursor") or row.get("hash") != event.get("hash"):
                raise RuntimeError("Raw History search view disagrees with canonical evidence")
        for event in events[len(searchable):]:
            logical_payload = resolve_evidence_blobs(
                event.get("payload"),
                self.root / "raw-history",
            )
            append_jsonl(search_path, _search_view_event(event, logical_payload))

        if state_cursor != durable_cursor or state_hash != durable_hash:
            write_json_atomic(
                self.state_path,
                {"cursor": durable_cursor, "last_hash": durable_hash},
            )
        return durable_cursor, durable_hash

    @property
    def cursor(self) -> int:
        return self._cursor

    async def append(
        self,
        event_type: str,
        payload: Any,
        *,
        task_id: str | None = None,
        mirror_role: str | None = "foreground",
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._append_sync,
                event_type,
                payload,
                task_id,
                mirror_role,
            )

    def _append_sync(
        self,
        event_type: str,
        payload: Any,
        task_id: str | None,
        mirror_role: str | None,
    ) -> dict[str, Any]:
        with _path_lock(self.root):
            state = read_json(self.state_path, {}) or {}
            state_cursor = int(state.get("cursor") or 0)
            state_hash = str(state.get("last_hash") or "")
            if state_cursor < self._cursor:
                raise RuntimeError("evidence cursor state moved backwards")
            if state_cursor > self._cursor:
                self._cursor = state_cursor
                self._last_hash = state_hash
            return self._append_sync_locked(event_type, payload, task_id, mirror_role)

    def _append_sync_locked(
        self,
        event_type: str,
        payload: Any,
        task_id: str | None,
        mirror_role: str | None,
    ) -> dict[str, Any]:
        cursor = self._cursor + 1
        raw_root = self.root / "raw-history"
        logical_payload = jsonable(payload)
        if isinstance(logical_payload, dict):
            durable_payload = {
                key: _blob_reference(
                    raw_root,
                    value,
                    inline_limit=_RAW_HISTORY_INLINE_LIMIT,
                )
                for key, value in logical_payload.items()
            }
        else:
            durable_payload = _blob_reference(
                raw_root,
                logical_payload,
                inline_limit=_RAW_HISTORY_INLINE_LIMIT,
            )
        event = {
            "cursor": cursor,
            "type": event_type,
            "session_id": self.session_id,
            "task_id": task_id,
            "created_at": utc_now(),
            "previous_hash": self._last_hash or None,
            "payload": durable_payload,
        }
        digest_source = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        event_hash = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        event["hash"] = event_hash

        # Raw History is the write-ahead evidence layer. A crash after this
        # append can leave an uncovered tail, but can never publish memory that
        # refers to evidence which was not durably recorded first.
        append_jsonl(self.raw_path, event)
        if mirror_role:
            append_jsonl(
                self.root / "agent-history" / mirror_role / "conversation.jsonl",
                event,
            )
        # A task-local structural mirror makes acceptance/audit reads bounded.
        # It contains the exact hash-chained event (including blob references),
        # so it never becomes a second semantic source of truth.
        if task_id:
            task_digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
            append_jsonl(raw_root / "tasks" / f"{task_digest}.jsonl", event)

        # The reference prompt requires direct evidence lookup.  This compact
        # view removes only fields that repeat the complete accumulated model
        # context; direct user messages, tool evidence and model responses stay
        # verbatim and retain their canonical cursor/hash backlink.
        append_jsonl(raw_root / "search.jsonl", _search_view_event(event, logical_payload))
        write_json_atomic(self.state_path, {"cursor": cursor, "last_hash": event_hash})
        self._cursor = cursor
        self._last_hash = event_hash
        return event

    async def append_agent_history(self, role: str, payload: Any) -> None:
        await asyncio.to_thread(
            _append_agent_history,
            self.root,
            role,
            payload,
        )

    async def append_audit(self, name: str, payload: Any) -> None:
        await asyncio.to_thread(
            append_jsonl,
            self.root / "audit" / f"{name}.jsonl",
            {"created_at": utc_now(), **jsonable(payload)},
        )


__all__ = [
    "EvidenceWriter",
    "append_jsonl",
    "jsonable",
    "read_json",
    "resolve_evidence_blobs",
    "utc_now",
    "write_json_atomic",
]
