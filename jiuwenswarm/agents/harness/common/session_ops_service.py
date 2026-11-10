from __future__ import annotations

import copy
import inspect
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from jiuwenswarm.common.utils import get_agent_sessions_dir, get_agent_workspace_dir
from jiuwenswarm.server.runtime.session.session_history import (
    get_read_history_path,
    history_exists,
    load_history_records,
    load_history_tail_request_id as read_history_tail_request_id,
    write_history_records,
    _write_records_to_path,
)

if TYPE_CHECKING:
    from openjiuwen.harness import DeepAgent

logger = logging.getLogger(__name__)


def _get_context_processors(react_agent: Any) -> list[tuple[str, Any]] | None:
    """Return the configured context processors for lifecycle-created contexts.

    Warmup and rewind run outside ``ReActAgent._init_context``.  They must
    explicitly carry the rail-populated processor chain when they create a
    context, otherwise the context is cached with no compressor/debug
    processor and later ReAct calls keep reusing that incomplete object.
    """
    config = getattr(react_agent, "_config", None)
    processors = getattr(config, "context_processors", None)
    if not isinstance(processors, (list, tuple)) or not processors:
        return None
    return list(processors)


def _derive_first_prompt(history: list[dict[str, Any]]) -> str:
    for record in history:
        if record.get("role") != "user":
            continue
        content = record.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        text = re.sub(r"\s+", " ", content).strip()
        return text[:100] if text else "Branched conversation"
    return "Branched conversation"


def _get_unique_fork_name(base_name: str, existing_titles: set[str]) -> str:
    """Generate a unique fork title.

    With custom name:  "custom-name (Branch)"
    Without name:      "(Branch)" or "(Branch N)"
    """
    candidate = f"{base_name} (Branch)" if base_name else "(Branch)"
    if candidate not in existing_titles:
        return candidate
    pattern = re.compile(
        r"^" + re.escape(base_name) + r" \(Branch(?: (\d+))?\)$"
        if base_name
        else r"^\(Branch(?: (\d+))?\)$"
    )
    used_numbers: set[int] = {1}
    for title in existing_titles:
        m = pattern.match(title)
        if m:
            num = int(m.group(1)) if m.group(1) else 1
            used_numbers.add(num)
    next_number = 2
    while next_number in used_numbers:
        next_number += 1
    return f"{base_name} (Branch {next_number})" if base_name else f"(Branch {next_number})"


def fork_session(
    *,
    source_session_id: str,
    target_session_id: str,
    title: str = "",
    channel_id: str = "tui",
) -> dict[str, Any]:
    sessions_dir = get_agent_sessions_dir()
    source_dir = sessions_dir / source_session_id
    target_dir = sessions_dir / target_session_id

    if not source_dir.exists():
        raise ValueError("source session not found")
    if target_dir.exists():
        raise ValueError("target session already exists")

    target_dir.mkdir(parents=True, exist_ok=True)

    history_data: list[dict[str, Any]] = []
    if history_exists(source_session_id):
        try:
            data = load_history_records(source_session_id)
            if isinstance(data, list):
                history_data = data
                forked_records: list[dict[str, Any]] = []
                for record in data:
                    forked_record = dict(record)
                    forked_record["forked_from"] = {
                        "session_id": source_session_id,
                        "original_id": record.get("id", ""),
                    }
                    forked_records.append(forked_record)
                write_history_records(
                    target_session_id,
                    forked_records,
                    preserve_existing_format=False,
                )
        except Exception as exc:
            logger.warning("fork: failed to add forked_from to history: %s", exc)

    from jiuwenswarm.server.runtime.session.session_metadata import (
        _current_timestamp,
        _enqueue_write,
        get_all_sessions_metadata,
        get_session_metadata,
    )

    source_meta = get_session_metadata(source_session_id)

    if title:
        base_name = title
    elif source_meta.get("title"):
        base_name = source_meta["title"]
    else:
        # Don't derive from first prompt — "(Branch)" alone is cleaner
        # for the status bar. First prompt like "hi" makes an ugly title.
        base_name = ""

    existing_titles: set[str] = set()
    try:
        all_sessions = get_all_sessions_metadata(limit=500, offset=0)
        if isinstance(all_sessions, list):
            for s in all_sessions:
                t = s.get("title", "")
                if t:
                    existing_titles.add(t)
    except Exception as exc:
        logger.debug("fork_session: failed to get existing titles: %s", exc)

    final_title = _get_unique_fork_name(base_name, existing_titles)
    source_mode = source_meta.get("mode", "code.normal")

    metadata = {
        "session_id": target_session_id,
        "channel_id": channel_id,
        "user_id": source_meta.get("user_id", ""),
        "created_at": _current_timestamp(),
        "last_message_at": source_meta.get("last_message_at", 0),
        "title": final_title,
        "message_count": source_meta.get("message_count", 0),
        "mode": source_mode,
        "forked_from": source_session_id,
        # 复制源会话的项目归属字段，确保分叉会话继承原项目归属
        "project_id": source_meta.get("project_id", ""),
        "project_dir": source_meta.get("project_dir", ""),
    }
    # 复制源会话的 channel_metadata，确保分叉会话在 /resume 按项目目录过滤时可见
    source_channel_meta = source_meta.get("channel_metadata")
    if source_channel_meta and isinstance(source_channel_meta, dict):
        metadata["channel_metadata"] = dict(source_channel_meta)
    _enqueue_write(target_session_id, metadata)

    return {
        "session_id": target_session_id,
        "source_session_id": source_session_id,
        "title": final_title,
    }


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

    from jiuwenswarm.server.runtime.session.session_metadata import update_session_metadata

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
    from jiuwenswarm.server.runtime.session.session_metadata import update_session_metadata

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


def get_last_turn_info(
    *,
    session_id: str,
) -> dict[str, Any]:
    """返回最后一轮 user message 的 turn_index 和 timestamp.

    用于"撤销本轮代码修改"(``project.git.discard_turn_changes``)功能:
    该接口需要最后一轮的 turn_index(传给 ``restore_session_files``)和
    timestamp(传给 ``truncate_file_ops_by_timestamp``)。

    与 ``list_session_turns`` 不同,本函数不过滤不可选的 user message,
    返回的是最后一条 user message 的信息(包括系统注入的消息)。

    Returns:
        ``{"turn_index": int, "timestamp": float}``;
        无 history 或无 user message 时返回 ``{"turn_index": 0, "timestamp": 0.0}``。
    """
    if not history_exists(session_id):
        return {"turn_index": 0, "timestamp": 0.0}

    try:
        history = load_history_records(session_id)
    except Exception as exc:
        logger.warning("get_last_turn_info: failed to read history: %s", exc)
        return {"turn_index": 0, "timestamp": 0.0}

    if not isinstance(history, list):
        return {"turn_index": 0, "timestamp": 0.0}

    user_count = 0
    last_timestamp: float = 0.0
    for record in history:
        if record.get("role") != "user":
            continue
        user_count += 1
        ts = record.get("timestamp", 0)
        if isinstance(ts, (int, float)):
            last_timestamp = float(ts)

    return {"turn_index": user_count, "timestamp": last_timestamp}


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


