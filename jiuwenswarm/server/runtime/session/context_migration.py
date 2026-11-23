"""Bounded legacy replay with real native compression and all-or-nothing install."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any

from jiuwenswarm.common.schema.message import SESSION_REQUEST_COMPLETED_EVENT
from jiuwenswarm.server.runtime.session import lifecycle as lc
from jiuwenswarm.server.runtime.session.context_snapshot import bind_recovery_boundary, save_context_snapshot
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayCursor, HistoryReplayError, is_context_record
from jiuwenswarm.server.runtime.session.history_stream import (
    history_request_id as _history_request_id,
    iter_history_rows,
)
from jiuwenswarm.server.runtime.session.recovery_budget import LegacyGroupLimit, LegacyLimitSource

logger = logging.getLogger(__name__)
_COMPRESS_AT_BYTES = 512 * 1024
# Leave room for the rest of a tool batch before the hard page limit.
_PAGE_AT_RECORDS = 1024
_MAX_PAGE_RECORDS = 2048
_MAX_CONTEXT_BYTES = 4 * 1024 * 1024


def _message_bytes(context: Any) -> int:
    total = 0
    for message in context.get_messages():
        total += len(message.model_dump_json().encode("utf-8"))
        if total > _MAX_CONTEXT_BYTES:
            break
    return total


def _closes_legacy_request(record: dict[str, Any], request_id: str) -> bool:
    # Match the durable receipt written by session_history. A waiting_user or
    # unknown status can still have a tool result pending.
    stamp = record.get("timestamp")
    return bool(
        request_id
        and record.get("status") in ("success", "error", "failed", "cancelled")
        and record.get("id") == f"{request_id}:request_completed"
        and record.get("role") == "assistant"
        and record.get("feedback_only") is True
        and type(stamp) in (int, float)
        and math.isfinite(stamp)
        and stamp >= 0
    )


def _next_page(iterator, pending: dict[str, Any], before_request_id: str | None, covered: set[str]):
    if "deferred_page" in pending:
        page, boundary = pending.pop("deferred_page")
        pending.update(boundary)
        return page

    records = []
    size = 0
    prefix_count = prefix_size = 0
    group_start = pending["offset"]
    prefix_boundary = {}
    open_calls: set[tuple[str, str]] = set()
    request_scope: set[str] = set(pending.get("legacy_request_scope", ()))

    def boundary():
        # Preserve a single owner across pages. Multiple or unknown owners stay
        # ambiguous, without retaining an unbounded set of queued request IDs.
        return {
            "legacy_request_scope": tuple(request_scope) if len(request_scope) <= 1 else ("",),
            "offset": pending["offset"],
            "had_compaction": pending["had_compaction"],
            "done": pending["done"],
        }

    def finish_page():
        if size > _MAX_CONTEXT_BYTES or len(records) > _MAX_PAGE_RECORDS:
            # The last group fits on its own. The iterator already consumed it,
            # so publish its rows and cursor together on the next call.
            pending["deferred_page"] = records[prefix_count:], boundary()
            pending.update(prefix_boundary)
            return records[:prefix_count]
        pending.update(boundary())
        return records

    for row in iterator:
        if records and not open_calls:
            prefix_count, prefix_size = len(records), size
            prefix_boundary = boundary()
        record = row.record
        request_id = _history_request_id(record)
        if (before_request_id and request_id == before_request_id
                and record.get("role") in {"user", "human"}):
            pending.update(offset=row.start, done=True)
            break
        pending["offset"] = row.end
        if request_id in covered:
            continue
        if record.get("event_type") == SESSION_REQUEST_COMPLETED_EVENT:
            if _closes_legacy_request(record, request_id):
                # The receipt follows this request's durable output, including
                # turns interrupted before a tool result was persisted. Only
                # matching owners can release their unmatched calls.
                closed = {call for call in open_calls if call[0] == request_id}
                if closed and (
                    row.has_later_activity is None
                    or row.has_later_activity(request_id, closed, row.end, before_request_id, covered)
                ):
                    continue
                request_scope.discard(request_id)
                open_calls.difference_update(closed)
                ends_tool_group = bool(closed) or record.get("status") == "cancelled"
                if ends_tool_group and records and not open_calls:
                    return finish_page()
            continue
        if not is_context_record(record):
            continue
        if record.get("role") in ("leader", "teammate"):
            # Team UI projections never become native model messages. Their
            # audit bytes and tool IDs must not consume a model group's budget.
            continue
        if request_id or record.get("role") in {"user", "human"}:
            request_scope.add(request_id)
        if record.get("event_type") == "context.compact_summary":
            # Auto summaries are audit projections, not effective windows. The
            # original messages are still present and get compressed once here.
            metadata = record.get("compact_metadata") or {}
            if metadata.get("trigger") != "manual_rewind":
                pending["had_compaction"] = True
                continue
        call = record.get("tool_call")
        if record.get("event_type") == "chat.tool_call":
            # Follow the native builder: a model call needs both a tool name
            # and tool_call_id; an audit object's generic id is insufficient.
            if not isinstance(call, dict) or not call.get("name") or not call.get("tool_call_id"):
                continue
            call_id = call["tool_call_id"]
            owner = request_id
            if not owner:
                candidates = request_scope | {key[0] for key in open_calls if key[0]}
                if len(candidates) == 1:
                    owner = next(iter(candidates))
            open_calls.add((owner, str(call_id)))
        if record.get("event_type") == "chat.tool_result":
            call_id = str(record.get("tool_call_id") or "")
            key = (request_id, call_id)
            if key in open_calls:
                open_calls.discard(key)
            else:
                # Older rows may omit request_id on either half of a pair.
                # Only accept an unambiguous match when that metadata is absent.
                legacy_matches = {call for call in open_calls
                                  if call[1] == call_id and (not request_id or not call[0])}
                if len(legacy_matches) == 1:
                    open_calls.difference_update(legacy_matches)
        if record.get("event_type") == "chat.final" and request_id and all(
            owner and owner != request_id for owner, _ in open_calls
        ):
            request_scope.discard(request_id)
        if len(records) == prefix_count:
            group_start = row.start
        records.append(record)
        size += len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
        group_bytes, group_records = size - prefix_size, len(records) - prefix_count
        if group_bytes > _MAX_CONTEXT_BYTES or group_records > _MAX_PAGE_RECORDS:
            logger.warning(
                "history migration limit: unit=%s byte_limit_exceeded=%s record_limit_exceeded=%s "
                "counted_bytes=%d records=%d unmatched_calls=%d start_offset=%d end_offset=%d",
                "single_record" if group_records == 1 else "tool_group",
                group_bytes > _MAX_CONTEXT_BYTES, group_records > _MAX_PAGE_RECORDS,
                group_bytes, group_records, len(open_calls), group_start, row.end,
            )
            raise LegacyGroupLimit()
        if (size >= _COMPRESS_AT_BYTES or len(records) >= _PAGE_AT_RECORDS) and not open_calls:
            return finish_page()
    else:
        pending["done"] = True
    if open_calls:
        raise HistoryReplayError("Legacy history has an unfinished tool group; original data was preserved")
    return finish_page()


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
    pages = 0
    compression_seconds = 0.0

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
            pages += 1
            logger.debug("Migrating history page: session=%s page=%d rows=%d end_offset=%d",
                         session_id, pages, len(records), pending["offset"])
            messages, _ = _build_context_messages_from_history(records)
            count += len(records)
            context.set_messages(list(context.get_messages()) + messages)
            if _message_bytes(context) >= _COMPRESS_AT_BYTES:
                started = time.monotonic()
                result = await context.compress_context(compression_trigger="history_migration")
                compression_seconds += time.monotonic() - started
                code = result.get("result") if isinstance(result, dict) else result
                compactions += int(code == "compressed")
                if code == "busy" or _message_bytes(context) > _MAX_CONTEXT_BYTES:
                    raise HistoryReplayError("Native compression could not bound the migrated context",
                                             code="HISTORY_RESTORE_LIMIT")
        if pending["had_compaction"] and context.get_messages():
            started = time.monotonic()
            result = await context.compress_context(compression_trigger="history_migration")
            compression_seconds += time.monotonic() - started
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
        started = time.monotonic()
        await save_context_snapshot(context_engine=context_engine, session=session)
        logger.info("context history migrated: session=%s records=%d compactions=%d retained_bytes=%d "
                    "pages=%d end_offset=%d compression_ms=%.1f snapshot_ms=%.1f",
                    session_id, count, compactions, _message_bytes(context), pages, cursor.offset,
                    compression_seconds * 1000, (time.monotonic() - started) * 1000)
        return True
    except BaseException as exc:
        await rollback()
        replayed_from_start = base_states is None and base_cursor is None and not covered_requests
        if isinstance(exc, LegacyGroupLimit) and replayed_from_start:
            exc.source = LegacyLimitSource(
                session_id, str(before_request_id or "").strip(), HistoryReplayCursor(path, generation, identity, 0),
            )
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
