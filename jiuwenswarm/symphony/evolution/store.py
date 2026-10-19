"""Append-only storage for Symphony evolution events and overlays."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

EVENT_DIR = "evolution"
EVENTS_FILE = "events.jsonl"
OVERLAY_FILE = "dynamic_graph_overlay.json"
_STORE_LOCK = threading.RLock()


@contextmanager
def evolution_store_transaction() -> Iterator[None]:
    """Serialize event append, deduplication, and overlay rebuild in-process."""

    with _STORE_LOCK:
        yield


def evolution_dir(score_dir: str | Path) -> Path:
    return Path(score_dir) / EVENT_DIR


def events_path(score_dir: str | Path) -> Path:
    return evolution_dir(score_dir) / EVENTS_FILE


def overlay_path(score_dir: str | Path) -> Path:
    return evolution_dir(score_dir) / OVERLAY_FILE


def append_event(score_dir: str | Path, event: dict[str, Any]) -> None:
    with _STORE_LOCK:
        path = events_path(score_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def read_events(
    score_dir: str | Path,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    with _STORE_LOCK:
        path = events_path(score_dir)
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
    if limit is not None and limit > 0:
        lines = lines[-limit:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def write_overlay(score_dir: str | Path, overlay: dict[str, Any]) -> None:
    with _STORE_LOCK:
        path = overlay_path(score_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(overlay, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(path)


def read_overlay(score_dir: str | Path) -> dict[str, Any] | None:
    with _STORE_LOCK:
        path = overlay_path(score_dir)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return payload if isinstance(payload, dict) else None


def overlay_file_mtime(score_dir: str | Path) -> float | None:
    with _STORE_LOCK:
        path = overlay_path(score_dir)
        try:
            return path.stat().st_mtime
        except OSError:
            return None
