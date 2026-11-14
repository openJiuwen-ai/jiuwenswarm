"""Effective context snapshots bound atomically through the native checkpoint.

Immutable files are prepared first. Only the reference in Session state makes
one authoritative; the on-disk hint merely suppresses speculative full reads.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.runtime.session import lifecycle as lc
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.history_artifacts import json_chunks
from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayCursor, HistoryReplayError
from jiuwenswarm.server.runtime.session.history_stream import iter_history_rows

logger = logging.getLogger(__name__)
SNAPSHOT_KEY = "jiuwenswarm_context_snapshot_v1"
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_COVERED_REQUESTS = 8192
_NAME = re.compile(r"[0-9a-f]{16}-[0-9a-f]{32}\.json")


@dataclass(frozen=True)
class ContextSnapshot:
    contexts: dict[str, Any]
    cursor: HistoryReplayCursor
    covered_requests: set[str]
    voice_cursor: HistoryReplayCursor
    voice_ids: set[str]


class _ContextSnapshotLock:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.owner = None
        self.depth = 0

    async def __aenter__(self):
        task = asyncio.current_task()
        if self.owner is not task:
            await self.lock.acquire()
            self.owner = task
        self.depth += 1
        return self

    async def __aexit__(self, *_args):
        self.depth -= 1
        if self.depth == 0:
            self.owner = None
            self.lock.release()


def context_snapshot_lock(context: Any) -> _ContextSnapshotLock:
    lock = getattr(context, "history_snapshot_lock", None)
    if not isinstance(lock, _ContextSnapshotLock):
        lock = _ContextSnapshotLock()
        context.history_snapshot_lock = lock
    return lock


def snapshot_directory(session_id: str) -> Path:
    return get_agent_sessions_dir() / lc.validate_id(session_id) / "context_snapshots"


def has_snapshot_hint(session_id: str) -> bool:
    return (snapshot_directory(session_id) / "checkpoint-hint.json").exists()


def invalidate_snapshot_hint(session_id: str) -> None:
    (snapshot_directory(session_id) / "checkpoint-hint.json").unlink(missing_ok=True)


def _identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    # st_ctime_ns 不纳入：Windows 上 os.stat 与 os.fstat 对创建时间的回报
    # 不一致，纳入会使跨源比对随机失败（同 history_replay._identity，JW-41）。
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _anchor(path: Path, offset: int) -> str:
    digest = hashlib.sha256()
    if not path.exists() and offset == 0:
        return digest.hexdigest()
    with path.open("rb") as stream:
        digest.update(stream.read(min(offset, 65536)))
        stream.seek(max(0, offset - 65536))
        digest.update(stream.read(min(offset, 65536)))
    return digest.hexdigest()


def bind_recovery_boundary(context: Any, cursor: HistoryReplayCursor, covered: set[str] | None = None) -> None:
    context.history_recovery_cursor = cursor
    context.history_recovery_covered = set(covered or ())
    voice_cursor = getattr(context, "duplex_history_cursor", None)
    if cursor.path.suffix.lower() != ".jsonl":
        context.duplex_history_cursor = None
        return
    if voice_cursor is None:
        context.duplex_history_cursor = cursor
        return
    if (voice_cursor.path != cursor.path or voice_cursor.generation != cursor.generation
            or voice_cursor.offset < cursor.offset):
        context.duplex_history_cursor = cursor


def _cursor_payload(cursor: HistoryReplayCursor) -> dict[str, Any]:
    return {"file": cursor.path.name, "generation": cursor.generation, "identity": list(cursor.identity),
            "offset": cursor.offset, "anchor": _anchor(cursor.path, cursor.offset)}


def _read_cursor(session_id: str, value: dict[str, Any]) -> HistoryReplayCursor | None:
    from jiuwenswarm.server.runtime.session.session_history import get_read_history_path

    path = get_read_history_path(session_id)
    generation = int(lc.state("session", session_id).get("generation", 0))
    identity = _identity(path) if path.exists() else (0, 0, 0, 0)
    old_identity = value.get("identity")
    offset = value.get("offset")
    # 兼容两种历史格式：旧快照 identity 含 st_ctime_ns（5 元），新快照为
    # 4 元（去掉 ctime，见 _identity 注释）。ctime 槽位本就不参与下面的
    # 有效性判断（只用 [:2] 与 [2]=size），因此旧快照无需迁移。
    if not isinstance(old_identity, list) or len(old_identity) not in (4, 5):
        raise HistoryReplayError("Invalid context snapshot boundary")
    if any(type(item) is not int for item in old_identity):
        raise HistoryReplayError("Invalid context snapshot boundary")
    if type(offset) is not int or offset < 0 or offset > old_identity[2]:
        raise HistoryReplayError("Invalid context snapshot boundary")
    if value.get("file") != path.name or value.get("generation") != generation:
        return None
    if old_identity[2] and tuple(old_identity[:2]) != identity[:2]:
        return None
    if identity[2] < offset or _anchor(path, offset) != value.get("anchor"):
        return None
    return HistoryReplayCursor(path, generation, identity, offset)


def _write_snapshot(session_id: str, states: dict[str, Any], cursor: HistoryReplayCursor,
                    covered: set[str], owner: str, voice_cursor: HistoryReplayCursor | None,
                    voice_ids: set[str]) -> dict[str, Any]:
    with lc.resource_lock("session", session_id):
        lc.write_guard(session_id)
        if int(lc.state("session", session_id).get("generation", 0)) != cursor.generation:
            raise HistoryReplayError("Session changed before snapshot publication", code="HISTORY_RESTORE_CHANGED")
        if cursor.path.exists() and cursor.identity[2] and _identity(cursor.path)[:2] != cursor.identity[:2]:
            raise HistoryReplayError("History changed before snapshot publication", code="HISTORY_RESTORE_CHANGED")
        return _write_snapshot_unfenced(session_id, states, cursor, covered, owner, voice_cursor, voice_ids)


def _write_snapshot_unfenced(session_id: str, states: dict[str, Any], cursor: HistoryReplayCursor,
                             covered: set[str], owner: str, voice_cursor: HistoryReplayCursor | None,
                             voice_ids: set[str]) -> dict[str, Any]:
    directory = snapshot_directory(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    name = owner + "-" + uuid.uuid4().hex + ".json"
    path = directory / name
    temporary = path.with_suffix(".tmp")
    value = {"version": 1, "session_id": session_id, "source": _cursor_payload(cursor),
             "covered_requests": sorted(covered), "contexts": states, "voice_ids": sorted(voice_ids)}
    if voice_cursor is not None:
        value["voice_source"] = _cursor_payload(voice_cursor)
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as stream:
            for chunk in json_chunks(value):
                size += len(chunk)
                if size > MAX_SNAPSHOT_BYTES:
                    raise HistoryReplayError(
                        "Effective context exceeds the snapshot budget", code="HISTORY_RESTORE_LIMIT"
                    )
                stream.write(chunk)
                digest.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # This is an optimization hint, never a commit pointer.
        lc.atomic_json(directory / "checkpoint-hint.json", {"version": 1})
        return {"version": 1, "file": name, "sha256": digest.hexdigest(), "bytes": size}
    finally:
        temporary.unlink(missing_ok=True)


def _decode_contexts(session_id: str, states: Any) -> dict[str, Any]:
    from openjiuwen.core.foundation.llm import AssistantMessage, BaseMessage, SystemMessage, ToolMessage, UserMessage

    if not isinstance(states, dict) or not states or len(states) > 16:
        raise HistoryReplayError("Invalid saved context collection")
    types = {"user": UserMessage, "assistant": AssistantMessage, "tool": ToolMessage, "system": SystemMessage}
    result = {}
    for context_id, state in states.items():
        if not isinstance(context_id, str) or not isinstance(state, dict):
            raise HistoryReplayError("Invalid saved context")
        messages = state.get("messages")
        if not isinstance(messages, list) or len(messages) > 8192:
            raise HistoryReplayError("Snapshot contains invalid messages")
        _validate_native_offloads(state)
        restored = []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in types:
                raise HistoryReplayError("Invalid saved context message")
            restored.append(types.get(message["role"], BaseMessage).model_validate(message))
        offloads = {}
        for handle, items in (state.get("offload_messages") or {}).items():
            offloads[handle] = [types.get(item.get("role"), BaseMessage).model_validate(item) for item in items]
        result[context_id] = {**state, "messages": restored, "offload_messages": offloads}
    return result


def _validate_native_offloads(state: dict[str, Any]) -> None:
    # Existing Core filesystem offloads remain native. In-memory fallback data
    # is included in the same bounded snapshot instead of introducing a new
    # Core state format or allowing its raw payload to grow without a budget.
    if state.get("offload_directory") or state.get("offload_versions"):
        raise HistoryReplayError("This snapshot requires a different Core offload format")
    offloads = state.get("offload_messages") or {}
    limit_message = "Native offloads exceed the snapshot budget"
    if not isinstance(offloads, dict) or len(offloads) > 8192:
        raise HistoryReplayError(limit_message, code="HISTORY_RESTORE_LIMIT")
    total_messages = 0
    for handle, items in offloads.items():
        if not isinstance(handle, str) or not handle or len(handle) > 1024:
            raise HistoryReplayError(limit_message, code="HISTORY_RESTORE_LIMIT")
        if not isinstance(items, list) or len(items) > 8192:
            raise HistoryReplayError(limit_message, code="HISTORY_RESTORE_LIMIT")
        total_messages += len(items)
    if total_messages > 8192:
        raise HistoryReplayError(limit_message, code="HISTORY_RESTORE_LIMIT")


def load_checkpoint_snapshot(session_id: str, reference: Any) -> ContextSnapshot | None:
    """Invalid active snapshots fail closed; stale history revisions use migration."""
    if not reference:
        return None
    try:
        if not isinstance(reference, dict) or reference.get("version") != 1:
            raise ValueError("invalid reference")
        if not isinstance(reference.get("file"), str) or _NAME.fullmatch(reference["file"]) is None:
            raise ValueError("invalid reference")
        if type(reference.get("bytes")) is not int or not 0 < reference["bytes"] <= MAX_SNAPSHOT_BYTES:
            raise ValueError("invalid reference")
        with lc.resource_lock("session", session_id):
            lc.guard(session_id)
            path = snapshot_directory(session_id) / reference["file"]
            with path.open("rb") as stream:
                raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
            if len(raw) != reference["bytes"] or hashlib.sha256(raw).hexdigest() != reference.get("sha256"):
                raise ValueError("snapshot checksum mismatch")
            value = json.loads(raw)
            if value.get("version") != 1 or value.get("session_id") != session_id:
                raise ValueError("snapshot identity mismatch")
            cursor = _read_cursor(session_id, value["source"])
            if cursor is None:
                return None
            covered = value.get("covered_requests")
            if (not isinstance(covered, list) or len(covered) > MAX_COVERED_REQUESTS
                    or any(not isinstance(item, str) or not item or len(item) > 256 for item in covered)):
                raise ValueError("invalid covered request set")
            voice_cursor = _read_cursor(session_id, value["voice_source"]) if value.get("voice_source") else cursor
            voice_ids = value.get("voice_ids", [])
            if (not isinstance(voice_ids, list) or len(voice_ids) > 8192
                    or any(not isinstance(item, str) or len(item) > 512 for item in voice_ids)):
                raise ValueError("invalid voice identities")
            return ContextSnapshot(_decode_contexts(session_id, value["contexts"]), cursor, set(covered),
                                   voice_cursor or cursor, set(voice_ids))
    except MemoryError as exc:
        raise HistoryReplayError("Insufficient memory for the saved context", code="HISTORY_RESTORE_RESOURCE") from exc
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise HistoryReplayError("The committed context snapshot is missing or invalid") from exc


def _advance_boundary(session_id: str, cursor: HistoryReplayCursor | None,
                      covered: set[str], voice_cursor: HistoryReplayCursor | None,
                      voice_ids: set[str]) -> HistoryReplayCursor:
    from jiuwenswarm.common.schema.message import SESSION_REQUEST_COMPLETED_EVENT
    from jiuwenswarm.server.runtime.session.session_history import get_read_history_path

    with lc.resource_lock("session", session_id):
        lc.write_guard(session_id)
        path = get_read_history_path(session_id)
        generation = int(lc.state("session", session_id).get("generation", 0))
        identity = _identity(path) if path.exists() else (0, 0, 0, 0)
        start = cursor.offset if cursor is not None else 0
        if cursor is not None:
            if cursor.path != path or cursor.generation != generation:
                raise HistoryReplayError(
                    "History revision changed before snapshot publication", code="HISTORY_RESTORE_CHANGED"
                )
            if ((cursor.identity[2] and cursor.identity[:2] != identity[:2]) or identity[2] < start):
                if path.suffix.lower() == ".jsonl":
                    raise HistoryReplayError(
                        "History revision changed before snapshot publication", code="HISTORY_RESTORE_CHANGED"
                    )
                start = 0  # Legacy JSON writers atomically replace the array on append.
        offset = start
        if path.exists():
            for row in iter_history_rows(path, session_id=session_id, start=start, end=identity[2], metadata_only=True):
                record = row.record
                voice_consumed = (record.get("channel_id") == "video_duplex" and (
                    (voice_cursor is not None and voice_cursor.path == path and row.end <= voice_cursor.offset)
                    or (record.get("id") or record.get("request_id")) in voice_ids))
                if (record.get("role") in {"user", "human"} and record.get("request_id") not in covered
                        and not voice_consumed):
                    offset = row.start
                    break
                offset = row.end
                if record.get("event_type") == SESSION_REQUEST_COMPLETED_EVENT:
                    covered.discard(str(record.get("request_id") or ""))
        return HistoryReplayCursor(path, generation, identity, offset)


def _prune_snapshots(session_id: str, current: dict, previous: Any) -> None:
    if not isinstance(current, dict) or not isinstance(current.get("file"), str):
        return
    directory = snapshot_directory(session_id)
    owner = current["file"].split("-", 1)[0]
    keep = {current["file"]}
    if isinstance(previous, dict):
        keep.add(previous.get("file"))
    candidates = sorted(directory.glob(owner + "-*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    keep.update(path.name for path in candidates[:4])
    for path in candidates:
        if path.name not in keep and _NAME.fullmatch(path.name):
            path.unlink(missing_ok=True)


async def save_context_snapshot(*, context_engine: Any, session: Any,
                                 consumed_request_ids: set[str] | None = None) -> bool:
    """Commit the actual window and its consumed boundary with native HITL state."""
    session_id = str(session.get_session_id())
    context = context_engine.get_context(session_id=session_id)
    if context is None:
        return False
    from jiuwenswarm.runtime.context_checkpointer import (
        bind_context_checkpoint, context_checkpoint_lock, prepared_context_checkpoint,
    )

    binding = bind_context_checkpoint(context_engine, session)
    async with context_checkpoint_lock(binding), context_snapshot_lock(context):
        covered = set(getattr(context, "history_recovery_covered", ()))
        covered.update(consumed_request_ids or ())
        context.history_recovery_covered = covered
        previous_reference = session.get_state(SNAPSHOT_KEY)
        previous_context = session.get_state("context")
        try:
            states = await context_engine.save_contexts(session)
            states = await prepare_context_snapshot(context_engine=context_engine, session=session, states=states)
            session.update_state({"context": None})
            session.update_state({"context": states})
            with prepared_context_checkpoint(binding):
                await session.commit()
        except BaseException:
            session.update_state({SNAPSHOT_KEY: None, "context": None})
            session.update_state({SNAPSHOT_KEY: previous_reference, "context": previous_context})
            raise
        reference = session.get_state(SNAPSHOT_KEY)
        try:
            await run_history_io(_prune_snapshots, session_id, reference, previous_reference)
        except (OSError, TypeError, KeyError):
            logger.warning("Snapshot cleanup failed for %s", session_id, exc_info=True)
        return True


async def prepare_context_snapshot(*, context_engine: Any, session: Any, states: dict[str, Any]) -> dict[str, Any]:
    """Host preparation before delegating the native Checkpointer commit."""
    session_id = str(session.get_session_id())
    context = context_engine.get_context(session_id=session_id)
    if not states:
        previous = session.get_state("context")
        if isinstance(previous, dict) and previous:
            return previous
        reference = session.get_state(SNAPSHOT_KEY)
        if reference:
            loaded = await run_history_io(load_checkpoint_snapshot, session_id, reference)
            if loaded is not None:
                return loaded.contexts
            session.update_state({SNAPSHOT_KEY: None})
        return states
    if context is None:
        session.update_state({SNAPSHOT_KEY: None})
        return states
    try:
        async with context_snapshot_lock(context):
            return await _prepare_context_snapshot_locked(context_engine=context_engine, session=session,
                                                           context=context, states=states)
    except HistoryReplayError:
        raise
    except MemoryError as exc:
        raise HistoryReplayError(
            "Insufficient memory to prepare context persistence", code="HISTORY_RESTORE_RESOURCE"
        ) from exc
    except Exception as exc:
        raise HistoryReplayError("Context persistence preparation failed") from exc


async def _prepare_context_snapshot_locked(*, context_engine: Any, session: Any, context: Any,
                                           states: dict[str, Any]) -> dict[str, Any]:
    if len(states) > 16:
        raise HistoryReplayError("Context count exceeds the snapshot budget", code="HISTORY_RESTORE_LIMIT")
    session_id = str(session.get_session_id())
    agent_id = session.get_agent_id() if callable(getattr(session, "get_agent_id", None)) else "default"
    owner = hashlib.sha256(str(agent_id).encode("utf-8")).hexdigest()[:16]
    for context_id in states:
        if (not isinstance(states[context_id].get("messages"), list)
                or len(states[context_id]["messages"]) > 8192):
            raise HistoryReplayError(
                "Effective message count exceeds the snapshot budget", code="HISTORY_RESTORE_LIMIT"
            )
        _validate_native_offloads(states[context_id])
    cursor = getattr(context, "history_recovery_cursor", getattr(context, "duplex_history_cursor", None))
    voice_cursor = getattr(context, "duplex_history_cursor", None)
    covered = set(getattr(context, "history_recovery_covered", ()))
    remembered = set(getattr(context, "history_recovery_recorded_ids", ()))
    current_ids = set()
    voice_ids = set()
    for state in states.values():
        saved_messages = []
        for message in state.get("messages", ()):
            metadata = getattr(message, "metadata", None) or {}
            if metadata.get("duplex_history_id"):
                voice_ids.add(metadata["duplex_history_id"])
            ids = metadata.get("jiuwenswarm_request_ids")
            if ids:
                if (not isinstance(ids, list) or len(ids) > MAX_COVERED_REQUESTS
                        or any(not isinstance(item, str) or not item or len(item) > 256 for item in ids)):
                    raise HistoryReplayError("Invalid consumed request metadata")
                current_ids.update(ids)
                if not metadata.get("jiuwenswarm_recovery_recorded"):
                    covered.update(set(ids) - remembered)
                    message = message.model_copy(
                        update={"metadata": {**metadata, "jiuwenswarm_recovery_recorded": True}}
                    )
            saved_messages.append(message)
        state["messages"] = saved_messages
    next_cursor = await run_history_io(_advance_boundary, session_id, cursor, covered, voice_cursor, voice_ids)
    if len(covered) > MAX_COVERED_REQUESTS:
        raise HistoryReplayError("Too many consumed requests await a durable boundary", code="HISTORY_RESTORE_LIMIT")
    # The current window is the sole source; audit summaries are never substituted.
    if voice_cursor is None or voice_cursor.offset < next_cursor.offset:
        voice_cursor = next_cursor
    for identity in getattr(context, "duplex_history_ids", ()):
        if isinstance(identity, str):
            voice_ids.add(identity)
        if len(voice_ids) > 8192:
            raise HistoryReplayError("Voice identities exceed the snapshot budget", code="HISTORY_RESTORE_LIMIT")
    reference = await run_history_io(_write_snapshot, session_id, states, next_cursor,
                                     covered, owner, voice_cursor, voice_ids)
    session.update_state({SNAPSHOT_KEY: None})
    session.update_state({SNAPSHOT_KEY: reference})
    bind_recovery_boundary(context, next_cursor, covered)
    context.history_recovery_recorded_ids = current_ids
    return states


async def restore_checkpoint_context(*, context_engine: Any, session: Any, processors: Any,
                                      before_request_id: str | None) -> bool:
    from jiuwenswarm.server.runtime.session.recovery_budget import admit_context_recovery

    try:
        async with admit_context_recovery():
            return await _restore_checkpoint_context(context_engine=context_engine, session=session,
                                                      processors=processors, before_request_id=before_request_id)
    except HistoryReplayError:
        raise
    except MemoryError as exc:
        raise HistoryReplayError("Insufficient memory for context restore", code="HISTORY_RESTORE_RESOURCE") from exc
    except Exception as exc:
        raise HistoryReplayError("The committed context could not be restored") from exc


async def _restore_checkpoint_context(*, context_engine: Any, session: Any, processors: Any,
                                       before_request_id: str | None) -> bool:
    """Restore the committed window and only its unconsumed durable tail."""
    session_id = str(session.get_session_id())
    reference = session.get_state(SNAPSHOT_KEY)
    if not isinstance(reference, dict):
        return False
    loaded = await run_history_io(load_checkpoint_snapshot, session_id, reference)
    if loaded is None:
        # A rewritten/rewound source cannot leave the old SDK window as the
        # fallback for a now-empty history prefix.
        await context_engine.clear_context(session_id=session_id)
        session.update_state({"context": None, SNAPSHOT_KEY: None})
        return False
    states, cursor, covered, voice_cursor = loaded.contexts, loaded.cursor, loaded.covered_requests, loaded.voice_cursor
    from jiuwenswarm.agents.harness.common.session_ops_service import _build_context_messages_from_history

    records = []
    saved_voice_ids = set(loaded.voice_ids) | {(getattr(message, "metadata", None) or {}).get("duplex_history_id")
                       for state in states.values() for message in state.get("messages", ())}
    retained = 0
    end = cursor.identity[2]
    prefix = end
    if cursor.path.exists() and cursor.offset < end:
        def read_tail() -> list[dict[str, Any]]:
            nonlocal retained, prefix
            result = []
            for row in iter_history_rows(cursor.path, session_id=session_id, start=cursor.offset, end=end):
                record = row.record
                rid = str(record.get("request_id") or "")
                if before_request_id and rid == before_request_id and record.get("role") in {"user", "human"}:
                    prefix = row.start
                    break
                if record.get("channel_id") == "video_duplex" and (
                    row.end <= voice_cursor.offset or (record.get("id") or record.get("request_id")) in saved_voice_ids
                ):
                    continue
                if rid in covered or record.get("event_type") == "context.compact_summary":
                    continue
                from jiuwenswarm.server.runtime.session.history_replay import is_context_record
                if is_context_record(record):
                    retained += len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
                    if retained > 4 * 1024 * 1024 or len(result) >= 2048:
                        raise HistoryReplayError(
                            "Incremental context tail needs bounded migration", code="HISTORY_RESTORE_LIMIT"
                        )
                    result.append(record)
            return result
        try:
            records = await run_history_io(read_tail)
        except HistoryReplayError as exc:
            if exc.code != "HISTORY_RESTORE_LIMIT":
                raise
            from jiuwenswarm.server.runtime.session.context_migration import migrate_history_context

            return await migrate_history_context(
                context_engine=context_engine, session=session, processors=processors,
                before_request_id=before_request_id, base_states=states, base_cursor=cursor,
                covered_requests=covered,
            )
    additions, _ = _build_context_messages_from_history(records)
    context_id = next(iter(states))
    states[context_id]["messages"] = list(states[context_id]["messages"]) + additions
    previous = session.get_state("context")
    try:
        session.update_state({"context": None})
        session.update_state({"context": states})
        await context_engine.create_context(session=session, processors=processors)
        context = context_engine.get_context(session_id=session_id)
        if context is None:
            raise HistoryReplayError("Core did not install the committed context")
        next_cursor = HistoryReplayCursor(cursor.path, cursor.generation, cursor.identity, prefix)
        bind_recovery_boundary(context, next_cursor, covered)
        if cursor.path.suffix.lower() == ".jsonl":
            context.duplex_history_cursor = voice_cursor if voice_cursor.offset > next_cursor.offset else next_cursor
        else:
            context.duplex_history_cursor = None
        context.duplex_history_ids = saved_voice_ids | {
            (getattr(message, "metadata", None) or {}).get("duplex_history_id")
            for message in context.get_messages()
        }
        return True
    except BaseException:
        await context_engine.clear_context(session_id=session_id)
        session.update_state({"context": None})
        session.update_state({"context": previous})
        raise
