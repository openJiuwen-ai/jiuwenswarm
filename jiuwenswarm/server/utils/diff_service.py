# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Turn-based diff service for /diff command."""

from __future__ import annotations

import difflib
import copy
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_sessions_dir, get_agent_workspace_dir, get_user_workspace_dir
from jiuwenswarm.server.runtime.session.session_history import load_history_records


logger = logging.getLogger(__name__)

INTERNAL_UNTRACKED_DIRS = {".agent_history"}


MAX_FILES = 50
MAX_DIFF_SIZE_BYTES = 1_000_000
MAX_FILES_FOR_DETAILS = 500
HISTORY_PRIORITY_PROJECT_ROOT = 0
HISTORY_PRIORITY_SHARED_WORKSPACE = 10
HISTORY_PRIORITY_EXTRA_ROOT = 20
HISTORY_PRIORITY_UNKNOWN = 50
WORKTREE_HISTORY_CONTAINERS: tuple[tuple[str, ...], ...] = (
    (".worktrees",),
    (".jiuwen", "worktrees"),
)

# change_sets.json 写入锁:保证同一进程内多线程惰性回填时不互相覆盖。
_CHANGE_SET_LOCK = threading.Lock()

# file_ops 条目上的软删除标记。被 conversation 回退"截断"掉的快照打上此标记后
# 对 turn diff 显示层不可见，但仍保留 old_content，从而不丢失文件回滚能力。
_REWOUND_KEY = "rewound_out"

# file_ops 条目上的 discard 软删除标记。由 ``discard_turn_changes`` 打上,
# 与 conversation rewind 的 ``rewound_out`` 区分:redo 只恢复 ``discarded_out``,
# 不会误暴露 rewind 软隐藏的"未来"条目。两者都对显示层不可见
# (见 ``_read_agent_history`` 的 ``include_rewound`` 过滤)。
_DISCARDED_KEY = "discarded_out"


class DiffHistoryExpiredError(RuntimeError):
    """历史 diff 索引仍存在但详情已无法重建。"""


