"""Persistent store for parsed trace records.

Provides helpers for persisting and loading TraceRecord objects,
as well as a convenience function that parses only new sessions
and stores their traces.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .parser import list_session_ids, parse_session, parse_all_sessions
from ..models import TraceRecord

LOGGER = logging.getLogger(__name__)


def _store_dir() -> Path:
    """Return the directory where processed traces are stored."""
    from jiuwenswarm.common.utils import get_agent_sessions_dir
    base = get_agent_sessions_dir()
    store = base.parent / "trace_store"
    store.mkdir(parents=True, exist_ok=True)
    return store


def _processed_index_path() -> Path:
    """Path to the JSON file tracking which sessions have been processed."""
    return _store_dir() / "processed_index.json"


def _records_path() -> Path:
    """Path to the JSONL file holding all stored TraceRecords."""
    return _store_dir() / "records.jsonl"


def _load_processed_index() -> set[str]:
    """Load the set of session IDs that have already been processed."""
    path = _processed_index_path()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("processed_ids", []))
    except Exception:
        LOGGER.warning("Failed to read processed index, starting fresh", exc_info=True)
        return set()


def _save_processed_index(ids: set[str]) -> None:
    """Save the set of processed session IDs."""
    path = _processed_index_path()
    data = {"processed_ids": sorted(ids)}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_and_store() -> list[TraceRecord]:
    """Parse only new (unprocessed) sessions and append their traces to the store.

    Returns the list of newly parsed TraceRecords.
    """
    all_ids = set(list_session_ids())
    already_processed = _load_processed_index()
    new_ids = sorted(all_ids - already_processed)

    if not new_ids:
        LOGGER.info("parse_and_store: no new sessions to process")
        return []

    new_records: list[TraceRecord] = []
    for session_id in new_ids:
        traces = parse_session(session_id)
        if traces:
            new_records.extend(traces)

    if new_records:
        # Append to records.jsonl
        records_path = _records_path()
        with open(records_path, "a", encoding="utf-8") as f:
            for record in new_records:
                f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        LOGGER.info("parse_and_store: stored %d new traces from %d sessions", len(new_records), len(new_ids))

    # Update processed index
    _save_processed_index(already_processed | set(new_ids))

    return new_records


def load_all_records() -> list[TraceRecord]:
    """Load all stored TraceRecords from the records.jsonl file."""
    records_path = _records_path()
    if not records_path.exists():
        return []

    records: list[TraceRecord] = []
    try:
        with open(records_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    records.append(TraceRecord.from_dict(data))
                except json.JSONDecodeError:
                    LOGGER.warning("Skipping malformed record line")
    except Exception:
        LOGGER.warning("Failed to load records", exc_info=True)

    LOGGER.info("load_all_records: loaded %d records", len(records))
    return records


def clear_store() -> None:
    """Remove all stored traces and the processed index."""
    store = _store_dir()
    for path in [store / "records.jsonl", store / "processed_index.json"]:
        if path.exists():
            path.unlink()
    LOGGER.info("clear_store: store cleared")


__all__ = ["parse_and_store", "load_all_records", "clear_store"]