def redo_session_files(
    *,
    session_id: str,
    turn_index: int,
    project_dir: str | None = None,
    extra_history_roots: list[str] | None = None,
) -> dict[str, Any]:
    """重新应用指定 turn 被 discard(soft) 撤销的文件修改.

    与 ``restore_session_files`` 对称:后者写回 old_content(撤销),
    本方法写回 new_content(重新应用)。

    注意:当 ``get_files_to_redo`` 返回空(例如 file_ops 缺失/损坏/没被打
    ``discarded_out`` 标记)时,本方法返回 ``redone_files=[] deleted_files=[]
    errors=[]``。这种"空成功"不应被当作真正的成功——调用方(如 redo handler)
    应自行判断空结果并返回 ``REDO_HISTORY_MISSING``,避免误清 discarded 状态。

    Args:
        session_id: 会话 ID
        turn_index: 目标重新应用轮次(1-based)
        project_dir: 项目目录路径(可选)
    """
    from jiuwenswarm.server.utils.diff_service import get_diff_service

    diff_service = get_diff_service()
    files_to_redo = diff_service.get_files_to_redo(
        session_id,
        turn_index,
        project_dir=project_dir,
        extra_history_roots=extra_history_roots,
    )

    if not files_to_redo:
        return {
            "session_id": session_id,
            "turn_index": turn_index,
            "redone_files": [],
            "deleted_files": [],
            "errors": [],
        }

    redone: list[str] = []
    deleted: list[str] = []
    errors: list[dict[str, str]] = []

    for file_path, info in files_to_redo.items():
        path = Path(file_path)
        try:
            if info["action"] == "write":
                # 写回 agent 修改后的内容(new_content)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    info["content"], encoding="utf-8", newline=""
                )
                redone.append(file_path)
            elif info["action"] == "delete":
                # 文件被 agent 删除,redo 时重新删除。
                # 文件不存在属于"目标状态已满足"(redo 后状态 == discard 前状态),
                # 仍记为已处理,避免 handler 把这种正常情况误判成 REDO_HISTORY_MISSING。
                if path.exists():
                    path.unlink()
                deleted.append(file_path)
        except Exception as exc:
            errors.append({"file": file_path, "error": str(exc)})
            logger.warning(
                "redo_session_files: failed to redo %s: %s",
                file_path, exc,
            )

    logger.info(
        "redo_session_files: session=%s turn=%s redone=%d deleted=%d errors=%d",
        session_id, turn_index, len(redone), len(deleted), len(errors),
    )

    return {
        "session_id": session_id,
        "turn_index": turn_index,
        "redone_files": redone,
        "deleted_files": deleted,
        "errors": errors,
    }


def _is_member_history_record(record: dict[str, Any]) -> bool:
    """团队成员归因记录判定：member_name 顶层字段或 member-* request_id 前缀。

    与 context_handoff.is_member_history_record 同口径（那边面向前情注入，
    这边面向上下文重建净化；各自独立避免跨模块耦合）。
    """
    if str(record.get("member_name") or "").strip():
        return True
    return str(record.get("request_id") or "").startswith("member-")


# 成员产出注记的截断长度（与 context_handoff 的交接口径一致）
_MEMBER_OUTPUT_NOTE_CHARS = 200

# 团队期身份复位注记：净化重建（drop_member_internals=True）且历史中含团队期
# 记录（mode=team 顶层字段或成员归因记录）时，追加在恢复消息序列末尾——
# 团队期 leader 答复（主理人口吻）逐字回归为"助手自己说过的话"，不给锚点
# 默认角色会把主理人身份当成自己的口吻延续。
_TEAM_IDENTITY_RESET_NOTE = (
    "（注：以上包含你此前以专家团主理人身份与用户协作期间的对话记录；"
    "自本条之后你已恢复为默认助手身份，请以默认助手身份继续与用户对话。）"
)


