"""Dict helpers for Git diff events already fetched from AgentServer."""

from __future__ import annotations

from typing import Any


def file_entry_to_dict_no_hunks(entry: dict[str, Any]) -> dict[str, Any]:
    """将已序列化的文件条目 dict 转换为不含 hunk 的事件格式。"""
    return {
        "file_path": entry.get("file_path", ""),
        "status": entry.get("status", "modified"),
        "change_type": entry.get("change_type", entry.get("status", "modified")),
        "lines_added": entry.get("lines_added", 0),
        "lines_removed": entry.get("lines_removed", 0),
        "is_binary": entry.get("is_binary", False),
        "is_new_file": entry.get("is_new_file", False),
        "is_deleted_file": entry.get("is_deleted_file", False),
        "is_untracked": entry.get("is_untracked", False),
        "is_large_file": entry.get("is_large_file", False),
        "is_truncated": entry.get("is_truncated", False),
        "hunks": [],
    }


def file_map_to_dict_no_hunks(
    files_dict: dict[str, Any] | None,
) -> dict[str, Any]:
    """批量转换文件映射:去除 hunk,过滤非 dict 条目。"""
    if not files_dict:
        return {}
    result: dict[str, Any] = {}
    for path, entry in files_dict.items():
        if not isinstance(entry, dict):
            continue
        result[path] = file_entry_to_dict_no_hunks(entry)
    return result


def extract_files_from_status(
    status_dict: dict[str, Any],
    source: str,
) -> dict[str, Any] | None:
    """从已序列化的 diff status 中提取指定 source 的 files 映射。"""
    if source == "current":
        current = status_dict.get("current")
        return (current or {}).get("files") if current else None
    if source == "last_turn":
        last_turn = status_dict.get("last_turn")
        return (last_turn or {}).get("files") if last_turn else None
    return None


def build_summary_entry(current: dict[str, Any] | None) -> dict[str, Any] | None:
    """构造 summary 事件中的 current 条目(``files`` 固定 ``{}``)。"""
    if not current:
        return None
    return {
        "kind": current.get("kind", "working_tree"),
        "is_dirty": current.get("is_dirty", False),
        "stats": current.get("stats", {}),
        "files": {},
        "files_truncated": bool(current.get("files_truncated", False)),
        "files_limit": int(current.get("files_limit", 0) or 0),
    }


def build_turn_summary_entry(last_turn: dict[str, Any] | None) -> dict[str, Any] | None:
    """构造 summary 事件中的 last_turn 条目(``files`` 固定 ``{}``)。"""
    if not last_turn:
        return None
    return {
        "kind": last_turn.get("kind", "conversation_turn"),
        "change_set_id": last_turn.get("change_set_id", ""),
        "turn_index": last_turn.get("turn_index", 0),
        "request_id": last_turn.get("request_id", ""),
        "assistant_message_id": last_turn.get("assistant_message_id", ""),
        "user_message_id": last_turn.get("user_message_id", ""),
        "status": last_turn.get("status", "completed"),
        "stats": last_turn.get("stats", {}),
        "files": {},
    }
