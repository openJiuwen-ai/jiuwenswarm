from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

from jiuwenclaw.agentserver.skilldev.session_history.schema import SkillDevSessionEventRecord
from jiuwenclaw.utils import format_session_log

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


class SkillDevSessionHistoryStore:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def _task_dir(self, task_id: str) -> Path:
        return self._base_dir / task_id

    def _history_dir(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "session_history"

    def _events_file(self, task_id: str) -> Path:
        return self._history_dir(task_id) / "events.jsonl"

    def _snapshot_file(self, task_id: str) -> Path:
        return self._history_dir(task_id) / "snapshot.json"

    def append_event(
        self,
        *,
        task_id: str,
        source: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> SkillDevSessionEventRecord:
        events_file = self._events_file(task_id)
        events_file.parent.mkdir(parents=True, exist_ok=True)
        next_seq = 1
        if events_file.exists():
            try:
                lines = events_file.read_text(encoding="utf-8").splitlines()
                if lines:
                    last = json.loads(lines[-1])
                    next_seq = int(last.get("seq", 0)) + 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    format_session_log(
                        task_id,
                        "[SkillDevSessionHistoryStore] 读取最后序号失败，重置为1: task_id=%s err=%s",
                    ),
                    task_id,
                    exc,
                )
                next_seq = 1
        record = SkillDevSessionEventRecord(
            seq=next_seq,
            timestamp=_utc_now_iso(),
            source=source,
            event_type=event_type,
            payload=payload,
        )
        with events_file.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record.to_dict(), ensure_ascii=False))
            fh.write("\n")
        return record

    def list_events(self, task_id: str) -> list[SkillDevSessionEventRecord]:
        events_file = self._events_file(task_id)
        if not events_file.exists():
            return []
        out: list[SkillDevSessionEventRecord] = []
        for line_no, line in enumerate(
            events_file.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            row = line.strip()
            if not row:
                continue
            try:
                parsed = json.loads(row)
                if isinstance(parsed, dict):
                    out.append(SkillDevSessionEventRecord.from_dict(parsed))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    format_session_log(
                        task_id,
                        "[SkillDevSessionHistoryStore] 事件解析失败: task_id=%s line=%s err=%s",
                    ),
                    task_id,
                    line_no,
                    exc,
                )
        out.sort(key=lambda item: item.seq)
        return out

    def save_snapshot(self, task_id: str, snapshot: dict[str, Any]) -> None:
        snapshot_file = self._snapshot_file(task_id)
        snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        snapshot_file.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_snapshot(self, task_id: str) -> dict[str, Any] | None:
        snapshot_file = self._snapshot_file(task_id)
        if not snapshot_file.exists():
            return None
        try:
            data = json.loads(snapshot_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                format_session_log(
                    task_id,
                    "[SkillDevSessionHistoryStore] 读取快照失败: task_id=%s err=%s",
                ),
                task_id,
                exc,
            )
            return None
        if not isinstance(data, dict):
            return None
        return data
