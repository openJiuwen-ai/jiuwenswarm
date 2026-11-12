# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded page-index reads, including histories with giant diagnostic rows."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError
from jiuwenswarm.server.runtime.session.history_stream import iter_history_rows

logger = logging.getLogger(__name__)
_BLOCK_BYTES = 64 * 1024
_RECORD_BYTES = 4 * 1024 * 1024
_PAGE_BYTES = 16 * 1024 * 1024
_SCAN_BYTES = 8 * 1024 * 1024 * 1024
_SCAN_SECONDS = 120


def _reverse_line_ranges(
    source: BinaryIO, end: int, check_time: Callable[[], None],
) -> Iterator[tuple[int, int]]:
    """Find row offsets in fixed blocks without retaining a potentially giant row."""
    position = line_end = end
    while position:
        check_time()
        start = max(0, position - _BLOCK_BYTES)
        source.seek(start)
        chunk = source.read(position - start)
        if len(chunk) != position - start:
            raise HistoryReplayError("History changed during page scan", code="HISTORY_RESTORE_CHANGED")
        search_end = len(chunk)
        while True:
            newline = chunk.rfind(b"\n", 0, search_end)
            if newline < 0:
                break
            line_start = start + newline + 1
            if line_start < line_end:
                yield line_start, line_end
            line_end = start + newline
            search_end = newline
        position = start
    if line_end:
        yield 0, line_end


def read_indexed_history(
    path: Path,
    *,
    session_id: str,
    page_idx: int,
    page_size: int,
    is_restorable: Callable[[dict[str, Any]], bool],
    source_stat: os.stat_result | None = None,
) -> dict[str, Any] | None:
    """Count restorable rows while retaining only the requested newest-first page.

    JSONL uses reverse block scans; old JSON arrays use two streaming passes.
    Oversized diagnostic fields are validated and skipped by the legacy parser.
    Semantic fields and the retained page have explicit allocation budgets.
    """
    before = path.stat() if source_stat is None else source_stat
    if before.st_size > _SCAN_BYTES:
        raise HistoryReplayError("History exceeds the page scan budget", code="HISTORY_RESTORE_LIMIT")
    deadline = time.monotonic() + _SCAN_SECONDS

    def validate_source() -> None:
        try:
            after = path.stat()
        except FileNotFoundError as exc:
            raise HistoryReplayError("History disappeared during page scan", code="HISTORY_RESTORE_CHANGED") from exc
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or after.st_size < before.st_size:
            raise HistoryReplayError("History changed during page scan", code="HISTORY_RESTORE_CHANGED")

    if source_stat is not None:
        validate_source()

    def check_time() -> None:
        if time.monotonic() > deadline:
            raise HistoryReplayError("History page scan time budget exceeded", code="HISTORY_RESTORE_LIMIT")

    def include_record(record: dict[str, Any]) -> bool:
        return record.get("role") == "user" or not record.get("event_type") or is_restorable(record)

    def projected_rows(start: int = 0, end: int = before.st_size) -> Iterator[dict[str, Any]]:
        for row in iter_history_rows(
            path, session_id=session_id, start=start, end=end,
            max_seconds=max(0, deadline - time.monotonic()), include_record=include_record,
            # Page replay needs tool payloads and usage; recovery omits them by default.
            omit_fields=frozenset({"prompt"}),
        ):
            check_time()
            yield row.record

    def jsonl_rows() -> Iterator[dict[str, Any]]:
        with path.open("rb") as source:
            for start, end in _reverse_line_ranges(source, before.st_size, check_time):
                check_time()
                if end - start > _RECORD_BYTES:
                    yield from projected_rows(start, end)
                    continue
                source.seek(start)
                raw = source.read(end - start)
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw.decode("utf-8-sig" if start == 0 else "utf-8"))
                except ValueError:
                    logger.warning("Invalid history page row skipped: session=%s offset=%s", session_id, start)
                    continue
                if isinstance(record, dict):
                    yield record

    records: list[dict[str, Any]] = []
    retained_bytes = 0

    def retain(record: dict[str, Any]) -> None:
        nonlocal retained_bytes
        retained_bytes += len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
        if retained_bytes > _PAGE_BYTES:
            raise HistoryReplayError("History page exceeds the payload budget", code="HISTORY_RESTORE_LIMIT")
        records.append(record)

    first = (page_idx - 1) * page_size
    total = 0
    try:
        if path.suffix.lower() == ".jsonl":
            for record in jsonl_rows():
                if is_restorable(record):
                    if first <= total < first + page_size:
                        retain(record)
                    total += 1
        else:
            total = sum(1 for record in projected_rows() if is_restorable(record))
            low, high = max(0, total - first - page_size), total - first
            index = 0
            if high > 0:
                for record in projected_rows():
                    if not is_restorable(record):
                        continue
                    if low <= index < high:
                        retain(record)
                    index += 1
                records.reverse()
    except MemoryError as exc:
        raise HistoryReplayError("History page allocation failed", code="HISTORY_RESTORE_LIMIT") from exc
    except (HistoryReplayError, OSError):
        validate_source()
        raise
    validate_source()
    total_pages = max(1, (total + page_size - 1) // page_size)
    if page_idx > total_pages:
        return None
    return {"messages": records, "total_pages": total_pages, "page_idx": page_idx}