def _build_context_messages_from_history(
        history_records: list[dict[str, Any]],
        *,
        drop_member_internals: bool = False,
) -> tuple[list[Any], int]:
    """Convert history.jsonl records into a list of openjiuwen BaseMessage.

    history.json stores raw streaming events, NOT clean messages.
    A single user turn produces many records across multiple LLM API calls:

      Per LLM call:
        chat.reasoning (N chunks)  → thinking text (concatenated)
        chat.usage_metadata         → end-of-call marker (skip)
        EITHER:
          chat.tool_call (1..N)     → AssistantMessage(reasoning + tool_calls)
          chat.tool_result (per tc) → ToolMessage
        OR:
          chat.delta (N chunks)     → skip (fragments of chat.final)
          chat.final                → AssistantMessage(reasoning + content)

      Other events (all skipped):
        chat.tool_update            → intermediate tool progress
        chat.usage_summary          → turn-level usage stats
        chat.ask_user_question      → UI interaction event

    Aligned with claude-code's approach: preserve thinking (reasoning),
    tool_call/tool_result structure, and final text to fully reconstruct
    the conversation context for the LLM.

    ``drop_member_internals``：团队模式历史混入成员归因记录
    （member_name / member-* request_id）。默认 False 保真重建（rewind 等
    场景不变）；True 时成员内部工具/推理事件丢弃（否则被当成主 agent 自己
    的 tool_calls 注入，角色错乱），成员 chat.final 折叠为带归属的 assistant
    注记（截断 200 字），且若历史中含团队期记录（mode=team 或成员归因），
    在恢复序列末尾追加身份复位注记（_TEAM_IDENTITY_RESET_NOTE），防止
    主理人口吻被默认角色当作自己的历史口吻延续。refresh/warmup 路径传 True。

    State machine:
      - reasoning_buffer: accumulates chat.reasoning text chunks
      - current_tool_calls: collects tool_calls for the current LLM call
      - When a NEW reasoning chunk arrives after tool_calls were collected,
        flush the pending AssistantMessage (one LLM call boundary crossed)
      - Consecutive tool_calls without reasoning between them belong to
        the same LLM call (parallel tool execution)

    Returns ``(context_messages, skipped_record_count)``.
    """
    from openjiuwen.core.foundation.llm.schema.message import (
        UserMessage,
        AssistantMessage,
        ToolMessage,
    )

    context_messages: list[Any] = []
    skipped = 0
    reasoning_buffer: list[str] = []
    current_tool_calls: list[dict[str, Any]] = []
    # 团队期记录检出（身份复位注记用）：mode=team 顶层字段或成员归因记录
    saw_team_era = False
    # Track all tool_call_ids that have been emitted in AssistantMessages.
    # Used to detect orphaned tool_results (e.g. ask_user's preliminary
    # empty result that arrives before the actual chat.tool_call event).
    emitted_tool_call_ids: set[str] = set()

    def _flush_pending_assistant() -> None:
        """Create an AssistantMessage from buffered reasoning + tool_calls."""
        nonlocal reasoning_buffer, current_tool_calls
        reasoning = "".join(reasoning_buffer).strip()
        tool_calls = current_tool_calls
        reasoning_buffer = []
        current_tool_calls = []
        if not reasoning and not tool_calls:
            return
        # Record emitted tool_call_ids for orphan detection
        for tc in tool_calls:
            emitted_tool_call_ids.add(tc["id"])
        context_messages.append(AssistantMessage(
            content="",
            reasoning_content=reasoning if reasoning else None,
            tool_calls=tool_calls if tool_calls else None,
        ))

    for record in history_records:
        event_type = (record.get("event_type") or "").strip()
        role = (record.get("role") or "").strip().lower()
        content = record.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(p) for p in content
                if isinstance(p, str) or (isinstance(p, dict) and p.get("type") == "text")
            )
        content = str(content)

        if not saw_team_era and (
            str(record.get("mode") or "") == "team"
            or _is_member_history_record(record)
        ):
            saw_team_era = True

        # ── 团队模式成员归因记录净化（drop_member_internals）──
        # 成员内部工具/推理事件若按主 agent 自己的行为重建，模型会看到"自己"
        # 调用了从未拥有的团队工具（角色错乱）；成员可见产出折叠为归属注记。
        if drop_member_internals and _is_member_history_record(record):
            if role == "assistant" and event_type == "chat.final" and content.strip():
                if reasoning_buffer or current_tool_calls:
                    _flush_pending_assistant()
                member_name = str(record.get("member_name") or "").strip() or "成员"
                note_text = content.strip()
                if len(note_text) > _MEMBER_OUTPUT_NOTE_CHARS:
                    note_text = note_text[:_MEMBER_OUTPUT_NOTE_CHARS].rstrip() + "…"
                context_messages.append(AssistantMessage(
                    content=f"（团队成员 {member_name} 的产出）：{note_text}",
                ))
            else:
                skipped += 1
            continue

        # ── User message ──
        if role == "user":
            if content.strip():
                context_messages.append(UserMessage(content=content))
            continue

        # ── Only process assistant events below ──
        if role != "assistant":
            skipped += 1
            continue

        if event_type == "chat.reasoning":
            # New reasoning after tool_calls → flush previous LLM call
            if current_tool_calls and reasoning_buffer == []:
                _flush_pending_assistant()
            if content:
                reasoning_buffer.append(content)

        elif event_type == "chat.tool_call":
            tc = record.get("tool_call", {})
            if not isinstance(tc, dict):
                skipped += 1
                continue
            tc_name = tc.get("name", "")
            tc_id = tc.get("tool_call_id", "")
            tc_args = tc.get("arguments", "")
            if not tc_name or not tc_id:
                skipped += 1
                continue
            if isinstance(tc_args, dict):
                tc_args = json.dumps(tc_args, ensure_ascii=False)
            elif not isinstance(tc_args, str):
                tc_args = str(tc_args)
            current_tool_calls.append({
                "type": "function",
                "id": tc_id,
                "function": {"name": tc_name, "arguments": tc_args},
            })

        elif event_type == "chat.tool_result":
            tc_id = record.get("tool_call_id", "")
            result_content = str(record.get("result", ""))
            if not tc_id:
                skipped += 1
                continue
            # Check if this tool_result has a matching tool_call — either
            # in the current buffer (pending flush) or already emitted.
            # Interactive tools like ask_user emit a preliminary empty
            # tool_result BEFORE the chat.tool_call event; skip those to
            # avoid orphaned ToolMessages (the real result arrives later
            # after the actual tool_call event and is handled correctly).
            pending_ids = {tc["id"] for tc in current_tool_calls}
            if tc_id not in pending_ids and tc_id not in emitted_tool_call_ids:
                skipped += 1
                continue
            # Flush pending AssistantMessage (reasoning + tool_calls) before
            # emitting ToolMessages — ensures correct message ordering:
            #   AssistantMessage(tool_calls) → ToolMessage(result)
            if reasoning_buffer or current_tool_calls:
                _flush_pending_assistant()
            context_messages.append(ToolMessage(
                tool_call_id=tc_id,
                content=result_content,
            ))

        elif event_type == "chat.final":
            # Final text response — flush any pending state first
            if current_tool_calls:
                _flush_pending_assistant()
            reasoning = "".join(reasoning_buffer).strip()
            reasoning_buffer = []
            if content.strip() or reasoning:
                context_messages.append(AssistantMessage(
                    content=content.strip() if content.strip() else "",
                    reasoning_content=reasoning if reasoning else None,
                ))

        elif event_type == "context.compact_summary":
            if current_tool_calls:
                _flush_pending_assistant()
            if content.strip():
                context_messages.append(UserMessage(content=content))

        elif event_type == "context.rewind_summary":
            if current_tool_calls:
                _flush_pending_assistant()
            if content.strip():
                context_messages.append(UserMessage(content=content))

        else:
            # chat.delta, chat.tool_update, chat.usage_metadata,
            # chat.usage_summary, chat.ask_user_question,
            # context.compact_boundary
            skipped += 1

    # Flush any remaining state (e.g. interrupted turn with only reasoning)
    if reasoning_buffer or current_tool_calls:
        _flush_pending_assistant()

    # --- Post-processing (aligned with claude-code's deserialization pipeline) ---

    # Filter out AssistantMessages whose tool_calls have no matching ToolMessage.
    # Analogous to claude-code's filterUnresolvedToolUses — these occur when
    # the history was truncated mid-turn (e.g. interrupted stream or crash)
    # and would cause API errors (the model can't see tool results that don't exist).
    tool_result_ids: set[str] = set()
    for msg in context_messages:
        if isinstance(msg, ToolMessage) and msg.tool_call_id:
            tool_result_ids.add(msg.tool_call_id)

    filtered_messages: list[Any] = []
    removed_unresolved = 0
    for msg in context_messages:
        if isinstance(msg, AssistantMessage) and msg.tool_calls:
            # Keep only tool_calls that have a matching ToolMessage result
            resolved = [
                tc for tc in msg.tool_calls
                if (tc.model_dump() if hasattr(tc, "model_dump") else tc).get("id") in tool_result_ids
            ]
            unresolved_count = len(msg.tool_calls) - len(resolved)
            if unresolved_count > 0:
                removed_unresolved += unresolved_count
                if not resolved and not msg.content and not msg.reasoning_content:
                    # Entire message was only unresolved tool_calls — drop it
                    continue
                # Rebuild message with only resolved tool_calls
                msg = AssistantMessage(
                    content=msg.content or "",
                    reasoning_content=msg.reasoning_content,
                    tool_calls=resolved if resolved else None,
                )
        filtered_messages.append(msg)

    if removed_unresolved > 0:
        logger.info(
            "_build_context_messages_from_history: removed %d unresolved tool_call(s)",
            removed_unresolved,
        )

    # 团队期身份复位注记：仅净化重建路径（drop_member_internals=True）且历史
    # 确实含团队期记录时追加，作为序列末条——leader 的主理人口吻答复逐字回归
    # 后，给默认角色一个明确的身份切换锚点。注记只进内存上下文，不回写
    # history.jsonl；后处理只过滤 unresolved tool_calls，不受影响。
    if drop_member_internals and saw_team_era:
        filtered_messages.append(AssistantMessage(content=_TEAM_IDENTITY_RESET_NOTE))

    return filtered_messages, skipped