class DiffService:
    """提供 turn-based diff 查询服务."""

    def __init__(self) -> None:
        self._agent_id = "jiuwenswarm"

    def get_turn_diffs(
        self,
        session_id: str,
        project_dir: str | None = None,
        repo_context: dict[str, Any] | None = None,
        extra_history_roots: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """获取 session 的所有 turn diff（完整信息）.

        Args:
            session_id: 会话 ID
            project_dir: 项目目录路径（可选，若不提供则从 session metadata 读取）

        Returns:
            turn diff 列表，按时间倒序排列（most recent first）
        """
        turns = self._compute_turn_diffs(session_id, project_dir, extra_history_roots=extra_history_roots)
        self._enrich_with_change_sets(session_id, turns, repo_context=repo_context)
        return list(reversed(turns))

    def get_turn_diff_summaries(
        self,
        session_id: str,
        project_dir: str | None = None,
        repo_context: dict[str, Any] | None = None,
        extra_history_roots: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """获取 session 的历史 turn diff 摘要，包含已持久化快照。

        与 ``get_turn_diffs`` 不同，此方法会把当前 file_ops 已经无法重建、
        但仍存在于 change_sets/snapshot 中的历史轮次也返回出来（例如已撤销
        的 turn）。
        """
        turns = self._compute_turn_diffs(session_id, project_dir, extra_history_roots=extra_history_roots)
        self._enrich_with_change_sets(session_id, turns, repo_context=repo_context)
        by_turn = {int(t.get("turnIndex", 0) or 0): t for t in turns}
        for entry in self._load_change_sets(session_id):
            try:
                turn_index = int(entry.get("turn_index", 0) or 0)
            except (TypeError, ValueError):
                continue
            if turn_index <= 0 or turn_index in by_turn:
                continue
            snapshot = self._load_turn_snapshot(session_id, str(entry.get("change_set_id") or ""))
            if snapshot is None:
                snapshot = self._turn_from_change_set_entry(entry)
            by_turn[turn_index] = snapshot
        return sorted(
            by_turn.values(),
            key=lambda t: int(t.get("turnIndex", 0) or 0),
            reverse=True,
        )

    def get_turn_diff(
        self,
        session_id: str,
        *,
        turn_index: int | None = None,
        change_set_id: str | None = None,
        project_dir: str | None = None,
        repo_context: dict[str, Any] | None = None,
        extra_history_roots: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """获取指定轮次的 turn diff。

        优先按 ``change_set_id`` 查询;未提供时按 ``turn_index`` 查询。
        两者均不提供时返回 None。

        Args:
            session_id: 会话 ID
            turn_index: 轮次序号(1-based,与 get_turn_diffs 返回的 turnIndex 对齐)
            change_set_id: 变更集 ID(优先于 turn_index)
            project_dir: 项目目录路径(可选,若不提供则从 session metadata 读取)

        Returns:
            匹配轮次的 turn diff dict;未命中时返回 None。
        """
        if change_set_id is None and turn_index is None:
            return None
        if change_set_id is not None:
            entries = self._load_change_sets(session_id)
            indexed_entry = None
            for entry in entries:
                if str(entry.get("change_set_id") or "") == change_set_id:
                    indexed_entry = entry
                    break
            if indexed_entry is None:
                if self._load_turn_snapshot(session_id, change_set_id) is not None:
                    raise DiffHistoryExpiredError(
                        f"diff history expired for change_set_id={change_set_id}"
                    )
                return None
            snapshot = self._load_turn_snapshot(session_id, change_set_id)
            if snapshot is not None:
                return snapshot
            turns = self.get_turn_diffs(
                session_id,
                project_dir,
                repo_context=repo_context,
                extra_history_roots=extra_history_roots,
            )
            for turn in turns:
                if turn.get("change_set_id") == change_set_id:
                    return turn
            raise DiffHistoryExpiredError(
                f"diff history expired for change_set_id={change_set_id}"
            )
        turns = self.get_turn_diffs(
            session_id,
            project_dir,
            repo_context=repo_context,
            extra_history_roots=extra_history_roots,
        )
        for turn in turns:
            if int(turn.get("turnIndex", 0) or 0) == turn_index:
                return turn
        if turn_index is not None:
            for entry in self._load_change_sets(session_id):
                if int(entry.get("turn_index", 0) or 0) == turn_index:
                    snapshot = self._load_turn_snapshot(
                        session_id, str(entry.get("change_set_id") or "")
                    )
                    if snapshot is not None:
                        return snapshot
                    raise DiffHistoryExpiredError(
                        f"diff history expired for turn_index={turn_index}"
                    )
        return None

    def _compute_turn_diffs(
        self,
        session_id: str,
        project_dir: str | None = None,
        *,
        extra_history_roots: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """计算 turn-based diffs."""
        history = self._read_history(session_id)

        if not history:
            return []

        agent_history = self._read_agent_history(
            session_id,
            project_dir,
            extra_history_roots=extra_history_roots,
        )

        turns: list[dict[str, Any]] = []

        i = 0
        while i < len(history):
            record = history[i]

            if record["role"] == "user":
                turn_start = record["timestamp"]
                # Use next user message timestamp as turn end boundary.
                # A turn logically spans from one user message to the next,
                # so this captures all file edits within the turn's scope
                # (including those after chat.final but before the next user msg).
                turn_end = self._find_next_user_time(history, i)

                turns.append({
                    "turnIndex": len(turns) + 1,
                    "userPromptPreview": record.get("content", "")[:30],
                    "timestamp": self._timestamp_to_iso(record["timestamp"]),
                    "start_timestamp": turn_start,
                    "end_timestamp": turn_end,
                    "request_id": record.get("request_id", ""),
                    "user_message_id": record.get("id", ""),
                    "assistant_message_id": self._find_assistant_message_id(history, i),
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
                        "isDeletedFile": False,
                        "isBinary": False,
                        "isLargeFile": False,
                        "isTruncated": False,
                        "isUntracked": False,
                        "linesAdded": 0,
                        "linesRemoved": 0,
                        "lastEditTime": None,
                    }

                file_entry = turn["files"][file_path]
                # 累积本文件本轮全部 hunk 并按字节统一判定大文件（与工作区
                # diff 的 _split_large_file_diffs 口径一致）：超过
                # MAX_DIFF_SIZE_BYTES 标记 isLargeFile、不返回内容，但仍计入
                # 完整行数 stats，与 tracked/untracked 大文件口径对齐。
                file_hunks: list[dict[str, Any]] = []
                file_diff_bytes = 0
                for op in edit_info["operations"]:
                    remaining_bytes = max(MAX_DIFF_SIZE_BYTES - file_diff_bytes, 0)
                    (
                        op_hunks,
                        op_lines_added,
                        op_lines_removed,
                        op_diff_bytes,
                        op_is_large,
                    ) = self._compute_hunks(
                        op["old_content"],
                        op["new_content"],
                        max_diff_bytes=remaining_bytes,
                    )
                    file_entry["lastEditTime"] = op["timestamp"]

                    if op["action"] == "write" and op["old_content"] is None:
                        file_entry["isNewFile"] = True
                    if op["new_content"] is None and op["old_content"] is not None:
                        file_entry["isDeletedFile"] = True

                    file_entry["linesAdded"] += op_lines_added
                    file_entry["linesRemoved"] += op_lines_removed
                    file_diff_bytes += op_diff_bytes
                    if not op_is_large and file_diff_bytes <= MAX_DIFF_SIZE_BYTES:
                        file_hunks.extend(op_hunks)

                if file_diff_bytes > MAX_DIFF_SIZE_BYTES:
                    file_entry["isLargeFile"] = True
                    file_entry["hunks"] = []
                else:
                    file_entry["hunks"] = file_hunks

            turn["stats"]["filesChanged"] = len(turn["files"])
            turn["stats"]["linesAdded"] = sum(
                f["linesAdded"] for f in turn["files"].values()
            )
            turn["stats"]["linesRemoved"] = sum(
                f["linesRemoved"] for f in turn["files"].values()
            )

        turns_with_files = [t for t in turns if t["files"]]
        # Keep original turnIndex (aligned with user_count in history)
        # instead of renumbering — allows list_session_turns to correctly
        # map stats by the actual turn position.
        return turns_with_files

    @staticmethod
    def _change_sets_path(session_id: str) -> Path:
        return get_agent_sessions_dir() / session_id / "change_sets.json"

    @staticmethod
    def _change_set_snapshots_dir(session_id: str) -> Path:
        return get_agent_sessions_dir() / session_id / "change_sets"

    @classmethod
    def _change_set_snapshot_path(cls, session_id: str, change_set_id: str) -> Path:
        safe_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in change_set_id)
        return cls._change_set_snapshots_dir(session_id) / f"{safe_id}.json"

    def _load_change_sets(self, session_id: str) -> list[dict[str, Any]]:
        path = self._change_sets_path(session_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read change_sets.json (%s): %s", path, exc)
        return []

    def _save_change_sets(self, session_id: str, change_sets: list[dict[str, Any]]) -> None:
        path = self._change_sets_path(session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            tmp_path.write_text(
                json.dumps(change_sets, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except OSError as exc:
            logger.warning("Failed to write change_sets.json (%s): %s", path, exc)
            try:
                if "tmp_path" in locals() and tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    def _load_turn_snapshot(self, session_id: str, change_set_id: str) -> dict[str, Any] | None:
        if not change_set_id:
            return None
        path = self._change_set_snapshot_path(session_id, change_set_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Snapshots may have been created before the per-file preview
                # limit existed.  Normalize them on every read so historical
                # previews obey the same bound as newly computed diffs.
                self._normalize_snapshot_diff_sizes(data)
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read change_set snapshot (%s): %s", path, exc)
        return None

    @staticmethod
    def _normalize_snapshot_diff_sizes(turn: dict[str, Any]) -> None:
        """Apply the per-file preview limit to a persisted turn snapshot.

        A snapshot contains the structured hunk lines sent to clients, so its
        byte accounting deliberately matches ``_compute_hunks`` rather than
        the size of the source file.  This also makes snapshots written by
        older versions safe to serve after an upgrade.
        """
        files = turn.get("files")
        if not isinstance(files, dict):
            return
        for entry in files.values():
            if not isinstance(entry, dict) or bool(entry.get("isLargeFile", False)):
                continue
            hunks = entry.get("hunks")
            if not isinstance(hunks, list):
                continue
            diff_bytes = 0
            is_large = False
            for hunk in hunks:
                if not isinstance(hunk, dict):
                    continue
                lines = hunk.get("lines")
                if not isinstance(lines, list):
                    continue
                for line in lines:
                    if not isinstance(line, str):
                        continue
                    diff_bytes += len(line.encode("utf-8", errors="replace"))
                    if diff_bytes > MAX_DIFF_SIZE_BYTES:
                        is_large = True
                        break
                if is_large:
                    break
            if is_large:
                entry["isLargeFile"] = True
                entry["hunks"] = []

    def _save_turn_snapshot(self, session_id: str, turn: dict[str, Any]) -> None:
        change_set_id = str(turn.get("change_set_id") or "")
        if not change_set_id:
            return
        path = self._change_set_snapshot_path(session_id, change_set_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            tmp_path.write_text(
                json.dumps(turn, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except OSError as exc:
            logger.warning("Failed to write change_set snapshot (%s): %s", path, exc)
            try:
                if "tmp_path" in locals() and tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    @staticmethod
    def _entry_matches_turn(entry: dict[str, Any], turn: dict[str, Any]) -> bool:
        """校验 change_set entry 是否仍属于当前 turn。"""
        entry_rid = str(entry.get("request_id", "") or "")
        turn_rid = str(turn.get("request_id", "") or "")
        entry_ts = str(entry.get("timestamp", "") or "")
        turn_ts = str(turn.get("timestamp", "") or "")
        if not entry_ts or entry_ts != turn_ts:
            return False
        if entry_rid or turn_rid:
            return bool(entry_rid and turn_rid and entry_rid == turn_rid)
        return True

    @staticmethod
    def _turn_from_change_set_entry(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "turnIndex": int(entry.get("turn_index", 0) or 0),
            "timestamp": str(entry.get("timestamp", "") or ""),
            "start_timestamp": entry.get("start_timestamp"),
            "end_timestamp": entry.get("end_timestamp"),
            "userPromptPreview": str(entry.get("user_prompt_preview", "") or ""),
            "request_id": str(entry.get("request_id", "") or ""),
            "change_set_id": str(entry.get("change_set_id", "") or ""),
            "assistant_message_id": str(entry.get("assistant_message_id", "") or ""),
            "user_message_id": str(entry.get("user_message_id", "") or ""),
            "status": str(entry.get("status", "completed") or "completed"),
            "repo_root": entry.get("repo_root"),
            "branch": entry.get("branch"),
            "base_head": entry.get("base_head"),
            "stats": entry.get("stats") if isinstance(entry.get("stats"), dict) else {
                "filesChanged": 0,
                "linesAdded": 0,
                "linesRemoved": 0,
            },
            "files": {},
        }

    @staticmethod
    def _apply_change_set_entry(turn: dict[str, Any], entry: dict[str, Any]) -> None:
        turn["change_set_id"] = entry.get("change_set_id", "")
        turn["request_id"] = entry.get("request_id", "") or turn.get("request_id", "")
        turn["assistant_message_id"] = entry.get("assistant_message_id", "")
        turn["user_message_id"] = entry.get("user_message_id", "")
        turn["status"] = entry.get("status", "completed")
        turn["repo_root"] = entry.get("repo_root")
        turn["branch"] = entry.get("branch")
        turn["base_head"] = entry.get("base_head")

    @staticmethod
    def _new_change_set_entry(
        session_id: str,
        turn: dict[str, Any],
        turn_index: int,
        repo_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = str(turn.get("request_id", "") or "")
        cs_id = f"cs_{session_id}_{turn_index}_{uuid.uuid4().hex[:8]}"
        user_msg_id = str(turn.get("user_message_id", "") or "")
        assistant_msg_id = str(turn.get("assistant_message_id", "") or "")
        if request_id:
            user_msg_id = user_msg_id or f"{request_id}:user"
            assistant_msg_id = assistant_msg_id or f"{request_id}:assistant"
        repo_context = repo_context or {}
        return {
            "change_set_id": cs_id,
            "turn_index": turn_index,
            "request_id": request_id,
            "assistant_message_id": assistant_msg_id,
            "user_message_id": user_msg_id,
            "timestamp": turn.get("timestamp", ""),
            "start_timestamp": turn.get("start_timestamp"),
            "end_timestamp": turn.get("end_timestamp"),
            "user_prompt_preview": turn.get("userPromptPreview", ""),
            "status": "completed",
            "repo_root": repo_context.get("repo_root") or turn.get("repo_root"),
            "branch": repo_context.get("branch") or turn.get("branch"),
            "base_head": repo_context.get("base_head") or turn.get("base_head"),
            "stats": copy.deepcopy(turn.get("stats", {})),
        }

    def mark_turn_discarded(
        self,
        session_id: str,
        turn_index: int,
        project_dir: str | None = None,
        *,
        extra_history_roots: list[str] | None = None,
    ) -> str | None:
        """将指定 turn 的 change_set 状态标记为 discarded。"""
        if turn_index <= 0:
            return None
        target = self.get_turn_diff(
            session_id, turn_index=turn_index, project_dir=project_dir,
            extra_history_roots=extra_history_roots,
        )
        change_set_id = str((target or {}).get("change_set_id") or "")
        if not change_set_id:
            return None
        with _CHANGE_SET_LOCK:
            entries = self._load_change_sets(session_id)
            changed = False
            for entry in entries:
                if entry.get("change_set_id") == change_set_id:
                    entry["status"] = "discarded"
                    changed = True
                    break
            if changed:
                self._save_change_sets(session_id, entries)
        snapshot = self._load_turn_snapshot(session_id, change_set_id) or target
        if snapshot is not None:
            snapshot["status"] = "discarded"
            self._save_turn_snapshot(session_id, snapshot)
        return change_set_id

    def unmark_turn_discarded(
        self,
        session_id: str,
        turn_index: int,
        project_dir: str | None = None,
        *,
        extra_history_roots: list[str] | None = None,
    ) -> str | None:
        """将指定 turn 的 status 恢复为 completed(与 ``mark_turn_discarded`` 对称).

        1. 将 change_sets.json 中该 entry 的 status 显式设回 ``"completed"``
           (而非 pop 掉——缺少 status 字段的 turn 在按 change_set_id 读取
           snapshot 时会与其他路径默认值不一致,显式写回保持状态模型一致)
        2. 将 snapshot 的 status 同样设回 ``"completed"``
        3. 去掉 file_ops 中该轮条目的 ``discarded_out`` 标记
           (只恢复 discard 标记,不触碰 rewind 的 ``rewound_out``,避免
           误暴露此前 conversation rewind 软隐藏的"未来"条目)
        """
        if turn_index <= 0:
            return None
        target = self.get_turn_diff(
            session_id, turn_index=turn_index, project_dir=project_dir,
            extra_history_roots=extra_history_roots,
        )
        change_set_id = str((target or {}).get("change_set_id") or "")
        if not change_set_id:
            return None

        # 1. 将 change_sets 的 status 显式设回 completed
        with _CHANGE_SET_LOCK:
            entries = self._load_change_sets(session_id)
            changed = False
            for entry in entries:
                if entry.get("change_set_id") == change_set_id:
                    entry["status"] = "completed"
                    changed = True
                    break
            if changed:
                self._save_change_sets(session_id, entries)

        # 2. 将 snapshot 的 status 显式设回 completed
        snapshot = self._load_turn_snapshot(session_id, change_set_id) or target
        if snapshot is not None:
            snapshot["status"] = "completed"
            self._save_turn_snapshot(session_id, snapshot)

        # 3. 去掉 file_ops 中该轮条目的 discarded_out 标记
        history = self._read_history(session_id)
        user_count = 0
        target_timestamp: float | None = None
        for record in history:
            if record.get("role") == "user":
                user_count += 1
                if user_count == turn_index:
                    target_timestamp = record.get("timestamp")
                    break
        if target_timestamp is not None:
            self.restore_rewound_entries_by_timestamp(
                session_id, target_timestamp, project_dir=project_dir,
                extra_history_roots=extra_history_roots,
                discarded=True,
            )

        return change_set_id

    def _enrich_with_change_sets(
        self,
        session_id: str,
        turns: list[dict[str, Any]],
        repo_context: dict[str, Any] | None = None,
    ) -> None:
        """惰性回填 change_set 索引并合并元数据到每轮 turn。"""
        if not turns:
            return
        with _CHANGE_SET_LOCK:
            existing = self._load_change_sets(session_id)
            index_by_turn: dict[int, dict[str, Any]] = {}
            for entry in existing:
                ti = entry.get("turn_index")
                if isinstance(ti, int):
                    index_by_turn[ti] = entry

            new_entries: list[dict[str, Any]] = []
            for turn in turns:
                turn_index = int(turn.get("turnIndex", 0) or 0)
                entry = index_by_turn.get(turn_index)
                if entry is not None and self._entry_matches_turn(entry, turn):
                    self._apply_change_set_entry(turn, entry)
                else:
                    entry = self._new_change_set_entry(
                        session_id, turn, turn_index, repo_context=repo_context,
                    )
                    self._apply_change_set_entry(turn, entry)
                    new_entries.append(entry)
                    index_by_turn[turn_index] = entry
                self._save_turn_snapshot(session_id, turn)

            if new_entries:
                all_entries = sorted(
                    list(index_by_turn.values()),
                    key=lambda e: e.get("turn_index", 0),
                )
                self._save_change_sets(session_id, all_entries)

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
    def _find_assistant_message_id(
        history: list[dict[str, Any]], user_index: int
    ) -> str:
        """查找当前 user turn 后第一条 assistant 消息 ID。"""
        request_id = str(history[user_index].get("request_id", "") or "")
        for j in range(user_index + 1, len(history)):
            record = history[j]
            if record.get("role") == "user":
                break
            if record.get("role") != "assistant":
                continue
            if request_id:
                assistant_request_id = str(record.get("request_id", "") or "")
                if assistant_request_id and assistant_request_id != request_id:
                    continue
            return str(record.get("id", "") or "")
        return ""

    @staticmethod
    def _read_history(session_id: str) -> list[dict[str, Any]]:
        """读取 session history."""
        try:
            return load_history_records(session_id)
        except Exception:
            return []

    @staticmethod
    def resolve_project_dir(session_id: str) -> str | None:
        """解析 session 的项目目录(``_get_project_dir_from_metadata`` 的公开入口).

        调用方若随后会写 ``metadata.json``(如 ``rewind_session`` 调
        ``update_session_metadata``)，**必须在写之前**调用本函数并把结果显式
        传给下游，不要让下游自己去推断：``metadata.json`` 是非原子的原地覆写
        且由后台线程执行，下游读到半截文件会 ``JSONDecodeError`` → 静默返回
        ``None`` → 扫不到项目目录下的 file_ops → 整个清理变成无声的空操作。
        """
        return DiffService._get_project_dir_from_metadata(session_id)

    @staticmethod
    def _get_project_dir_from_metadata(session_id: str) -> str | None:
        """从 session metadata.json 中读取项目目录.

        读取顺序(任一命中即返回):
          1. ``channel_metadata.cwd`` (TUI 等显式传 cwd 的通道)
          2. ``delivery_context.route_metadata.cwd`` (路由元数据中的 cwd)
          3. 顶层 ``project_dir`` (Web 等通道由 ``init_session_metadata`` 写入)

        前两者是历史路径,保留以向后兼容;顶层 ``project_dir`` 是新 schema
        (``init_session_metadata`` 创建会话时必写),覆盖 Web/code 模式等
        ``channel_metadata`` 不含 ``cwd`` 的场景,避免 file_ops 漏读项目目录。
        """
        metadata_file = get_agent_sessions_dir() / session_id / "metadata.json"
        if not metadata_file.exists():
            return None
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            # 1. channel_metadata.cwd
            channel_meta = metadata.get("channel_metadata", {})
            if isinstance(channel_meta, dict):
                cwd = channel_meta.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    return cwd.strip()
            # 2. delivery_context.route_metadata.cwd
            delivery_ctx = metadata.get("delivery_context", {})
            if isinstance(delivery_ctx, dict):
                route_meta = delivery_ctx.get("route_metadata", {})
                if isinstance(route_meta, dict):
                    cwd = route_meta.get("cwd")
                    if isinstance(cwd, str) and cwd.strip():
                        return cwd.strip()
            # 3. 顶层 project_dir (新 schema,init_session_metadata 写入)
            top_level = metadata.get("project_dir")
            if isinstance(top_level, str) and top_level.strip():
                return top_level.strip()
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Failed to read metadata file %s: %s", metadata_file, e)
        return None

    @staticmethod
    def _is_valid_file_ops_file(
        name: str, session_id: str | None, require_session: bool = False
    ) -> bool:
        """检查文件名是否是有效的 file_ops 文件.

        文件名约定: ``file_ops_{agent_id}_{session_id}.json``,其中 session_id
        始终是 ``.json`` 前的最后一段。使用 ``_{session_id}.json`` 后缀匹配替代
        子串匹配,避免短 session_id 误匹配其他 agent 的 file_ops 文件。

        当 session_id 对应的是父会话时,也接受子 agent 会话(后缀形如
        ``_sub_{type}_{suffix}``)的 file_ops 文件,使 diff 统计能覆盖子 agent
        的文件变更。
        """
        if not name.startswith("file_ops_"):
            return False
        if not name.endswith(".json"):
            return False
        if not session_id:
            return not require_session
        suffix = f"_{session_id}.json"
        if name.endswith(suffix):
            return True
        sub_marker = f"_{session_id}_sub_"
        marker_pos = name.find(sub_marker, len("file_ops_"))
        if marker_pos < 0:
            return False
        agent_id = name[len("file_ops_"):marker_pos]
        return bool(agent_id) and "_" not in agent_id

    @staticmethod
    def _agent_history_dirs_for_roots(
        history_roots: list[str],
        *,
        include_child_workspaces: bool = False,
    ) -> list[Path]:
        """Return .agent_history dirs for roots and optional immediate workspaces."""
        result: list[Path] = []
        seen_history_dirs: set[Path] = set()

        def add_history_dir(hist_dir: Path) -> None:
            try:
                key = hist_dir.resolve()
            except Exception:
                key = hist_dir
            if key in seen_history_dirs:
                return
            seen_history_dirs.add(key)
            result.append(hist_dir)

        for history_root in history_roots:
            root = Path(history_root)
            add_history_dir(root / ".agent_history")
            if not include_child_workspaces:
                continue
            if not root.is_dir():
                continue
            try:
                children = list(root.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    add_history_dir(child / ".agent_history")
        return result

    @staticmethod
    def _default_worktree_history_roots(project_dir: str | None) -> list[str]:
        """Return known local worktree container dirs under the project root."""
        if not project_dir:
            return []
        root = Path(project_dir)
        return [str(root.joinpath(*parts)) for parts in WORKTREE_HISTORY_CONTAINERS]

    @classmethod
    def _history_roots_with_worktree_containers(
        cls,
        project_dir: str | None,
        extra_history_roots: list[str] | None = None,
    ) -> list[str]:
        """Return explicit roots plus known worktree containers below each root."""
        roots: list[str] = []
        seen: set[str] = set()

        def add_root(value: str | None) -> None:
            raw = str(value or "").strip()
            if not raw:
                return
            try:
                key = os.path.normcase(str(Path(raw).expanduser().resolve()))
            except Exception:
                key = os.path.normcase(raw)
            if key in seen:
                return
            seen.add(key)
            roots.append(raw)

        for root in cls._default_worktree_history_roots(project_dir):
            add_root(root)
        for root in extra_history_roots or []:
            if not isinstance(root, str):
                continue
            raw = root.strip()
            if not raw:
                continue
            add_root(raw)
            for child_root in cls._default_worktree_history_roots(raw):
                add_root(child_root)
        return roots

    @staticmethod
    def _get_git_common_worktree_root(worktree_root: Path) -> Path | None:
        """Return the canonical repo root for a linked git worktree, if known."""
        if not worktree_root.is_dir():
            return None
        common_dir = DiffService._run_git_command(
            str(worktree_root),
            ["rev-parse", "--git-common-dir"],
        )
        if not common_dir or not common_dir.strip():
            return None
        common_path = Path(common_dir.strip())
        if not common_path.is_absolute():
            common_path = worktree_root / common_path
        try:
            common_path = common_path.resolve()
        except OSError:
            pass
        if common_path.name != ".git":
            return None
        canonical_root = common_path.parent
        try:
            worktree_resolved = worktree_root.resolve()
            canonical_resolved = canonical_root.resolve()
        except OSError:
            return None
        if worktree_resolved == canonical_resolved:
            return None
        return canonical_resolved

    @staticmethod
    def _map_worktree_file_path(
        file_path: str,
        *,
        source_root: Path,
        target_root: Path | None,
    ) -> str:
        """Map a file-op path from a linked worktree back to the canonical repo."""
        if target_root is None:
            return file_path
        try:
            path = Path(file_path).expanduser().resolve()
            source = source_root.expanduser().resolve()
            rel = path.relative_to(source)
        except Exception:
            return file_path
        return str(target_root / rel)

    def _read_agent_history(
        self,
        session_id: str | None = None,
        project_dir: str | None = None,
        *,
        extra_history_roots: list[str] | None = None,
        include_rewound: bool = False
    ) -> dict[str, Any]:
        """读取 .agent_history（同时读取全局与 session-specific 文件并合并）.

        Args:
            session_id: 若提供，额外扫描匹配该 session 的 file_ops 文件。
            project_dir: 项目目录路径，若提供则也从项目目录读取 .agent_history。
            extra_history_roots: 额外写入根目录，例如 team/member workspace。
            include_rewound: 是否包含被标记为 ``rewound_out`` / ``discarded_out``
                的条目(软删除快照)。默认 ``False``——**显示层**(turn diff)不应
                看到它们,否则会展示已被回退掉的 turn 的改动。**还原层**
                (``get_files_to_restore`` / ``get_files_to_redo``) 必须传 ``True``:
                这些快照仍持有文件的原始/修改后内容,是回滚/重做能力的唯一来源。
                详见 ``truncate_file_ops_by_timestamp`` 的 ``soft`` 参数。
        """
        result: dict[str, Any] = {}
        history_file_priorities: dict[str, int] = {}

        def path_key(path: Path) -> str:
            try:
                return os.path.normcase(str(path.resolve()))
            except OSError:
                return os.path.normcase(str(path))

        def add_history_file(path: Path, priority: int) -> None:
            paths.append(path)
            history_file_priorities.setdefault(path_key(path), priority)

        # 1. 从 Agent Workspace 和 User Workspace 读取（公共位置）
        paths: list[Path] = []
        add_history_file(
            get_agent_workspace_dir() / ".agent_history" / f"file_ops_{self._agent_id}.json",
            HISTORY_PRIORITY_SHARED_WORKSPACE,
        )
        add_history_file(
            get_user_workspace_dir() / ".agent_history" / f"file_ops_{self._agent_id}.json",
            HISTORY_PRIORITY_SHARED_WORKSPACE,
        )

        # 2. session-specific file_ops（如 file_ops_jiuwenswarm_tui_xxx.json）
        if session_id:
            for base_dir in (get_agent_workspace_dir(), get_user_workspace_dir()):
                hist_dir = base_dir / ".agent_history"
                if not hist_dir.is_dir():
                    continue
                for f in hist_dir.iterdir():
                    name = f.name
                    if self._is_valid_file_ops_file(name, session_id, require_session=True):
                        add_history_file(f, HISTORY_PRIORITY_SHARED_WORKSPACE)

        # 3. 从项目目录读取（实际写入位置）
        # 如果未传入 project_dir，尝试从 session metadata 获取
        if project_dir is None and session_id:
            project_dir = self._get_project_dir_from_metadata(session_id)
        if project_dir:
            project_hist_dir = Path(project_dir) / ".agent_history"
            if project_hist_dir.is_dir():
                for f in project_hist_dir.iterdir():
                    name = f.name
                    if self._is_valid_file_ops_file(name, session_id):
                        add_history_file(f, HISTORY_PRIORITY_PROJECT_ROOT)
                global_file = project_hist_dir / f"file_ops_{self._agent_id}.json"
                if global_file.exists():
                    add_history_file(global_file, HISTORY_PRIORITY_PROJECT_ROOT)
        extra_roots = self._history_roots_with_worktree_containers(
            project_dir,
            extra_history_roots,
        )

        for project_hist_dir in self._agent_history_dirs_for_roots(
            extra_roots,
            include_child_workspaces=True,
        ):
            if project_hist_dir.is_dir():
                # 读取 session-specific file_ops 文件
                for f in project_hist_dir.iterdir():
                    name = f.name
                    if self._is_valid_file_ops_file(name, session_id):
                        add_history_file(f, HISTORY_PRIORITY_EXTRA_ROOT)
                # 也读取全局 file_ops 文件（不带 session_id 后缀的）
                global_file = project_hist_dir / f"file_ops_{self._agent_id}.json"
                if global_file.exists():
                    add_history_file(global_file, HISTORY_PRIORITY_EXTRA_ROOT)

        worktree_root_cache: dict[Path, Path | None] = {}

        def mapped_file_path_for_history(file_path: str, history_file: Path) -> str:
            source_root = history_file.parent.parent
            try:
                source_key = source_root.resolve()
            except OSError:
                source_key = source_root
            if source_key not in worktree_root_cache:
                worktree_root_cache[source_key] = self._get_git_common_worktree_root(source_root)
            return self._map_worktree_file_path(
                file_path,
                source_root=source_root,
                target_root=worktree_root_cache[source_key],
            )

        # 用于规范化路径，避免大小写差异导致的重复
        def normalize_path(p: str) -> str:
            """规范化路径：统一大小写和斜杠方向"""
            # 使用 pathlib.Path 规范化路径
            try:
                return str(Path(p).resolve())
            except OSError:
                return p.replace("\\", "/").lower()

        def comparable_path_key(p: str) -> str:
            return p.lower()

        result_entry_priorities: dict[str, list[int]] = {}
        result_path_by_comparable_key: dict[str, str] = {}

        for history_file in paths:
            if history_file.exists():
                try:
                    data = json.loads(history_file.read_text(encoding="utf-8"))
                    history_priority = history_file_priorities.get(
                        path_key(history_file),
                        HISTORY_PRIORITY_UNKNOWN,
                    )
                    for file_path, entries in data.items():
                        mapped_file_path = mapped_file_path_for_history(file_path, history_file)
                        # 规范化路径，避免大小写差异导致的重复
                        normalized_path = normalize_path(mapped_file_path)
                        comparable_key = comparable_path_key(normalized_path)
                        normalized_path = result_path_by_comparable_key.setdefault(
                            comparable_key,
                            normalized_path,
                        )
                        if normalized_path not in result:
                            result[normalized_path] = []
                            result_entry_priorities[normalized_path] = []
                        # 合并条目，避免时间戳相近的重复记录
                        for entry in entries:
                            # 软删除的快照默认对显示层不可见（见 include_rewound）
                            if not include_rewound and (
                                entry.get(_REWOUND_KEY) or entry.get(_DISCARDED_KEY)
                            ):
                                continue
                            # 检查是否已存在相同时间戳（±1秒）的相同操作
                            ts = entry.get("timestamp", "")
                            action = entry.get("action", "")
                            is_duplicate = False
                            duplicate_index: int | None = None
                            for idx, existing in enumerate(result[normalized_path]):
                                existing_ts = existing.get("timestamp", "")
                                existing_action = existing.get("action", "")
                                same_content = (
                                    entry.get("old_content") == existing.get("old_content")
                                    and entry.get("new_content") == existing.get("new_content")
                                )
                                if action == existing_action and same_content:
                                    # 比较时间戳是否相近（同一秒内）
                                    try:
                                        t1 = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                        t2 = datetime.fromisoformat(existing_ts.replace("Z", "+00:00"))
                                        if abs((t1 - t2).total_seconds()) < 2:
                                            is_duplicate = True
                                            duplicate_index = idx
                                            break
                                    except (ValueError, TypeError):
                                        # 时间戳格式无效，无法比较，跳过此条目比较
                                        continue
                            if is_duplicate:
                                priorities = result_entry_priorities[normalized_path]
                                if (
                                    duplicate_index is not None
                                    and duplicate_index < len(priorities)
                                    and history_priority < priorities[duplicate_index]
                                ):
                                    result[normalized_path][duplicate_index] = entry
                                    priorities[duplicate_index] = history_priority
                            else:
                                result[normalized_path].append(entry)
                                result_entry_priorities[normalized_path].append(history_priority)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to read agent history file %s: %s", history_file, e)

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

    # 与 str.splitlines() 一致的换行符集合（\r\n 计作一个换行）。
    _LINE_BREAK_RE = re.compile(r"\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")
    _LINE_BREAK_CHARS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"

    @staticmethod
    def _count_text_lines(content: str) -> int:
        """Return ``str.splitlines()``'s line count without allocating a line list."""
        if not content:
            return 0

        count = sum(1 for _ in DiffService._LINE_BREAK_RE.finditer(content))
        return count if content[-1] in DiffService._LINE_BREAK_CHARS else count + 1

    @staticmethod
    def _compute_hunks(
        old_content: str | None,
        new_content: str | None,
        *,
        max_diff_bytes: int | None = None,
    ) -> tuple[list[dict[str, Any]], int, int, int, bool]:
        """计算结构化 diff hunks（与 ``git diff --unified=3`` 对齐）。

        返回 ``(hunks, lines_added, lines_removed, diff_bytes, is_large)``。
        ``max_diff_bytes`` 用于历史 diff：超过额度时立即停止构造 hunk，避免先
        展开整份大文件预览再丢弃；行数统计仍按完整改动返回。
        """
        # 处理删除文件的情况：new_content 为 None
        if new_content is None:
            if old_content is None:
                return [], 0, 0, 0, False
            # 对单边变更而言，字符数超过额度已经足以判定为大文件。先在此处
            # 返回，避免 splitlines/f-string 为超大历史写入额外分配整份内容。
            if max_diff_bytes is not None and len(old_content) > max_diff_bytes:
                return [], 0, DiffService._count_text_lines(old_content), max_diff_bytes + 1, True
            # 文件被删除：显示所有行被移除
            lines = old_content.splitlines()
            diff_bytes = sum(
                len(f"-{line}".encode("utf-8", errors="replace")) for line in lines
            )
            if max_diff_bytes is not None and diff_bytes > max_diff_bytes:
                return [], 0, len(lines), max_diff_bytes + 1, True
            return [{
                "oldStart": 1,
                "oldLines": len(lines),
                "newStart": 0,
                "newLines": 0,
                "lines": [f"-{line}" for line in lines],
            }], 0, len(lines), diff_bytes, False

        # 处理新建文件的情况：old_content 为 None
        if old_content is None:
            # 同删除路径：新建大文件无需构造 hunk，统计行数以无额外大列表的
            # 方式计算。
            if max_diff_bytes is not None and len(new_content) > max_diff_bytes:
                return [], DiffService._count_text_lines(new_content), 0, max_diff_bytes + 1, True
            lines = new_content.splitlines()
            diff_bytes = sum(
                len(f"+{line}".encode("utf-8", errors="replace")) for line in lines
            )
            if max_diff_bytes is not None and diff_bytes > max_diff_bytes:
                return [], len(lines), 0, max_diff_bytes + 1, True
            return [{
                "oldStart": 0,
                "oldLines": 0,
                "newStart": 1,
                "newLines": len(lines),
                "lines": [f"+{line}" for line in lines],
            }], len(lines), 0, diff_bytes, False

        if old_content == new_content:
            return [], 0, 0, 0, False

        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)

        if not old_lines and not new_lines:
            return [], 0, 0, 0, False

        # Emit unified hunks with context_lines of surrounding context and
        # merge adjacent changes whose context windows overlap, matching
        # `git diff --unified=3` / jsdiff's structuredPatch. The previous
        # implementation skipped equal opcodes entirely, producing context-less
        # isolated hunks that showed far less content than `git diff`.
        context_lines = 3
        opcodes = difflib.SequenceMatcher(
            None, old_lines, new_lines
        ).get_opcodes()
        n_old = len(old_lines)
        n_new = len(new_lines)

        hunks: list[dict[str, Any]] = []
        lines_added = sum(
            j2 - j1 for tag, _i1, _i2, j1, j2 in opcodes
            if tag in ("insert", "replace")
        )
        lines_removed = sum(
            i2 - i1 for tag, i1, i2, _j1, _j2 in opcodes
            if tag in ("delete", "replace")
        )
        diff_bytes = 0

        i = 0
        while i < len(opcodes):
            tag, i1, i2, j1, j2 = opcodes[i]
            if tag == "equal":
                i += 1
                continue

            # First change of this hunk is at opcodes[i]; absorb following
            # changes whose separating equal run is short enough that their
            # context windows bridge the gap (run length <= 2*context_lines).
            o_lo = max(0, i1 - context_lines)
            n_lo = max(0, j1 - context_lines)
            last_i2 = i2
            last_j2 = j2
            k = i + 1
            while k < len(opcodes):
                ntag, ni1, ni2, nj1, nj2 = opcodes[k]
                if ntag == "equal":
                    if (ni2 - ni1) > 2 * context_lines:
                        break
                    k += 1
                    continue
                last_i2 = ni2
                last_j2 = nj2
                k += 1

            o_hi = min(n_old, last_i2 + context_lines)
            n_hi = min(n_new, last_j2 + context_lines)

            # Include the leading equal opcode (i-1) and the trailing equal
            # opcode (k, when present) so leading/trailing context lines are
            # emitted; both are clamped to the window below.
            start_idx = (
                i - 1 if i - 1 >= 0 and opcodes[i - 1][0] == "equal" else i
            )
            end_idx = (
                k + 1
                if k < len(opcodes) and opcodes[k][0] == "equal"
                else k
            )

            lines: list[str] = []
            for idx in range(start_idx, end_idx):
                tag2, ii1, ii2, jj1, jj2 = opcodes[idx]
                if tag2 == "equal":
                    for m in range(max(ii1, o_lo), min(ii2, o_hi)):
                        rendered = f" {old_lines[m].rstrip()}"
                        diff_bytes += len(rendered.encode("utf-8", errors="replace"))
                        if max_diff_bytes is not None and diff_bytes > max_diff_bytes:
                            return [], lines_added, lines_removed, max_diff_bytes + 1, True
                        lines.append(rendered)
                elif tag2 == "delete":
                    for m in range(max(ii1, o_lo), min(ii2, o_hi)):
                        rendered = f"-{old_lines[m].rstrip()}"
                        diff_bytes += len(rendered.encode("utf-8", errors="replace"))
                        if max_diff_bytes is not None and diff_bytes > max_diff_bytes:
                            return [], lines_added, lines_removed, max_diff_bytes + 1, True
                        lines.append(rendered)
                elif tag2 == "insert":
                    for m in range(max(jj1, n_lo), min(jj2, n_hi)):
                        rendered = f"+{new_lines[m].rstrip()}"
                        diff_bytes += len(rendered.encode("utf-8", errors="replace"))
                        if max_diff_bytes is not None and diff_bytes > max_diff_bytes:
                            return [], lines_added, lines_removed, max_diff_bytes + 1, True
                        lines.append(rendered)
                else:  # replace
                    for m in range(max(ii1, o_lo), min(ii2, o_hi)):
                        rendered = f"-{old_lines[m].rstrip()}"
                        diff_bytes += len(rendered.encode("utf-8", errors="replace"))
                        if max_diff_bytes is not None and diff_bytes > max_diff_bytes:
                            return [], lines_added, lines_removed, max_diff_bytes + 1, True
                        lines.append(rendered)
                    for m in range(max(jj1, n_lo), min(jj2, n_hi)):
                        rendered = f"+{new_lines[m].rstrip()}"
                        diff_bytes += len(rendered.encode("utf-8", errors="replace"))
                        if max_diff_bytes is not None and diff_bytes > max_diff_bytes:
                            return [], lines_added, lines_removed, max_diff_bytes + 1, True
                        lines.append(rendered)

            hunks.append({
                "oldStart": o_lo + 1,
                "oldLines": o_hi - o_lo,
                "newStart": n_lo + 1,
                "newLines": n_hi - n_lo,
                "lines": lines,
            })
            i = k

        return hunks, lines_added, lines_removed, diff_bytes, False

    @staticmethod
    def _decode_c_escaped(inner: str) -> str:
        """Decode git's C-style escapes in an already-unquoted path segment.

        Handles ``\\t \\n \\r \\a \\b \\v \\f \\" \\\\`` , octal ``\\NNN`` and
        hex ``\\xNN``; unknown escapes keep the backslash literally.
        """
        simple = {
            "a": 0x07, "b": 0x08, "t": 0x09, "n": 0x0A,
            "v": 0x0B, "f": 0x0C, "r": 0x0D, '"': 0x22, "\\": 0x5C,
        }
        out = bytearray()
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch != "\\":
                out.extend(ch.encode("utf-8"))
                i += 1
                continue
            i += 1
            if i >= len(inner):
                break
            esc = inner[i]
            if esc in simple:
                out.append(simple[esc])
                i += 1
                continue
            if esc == "x":
                hexd = inner[i + 1:i + 3]
                if len(hexd) == 2 and all(c in "0123456789abcdefABCDEF" for c in hexd):
                    out.append(int(hexd, 16) & 0xFF)
                    i += 3
                    continue
            if esc in "01234567":
                j = i
                oct_digits = ""
                while j < len(inner) and inner[j] in "01234567" and len(oct_digits) < 3:
                    oct_digits += inner[j]
                    j += 1
                out.append(int(oct_digits, 8) & 0xFF)
                i = j
                continue
            # Unknown escape: keep backslash and the char literally.
            out.extend(b"\\")
            out.extend(esc.encode("utf-8"))
            i += 1
        return out.decode("utf-8", errors="replace")

    @staticmethod
    def _unquote_git_path(path: str) -> str:
        """Decode a git-quoted path back to its raw bytes.

        git wraps paths containing control chars / quotes / backslashes in
        double quotes and C-escapes the offending bytes. This quoting is
        independent of ``core.quotepath`` (which only governs non-ASCII bytes),
        so a literal TAB in a filename is emitted as ``"dir\\tfile.txt"``
        regardless of that setting. Feeding the quoted form straight into
        ``Path(repo) / path`` resolves to a non-existent file, so numstat,
        diff headers and ls-files paths must be unquoted here to match the
        real on-disk relative path. Unquoted paths are returned verbatim.
        """
        if not (len(path) >= 2 and path.startswith('"') and path.endswith('"')):
            return path
        return DiffService._decode_c_escaped(path[1:-1])

    @staticmethod
    def _extract_diff_header_path(token: str) -> str | None:
        """Extract the on-disk relative path from a ``--- a/`` / ``+++ b/`` token.

        git quotes the whole ``a/<path>`` / ``b/<path>`` form when the path
        contains control chars (e.g. ``+++ "b/dir\\tfile.txt"``), so the prefix
        lives inside the quotes. Strip the prefix and decode, returning the
        real relative path, or ``None`` for ``/dev/null`` (deleted/new file
        counterpart).
        """
        if token == "/dev/null":
            return None
        quoted = len(token) >= 2 and token.startswith('"') and token.endswith('"')
        inner = token[1:-1] if quoted else token
        for prefix in ("b/", "a/"):
            if inner.startswith(prefix):
                rel = inner[len(prefix):]
                return DiffService._decode_c_escaped(rel) if quoted else rel
        return DiffService._decode_c_escaped(inner) if quoted else inner

    @staticmethod
    def _run_git_command(project_dir: str, args: list[str]) -> str | None:
        """在 project_dir 中运行 git 命令，返回 stdout 或 None."""
        import subprocess

        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=project_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            if result.returncode != 0:
                return None
            return result.stdout
        except Exception:
            return None

    @staticmethod
    def _run_git_diff_limited(
        project_dir: str,
        args: list[str],
        *,
        max_bytes: int = MAX_DIFF_SIZE_BYTES,
    ) -> tuple[str | None, bool]:
        """运行单文件 ``git diff``，并在超过阈值时停止保留输出。

        stdout 仍会被持续读取直至子进程退出，以免 Git 因管道写满而阻塞；但
        超过 ``max_bytes`` 后不再在 Python 中累积内容。返回 ``(output,
        is_large)``；执行失败时返回 ``(None, False)``。
        """
        import subprocess

        try:
            process = subprocess.Popen(
                ["git", *args],
                cwd=project_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if process.stdout is None:
                return None, False

            output = bytearray()
            is_large = False
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                if is_large:
                    continue
                if len(output) + len(chunk) > max_bytes:
                    output.clear()
                    is_large = True
                else:
                    output.extend(chunk)

            if process.wait(timeout=10) != 0:
                return None, False
            if is_large:
                return None, True
            return output.decode("utf-8", errors="replace"), False
        except Exception:
            return None, False

    @staticmethod
    def _get_git_toplevel(project_dir: str) -> str | None:
        """返回 git 仓库根目录；project_dir 可以是仓库内任意子目录."""
        import subprocess

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
            )
            if result.returncode != 0:
                return None
            root = result.stdout.strip()
            return str(Path(root).resolve()) if root else None
        except Exception:
            return None

    @staticmethod
    def _is_in_transient_git_state(project_dir: str) -> bool:
        """检测是否处于 merge/rebase/cherry-pick/revert 等瞬态 git 状态.

        这些状态下工作区包含 incoming 改动（非用户意图编辑），
        应跳过 diff 计算以避免显示误导性内容。
        """
        import subprocess

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
            )
            if result.returncode != 0:
                return False
            git_dir = Path(result.stdout.strip())
            if not git_dir.is_absolute():
                git_dir = Path(project_dir) / git_dir
        except Exception:
            return False

        transient_files = [
            "MERGE_HEAD",
            "REBASE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
        ]
        return any((git_dir / name).exists() for name in transient_files)

    @staticmethod
    def _parse_git_numstat(output: str) -> dict[str, dict[str, int | bool]]:
        """解析 git diff --numstat 输出为 per-file 统计.

        输入格式:
            3\t2\tpath/to/file.py
            -\t-\tbinary_file.png

        返回:
            { "/abs/path/file.py": {"added": 3, "removed": 2, "isBinary": false}, ... }
        """
        result: dict[str, dict[str, int | bool]] = {}
        for line in output.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added_str, removed_str = parts[0], parts[1]
            file_path = "\t".join(parts[2:])
            # rename 的 numstat 路径需归一化为新路径，否则与 _parse_git_diff_hunks
            # 从 "+++ b/new" 提取的 key 不一致，导致 hunks 丢失。
            # 两种形式:
            #   - brace 简写(有共同前/后缀): a/{b => c}/d.txt  -> a/c/d.txt
            #     (可能嵌套或多段: a/{b => c}/{d => e}.txt)
            #   - 裸形式(无共同前/后缀): old => new  -> new
            while True:
                m = re.search(r"\{([^{}]*)\s=>\s([^{}]*)\}", file_path)
                if not m:
                    break
                file_path = file_path[:m.start()] + m.group(2) + file_path[m.end():]
            if " => " in file_path:
                file_path = file_path.rsplit(" => ", 1)[-1].strip()
            # 控制字符路径（如含 TAB）会被 git 加引号并 C 转义（与
            # core.quotepath 无关），解码回原始字节串才能对应磁盘真实文件。
            file_path = DiffService._unquote_git_path(file_path)
            is_binary = added_str == "-" and removed_str == "-"
            result[file_path] = {
                "added": 0 if is_binary else int(added_str),
                "removed": 0 if is_binary else int(removed_str),
                "isBinary": is_binary,
            }
        return result

    @staticmethod
    def _parse_git_name_status(output: str) -> dict[str, str]:
        """解析 git diff --name-status 输出为路径到状态的映射。"""
        result: dict[str, str] = {}
        for line in (output or "").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code = parts[0].strip()
            status_letter = code[0] if code else "M"
            if status_letter == "R" and len(parts) >= 3:
                path = "\t".join(parts[2:])
            else:
                path = "\t".join(parts[1:])
            path = DiffService._unquote_git_path(path)
            status = {
                "A": "added",
                "D": "deleted",
                "R": "renamed",
                "C": "added",
                "T": "modified",
                "M": "modified",
            }.get(status_letter, "modified")
            result[path] = status
        return result

    @staticmethod
    def _parse_git_porcelain_status(output: str) -> dict[str, str]:
        """解析 git status --porcelain=v1 输出为路径到状态的映射。"""
        result: dict[str, str] = {}
        for line in (output or "").splitlines():
            if len(line) < 4:
                continue
            x_status = line[0]
            y_status = line[1]
            raw_path = line[3:]
            if raw_path.startswith('"'):
                path = DiffService._unquote_git_path(raw_path)
            elif " -> " in raw_path:
                path = raw_path.rsplit(" -> ", 1)[-1]
            else:
                path = raw_path
            path = DiffService._unquote_git_path(path.strip())

            if x_status == "?" and y_status == "?":
                status = "added"
            elif "R" in (x_status, y_status):
                status = "renamed"
            elif y_status == "D" and x_status != "D":
                status = "missing"
            elif x_status == "D" or y_status == "D":
                status = "deleted"
            elif x_status == "A" or y_status == "A":
                status = "added"
            else:
                status = "modified"
            result[path] = status
        return result

    @staticmethod
    def _parse_shortstat(output: str) -> dict[str, int] | None:
        """解析 git diff --shortstat 输出.

        格式: " N files changed, N insertions(+), N deletions(-)"
        用于在加载完整 diff 前快速探测规模。
        """
        match = re.match(
            r"(\d+)\s+files?\s+changed(?:,\s+(\d+)\s+insertions?\(\+\))?(?:,\s+(\d+)\s+deletions?\(-\))?",
            output.strip(),
        )
        if not match:
            return None
        return {
            "filesChanged": int(match.group(1) or "0"),
            "linesAdded": int(match.group(2) or "0"),
            "linesRemoved": int(match.group(3) or "0"),
        }

    @staticmethod
    def _parse_git_diff_hunks(output: str) -> dict[str, list[dict[str, Any]]]:
        """解析 git diff 输出为按文件分组的 hunk 列表.

        每个 hunk 格式与 _compute_hunks() 一致:
            {
                "oldStart": int, "oldLines": int,
                "newStart": int, "newLines": int,
                "lines": ["-removed line", "+added line", " context line"],
            }
        """
        files: dict[str, list[dict[str, Any]]] = {}
        current_file: str | None = None
        current_hunk: dict[str, Any] | None = None

        # 匹配 diff 头部: --- a/path, +++ b/path
        # 控制字符路径会被整体加引号（如 +++ "b/dir\tfile.txt"），b/ 前缀在引号内，
        # 所以捕获整个 token（含引号）再用 _extract_diff_header_path 剥前缀+解码。
        # 限定 b//a/ 前缀或两端引号，避免把以 "++ " 开头的 hunk 内容行（会变成
        # "+++ ..."，无 b/ 前缀也非两端引号）误判为文件头。
        # 对于删除文件，+++ b/ 行是 +++ /dev/null 不会匹配，需要回退到 --- a/ 行
        file_header_new_re = re.compile(r'^\+\+\+ (b/.*|".*")$')
        file_header_old_re = re.compile(r'^--- (a/.*|".*")$')
        # 匹配 hunk 头部: @@ -oldStart,oldLines +newStart,newLines @@
        hunk_header_re = re.compile(
            r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@"
        )

        for line in output.splitlines():
            # 检测文件头（优先 +++ b/ 行）
            file_match = file_header_new_re.match(line)
            if file_match:
                resolved = DiffService._extract_diff_header_path(file_match.group(1))
                if resolved is not None:
                    current_file = resolved
                    if current_file not in files:
                        files[current_file] = []
                    current_hunk = None
                continue

            # 回退：对于删除文件，+++ b/ 不匹配（是 +++ /dev/null），
            # 从 --- a/ 行提取文件路径
            file_match = file_header_old_re.match(line)
            if file_match:
                resolved = DiffService._extract_diff_header_path(file_match.group(1))
                if resolved is not None:
                    current_file = resolved
                    if current_file not in files:
                        files[current_file] = []
                    current_hunk = None
                continue

            if current_file is None:
                continue

            # 检测 hunk 头
            hunk_match = hunk_header_re.match(line)
            if hunk_match:
                old_start = int(hunk_match.group(1))
                old_lines = int(hunk_match.group(2) or "1")
                new_start = int(hunk_match.group(3))
                new_lines = int(hunk_match.group(4) or "1")

                current_hunk = {
                    "oldStart": old_start,
                    "oldLines": old_lines,
                    "newStart": new_start,
                    "newLines": new_lines,
                    "lines": [],
                }
                files[current_file].append(current_hunk)
                continue

            if current_hunk is None:
                continue

            # 收集 hunk 行（+, -, 空格前缀的上下文行）。超过 MAX_DIFF_SIZE_BYTES
            # 的大文件 diff 块已在 _split_large_file_diffs 整体剔除，此处剩余 diff
            # 均 ≤ 1MB，整段返回不做行截断，与 untracked 大文件口径对齐。
            if line.startswith("+") or line.startswith("-") or line.startswith(" "):
                current_hunk["lines"].append(line)

        return files

    @staticmethod
    def _split_large_file_diffs(
        output: str,
    ) -> tuple[str, set[str]]:
        """将 git diff 输出按文件切分，跳过超过 MAX_DIFF_SIZE_BYTES 的文件块.

        返回 (过滤后的 diff 输出, 被跳过的大文件路径集合)。
        被跳过的文件不参与 hunk 解析，但 numstat 统计仍会保留。
        """
        if not output:
            return "", set()
        # 以 "diff --git " 为分隔切分（首段通常为空）
        chunks = output.split("diff --git ")
        kept: list[str] = []
        large_files: set[str] = set()
        for chunk in chunks:
            if not chunk:
                continue
            full = "diff --git " + chunk
            if len(full.encode("utf-8", errors="replace")) > MAX_DIFF_SIZE_BYTES:
                # 提取文件路径用于标记。路径可能被引号包裹（控制字符），
                # 需用 _extract_diff_header_path 解码以与 numstat key 对齐。
                m = re.search(r'^\+\+\+ (b/.*|".*")$', full, re.MULTILINE)
                if m:
                    resolved = DiffService._extract_diff_header_path(m.group(1))
                    if resolved is not None:
                        large_files.add(resolved)
                else:
                    m2 = re.search(r'^--- (a/.*|".*")$', full, re.MULTILINE)
                    if m2:
                        resolved = DiffService._extract_diff_header_path(m2.group(1))
                        if resolved is not None:
                            large_files.add(resolved)
                continue
            kept.append(full)
        return "".join(kept), large_files

    def _get_untracked_files(
        self,
        project_dir: str,
        max_files: int = MAX_FILES,
        *,
        include_hunks: bool = True,
        hunk_paths: set[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """获取未跟踪文件列表，并读取内容计算行数与 hunk.

        与 tracked 文件走 ``git diff HEAD`` 不同，untracked 文件不在 git
        索引中、无 old_content 可 diff。尤其 unborn HEAD（仓库无任何
        commit）场景下 ``git diff HEAD`` 会失败，工作区改动几乎都是
        untracked；若此处仍把 ``linesAdded`` 写死为 0，会导致
        ``get_git_diff`` 返回的 ``stats.linesAdded`` 恒为 0，前端"变更"
        栏目看不到行数变化。此处将整文件视为新增 hunk，给出与 tracked
        文件一致的 stats 口径。

        二进制文件（前 8KB 出现 NUL 字节）不计行数；超过
        ``MAX_DIFF_SIZE_BYTES`` 的大文件标记 ``isLargeFile`` 且不返回内容，
        与 tracked 文件 ``_split_large_file_diffs`` 口径一致；1MB 以内整
        文件作为新增 hunk 返回，不做行截断。
        """
        # core.quotepath=false 让 git 对非 ASCII 字节直接输出原始 UTF-8 文件名
        # （而非八进制转义串），否则中文路径无法对应磁盘真实路径。但 ASCII 控制字符
        # （如 TAB）无论该设置如何都会被加引号并 C 转义（如 "dir\tfile.txt"），
        # 仍需 _unquote_git_path 解码才能对应磁盘真实文件。
        output = self._run_git_command(
            project_dir,
            ["-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard"],
        )
        if not output or not output.strip():
            return {}

        files: dict[str, dict[str, Any]] = {}
        for rel_path in output.strip().splitlines():
            if len(files) >= max_files:
                break
            rel_path = rel_path.strip()
            if not rel_path:
                continue
            rel_path = DiffService._unquote_git_path(rel_path)
            if self._is_internal_untracked_path(rel_path):
                continue
            abs_path = str(Path(project_dir) / rel_path)

            entry: dict[str, Any] = {
                "filePath": abs_path,
                "hunks": [],
                "isNewFile": True,
                "isBinary": False,
                "isLargeFile": False,
                "isTruncated": False,
                "isUntracked": True,
                "linesAdded": 0,
                "linesRemoved": 0,
                "lastEditTime": None,
            }

            # 符号链接不解引用：避免未跟踪 symlink 指向工作区外敏感/超大文件，
            # 通过 diff API 暴露内容。symlink 不计行数，hunks 留空。
            if Path(abs_path).is_symlink():
                files[abs_path] = entry
                continue

            # 二进制探测：前 8KB 出现 NUL 字节视为二进制，不计行数
            try:
                with open(abs_path, "rb") as f:
                    head = f.read(8192)
            except OSError:
                files[abs_path] = entry
                continue
            if b"\x00" in head:
                entry["isBinary"] = True
                files[abs_path] = entry
                continue

            # 流式逐行读取：完整行数计入 stats（与 tracked 文件 git numstat
            # 口径一致）。大文件判定与 tracked/turn diff 口径一致：累加渲染后
            # diff（"+{line}"）的 UTF-8 字节数，超过 MAX_DIFF_SIZE_BYTES 标记
            # isLargeFile、不返回内容（hunks 留空），不用磁盘文件大小近似。
            # 仅在 detail 层需要该文件时保留 hunk lines，避免 summary/files
            # 层为未展开文件构造整文件 hunk。
            hunk_lines: list[str] = []
            total_lines = 0
            diff_bytes = 0
            is_large = False
            wants_hunks = include_hunks and (
                hunk_paths is None or rel_path in hunk_paths or abs_path in hunk_paths
            )
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace", newline="") as f:
                    for line in f:
                        total_lines += 1
                        if is_large:
                            continue
                        stripped = line.rstrip("\r\n")
                        diff_bytes += len(f"+{stripped}".encode("utf-8", errors="replace"))
                        if diff_bytes > MAX_DIFF_SIZE_BYTES:
                            is_large = True
                            hunk_lines.clear()
                        elif wants_hunks:
                            hunk_lines.append(stripped)
            except OSError:
                files[abs_path] = entry
                continue

            if is_large:
                entry["isLargeFile"] = True
            elif wants_hunks:
                entry["hunks"] = [{
                    "oldStart": 0,
                    "oldLines": 0,
                    "newStart": 1,
                    "newLines": len(hunk_lines),
                    "lines": [f"+{line}" for line in hunk_lines],
                }]
            entry["isTruncated"] = False
            entry["linesAdded"] = total_lines
            files[abs_path] = entry

        return files

    @staticmethod
    def _is_internal_untracked_path(rel_path: str) -> bool:
        parts = Path(rel_path).parts
        return any(part in INTERNAL_UNTRACKED_DIRS for part in parts)

    @staticmethod
    def _normalize_hunk_paths(
        repo_dir: str,
        hunk_paths: list[str] | set[str] | tuple[str, ...] | None,
    ) -> set[str] | None:
        """Normalize requested detail paths to repo-relative POSIX-style paths."""
        if not hunk_paths:
            return None
        result: set[str] = set()
        for raw in hunk_paths:
            text = str(raw or "").strip()
            if not text:
                continue
            candidate = Path(text)
            rel = text
            if candidate.is_absolute():
                try:
                    rel = os.path.relpath(str(candidate), repo_dir)
                except ValueError:
                    rel = text
            rel = rel.replace("\\", "/").lstrip("/")
            result.add(rel)
        return result or None

    def get_git_diff(
        self,
        project_dir: str | None,
        *,
        include_files: bool = True,
        include_hunks: bool = True,
        hunk_paths: list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        """获取工作区相对于 HEAD 的 git diff，含未跟踪文件行数.

        已跟踪文件走 ``git diff HEAD``；untracked 文件（含 unborn HEAD
        仓库无 commit 场景）由 ``_get_untracked_files`` 读取内容计算行数
        与 hunk，并累加进 stats，避免工作区仅有新增文件时 lines_added
        恒为 0。

        Args:
            project_dir: 项目目录路径.

        Returns:
            {
                "stats": {"filesChanged": int, "linesAdded": int, "linesRemoved": int},
                "files": { file_path: { "filePath": str, "hunks": [...],
                    "isNewFile": bool, "linesAdded": int, "linesRemoved": int } }
            }
            如果不是 git 仓库或没有任何改动，返回 None.
        """
        if not project_dir:
            return None
        repo_dir = self._get_git_toplevel(project_dir)
        if not repo_dir:
            return None
        if self._is_in_transient_git_state(repo_dir):
            return None
        effective_include_files = include_files or include_hunks
        requested_hunk_paths = self._normalize_hunk_paths(repo_dir, hunk_paths)

        files: dict[str, dict[str, Any]] = {}
        total_files_changed = 0
        total_added = 0
        total_removed = 0

        # 1. 已跟踪文件的改动: git diff HEAD
        # 先用 --shortstat 快速探测规模，避免对超大 diff 加载完整内容
        shortstat = self._run_git_command(repo_dir, ["diff", "HEAD", "--shortstat"])
        has_tracked_changes = shortstat and shortstat.strip() != ""

        # 解析 shortstat 取得准确的文件/行数总计
        shortstat_stats = self._parse_shortstat(shortstat) if has_tracked_changes else None
        if shortstat_stats and shortstat_stats["filesChanged"] > MAX_FILES_FOR_DETAILS:
            # 文件数过多，仅返回统计以避免加载数百 MB 内容
            return {
                "stats": {
                    "filesChanged": shortstat_stats["filesChanged"],
                    "linesAdded": shortstat_stats["linesAdded"],
                    "linesRemoved": shortstat_stats["linesRemoved"],
                },
                "files": {},
            }

        if has_tracked_changes and not effective_include_files and shortstat_stats:
            total_files_changed += shortstat_stats["filesChanged"]
            total_added += shortstat_stats["linesAdded"]
            total_removed += shortstat_stats["linesRemoved"]
        elif has_tracked_changes:
            numstat_output = self._run_git_command(repo_dir, ["diff", "HEAD", "--numstat"])
            if numstat_output:
                per_file_stats = self._parse_git_numstat(numstat_output)
                total_files_changed += len(per_file_stats)
                total_added += sum(int(stats["added"]) for stats in per_file_stats.values())
                total_removed += sum(int(stats["removed"]) for stats in per_file_stats.values())

                if effective_include_files:
                    name_status_output = self._run_git_command(repo_dir, ["diff", "HEAD", "--name-status"])
                    porcelain_status_output = self._run_git_command(
                        repo_dir, ["-c", "core.quotepath=false", "status", "--porcelain=v1"]
                    )
                    per_file_status = self._parse_git_name_status(name_status_output or "")
                    per_file_status.update(
                        self._parse_git_porcelain_status(porcelain_status_output or "")
                    )
                else:
                    per_file_status = {}

                all_hunks: dict[str, list[dict[str, Any]]] = {}
                large_files: set[str] = set()
                if include_hunks:
                    # 一次 ``git diff`` 会先把所有文件的 patch 聚合到一个字符串，
                    # 即使稍后跳过 >1MB 的文件也已经造成峰值内存。按文件流式获取，
                    # 超过阈值后停止保留 stdout；前端详情层通常只传当前选中的路径，
                    # 因此也避免为未查看文件生成 patch。
                    if requested_hunk_paths is None:
                        detail_paths = list(per_file_stats)[:MAX_FILES]
                    else:
                        detail_paths = sorted(
                            path for path in requested_hunk_paths
                            if path in per_file_stats
                        )
                    for rel_path in detail_paths:
                        diff_output, is_large = self._run_git_diff_limited(
                            repo_dir,
                            ["--literal-pathspecs", "diff", "HEAD", "--", rel_path],
                        )
                        if is_large:
                            large_files.add(rel_path)
                            continue
                        if not diff_output:
                            continue
                        filtered_output, file_large = self._split_large_file_diffs(diff_output)
                        large_files.update(file_large)
                        if filtered_output:
                            all_hunks.update(self._parse_git_diff_hunks(filtered_output))

                if not effective_include_files:
                    per_file_stats = {}
                for rel_path, stats in list(per_file_stats.items())[:MAX_FILES]:
                    abs_path = str(Path(repo_dir) / rel_path)
                    is_binary = bool(stats.get("isBinary", False))
                    is_large = rel_path in large_files
                    if is_binary or is_large:
                        hunks = []
                    else:
                        hunks = all_hunks.get(rel_path, [])
                    lines_added = stats["added"]
                    lines_removed = stats["removed"]

                    files[abs_path] = {
                        "filePath": abs_path,
                        "status": per_file_status.get(rel_path, "modified"),
                        "hunks": hunks,
                        "isNewFile": per_file_status.get(rel_path) == "added",
                        "isDeletedFile": per_file_status.get(rel_path) == "deleted",
                        "isBinary": is_binary,
                        "isLargeFile": is_large,
                        "isTruncated": False,
                        "isUntracked": False,
                        "linesAdded": lines_added,
                        "linesRemoved": lines_removed,
                        "lastEditTime": None,
                    }

        untracked_files = self._get_untracked_files(
            repo_dir,
            max_files=max(0, MAX_FILES - len(files)) if effective_include_files else MAX_FILES,
            include_hunks=include_hunks,
            hunk_paths=requested_hunk_paths,
        )
        if not effective_include_files:
            untracked_stats_files = untracked_files
            untracked_files = {}
        else:
            untracked_stats_files = untracked_files
        for file_path, entry in untracked_files.items():
            entry["status"] = "added"
            files[file_path] = entry
        total_files_changed += len(untracked_stats_files)
        # untracked 文件无 git diff 可统计，_get_untracked_files 已按文件内容
        # 计算行数；此处补回 stats，避免 unborn HEAD 等场景下 lines_added 恒为 0。
        total_added += sum(int(f.get("linesAdded", 0) or 0) for f in untracked_stats_files.values())
        total_removed += sum(int(f.get("linesRemoved", 0) or 0) for f in untracked_stats_files.values())

        if total_files_changed <= 0 and not files:
            return None

        return {
            "stats": {
                "filesChanged": total_files_changed,
                "linesAdded": total_added,
                "linesRemoved": total_removed,
            },
            "files": files,
        }

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

    def get_files_to_restore(
        self,
        session_id: str,
        turn_index: int,
        project_dir: str | None = None,
        *,
        extra_history_roots: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """返回需要恢复的文件及其目标内容.

        对于在 turn_index 及之后所有 turn 中被修改的文件，
        找到它们在 turn_index 开始前的状态（old_content of the first
        edit at/after the target turn），以便恢复操作将文件写回。

        Args:
            session_id: 会话 ID
            turn_index: 目标回退轮次（1-based，即 /rewind 使用的编号）
            project_dir: 项目目录路径（可选，若不提供则从 session metadata 读取）

        Returns:
            { file_path: { "restore_content": str | None, "action": "write" | "delete" } }
            restore_content 为 None 表示文件在目标 turn 之前不存在，应删除。
        """
        history = self._read_history(session_id)
        if not history:
            return {}

        # 1. 找到目标 turn 的起始时间（第 N 条 user 消息的 timestamp）
        user_count = 0
        target_timestamp: float | None = None
        for record in history:
            if record.get("role") == "user":
                user_count += 1
                if user_count == turn_index:
                    target_timestamp = record.get("timestamp")
                    break

        if target_timestamp is None:
            return {}

        # 2. 读取 file_ops 日志
        #    include_rewound=True: 之前的 conversation 回退只截断了对话、没有动
        #    工作区，那些被软删除的快照仍是对应文件唯一的原始内容来源，必须纳入，
        #    否则文件会永久失去回滚能力。
        agent_history = self._read_agent_history(
            session_id, project_dir, include_rewound=True,
            extra_history_roots=extra_history_roots,
        )

        # 3. 对于每个文件，找到第一条 timestamp >= target_timestamp 的 entry
        #    该 entry 的 old_content 即为目标 turn 开始前的文件状态
        files_to_restore: dict[str, dict[str, Any]] = {}
        for file_path, entries in agent_history.items():
            # entries 按 timestamp 排序（写入时序）
            for entry in entries:
                edit_time = self._iso_to_timestamp(entry["timestamp"])
                if edit_time >= target_timestamp:
                    if entry.get("old_content") is not None:
                        files_to_restore[file_path] = {
                            "restore_content": entry["old_content"],
                            "action": "write",
                        }
                    else:
                        # 文件由 agent 创建，恢复时应删除
                        files_to_restore[file_path] = {
                            "restore_content": None,
                            "action": "delete",
                        }
                    break  # 只需要第一条匹配的 entry

        return files_to_restore

    def get_files_to_redo(
        self,
        session_id: str,
        turn_index: int,
        project_dir: str | None = None,
        *,
        extra_history_roots: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """返回需要重新应用的文件及其新内容(与 ``get_files_to_restore`` 对称).

        discard(soft) 后 file_ops 中 timestamp >= target 的条目被标记
        ``discarded_out``。本方法找出这些条目,返回它们的 ``new_content``
        (即 agent 修改后的内容),供 redo 写回文件。

        Args:
            session_id: 会话 ID
            turn_index: 目标重新应用轮次(1-based)
            project_dir: 项目目录路径(可选)

        Returns:
            { file_path: { "content": str | None, "action": "write" | "delete" } }
            content 为 None 表示文件被 agent 删除,redo 时应删除文件。
        """
        history = self._read_history(session_id)
        if not history:
            return {}

        # 1. 找到目标 turn 的起始时间(第 N 条 user 消息的 timestamp)
        user_count = 0
        target_timestamp: float | None = None
        for record in history:
            if record.get("role") == "user":
                user_count += 1
                if user_count == turn_index:
                    target_timestamp = record.get("timestamp")
                    break

        if target_timestamp is None:
            return {}

        # 2. 读取 file_ops 日志(include_rewound=True 才能看到 discarded_out 条目)
        agent_history = self._read_agent_history(
            session_id, project_dir, include_rewound=True,
            extra_history_roots=extra_history_roots,
        )

        # 3. 对每个文件,遍历所有 timestamp >= target_timestamp 且被 discard 标记
        #    的 entry,取**最后一条**的 new_content(即 agent 修改后的最终态)。
        #    不能取第一条就 break——同一 turn 内同一文件可能被多次编辑,
        #    取中间态写回会导致 redo 后文件内容与 discard 前不一致。
        files_to_redo: dict[str, dict[str, Any]] = {}
        for file_path, entries in agent_history.items():
            last_entry: dict[str, Any] | None = None
            for entry in entries:
                if not entry.get(_DISCARDED_KEY):
                    continue  # 只看被 discard 标记的条目(非 rewind 的 rewound_out)
                edit_time = self._iso_to_timestamp(entry["timestamp"])
                if edit_time >= target_timestamp:
                    last_entry = entry  # 持续覆盖,保留最后一条
            if last_entry is not None:
                if last_entry.get("new_content") is not None:
                    files_to_redo[file_path] = {
                        "content": last_entry["new_content"],
                        "action": "write",
                    }
                else:
                    # new_content 为 None: 文件被 agent 删除,redo 时应删除
                    files_to_redo[file_path] = {
                        "content": None,
                        "action": "delete",
                    }

        return files_to_redo

    def _collect_session_file_ops_paths(
        self,
        session_id: str,
        project_dir: str | None = None,
        *,
        extra_history_roots: list[str] | None = None,
    ) -> list[Path]:
        """收集所有 session-specific file_ops 文件路径。

        扫描范围与 ``truncate_file_ops_by_timestamp`` 一致:
        agent/user workspace、project_dir、extra_history_roots(含 worktree 容器)。
        """
        file_ops_paths: list[Path] = []

        for base_dir in (get_agent_workspace_dir(), get_user_workspace_dir()):
            hist_dir = base_dir / ".agent_history"
            if not hist_dir.is_dir():
                continue
            for f in hist_dir.iterdir():
                if self._is_valid_file_ops_file(f.name, session_id, require_session=True):
                    file_ops_paths.append(f)

        resolved_project_dir = project_dir or self._get_project_dir_from_metadata(session_id)
        if resolved_project_dir:
            project_hist_dir = Path(resolved_project_dir) / ".agent_history"
            if project_hist_dir.is_dir():
                for f in project_hist_dir.iterdir():
                    if self._is_valid_file_ops_file(f.name, session_id, require_session=True):
                        if f not in file_ops_paths:
                            file_ops_paths.append(f)
        extra_roots = self._history_roots_with_worktree_containers(
            resolved_project_dir,
            extra_history_roots,
        )

        for project_hist_dir in self._agent_history_dirs_for_roots(
            extra_roots,
            include_child_workspaces=True,
        ):
            if project_hist_dir.is_dir():
                for f in project_hist_dir.iterdir():
                    if self._is_valid_file_ops_file(f.name, session_id, require_session=True):
                        if f not in file_ops_paths:
                            file_ops_paths.append(f)

        return file_ops_paths

    def truncate_file_ops_by_timestamp(
        self,
        session_id: str,
        cutoff_ts: float,
        project_dir: str | None = None,
        soft: bool = False,
        *,
        extra_history_roots: list[str] | None = None,
        discarded: bool = False,
    ) -> None:
        """截断 file_ops 日志，移除 timestamp >= cutoff_ts 的条目.

        ``soft`` 决定"截断"的含义，取值应与调用方**是否同时还原了工作区文件**一致:

           - ``soft=False``(硬删除,默认): 条目被物理移除。仅当调用方已经把这些
             文件写回原始内容时才正确(如 ``discard_turn_changes``)——文件已回到
             旧状态，快照失去意义。
           - ``soft=True``(软删除): 条目保留 ``old_content``，只打上软删除标记。
             用于**只回退对话、不动文件**的场景(``rewind_session`` 的
             conversation 模式、``compact_partial_session``)或需要保留快照供
             redo 的场景(``discard_turn_changes``)。此时硬删除会让文件
             陷入"已被修改、但系统不再持有其原始内容"的状态，后续任何 /rewind 都
             无法还原它，且不会报错(issue #2241)。标记后显示层照旧看不到这些条目，
             还原层仍可用——显示一致性与回滚能力各取所需。

        ``discarded`` 控制 ``soft=True`` 时使用哪种标记(仅 ``soft=True`` 时有意义):

           - ``discarded=False``(默认): 打 ``rewound_out`` 标记(conversation
             rewind / compact 路径)。``restore_rewound_entries_by_timestamp``
             默认也只恢复 ``rewound_out``。
           - ``discarded=True``: 打 ``discarded_out`` 标记(``discard_turn_changes``
             路径)。与 ``rewound_out`` 区分后,``redo_turn_changes`` 只恢复
             ``discarded_out`` 条目,不会误暴露此前 conversation rewind 软隐藏的
             "未来"条目,避免 last turn diff 混入不属于当前 history 的修改。

        在 rewind / discard_turn_changes 操作后调用，确保 file_ops 日志与
        截断后的 history.json / 实际工作区一致。

        清理范围:
          - **session-specific file_ops**(文件名包含 session_id):
            全部条目按 timestamp 过滤(因为这些条目只属于该 session)。
          - **全局 file_ops**(文件名不含 session_id,如 ``file_ops_jiuwenswarm.json``):
            **不清理**。全局 file_ops 缺少 session 归属字段,若按路径 + timestamp
            清理会误伤其他 session 在同一文件上的后续修改(详见 P1 修复)。
            撤销后 last_turn diff 可能残留历史全局记录,这是已知局限——
            用户撤销本轮后一般不需要查看 last_turn,且 session-specific 日志
            已足够支撑单 session 场景的精确恢复。

        Args:
            session_id: 会话 ID
            cutoff_ts: 截断阈值（Unix timestamp），>= 此时间的条目将被移除
            project_dir: 项目目录路径。显式传入可避免底层从 metadata 推断,
                覆盖 ``channel_metadata.cwd`` 缺失的场景(如 Web/code 模式新会话)。
                为 ``None`` 时底层从 session metadata 推断(读取顺序见
                ``_get_project_dir_from_metadata``)。
            soft: 见上文。调用方未还原工作区文件时必须传 ``True``。
            discarded: 见上文。``soft=True`` 时决定标记类型。
        """

        marker = _DISCARDED_KEY if discarded else _REWOUND_KEY

        file_ops_paths = self._collect_session_file_ops_paths(
            session_id, project_dir, extra_history_roots=extra_history_roots,
        )

        for file_ops_path in file_ops_paths:
            try:
                data = json.loads(file_ops_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue

                truncated = False
                new_data: dict[str, Any] = {}
                for file_path, entries in data.items():
                    if not isinstance(entries, list):
                        continue
                    filtered = []
                    for e in entries:
                        try:
                            entry_ts = self._iso_to_timestamp(e.get("timestamp", ""))
                        except (ValueError, TypeError):
                            filtered.append(e)  # 无法解析的条目保留
                            continue
                        if entry_ts < cutoff_ts:
                            filtered.append(e)
                        elif soft:
                            # 保留快照(old_content)，仅对显示层隐藏
                            if not e.get(marker):
                                e[marker] = True
                                truncated = True
                            filtered.append(e)
                        else:
                            truncated = True
                    if len(filtered) != len(entries):
                        truncated = True
                    if filtered:
                        new_data[file_path] = filtered

                if truncated:
                    file_ops_path.write_text(
                        json.dumps(new_data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    logger.info(
                        "truncate_file_ops: cleaned %s (cutoff_ts=%s, soft=%s, marker=%s)",
                        file_ops_path.name, cutoff_ts, soft, marker,
                    )
            except Exception as exc:
                logger.warning(
                    "truncate_file_ops: failed to process %s: %s",
                    file_ops_path, exc,
                )

    def restore_rewound_entries_by_timestamp(
        self,
        session_id: str,
        cutoff_ts: float,
        project_dir: str | None = None,
        *,
        extra_history_roots: list[str] | None = None,
        discarded: bool = False,
    ) -> None:
        """去掉 file_ops 中 timestamp >= cutoff_ts 的条目的软删除标记.

        与 ``truncate_file_ops_by_timestamp(soft=True)`` 对称:
        后者加标记(隐藏条目),本方法去标记(恢复条目可见性)。
        用于 ``redo_turn_changes`` 恢复被 ``discard(soft)`` 隐藏的 file_ops 条目。

        ``discarded`` 控制恢复哪种标记,应与当初打标记时一致:

           - ``discarded=False``(默认): 恢复 ``rewound_out`` 条目
             (conversation rewind 路径)。
           - ``discarded=True``: 恢复 ``discarded_out`` 条目
             (``discard_turn_changes`` → ``redo_turn_changes`` 路径)。
             只恢复 discard 标记,不触碰 rewind 标记,避免误暴露此前
             conversation rewind 软隐藏的"未来"条目。

        Args:
            session_id: 会话 ID
            cutoff_ts: 阈值(Unix timestamp),>= 此时间的匹配标记条目将被恢复
            project_dir: 项目目录路径(可选)
        """
        marker = _DISCARDED_KEY if discarded else _REWOUND_KEY

        file_ops_paths = self._collect_session_file_ops_paths(
            session_id, project_dir, extra_history_roots=extra_history_roots,
        )

        for file_ops_path in file_ops_paths:
            try:
                data = json.loads(file_ops_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue

                restored = False
                new_data: dict[str, Any] = {}
                for file_path, entries in data.items():
                    if not isinstance(entries, list):
                        continue
                    filtered = []
                    for e in entries:
                        try:
                            entry_ts = self._iso_to_timestamp(e.get("timestamp", ""))
                        except (ValueError, TypeError):
                            filtered.append(e)
                            continue
                        if entry_ts >= cutoff_ts and e.get(marker):
                            e.pop(marker, None)
                            restored = True
                        filtered.append(e)
                    if filtered:
                        new_data[file_path] = filtered

                if restored:
                    file_ops_path.write_text(
                        json.dumps(new_data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    logger.info(
                        "restore_rewound_entries: restored %s (cutoff_ts=%s, marker=%s)",
                        file_ops_path.name, cutoff_ts, marker,
                    )
            except Exception as exc:
                logger.warning(
                    "restore_rewound_entries: failed to process %s: %s",
                    file_ops_path, exc,
                )


_diff_service: DiffService | None = None


def get_diff_service() -> DiffService:
    """获取 DiffService 单例实例."""
    global _diff_service
    if _diff_service is None:
        _diff_service = DiffService()
    return _diff_service
