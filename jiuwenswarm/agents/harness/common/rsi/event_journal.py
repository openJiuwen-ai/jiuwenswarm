"""Append-only task journal for raw RSI engine events."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class RsiEventJournal:
    """Persist every received engine event as one JSON line per task."""

    def __init__(self, tasks_root: str | Path) -> None:
        self.tasks_root = Path(tasks_root).expanduser().resolve()
        self._lock = threading.Lock()

    def append(self, task_id: str, event: Any) -> Path:
        task = str(task_id or "").strip()
        if not task or Path(task).name != task or task in {".", ".."}:
            raise ValueError(f"task_id 非法: {task_id}")

        task_dir = (self.tasks_root / task).resolve()
        try:
            task_dir.relative_to(self.tasks_root)
        except ValueError as exc:
            raise ValueError(f"task_id 超出 RSI 根目录: {task_id}") from exc

        record = {
            "schema_version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "task_id": task,
            "event_type": _event_type(event),
            "event": _event_payload(event),
        }
        line = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_default,
        )
        target = task_dir / "events.jsonl"
        with self._lock:
            task_dir.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
        return target


def _event_type(event: Any) -> str:
    provider_type = str(getattr(event, "event_type", "") or "").strip()
    if provider_type:
        return provider_type
    family = str(getattr(event, "family", "") or "").strip()
    kind = str(getattr(event, "kind", "") or "").strip()
    if family and kind:
        return f"{family}.{kind}"
    return type(event).__name__


def _event_payload(event: Any) -> Any:
    to_dict = getattr(event, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if is_dataclass(event):
        return asdict(event)
    if isinstance(event, Mapping):
        return dict(event)
    values = getattr(event, "__dict__", None)
    if isinstance(values, dict):
        return dict(values)
    return {"value": str(event)}


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return str(value)


__all__ = ["RsiEventJournal"]