async def _try_restore_from_checkpointer(
    *,
    context_engine: Any,
    session: Any,
    react_agent: Any,
    session_id: str,
) -> bool:
    """恢复持久化 checkpointer 中压缩后的 context，避免重启后重复压缩。

    上一轮 turn 结束时的 ``save_contexts(session)`` + ``session.commit()`` 已把
    压缩后的 context 快照写进 session state，并由 checkpointer 持久化到 sqlite。
    这里调用 ``create_context`` **不带** ``history_messages``，走
    ``context_engine._load_state_from_session`` → ``context.load_state(states)``
    恢复压缩快照；若带 ``history_messages`` 则会覆盖这些快照、触发再次压缩——
    这正是本改造要消除的行为。

    Returns:
        恢复出非空 context 返回 True；checkpointer 无可用快照（或恢复失败）
        返回 False，由调用方回退到 history.jsonl 全量重灌。
    """
    try:
        await context_engine.create_context(
            session=session,
            processors=_get_context_processors(react_agent),
        )
    except Exception as exc:
        logger.warning(
            "warmup_session_context: checkpointer restore failed for %s (%s); "
            "falling back to history rebuild",
            session_id, exc,
        )
        return False

    context = context_engine.get_context(session_id=session_id)
    if context is not None and context.get_messages():
        logger.info(
            "warmup_session_context: session=%s restored %d messages from checkpointer "
            "(compressed, no re-compression)",
            session_id, len(context.get_messages()),
        )
        return True

    # checkpointer 无可用快照（新会话 / 从未 commit）→ 清掉空 context，让
    # history 重灌在干净的 buffer 上进行。
    try:
        await context_engine.clear_context(session_id=session_id)
    except Exception as exc:
        logger.warning("warmup_session_context: clear_context failed for %s: %s", session_id, exc)
    return False


def _exclude_history_request_id(
    history_records: list[Any],
    exclude_request_id: str | None,
) -> list[Any]:
    excluded = (exclude_request_id or "").strip()
    if not excluded:
        return list(history_records)
    return [
        record
        for record in history_records
        if not (
            isinstance(record, dict)
            and str(record.get("request_id") or "").strip() == excluded
        )
    ]


def history_tail_request_id(
    history_records: list[Any] | None,
    *,
    exclude_request_id: str | None = None,
) -> str | None:
    """Last non-empty ``request_id`` on disk, optionally skipping the in-flight turn."""
    if not isinstance(history_records, list):
        return None
    excluded = (exclude_request_id or "").strip()
    tail: str | None = None
    for record in history_records:
        if not isinstance(record, dict):
            continue
        rid = str(record.get("request_id") or "").strip()
        if not rid or (excluded and rid == excluded):
            continue
        tail = rid
    return tail


def load_history_tail_request_id(
    session_id: str,
    *,
    exclude_request_id: str | None = None,
) -> str | None:
    try:
        return read_history_tail_request_id(
            session_id, exclude_request_id=exclude_request_id
        )
    except OSError as exc:
        logger.warning(
            "load_history_tail_request_id: failed to read history for %s: %s",
            session_id,
            exc,
        )
        return None


def _slice_history_after_fingerprint(
    history_records: list[Any],
    fingerprint: str | None,
) -> list[Any] | None:
    """Records after the last matching fingerprint. None if the cut point is missing."""
    synced = str(fingerprint or "").strip()
    if not synced:
        return None
    last_idx: int | None = None
    for index, record in enumerate(history_records):
        if not isinstance(record, dict):
            continue
        rid = str(record.get("request_id") or "").strip()
        if rid == synced:
            last_idx = index
    if last_idx is None:
        return None
    return list(history_records[last_idx + 1 :])


async def _add_messages_to_context(context: Any, messages: list[Any]) -> None:
    adder = getattr(context, "add_messages", None)
    if not callable(adder):
        raise TypeError("context has no add_messages")
    for message in messages:
        result = adder(message)
        if inspect.isawaitable(result):
            await result


async def _append_peer_history_to_context(
        *,
        deep_agent: "DeepAgent",
        session_id: str,
        delta_records: list[Any],
        log_label: str,
) -> bool:
    """Append peer turns onto the live compressed window. Do not rebuild or persist."""
    react_agent = getattr(deep_agent, "react_agent", None)
    if react_agent is None:
        logger.warning("%s: no react_agent for %s", log_label, session_id)
        return False

    context_engine = react_agent.context_engine
    getter = getattr(context_engine, "get_context", None)
    context = getter(session_id=session_id) if callable(getter) else None
    if context is None:
        return False

    if not delta_records:
        return True

    # refresh 场景面向"主 agent 视角的续聊上下文"：成员内部事件净化
    context_messages, skipped = _build_context_messages_from_history(
        delta_records, drop_member_internals=True
    )
    if not context_messages:
        logger.info(
            "%s: delta had no rebuildable messages session=%s skipped=%d",
            log_label,
            session_id,
            skipped,
        )
        return True

    try:
        await _add_messages_to_context(context, context_messages)
    except Exception as exc:
        logger.warning(
            "%s: add_messages failed session=%s: %s",
            log_label,
            session_id,
            exc,
        )
        return False

    logger.info(
        "%s: session=%s appended %d peer messages from disk history "
        "(skipped %d streaming/metadata records)",
        log_label,
        session_id,
        len(context_messages),
        skipped,
    )
    return True


async def _persist_session_context(
    *,
    session: Any,
    context_engine: Any,
    session_id: str,
    log_label: str,
) -> bool:
    """Write the live context window onto Session.context; do not touch agent state."""
    try:
        await context_engine.save_contexts(session)
        return True
    except Exception as exc:
        logger.warning(
            "%s: save_contexts failed session=%s: %s",
            log_label,
            session_id,
            exc,
        )
        return False


