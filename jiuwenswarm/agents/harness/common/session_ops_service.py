from __future__ import annotations

import copy
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


def _build_context_messages_from_history(
    history_records: list[dict[str, Any]],
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

    return filtered_messages, skipped


async def warmup_session_context(
    *,
    deep_agent: "DeepAgent",
    session_id: str,
    history_before_request_id: str | None = None,
) -> bool:
    """Restart-safe restore of context_engine messages from on-disk history.

    对话消息只存在于 context_engine 的进程内存（``_context_pool``），不落
    checkpointer。server 重启或 session adapter 被空闲驱逐后重建时 pool 为
    空，而 chat.send 主路径不会把磁盘 history.jsonl 回灌给模型，导致
    "能看到历史列表但继续对话失忆"。

    在新建 session adapter（``start_interaction`` 之后）调用：若内存 context
    缺失且磁盘上有历史记录，则将 history 转换为 openjiuwen 消息并灌回
    context_engine。chat.send 会先落盘当前用户消息再创建 adapter，因此传入
    ``history_before_request_id`` 时只恢复该请求之前的记录，避免当前消息同时
    作为历史和实时 query 注入。与 ``rewind_session_context`` 的区别：不改写
    history、不清理 Session state（agent/workflow 状态已由 checkpointer 在
    pre_run 恢复）、不强写 checkpointer（消息持久化本就由 history.jsonl 承担）。
    """
    react_agent = getattr(deep_agent, "react_agent", None)
    if react_agent is None:
        logger.warning("warmup_session_context: no react_agent for %s", session_id)
        return False

    context_engine = react_agent.context_engine
    if context_engine.get_context(session_id=session_id) is not None:
        # 内存上下文已存在（进程未重启 / 已 warmup / rewind 重建过）
        return True

    if not history_exists(session_id):
        # 全新会话，磁盘无历史，静默跳过
        return False

    try:
        history_records = load_history_records(session_id)
    except OSError as exc:
        logger.warning("warmup_session_context: failed to read history for %s: %s", session_id, exc)
        return False

    if not isinstance(history_records, list) or not history_records:
        return False

    boundary_request_id = str(history_before_request_id or "").strip()
    if boundary_request_id:
        for index, record in enumerate(history_records):
            if str(record.get("request_id") or "").strip() == boundary_request_id:
                history_records = history_records[:index]
                break

    context_messages, skipped = _build_context_messages_from_history(history_records)
    if not context_messages:
        logger.info(
            "warmup_session_context: no rebuildable messages in history for %s", session_id
        )
        return False

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
            return False

    try:
        await context_engine.create_context(
            session=session,
            processors=_get_context_processors(react_agent),
            history_messages=context_messages,
        )
    except Exception as exc:
        logger.warning("warmup_session_context: create_context failed for %s: %s", session_id, exc)
        return False

    logger.info(
        "warmup_session_context: session=%s restored context from disk history with %d messages "
        "(skipped %d streaming/metadata records)",
        session_id, len(context_messages), skipped,
    )
    return True


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
        return await _apply_rewound_context(
            deep_agent=deep_agent,
            react_agent=react_agent,
            session_id=session_id,
            turn_index=turn_index,
            context_messages=[],
            skipped=0,
        )

    # --- 2. Convert history.json records → openjiuwen BaseMessage list ---
    context_messages, skipped = _build_context_messages_from_history(history_records)

    # If conversation ends with an AssistantMessage, append a synthetic
    # continuation user message so the next API call has proper role
    # alternation.  Analogous to claude-code's NO_RESPONSE_REQUESTED sentinel.
    if context_messages and isinstance(context_messages[-1], AssistantMessage):
        context_messages.append(UserMessage(
            content="[Continue from where the conversation was rewound.]"
        ))

    return await _apply_rewound_context(
        deep_agent=deep_agent,
        react_agent=react_agent,
        session_id=session_id,
        turn_index=turn_index,
        context_messages=context_messages,
        skipped=skipped,
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
