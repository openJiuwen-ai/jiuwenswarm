"""Bounded legacy replay with real native compression and all-or-nothing install."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from jiuwenswarm.server.runtime.session import lifecycle as lc
from jiuwenswarm.server.runtime.session.context_snapshot import bind_recovery_boundary, save_context_snapshot
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayCursor, HistoryReplayError, is_context_record
from jiuwenswarm.server.runtime.session.history_stream import iter_history_rows

logger = logging.getLogger(__name__)
_COMPRESS_AT_BYTES = 512 * 1024
_MAX_CONTEXT_BYTES = 4 * 1024 * 1024


def _message_bytes(context: Any) -> int:
    total = 0
    for message in context.get_messages():
        total += len(message.model_dump_json().encode("utf-8"))
        if total > _MAX_CONTEXT_BYTES:
            break
    return total


def _next_page(iterator, pending: dict[str, Any], before_request_id: str | None, covered: set[str]):
    records = []
    size = 0
    open_calls: set[str] = set()
    for row in iterator:
        record = row.record
        if (before_request_id and record.get("request_id") == before_request_id
                and record.get("role") in {"user", "human"}):
            pending.update(offset=row.start, done=True)
            break
        pending["offset"] = row.end
        if str(record.get("request_id") or "") in covered:
            continue
        if not is_context_record(record):
            continue
        if record.get("event_type") == "context.compact_summary":
            # Auto summaries are audit projections, not effective windows. The
            # original messages are still present and get compressed once here.
            metadata = record.get("compact_metadata") or {}
            if metadata.get("trigger") != "manual_rewind":
                pending["had_compaction"] = True
                continue
        call = record.get("tool_call")
        if record.get("event_type") == "chat.tool_call" and isinstance(call, dict):
            call_id = call.get("tool_call_id") or call.get("id")
            if call_id:
                open_calls.add(str(call_id))
        if record.get("event_type") == "chat.tool_result":
            open_calls.discard(str(record.get("tool_call_id") or ""))
        records.append(record)
        size += len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
        if size > _MAX_CONTEXT_BYTES or len(records) > 2048:
            raise HistoryReplayError("A legacy tool group exceeds the migration budget", code="HISTORY_RESTORE_LIMIT")
        if size >= _COMPRESS_AT_BYTES and not open_calls:
            return records
    else:
        pending["done"] = True
    if open_calls:
        raise HistoryReplayError("Legacy history has an unfinished tool group; original data was preserved")
    return records


async def migrate_history_context(*, context_engine: Any, session: Any, processors: Any,
                                   before_request_id: str | None, base_states: dict | None = None,
                                   base_cursor: HistoryReplayCursor | None = None,
                                   covered_requests: set[str] | None = None) -> bool:
    from jiuwenswarm.server.runtime.session.recovery_budget import admit_context_recovery

    async with admit_context_recovery():
        return await _migrate_history_context(
            context_engine=context_engine, session=session, processors=processors,
            before_request_id=before_request_id, base_states=base_states,
            base_cursor=base_cursor, covered_requests=covered_requests,
        )


async def _migrate_history_context(*, context_engine: Any, session: Any, processors: Any,
                                    before_request_id: str | None, base_states: dict | None = None,
                                    base_cursor: HistoryReplayCursor | None = None,
                                    covered_requests: set[str] | None = None) -> bool:
    """Stream legacy data; publish a checkpoint only after complete migration.

    Compression uses the configured native processors/model. Failure preserves
    the prior checkpoint and original audit file, and rejects the new request.
    """
    from jiuwenswarm.agents.harness.common.session_ops_service import _build_context_messages_from_history
    from jiuwenswarm.server.runtime.session.session_history import get_read_history_path

    session_id = str(session.get_session_id())
    with lc.resource_lock("session", session_id):
        lc.guard(session_id)
        path = get_read_history_path(session_id)
        if not path.exists():
            return False
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        generation = int(lc.state("session", session_id).get("generation", 0))
    previous_context = context_engine.get_context(session_id=session_id)
    previous_state = previous_context.save_state() if previous_context is not None else None
    previous_session_context = session.get_state("context")
    start = base_cursor.offset if base_cursor is not None else 0
    covered = set(covered_requests or ())
    iterator = iter_history_rows(path, session_id=session_id, start=start, end=identity[2])
    pending = {"offset": start, "done": False, "had_compaction": False}
    migration_processors = []
    for key, config in processors or ():
        if key == "RoundLevelCompressor":
            if callable(getattr(config, "model_copy", None)):
                config = config.model_copy(update={"keep_recent_messages": 0, "min_target_context_ratio": 0.0})
            else:
                config = {**config, "keep_recent_messages": 0, "min_target_context_ratio": 0.0}
            migration_processors.append((key, config))
    count = 0
    compactions = 0

    async def rollback() -> None:
        await context_engine.clear_context(session_id=session_id)
        session.update_state({"context": None})
        session.update_state({"context": previous_session_context})
        if previous_state is not None:
            restored = await context_engine.create_context(session=session, processors=processors)
            restored.load_state({restored.context_id(): previous_state})

    try:
        if previous_context is not None:
            await context_engine.clear_context(session_id=session_id)
        session.update_state({"context": None})
        if base_states:
            session.update_state({"context": base_states})
        context = await context_engine.create_context(session=session, processors=migration_processors)
        while not pending["done"]:
            records = await run_history_io(_next_page, iterator, pending, before_request_id, covered)
            if not records:
                continue
            messages, _ = _build_context_messages_from_history(records)
            count += len(records)
            context.set_messages(list(context.get_messages()) + messages)
            if _message_bytes(context) >= _COMPRESS_AT_BYTES:
                result = await context.compress_context(compression_trigger="history_migration")
                code = result.get("result") if isinstance(result, dict) else result
                compactions += int(code == "compressed")
                if code == "busy" or _message_bytes(context) > _MAX_CONTEXT_BYTES:
                    raise HistoryReplayError("Native compression could not bound the migrated context",
                                             code="HISTORY_RESTORE_LIMIT")
        if pending["had_compaction"] and context.get_messages():
            result = await context.compress_context(compression_trigger="history_migration")
            code = result.get("result") if isinstance(result, dict) else result
            compactions += int(code == "compressed")
        # Append-only growth is valid. Replacement, truncation or lifecycle
        # changes invalidate the complete candidate before it can be published.
        with lc.resource_lock("session", session_id):
            lc.guard(session_id)
            latest = path.stat()
            if get_read_history_path(session_id) != path or (latest.st_dev, latest.st_ino) != identity[:2]:
                raise HistoryReplayError("History changed during migration", code="HISTORY_RESTORE_CHANGED")
            if (latest.st_size < identity[2]
                    or int(lc.state("session", session_id).get("generation", 0)) != generation):
                raise HistoryReplayError("History changed during migration", code="HISTORY_RESTORE_CHANGED")
        if not context.get_messages():
            if isinstance(previous_session_context, dict) and previous_session_context:
                # Some legacy installations retained only diagnostic history
                # while the native checkpoint still contains the real window.
                await context_engine.clear_context(session_id=session_id)
                session.update_state({"context": None})
                session.update_state({"context": previous_session_context})
                context = await context_engine.create_context(session=session, processors=processors)
            if not context.get_messages():
                if pending["had_compaction"]:
                    raise HistoryReplayError(
                        "Only audit summaries remain; a verified native window or original history is required"
                    )
                await rollback()
                return False
        # Only the temporary migration context uses zero protected tail; the
        # live agent resumes with its original processor policy and tool pairs.
        migrated_state = context.save_state()
        await context_engine.clear_context(session_id=session_id)
        session.update_state({"context": None})
        session.update_state({"context": {context.context_id(): migrated_state}})
        context = await context_engine.create_context(session=session, processors=processors)
        cursor = HistoryReplayCursor(path, generation, identity, pending["offset"])
        bind_recovery_boundary(context, cursor, covered)
        context.duplex_history_ids = set()
        await save_context_snapshot(context_engine=context_engine, session=session)
        logger.info("context history migrated: session=%s records=%d compactions=%d retained_bytes=%d",
                    session_id, count, compactions, _message_bytes(context))
        return True
    except BaseException as exc:
        await rollback()
        if isinstance(exc, (asyncio.CancelledError, HistoryReplayError)):
            raise
        if isinstance(exc, MemoryError):
            raise HistoryReplayError(
                "Insufficient memory for legacy migration", code="HISTORY_RESTORE_RESOURCE"
            ) from exc
        if isinstance(exc, Exception):
            raise HistoryReplayError("Legacy context migration failed; original history was preserved") from exc
        raise
    finally:
        iterator.close()
