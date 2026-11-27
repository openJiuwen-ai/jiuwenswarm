# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Turn-based diff service for /diff command."""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenclaw.utils import get_agent_sessions_dir, get_agent_workspace_dir, get_user_workspace_dir


logger = logging.getLogger(__name__)


class DiffService:
    """提供 turn-based diff 查询服务."""

    def __init__(self) -> None:
        self._agent_id = "jiuwenclaw"

    def get_turn_diffs(self, session_id: str) -> list[dict[str, Any]]:
        """获取 session 的所有 turn diff（完整信息）.

        Args:
            session_id: 会话 ID

        Returns:
            turn diff 列表，按时间倒序排列（most recent first）
        """
        turns = self._compute_turn_diffs(session_id)
        return list(reversed(turns))

    def _compute_turn_diffs(self, session_id: str) -> list[dict[str, Any]]:
        """计算 turn-based diffs."""
        history = self._read_history(session_id)
        agent_history = self._read_agent_history()

        if not history:
            return []

        turns: list[dict[str, Any]] = []

        i = 0
        while i < len(history):
            record = history[i]

            if record["role"] == "user":
                turn_start = record["timestamp"]
                turn_end = None

                for j in range(i + 1, len(history)):
                    next_record = history[j]
                    if next_record["role"] == "user":
                        break
                    if self._is_turn_end(next_record):
                        turn_end = next_record["timestamp"]
                        break

                turns.append({
                    "turnIndex": len(turns) + 1,
                    "userPromptPreview": record.get("content", "")[:30],
                    "timestamp": self._timestamp_to_iso(record["timestamp"]),
                    "start_timestamp": turn_start,
                    "end_timestamp": turn_end,
                    "files": {},
                    "stats": {
                        "filesChanged": 0,
                        "linesAdded": 0,
                        "linesRemoved": 0,
                    },
                })

            i += 1

        for turn in turns:
            file_edits = self._find_file_edits_by_time_range(
                agent_history,
                start_time=turn["start_timestamp"],
                end_time=turn["end_timestamp"],
            )

            for file_path, edit_info in file_edits.items():
                if file_path not in turn["files"]:
                    turn["files"][file_path] = {
                        "filePath": file_path,
                        "hunks": [],
                        "isNewFile": False,
                        "linesAdded": 0,
                        "linesRemoved": 0,
                        "lastEditTime": None,
                    }

                for op in edit_info["operations"]:
                    hunks = self._compute_hunks(
                        op["old_content"],
                        op["new_content"],
                    )
                    turn["files"][file_path]["hunks"].extend(hunks)
                    turn["files"][file_path]["lastEditTime"] = op["timestamp"]

                    if op["action"] == "write" and op["old_content"] is None:
                        turn["files"][file_path]["isNewFile"] = True

                    for hunk in hunks:
                        for line in hunk["lines"]:
                            if line.startswith("+") and not line.startswith("+++"):
                                turn["files"][file_path]["linesAdded"] += 1
                            elif line.startswith("-") and not line.startswith("---"):
                                turn["files"][file_path]["linesRemoved"] += 1

            turn["stats"]["filesChanged"] = len(turn["files"])
            turn["stats"]["linesAdded"] = sum(
                f["linesAdded"] for f in turn["files"].values()
            )
            turn["stats"]["linesRemoved"] = sum(
                f["linesRemoved"] for f in turn["files"].values()
            )

        turns_with_files = [t for t in turns if t["files"]]
        for i, turn in enumerate(turns_with_files):
            turn["turnIndex"] = i + 1

        return turns_with_files

    @staticmethod
    def _is_turn_end(record: dict[str, Any]) -> bool:
        """判断一条记录是否是 turn 的结束."""
        event_type = record.get("event_type")
        if event_type == "chat.final":
            return True
        if event_type == "chat.evolution_status" and record.get("status") == "end":
            return True
        return False

    @staticmethod
    def _find_next_user_time(
        history: list[dict[str, Any]], user_index: int
    ) -> float | None:
        """查找下次用户消息时间."""
        for j in range(user_index + 1, len(history)):
            if history[j]["role"] == "user":
                return history[j]["timestamp"]
        return None

    @staticmethod
    def _read_history(session_id: str) -> list[dict[str, Any]]:
        """读取 session history."""
        history_file = get_agent_sessions_dir() / session_id / "history.json"
        if not history_file.exists():
            return []
        try:
            return json.loads(history_file.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _read_agent_history(self) -> dict[str, Any]:
        """读取 .agent_history（同时读取两个可能的位置并合并）."""
        result: dict[str, Any] = {}

        paths = [
            get_agent_workspace_dir() / ".agent_history" / f"file_ops_{self._agent_id}.json",
            get_user_workspace_dir() / ".agent_history" / f"file_ops_{self._agent_id}.json",
        ]

        for history_file in paths:
            if history_file.exists():
                try:
                    data = json.loads(history_file.read_text(encoding="utf-8"))
                    for file_path, entries in data.items():
                        if file_path not in result:
                            result[file_path] = []
                        result[file_path].extend(entries)
                except Exception as e:
                    logger.warning(f"Failed to read agent history file {history_file}: {e}")

        return result

    def _find_file_edits_by_time_range(
        self,
        agent_history: dict[str, Any],
        start_time: float,
        end_time: float | None,
    ) -> dict[str, dict[str, Any]]:
        """根据时间范围查找文件编辑记录.

        时间区间：[start_time, end_time) 左闭右开
        """
        file_edits: dict[str, dict[str, Any]] = {}

        for file_path, entries in agent_history.items():
            for entry in entries:
                edit_time = self._iso_to_timestamp(entry["timestamp"])

                if edit_time >= start_time:
                    if end_time is None or edit_time < end_time:
                        if file_path not in file_edits:
                            file_edits[file_path] = {
                                "file_path": file_path,
                                "operations": [],
                            }
                        file_edits[file_path]["operations"].append({
                            "action": entry["action"],
                            "timestamp": entry["timestamp"],
                            "old_content": entry["old_content"],
                            "new_content": entry["new_content"],
                        })

        return file_edits

    @staticmethod
    def _iso_to_timestamp(iso_str: str) -> float:
        """将 ISO 8601 字符串转换为 Unix timestamp."""
        dt = datetime.fromisoformat(iso_str)
        return dt.timestamp()

    @staticmethod
    def _timestamp_to_iso(timestamp: float) -> str:
        """将 Unix timestamp 转换为 ISO 8601 字符串."""
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return dt.isoformat()

    @staticmethod
    def _compute_hunks(
        old_content: str | None,
        new_content: str,
    ) -> list[dict[str, Any]]:
        """计算结构化 diff hunks."""
        if old_content is None:
            lines = new_content.splitlines()
            return [{
                "oldStart": 0,
                "oldLines": 0,
                "newStart": 1,
                "newLines": len(lines),
                "lines": [f"+{line}" for line in lines],
            }]

        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)

        if not old_lines and not new_lines:
            return []

        matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
        hunks: list[dict[str, Any]] = []

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue

            old_start = i1 + 1
            old_count = i2 - i1
            new_start = j1 + 1
            new_count = j2 - j1

            lines: list[str] = []

            for k in range(i1, i2):
                lines.append(f"-{old_lines[k].rstrip()}")

            for k in range(j1, j2):
                lines.append(f"+{new_lines[k].rstrip()}")

            hunks.append({
                "oldStart": old_start,
                "oldLines": old_count,
                "newStart": new_start,
                "newLines": new_count,
                "lines": lines,
            })

        return hunks

    @staticmethod
    def _finalize_turn(turn: dict[str, Any]) -> None:
        """完成 turn 的统计信息计算."""
        turn["stats"]["filesChanged"] = len(turn["files"])
        turn["stats"]["linesAdded"] = sum(
            f["linesAdded"] for f in turn["files"].values()
        )
        turn["stats"]["linesRemoved"] = sum(
            f["linesRemoved"] for f in turn["files"].values()
        )


_diff_service: DiffService | None = None


def get_diff_service() -> DiffService:
    """获取 DiffService 单例实例."""
    global _diff_service
    if _diff_service is None:
        _diff_service = DiffService()
    return _diff_service