async def _restore_session_context_from_history(
        *,
        deep_agent: "DeepAgent",
        session_id: str,
        history_records: list[Any],
        log_label: str = "warmup_session_context",
        drop_member_internals: bool = False,
) -> bool:
    """Cold-start restore: create_context from history records. Does not clear a live window."""
    react_agent = getattr(deep_agent, "react_agent", None)
    if react_agent is None:
        logger.warning("%s: no react_agent for %s", log_label, session_id)
        return False

    if not isinstance(history_records, list) or not history_records:
        return False

    context_messages, skipped = _build_context_messages_from_history(
        history_records, drop_member_internals=drop_member_internals
    )
    if not context_messages:
        logger.info("%s: no rebuildable messages in history for %s", log_label, session_id)
        return False

    live_session = resolve_live_agent_session(deep_agent, session_id)
    session = live_session
    if session is None:
        try:
            from openjiuwen.core.single_agent import create_agent_session

            session = create_agent_session(
                session_id=session_id, card=getattr(deep_agent, "card", None)
            )
            await session.pre_run(inputs=None)
        except Exception as exc:
            logger.warning("%s: pre_run failed for %s: %s", log_label, session_id, exc)
            return False

    try:
        await react_agent.context_engine.create_context(
            session=session,
            processors=_get_context_processors(react_agent),
            history_messages=context_messages,
        )
    except Exception as exc:
        logger.warning("%s: create_context failed for %s: %s", log_label, session_id, exc)
        return False

    logger.info(
        "%s: session=%s restored context from disk history with %d messages "
        "(skipped %d streaming/metadata records)",
        log_label,
        session_id,
        len(context_messages),
        skipped,
    )
    return True


async def warmup_session_context(
    *,
    deep_agent: "DeepAgent",
    session_id: str,
    exclude_request_id: str | None = None,
) -> str | None:
    """Restart-safe restore of context_engine messages.

    在新建 session adapter（``start_interaction`` 之后）调用。恢复顺序：
    1. 优先从持久化 checkpointer 恢复压缩后的 context 快照（由上一轮
       ``save_contexts``+``commit`` 写入），避免重启后用全量 history 重灌导致
       再次压缩——这是「重启后不重复压缩」的核心。
    2. 仅当 checkpointer 无可用快照时，才从磁盘 history.jsonl 全量重灌兜底
       （历史上只有 history.jsonl 一种来源，故保留该分支）。

    与 ``rewind_session_context`` 的区别：不截断 history、不强写 checkpointer。

    ``chat.send`` 会在 adapter 冷启动前先把当前用户消息持久化。调用方可传入
    ``exclude_request_id``，使 warmup 仅恢复此前历史；当前轮仍由正常的 inputs
    路径注入一次，避免首轮在模型上下文中重复。

    返回本次恢复覆盖到的 history 尾 request_id，供调用方作 synced 指纹：
    - 快照恢复 → 快照尾指纹（session metadata ``context_snapshot_tail``；
      老会话无记录回退磁盘尾=旧行为）。快照可能停在团队期之前（团队轮次不
      经默认 agent、不写其快照），若回退磁盘尾会让 refresh 误判"已同步"，
      团队期增量永远补不回来——快照尾指纹即为此而设。
    - 历史重灌 → 重灌记录的尾（已排除在途请求）。
    - 未恢复（全新会话/失败）→ None。
    """
    react_agent = getattr(deep_agent, "react_agent", None)
    if react_agent is None:
        logger.warning("warmup_session_context: no react_agent for %s", session_id)
        return None

    context_engine = react_agent.context_engine
    if context_engine.get_context(session_id=session_id) is not None:
        # 内存上下文已存在（进程未重启 / 已 warmup / rewind 重建过）
        return load_history_tail_request_id(
            session_id, exclude_request_id=exclude_request_id
        )

    if not history_exists(session_id):
        # 全新会话，磁盘无历史，静默跳过
        return None

    session = resolve_live_agent_session(deep_agent, session_id)
    if session is None:
        # 正常调用点（start_interaction 之后）live session 必在；兜底临时 Session。
        try:
            from openjiuwen.core.single_agent import create_agent_session

            session = create_agent_session(
                session_id=session_id, card=getattr(deep_agent, "card", None)
            )
            await session.pre_run(inputs=None)
        except Exception as exc:
            logger.warning("warmup_session_context: pre_run failed for %s: %s", session_id, exc)
            return None

    # ── 优先：从持久化 checkpointer 恢复压缩后的 context ──
    # create_context 不带 history_messages → _load_state_from_session 恢复压缩
    # 快照，不覆盖已压好的内容，从而消掉重启后的重复压缩。
    if await _try_restore_from_checkpointer(
        context_engine=context_engine,
        session=session,
        react_agent=react_agent,
        session_id=session_id,
    ):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_context_snapshot_tail,
        )

        return get_context_snapshot_tail(session_id) or load_history_tail_request_id(
            session_id, exclude_request_id=exclude_request_id
        )

    # ── 兜底：checkpointer 无可用快照时，从磁盘 history.jsonl 全量重灌 ──
    try:
        history_records = load_history_records(session_id)
    except OSError as exc:
        logger.warning("warmup_session_context: failed to read history for %s: %s", session_id, exc)
        return None

    if not isinstance(history_records, list) or not history_records:
        return None

    history_records = _exclude_history_request_id(history_records, exclude_request_id)
    if not history_records:
        return None

    restored = await _restore_session_context_from_history(
        deep_agent=deep_agent,
        session_id=session_id,
        history_records=history_records,
        log_label="warmup_session_context",
        # warmup 面向"主 agent 续聊"：团队成员内部事件净化；
        # rewind 的同名调用保持默认 False（保真重建是有意契约）
        drop_member_internals=True,
    )
    if not restored:
        return None
    return history_tail_request_id(history_records)


