# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Legacy shared-directory session fallback owned by Gateway.

Only history rewind, turn listing, file restore, and partial compact. Context
rebuild stays in ``jiuwenswarm.agents``. History and diff reads still use the
server helpers that own those files.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.session.session_history import (
    get_read_history_path,
    history_exists,
    load_history_records,
)

logger = logging.getLogger(__name__)


def rewind_session(
    *,
    session_id: str,
    turn_index: int,
) -> dict[str, Any]:
    if turn_index < 1:
        raise ValueError("turn_index must be >= 1")

    history_path = get_read_history_path(session_id)
    if not history_path.exists():
        raise ValueError("session history not found")

    from jiuwenswarm.server.runtime.session.session_history import truncate_history_records

    history = load_history_records(session_id)
    if not isinstance(history, list):
        raise ValueError("invalid history format")

    user_positions = []
    for i, record in enumerate(history):
        if record.get("role") == "user":
            user_positions.append(i)

    total_turns = len(user_positions)
    if total_turns == 0:
        raise ValueError("no user messages in session")
    if turn_index > total_turns:
        raise ValueError(
            f"turn_index {turn_index} exceeds total turns ({total_turns})"
        )

    target_user_index = user_positions[turn_index - 1]
    cut_index = target_user_index

    removed_turn_content = ""
    if 0 <= target_user_index < len(history):
        content = history[target_user_index].get("content", "")
        raw = content if isinstance(content, str) else str(content)
        # 剥离 <file-content> 块（系统注入的文件元数据，非用户实际输入）
        removed_turn_content = re.sub(r"<file-content[^>]*>.*?</file-content>", "", raw, flags=re.DOTALL).strip()

    # 在截断 history 之前，记录目标 turn 的时间戳（用于后续清理 file_ops）
    cut_timestamp = history[cut_index].get("timestamp")

    # 同样必须在下面 update_session_metadata 之前解析项目目录：metadata.json 是
    # 非原子的原地覆写且走后台线程，之后再让 truncate_file_ops 自己去推断，会撞上
    # 半截文件 → JSONDecodeError → 静默返回 None → 扫不到 file_ops → 清理无声失效。
    project_dir: str | None = None
    try:
        from jiuwenswarm.server.utils.diff_service import get_diff_service

        project_dir = get_diff_service().resolve_project_dir(session_id)
    except Exception as exc:
        logger.warning("rewind_session: failed to resolve project_dir: %s", exc)

    result = truncate_history_records(session_id=session_id, cut_index=cut_index)

    from jiuwenswarm.gateway.embedded.runtime.session.session_metadata import update_session_metadata

    update_session_metadata(
        session_id=session_id,
        set_message_count=result["remaining_records"],
    )

    # 清理 session-specific file_ops 日志，使 turn diff 显示与截断后的 history 一致
    # 必须在 truncate_history_records 之后调用，但传入截断前获取的时间戳
    #
    # soft=True: 本函数只回退对话、不动工作区文件。硬删除快照会让这些文件永久
    # 失去回滚能力（后续 /rewind 选 code 找不到它们，却仍报告成功）。
    if cut_timestamp is not None:
        try:
            from jiuwenswarm.server.utils.diff_service import get_diff_service

            get_diff_service().truncate_file_ops_by_timestamp(
                session_id, cut_timestamp, project_dir=project_dir, soft=True,
            )
        except Exception as exc:
            logger.warning("rewind_session: failed to truncate file_ops: %s", exc)

    return {
        "session_id": session_id,
        "turn_index": turn_index,
        "content": removed_turn_content,
        "content_preview": removed_turn_content[:80] if removed_turn_content else "",
        "remaining_records": result["remaining_records"],
        "removed_records": result["removed_records"],
    }
def compact_partial_session(
    *,
    session_id: str,
    turn_index: int,
    direction: str = "from",
    llm_summary: str | None = None,
) -> dict[str, Any]:
    if turn_index < 1:
        raise ValueError("turn_index must be >= 1")

    history_path = get_read_history_path(session_id)
    if not history_path.exists():
        raise ValueError("session history not found")

    history = load_history_records(session_id)
    if not isinstance(history, list):
        raise ValueError("invalid history format")

    user_positions = []
    for i, record in enumerate(history):
        if record.get("role") == "user":
            user_positions.append(i)

    total_turns = len(user_positions)
    if total_turns == 0:
        raise ValueError("no user messages in session")
    if turn_index > total_turns:
        raise ValueError(
            f"turn_index {turn_index} exceeds total turns ({total_turns})"
        )

    target_user_index = user_positions[turn_index - 1]

    import uuid
    from jiuwenswarm.server.runtime.session.session_history import (
        _FILE_LOCK,
        _WRITE_QUEUE,
        truncate_history_records,
    )
    from jiuwenswarm.gateway.embedded.runtime.session.session_metadata import update_session_metadata

    removed_turn_content = ""
    if 0 <= target_user_index < len(history):
        content = history[target_user_index].get("content", "")
        raw = content if isinstance(content, str) else str(content)
        removed_turn_content = re.sub(r"<file-content[^>]*>.*?</file-content>", "", raw, flags=re.DOTALL).strip()

    if direction == "from":
        cut_timestamp = history[target_user_index].get("timestamp")
        summarized_count = len(history) - target_user_index
        # 在 update_session_metadata 之前解析（同 rewind_session，避免元数据写入竞态）
        compact_project_dir: str | None = None
        try:
            from jiuwenswarm.server.utils.diff_service import get_diff_service

            compact_project_dir = get_diff_service().resolve_project_dir(session_id)
        except Exception as exc:
            logger.warning("compact_partial_session: failed to resolve project_dir: %s", exc)

        result = truncate_history_records(session_id=session_id, cut_index=target_user_index)
        remaining = result["remaining_records"]
        removed = result["removed_records"]

        # soft=True: 摘要化同样只改对话、不动工作区文件（同 rewind_session）
        if cut_timestamp is not None:
            try:
                from jiuwenswarm.server.utils.diff_service import get_diff_service
                get_diff_service().truncate_file_ops_by_timestamp(
                    session_id, cut_timestamp,
                    project_dir=compact_project_dir, soft=True,
                )
            except Exception as exc:
                logger.warning("compact_partial_session: failed to truncate file_ops: %s", exc)

    elif direction == "up_to":
        kept = history[target_user_index:]
        summarized_count = target_user_index
        removed = summarized_count
        remaining = len(kept)

        _WRITE_QUEUE.join()
        with _FILE_LOCK:
            _write_records_to_path(history_path, kept)
    else:
        raise ValueError(f"unknown direction: {direction}")

    update_session_metadata(
        session_id=session_id,
        set_message_count=remaining,
    )

    request_id = str(uuid.uuid4())
    now = time.time()

    short_text = (
        f"Summarized {summarized_count} messages from this point."
        if direction == "from"
        else f"Summarized {summarized_count} messages up to this point."
    )

    boundary_record = {
        "id": f"{request_id}:assistant",
        "role": "assistant",
        "request_id": request_id,
        "channel_id": "tui",
        "timestamp": now,
        "content": "Conversation compacted",
        "event_type": "context.compact_boundary",
        "compact_metadata": {
            "trigger": "manual_rewind",
            "direction": direction,
            "turn_index": turn_index,
            "summarized_messages": summarized_count,
        },
    }

    summary_record = {
        "id": f"{request_id}:assistant_summary",
        "role": "assistant",
        "request_id": request_id,
        "channel_id": "tui",
        "timestamp": now + 0.001,
        "content": short_text,
        "event_type": "context.rewind_summary",
        "compact_metadata": {
            "trigger": "manual_rewind",
            "direction": direction,
            "turn_index": turn_index,
            "summarized_messages": summarized_count,
        },
        "is_compact_summary": True,
    }

    _WRITE_QUEUE.join()
    with _FILE_LOCK:
        existing = load_history_records(session_id) if history_path.exists() else []
        if not isinstance(existing, list):
            existing = []
        existing.append(boundary_record)
        existing.append(summary_record)

        if llm_summary:
            compact_summary_record = {
                "id": f"{request_id}:assistant_csummary",
                "role": "assistant",
                "request_id": request_id,
                "channel_id": "tui",
                "timestamp": now + 0.002,
                "content": llm_summary,
                "event_type": "context.compact_summary",
                "compact_metadata": {
                    "trigger": "manual_rewind",
                    "direction": direction,
                    "turn_index": turn_index,
                    "summarized_messages": summarized_count,
                },
                "is_compact_summary": True,
                "transcript_only": True,
            }
            existing.append(compact_summary_record)

        _write_records_to_path(history_path, existing)

    return {
        "session_id": session_id,
        "turn_index": turn_index,
        "content": removed_turn_content,
        "content_preview": removed_turn_content[:80] if removed_turn_content else "",
        "remaining_records": remaining + 2,
        "removed_records": removed,
        "summarized_messages": summarized_count,
        "direction": direction,
    }
_NON_USER_AUTHORED_TAGS = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<task-notification>",
    "<tick>",
    "<teammate-message",
)


def _is_selectable_user_message(content: str) -> bool:
    for tag in _NON_USER_AUTHORED_TAGS:
        if tag in content:
            return False
    return True