async def refresh_session_context_if_stale(
    *,
    deep_agent: "DeepAgent | None",
    session_id: str,
    exclude_request_id: str | None = None,
    synced_tail_request_id: str | None = None,
) -> str | None:
    """Append peer-channel turns when another adapter wrote newer history.

    ``xiaoyi`` / ``desktop`` each hold a session adapter. Warmup only runs on
    create, so a reused adapter can miss turns the other channel already
    persisted to ``history.jsonl``. Compare the last on-disk ``request_id``
    (excluding the in-flight turn) with ``synced_tail_request_id`` and, if they
    differ, append the delta onto the live compressed window. Missing
    fingerprint or append/persist failure keeps the old fingerprint.
    HITL / plan session state is left untouched.

    Returns the disk tail the caller should store as the new fingerprint.
    """
    synced = str(synced_tail_request_id or "").strip() or None
    if deep_agent is None:
        return synced

    disk_tail: str | None = None
    if history_exists(session_id):
        disk_tail = load_history_tail_request_id(
            session_id, exclude_request_id=exclude_request_id
        )

    react_agent = getattr(deep_agent, "react_agent", None)
    if react_agent is None:
        return disk_tail or synced

    context_engine = react_agent.context_engine
    if context_engine.get_context(session_id=session_id) is None:
        warmed_tail = await warmup_session_context(
            deep_agent=deep_agent,
            session_id=session_id,
            exclude_request_id=exclude_request_id,
        )
        if warmed_tail is not None:
            # warmup 自报覆盖尾：快照恢复时可能是快照尾（早于磁盘尾），
            # 原样返回让调用方存为指纹，下一问 refresh 才能把增量补回
            return warmed_tail
        return disk_tail if disk_tail is not None else synced

    if disk_tail is None or disk_tail == synced:
        return disk_tail if disk_tail is not None else synced

    try:
        loaded = load_history_records(session_id)
    except OSError as exc:
        logger.warning(
            "refresh_session_context_if_stale: failed to read history for %s: %s",
            session_id,
            exc,
        )
        return synced
    history_records = loaded if isinstance(loaded, list) else []
    filtered = _exclude_history_request_id(history_records, exclude_request_id)
    delta = _slice_history_after_fingerprint(filtered, synced)
    log_label = "refresh_session_context_if_stale"
    if delta is None:
        # 指纹失效（rewind 截断/历史轮换等）旧行为是 WARNING 后放弃同步——
        # 上下文凭空停更且无任何恢复。改为全量重建兜底：活窗内容本就源自
        # 磁盘历史（在途请求由 exclude_request_id 排除在外），清窗重建不丢数据
        logger.warning(
            "%s: fingerprint missing from history session=%s disk_tail=%s previous=%s; "
            "falling back to full rebuild",
            log_label,
            session_id,
            disk_tail,
            synced,
        )
        # 保留挂起运行的在途轮次（重建保留修复）：活窗 = 磁盘已完成轮 +
        # 挂起运行未落盘的在途轮次（HITL 挂起时本轮 query/段1/工具调用都在
        # 内存）。清窗前快照活窗消息，磁盘重建后把"磁盘未覆盖的在途尾部"
        # 按序并回——不并回则模型上下文只剩磁盘已完成轮（问候轮），续跑
        # 输出退化为问候复述（2026-11-07 a.py 轮实测）。
        _live_msgs: list = []
        _live_key_used = ""
        try:
            _live_ctx = context_engine.get_context(session_id=session_id)
            if _live_ctx is not None:
                _live_msgs = list(_live_ctx.get_messages() or [])
                _live_key_used = "{}_default_context_id".format(session_id)
            if not _live_msgs:
                # 上下文池兜底：运行期活窗可能挂在同会话的非默认 context_id
                # 下（react_agent 按 context_id 分池），扫描同会话全部键取
                # 消息数最多的一个；池键诊断日志用于定位键不一致问题。
                _pool = getattr(context_engine, "_context_pool", None) or {}
                for _k, _v in _pool.items():
                    if not str(_k).startswith("{}_".format(session_id)):
                        continue
                    _msgs = list(_v.get_messages() or []) if _v is not None else []
                    logger.info(
                        "%s: context pool scan session=%s key=%s msgs=%d",
                        log_label,
                        session_id,
                        _k,
                        len(_msgs),
                    )
                    if len(_msgs) > len(_live_msgs):
                        _live_msgs = _msgs
                        _live_key_used = str(_k)
            logger.info(
                "%s: in-flight snapshot session=%s key=%s msgs=%d",
                log_label,
                session_id,
                _live_key_used or "-",
                len(_live_msgs),
            )
        except Exception as exc:
            logger.warning(
                "%s: snapshot live context failed session=%s: %s",
                log_label,
                session_id,
                exc,
            )
        try:
            await context_engine.clear_context(session_id=session_id)
        except Exception as exc:
            logger.warning(
                "%s: clear_context before rebuild failed session=%s: %s",
                log_label,
                session_id,
                exc,
            )
            return synced
        rebuilt = await _restore_session_context_from_history(
            deep_agent=deep_agent,
            session_id=session_id,
            history_records=filtered,
            log_label=log_label,
            drop_member_internals=True,
        )
        if not rebuilt:
            return synced
        # 在途尾部并回：前向对齐 restored 与活窗（role+content），活窗游标
        # 之后的剩余消息即在途轮次，add_messages 按序回填。至少匹配到一条
        # 才并回（全不匹配说明对齐失败，宁缺勿错）；无在途则记日志跳过。
        if _live_msgs:
            try:
                _rebuilt_ctx = context_engine.get_context(session_id=session_id)
                _restored_msgs = (
                    list(_rebuilt_ctx.get_messages() or []) if _rebuilt_ctx else []
                )

                def _msg_key(m: Any) -> str:
                    # 防御式取值：活窗可能含工具记录等非标准消息对象，
                    # 缺 role/content 属性时给占位键，不让单条异常中止并回
                    try:
                        content = getattr(m, "content", None)
                        if isinstance(content, list):
                            content = json.dumps(content, ensure_ascii=False)
                        return "{}|{}".format(getattr(m, "role", ""), content)
                    except Exception:
                        return "<unkeyable>"

                # 严格位置对齐（同索引逐条比较）：磁盘重建是同一批轮次的
                # 重放，前缀应逐条一致；首处失配即在途边界。禁止跳跃扫描——
                # 子序列式跳过会把在途尾部当"不匹配"消费掉（16:21 轮实测：
                # restored 8 条对齐后游标越过活窗末尾，在途 6 条全部丢失，
                # 模型复读上一轮"北京天气已查询完毕"）。
                _boundary = 0
                for _i, _r in enumerate(_restored_msgs):
                    if _i >= len(_live_msgs):
                        break
                    if _msg_key(_r) != _msg_key(_live_msgs[_i]):
                        break
                    _boundary = _i + 1
                _in_flight = _live_msgs[_boundary:]
                if _in_flight:
                    if _boundary == 0 and _restored_msgs:
                        # 零匹配：活窗与磁盘重建无重叠（分池/分轮上下文）——
                        # 整窗并回（端侧缓冲已由卡片 append=false 重置，不重复）
                        logger.warning(
                            "%s: in-flight alignment found no overlap session=%s, "
                            "re-append whole live window (%d messages)",
                            log_label,
                            session_id,
                            len(_in_flight),
                        )
                    await _rebuilt_ctx.add_messages(_in_flight)
                    logger.info(
                        "%s: re-appended %d in-flight message(s) session=%s",
                        log_label,
                        len(_in_flight),
                        session_id,
                    )
            except Exception as exc:
                logger.warning(
                    "%s: re-append in-flight messages failed session=%s: %s",
                    log_label,
                    session_id,
                    exc,
                )
        session = resolve_live_agent_session(deep_agent, session_id)
        if session is None:
            logger.warning(
                "%s: no live session to persist rebuilt context for %s",
                log_label,
                session_id,
            )
            return synced
        if not await _persist_session_context(
            session=session,
            context_engine=context_engine,
            session_id=session_id,
            log_label=log_label,
        ):
            return synced
        logger.info(
            "%s: session=%s rebuilt context after fingerprint loss disk_tail=%s",
            log_label,
            session_id,
            disk_tail,
        )
        return disk_tail

    restored = await _append_peer_history_to_context(
        deep_agent=deep_agent,
        session_id=session_id,
        delta_records=delta,
        log_label=log_label,
    )
    if not restored:
        logger.warning(
            "%s: append failed session=%s disk_tail=%s previous=%s",
            log_label,
            session_id,
            disk_tail,
            synced,
        )
        return synced

    session = resolve_live_agent_session(deep_agent, session_id)
    if session is None:
        logger.warning(
            "%s: no live session to persist context for %s",
            log_label,
            session_id,
        )
        return synced
    persist_ok = await _persist_session_context(
        session=session,
        context_engine=context_engine,
        session_id=session_id,
        log_label=log_label,
    )
    if not persist_ok:
        return synced

    logger.info(
        "refresh_session_context_if_stale: session=%s synced context disk_tail=%s previous=%s",
        session_id,
        disk_tail,
        synced,
    )
    return disk_tail


async def rewind_session_context(
    *,
    deep_agent: "DeepAgent",
    session_id: str,
    turn_index: int,
) -> bool:
    """Rebuild context_engine from truncated history.json and persist to checkpointer.

    The context_engine buffer only holds a sliding window (older messages are
    compressed by ``round_level_compressor`` / ``dialogue_compressor``), so we
    cannot simply slice the in-memory buffer.  Instead we reload the truncated
    history.json, convert its records to openjiuwen messages, tear down the old
    context, and build a fresh one.

    When DeepAgent has a live ``_interaction_session`` for this session_id, the
    rebuild mutates that Session in place (commit, not post_run) so the next
    chat round does not reload stale pre-rewind messages from memory.
    """
    from openjiuwen.core.foundation.llm.schema.message import (
        UserMessage,
        AssistantMessage,
    )

    react_agent = deep_agent.react_agent
    if react_agent is None:
        logger.warning("rewind_session_context: no react_agent for %s", session_id)
        return False

    # --- 1. Load truncated history.json (already cut by caller) ---
    history_path = get_read_history_path(session_id)
    if not history_path.exists():
        logger.warning("rewind_session_context: history not found for %s", session_id)
        return False

    try:
        history_records = load_history_records(session_id)
    except OSError as exc:
        logger.warning("rewind_session_context: failed to read history for %s: %s", session_id, exc)
        return False

    if not isinstance(history_records, list):
        logger.warning("rewind_session_context: invalid history for %s", session_id)
        return False

    # Empty history (e.g. rewind removed every turn) still requires clearing the
    # live context — otherwise the next chat.send keeps the rewound turns.
    if not history_records:
        logger.info("rewind_session_context: empty history for %s; clearing live context", session_id)
        applied = await _apply_rewound_context(
            deep_agent=deep_agent,
            react_agent=react_agent,
            session_id=session_id,
            turn_index=turn_index,
            context_messages=[],
            skipped=0,
        )
        if applied:
            _record_snapshot_tail(session_id, None)
        return applied

    # --- 2. Convert history.json records → openjiuwen BaseMessage list ---
    context_messages, skipped = _build_context_messages_from_history(history_records)

    # If conversation ends with an AssistantMessage, append a synthetic
    # continuation user message so the next API call has proper role
    # alternation.  Analogous to claude-code's NO_RESPONSE_REQUESTED sentinel.
    if context_messages and isinstance(context_messages[-1], AssistantMessage):
        context_messages.append(UserMessage(
            content="[Continue from where the conversation was rewound.]"
        ))

    applied = await _apply_rewound_context(
        deep_agent=deep_agent,
        react_agent=react_agent,
        session_id=session_id,
        turn_index=turn_index,
        context_messages=context_messages,
        skipped=skipped,
    )
    if applied:
        # rewind 已提交快照：快照覆盖尾回收到截断后的历史尾，
        # 否则 warmup 快照恢复会拿着截断前的指纹让 refresh 误判
        _record_snapshot_tail(session_id, history_tail_request_id(history_records))
    return applied


def _record_snapshot_tail(session_id: str, tail_request_id: Any) -> None:
    """快照尾指纹维护的安全包装：失败仅告警（ rewind 等主流程不受影响）。"""
    try:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            clear_context_snapshot_tail,
            set_context_snapshot_tail,
        )

        if tail_request_id:
            set_context_snapshot_tail(session_id, tail_request_id)
        else:
            clear_context_snapshot_tail(session_id)
    except Exception as exc:
        logger.warning(
            "record snapshot tail failed: session=%s tail=%s error=%s",
            session_id,
            tail_request_id,
            exc,
        )


def resolve_live_agent_session(deep_agent: "DeepAgent", session_id: str) -> Any | None:
    """Return DeepAgent's long-lived Session if it matches ``session_id``.

    Chat rounds reuse ``_interaction_session`` (pre_run once). Writing through a
    fresh Session only updates the checkpointer; the next turn still reads the
    stale in-memory snapshot cached on the bound session — this bites both the
    rewound context and ``DeepAgentState.plan_mode``.
    """
    for attr in ("_interaction_session", "_loop_session"):
        session_obj = getattr(deep_agent, attr, None)
        if session_obj is None:
            continue
        get_sid = getattr(session_obj, "get_session_id", None)
        if not callable(get_sid):
            continue
        try:
            if str(get_sid()) == str(session_id):
                return session_obj
        except Exception as exc:
            # Skip this candidate and try the next attr / fall back to a temp
            # Session; a broken get_session_id must not abort the caller.
            logger.warning(
                "resolve_live_agent_session: get_session_id failed on %s for %s: %s",
                attr, session_id, exc,
            )
            continue
    return None


async def _wipe_session_runtime_state(
    *,
    session: Any,
    react_agent: Any,
    session_id: str,
) -> None:
    """Clear context / deep-agent / HITL keys on a live or temp Session."""
    from openjiuwen.harness.schema.state import _SESSION_STATE_KEY

    try:
        session.update_state({"context": None})
        session.update_state({_SESSION_STATE_KEY: None})
        try:
            from openjiuwen.core.single_agent.interrupt.state import (
                INTERRUPTION_KEY,
                INTERRUPT_AUTO_CONFIRM_KEY,
            )
            session.update_state({INTERRUPTION_KEY: None})
            session.update_state({INTERRUPT_AUTO_CONFIRM_KEY: None})
        except Exception as int_exc:
            logger.warning(
                "rewind_session_context: HITL interrupt wipe failed for %s: %s",
                session_id, int_exc,
            )
        try:
            hitl_handler = getattr(react_agent, "_hitl_handler", None)
            if hitl_handler is not None:
                hitl_handler.clear(session)
        except Exception as int_exc:
            logger.warning(
                "rewind_session_context: in-memory HITL clear failed for %s: %s",
                session_id, int_exc,
            )
    except Exception as exc:
        logger.warning("rewind_session_context: state wipe failed for %s: %s", session_id, exc)


async def _persist_rewound_session(
    *,
    session: Any,
    deep_agent: "DeepAgent",
    context_engine: Any,
    session_id: str,
    is_live_session: bool,
) -> bool:
    """Save rebuilt context; commit live sessions without post_run side effects."""
    try:
        await context_engine.save_contexts(session)
        try:
            deep_agent.save_state(session)
        except Exception as save_exc:
            logger.warning(
                "rewind_session_context: deep_agent.save_state failed for %s: %s",
                session_id, save_exc,
            )
        if is_live_session:
            # post_run closes the interaction stream and marks the session done;
            # chat must keep using the same Session object.
            commit = getattr(session, "commit", None)
            if callable(commit):
                await commit()
            else:
                await session.post_run()
        else:
            await session.post_run()
        return True
    except Exception as exc:
        logger.warning(
            "rewind_session_context: checkpointer persist failed for %s: %s",
            session_id, exc,
        )
        return False