def list_session_turns(
    *,
    session_id: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    if not history_exists(session_id):
        return {"turns": [], "total": 0}

    try:
        history = load_history_records(session_id)
    except Exception as exc:
        logger.warning("list_session_turns: failed to read history: %s", exc)
        return {"turns": [], "total": 0}

    if not isinstance(history, list):
        return {"turns": [], "total": 0}

    diff_stats_map: dict[int, dict[str, int]] = {}
    diff_files_map: dict[int, list[dict[str, Any]]] = {}
    try:
        from jiuwenswarm.server.utils.diff_service import get_diff_service

        diff_service = get_diff_service()
        turn_diffs = diff_service.get_turn_diffs(session_id, project_dir)
        if isinstance(turn_diffs, list):
            for td in turn_diffs:
                ti = td.get("turnIndex")
                if isinstance(ti, int) and ti > 0:
                    diff_stats_map[ti] = td.get("stats", {})
                    files_data: list[dict[str, Any]] = []
                    for fp, finfo in td.get("files", {}).items():
                        files_data.append({
                            "path": fp,
                            "linesAdded": finfo.get("linesAdded", 0),
                            "linesRemoved": finfo.get("linesRemoved", 0),
                            "isNewFile": finfo.get("isNewFile", False),
                        })
                    diff_files_map[ti] = files_data
    except Exception as exc:
        logger.debug("list_session_turns: diff service unavailable: %s", exc)

    turns = []
    user_count = 0
    for record in history:
        if record.get("role") != "user":
            continue
        user_count += 1
        content = record.get("content", "")
        if isinstance(content, str) and not _is_selectable_user_message(content):
            continue
        if isinstance(content, str):
            # 剥离 <file-content>...</file-content> 块（系统元数据），只保留用户实际输入
            cleaned = re.sub(r"<file-content[^>]*>.*?</file-content>", "", content, flags=re.DOTALL)
            preview = cleaned.strip()[:80]
        else:
            preview = ""
        stats = diff_stats_map.get(user_count, {
            "filesChanged": 0,
            "linesAdded": 0,
            "linesRemoved": 0,
        })
        turns.append({
            "turn_index": user_count,
            "content_preview": preview,
            "timestamp": record.get("timestamp", 0),
            "id": record.get("id", ""),
            "request_id": record.get("request_id", ""),
            "stats": stats,
            "files": diff_files_map.get(user_count, []),
        })

    return {"turns": turns, "total": user_count}
def restore_session_files(
    *,
    session_id: str,
    turn_index: int,
    project_dir: str | None = None,
    extra_history_roots: list[str] | None = None,
) -> dict[str, Any]:
    """恢复指定 turn 之后所有被修改的文件到目标 turn 开始前的状态.

    基于 DiffService.get_files_to_restore() 确定需要恢复的文件，
    然后将每个文件写回其 old_content（或删除 agent 新建的文件）。

    Args:
        session_id: 会话 ID
        turn_index: 目标回退轮次(1-based)
        project_dir: 项目目录路径。显式传入可避免底层从 metadata 推断,
            覆盖 ``channel_metadata.cwd`` 缺失的场景(如 Web/code 模式新会话)。
            为 ``None`` 时底层从 session metadata 推断(读取顺序见
            ``DiffService._get_project_dir_from_metadata``)。

    局限性（底层暂不支持，后续迭代）：
    - bash 命令修改的文件不在 file_ops 日志中，无法恢复
    - 文件删除操作未记录在 file_ops 中，无法恢复被删除的文件
    - 多 session 共享 file_ops 日志，若其他 session 也修改了同一文件，
      时间戳匹配可能不够精确
    """
    from jiuwenswarm.server.utils.diff_service import get_diff_service

    diff_service = get_diff_service()
    files_to_restore = diff_service.get_files_to_restore(
        session_id,
        turn_index,
        project_dir=project_dir,
        extra_history_roots=extra_history_roots,
    )

    if not files_to_restore:
        return {
            "session_id": session_id,
            "turn_index": turn_index,
            "restored_files": [],
            "deleted_files": [],
            "errors": [],
        }

    restored: list[str] = []
    deleted: list[str] = []
    errors: list[dict[str, str]] = []

    for file_path, info in files_to_restore.items():
        path = Path(file_path)
        try:
            if info["action"] == "write":
                # 文件在目标 turn 前已有内容，写回 old_content
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    info["restore_content"], encoding="utf-8", newline=""
                )
                restored.append(file_path)
            elif info["action"] == "delete":
                # 文件由 agent 在目标 turn 后创建，删除
                if path.exists():
                    path.unlink()
                    deleted.append(file_path)
        except Exception as exc:
            errors.append({"file": file_path, "error": str(exc)})
            logger.warning(
                "restore_session_files: failed to restore %s: %s",
                file_path, exc,
            )

    logger.info(
        "restore_session_files: session=%s turn=%s restored=%d deleted=%d errors=%d",
        session_id, turn_index, len(restored), len(deleted), len(errors),
    )

    return {
        "session_id": session_id,
        "turn_index": turn_index,
        "restored_files": restored,
        "deleted_files": deleted,
        "errors": errors,
    }