async def _apply_rewound_context(
    *,
    deep_agent: "DeepAgent",
    react_agent: Any,
    session_id: str,
    turn_index: int,
    context_messages: list[Any],
    skipped: int,
) -> bool:
    """Clear + rebuild context_engine and sync the Session the next turn will use."""
    from openjiuwen.core.single_agent import create_agent_session

    context_engine = react_agent.context_engine
    context = context_engine.get_context(session_id=session_id)
    if context is not None:
        logger.info(
            "rewind_session_context: clearing old context for %s (%d messages in buffer)",
            session_id, len(context.get_messages()),
        )
    await context_engine.clear_context(session_id=session_id)

    live_session = resolve_live_agent_session(deep_agent, session_id)
    is_live_session = live_session is not None
    if is_live_session:
        session = live_session
        logger.info(
            "rewind_session_context: reusing live interaction session for %s",
            session_id,
        )
    else:
        try:
            session = create_agent_session(session_id=session_id, card=deep_agent.card)
            await session.pre_run(inputs=None)
        except Exception as exc:
            logger.warning("rewind_session_context: pre_run failed for %s: %s", session_id, exc)
            return False

    await _wipe_session_runtime_state(
        session=session,
        react_agent=react_agent,
        session_id=session_id,
    )

    try:
        await context_engine.create_context(
            session=session,
 	        processors=_get_context_processors(react_agent),
            history_messages=context_messages,
        )
    except Exception as exc:
        logger.warning("rewind_session_context: create_context failed for %s: %s", session_id, exc)
        return False

    persist_ok = await _persist_rewound_session(
        session=session,
        deep_agent=deep_agent,
        context_engine=context_engine,
        session_id=session_id,
        is_live_session=is_live_session,
    )

    logger.info(
        "rewind_session_context: session=%s turn=%d rebuilt context with %d messages "
        "(skipped %d streaming/metadata records) persist=%s live_session=%s",
        session_id, turn_index, len(context_messages), skipped, persist_ok, is_live_session,
    )
    return True


def _flush_source_state(deep_agent: "DeepAgent", session_id: str) -> None:
    react = deep_agent.react_agent
    if react is None:
        return
    ctx = react.context_engine.get_context(session_id=session_id)
    session_obj = getattr(ctx, "session", None)
    if session_obj is not None:
        deep_agent.save_state(session_obj)


async def copy_session_state(
    source_session_id: str,
    target_session_id: str,
    card: Any,
    deep_agent: "DeepAgent | None" = None,
) -> bool:
    """Copy DeepAgentState from source to target session via Checkpointer.

    Reads source state from the Checkpointer SQLite database, transforms it
    for a branched session (reset iteration, clear transient state, generate
    new plan slug), and writes it to the target session's Checkpointer entry.

    Returns True on success, False if state copy was skipped or failed.
    """
    from openjiuwen.core.single_agent import create_agent_session
    from openjiuwen.harness.schema.state import _SESSION_STATE_KEY

    # Flush source runtime state to Checkpointer if deep_agent is available
    if deep_agent is not None:
        try:
            _flush_source_state(deep_agent, source_session_id)
        except Exception as exc:
            logger.debug(
                "copy_session_state: cannot flush source state: %s", exc
            )

    # Read source state from Checkpointer
    source_session = None
    source_state_dict: Any = None
    try:
        source_session = create_agent_session(
            session_id=source_session_id, card=card
        )
        await source_session.pre_run()
        source_state_dict = source_session.get_state(_SESSION_STATE_KEY)
    except Exception as exc:
        logger.warning(
            "copy_session_state: cannot read source state from Checkpointer: %s",
            exc,
        )
        return False
    finally:
        if source_session is not None:
            try:
                await source_session.post_run()
            except Exception as exc:
                logger.warning(
                    "copy_session_state: error during source session cleanup: %s", exc
                )

    if not source_state_dict:
        logger.info(
            "copy_session_state: no DeepAgentState for %s, skipping",
            source_session_id,
        )
        return False

    # Transform state for branched session (deep copy to avoid mutating source)
    modified_state = copy.deepcopy(source_state_dict)
    modified_state["iteration"] = 0
    modified_state["stop_condition_state"] = None
    modified_state["pending_follow_ups"] = []

    # Generate new plan slug and copy plan file
    plan_mode = modified_state.get("plan_mode") or {}
    old_slug = plan_mode.get("plan_slug")
    if old_slug:
        try:
            from openjiuwen.harness.tools.agent_mode_tools import (
                get_or_create_plan_slug,
                resolve_plan_file_path,
            )

            workspace_root = str(get_agent_workspace_dir())
            new_slug = get_or_create_plan_slug(workspace_root)
            old_plan_path = resolve_plan_file_path(workspace_root, old_slug)
            new_plan_path = resolve_plan_file_path(workspace_root, new_slug)
            if old_plan_path.exists():
                shutil.copy2(old_plan_path, new_plan_path)
            plan_mode["plan_slug"] = new_slug
            modified_state["plan_mode"] = plan_mode
            logger.info(
                "copy_session_state: plan file copied: %s → %s",
                old_slug, new_slug,
            )
        except Exception as exc:
            logger.debug(
                "copy_session_state: plan file copy failed (non-critical): %s",
                exc,
            )

    # Write transformed state to target via Checkpointer
    try:
        target_session = create_agent_session(
            session_id=target_session_id, card=card
        )
        await target_session.pre_run()
        target_session.update_state({_SESSION_STATE_KEY: modified_state})
        await target_session.post_run()
        logger.info(
            "copy_session_state: copied DeepAgentState from %s to %s",
            source_session_id, target_session_id,
        )
        return True
    except Exception as exc:
        logger.warning(
            "copy_session_state: failed to write target state: %s", exc
        )
        return False


async def copy_session_context(
    deep_agent: "DeepAgent",
    source_session_id: str,
    target_session_id: str,
) -> bool:
    """Copy in-memory conversation context from source to target session.

    Uses DeepAgent.get_current_context() to read the source session's
    accumulated LLM conversation history (UserMessage, AssistantMessage,
    ToolMessage objects), then calls create_new_context_engine() to
    seed the target session with identical history.

    Returns True on success, False if context copy was skipped or failed.
    """
    try:
        messages = deep_agent.get_current_context(session_id=source_session_id)
    except Exception as exc:
        logger.warning(
            "copy_session_context: cannot read source context: %s", exc
        )
        return False

    if not messages:
        logger.info(
            "copy_session_context: no in-memory messages for %s, skipping",
            source_session_id,
        )
        return False

    try:
        await deep_agent.create_new_context_engine(
            session_id=target_session_id,
            messages=messages,
        )
        logger.info(
            "copy_session_context: copied %d messages from %s to %s",
            len(messages), source_session_id, target_session_id,
        )
        return True
    except Exception as exc:
        logger.warning(
            "copy_session_context: failed to seed target context: %s", exc
        )
        return False
