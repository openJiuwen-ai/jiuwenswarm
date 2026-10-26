"""DiffStatusService: 面向 Web 的 diff 状态聚合服务(设计文档 §2.4 / §3.5 / §4.1.16)。

聚合"当前工作区 diff"与"上一轮对话 diff"两路来源,复用
``DiffService.get_git_diff()`` / ``get_turn_diff_summaries()`` 并转换为 snake_case schema,
合并 ``ProjectGitService`` 的 repo 状态。

第一版能力边界(§2.7):staged/unstaged 分类计数不在范围内。
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jiuwenswarm.server.runtime.session.project_git import GitError, GitOperationError
from jiuwenswarm.server.utils.diff_service import get_diff_service

logger = logging.getLogger(__name__)


def _safe_team_path_segment(value: str, fallback: str = "_") -> str:
    """Sanitize a value into one path segment for team workspace paths."""
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    normalized = normalized.strip("._-")
    return normalized[:96] or fallback


def _session_team_member_names(session_id: str | None) -> list[str]:
    """Return member names observed in persisted team events for a session."""
    sid = str(session_id or "").strip()
    if not sid:
        return []
    try:
        from jiuwenswarm.server.runtime.session.session_history import (
            load_history_records,
        )

        history = load_history_records(sid)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(history, list):
        return []

    names: list[str] = []
    seen: set[str] = set()

    def add_name(raw: Any) -> None:
        if not isinstance(raw, str) or not raw.strip():
            return
        name = raw.strip()
        if name in seen:
            return
        seen.add(name)
        names.append(name)

    for record in history:
        if not isinstance(record, dict):
            continue
        extra = record.get("extra")
        event = extra.get("event") if isinstance(extra, dict) else None
        if not isinstance(event, dict):
            continue
        if event.get("type") != "team.member.spawned":
            continue
        add_name(event.get("name"))
        add_name(event.get("member_id"))
    return names


@dataclass(slots=True)
class DiffStats:
    """Diff 变更统计,``DiffSummary`` 和 ``DiffTurnSummary`` 共用。"""

    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_changed": self.files_changed,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
        }


@dataclass(slots=True)
class DiffHunk:
    """单个 hunk 的结构化表示。"""

    old_start: int = 0
    old_lines: int = 0
    new_start: int = 0
    new_lines: int = 0
    lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_start": self.old_start,
            "old_lines": self.old_lines,
            "new_start": self.new_start,
            "new_lines": self.new_lines,
            "lines": list(self.lines),
        }


@dataclass(slots=True)
class DiffFileEntry:
    """单个文件的 diff 条目。"""

    file_path: str = ""
    status: str = "modified"  # modified | added | deleted | renamed | missing
    lines_added: int = 0
    lines_removed: int = 0
    is_binary: bool = False
    is_new_file: bool = False
    is_deleted_file: bool = False
    is_untracked: bool = False
    is_large_file: bool = False
    is_truncated: bool = False
    hunks: list[DiffHunk] = field(default_factory=list)

    def to_dict(self, *, include_hunks: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "file_path": self.file_path,
            "status": self.status,
            "change_type": self.status,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "is_binary": self.is_binary,
            "is_new_file": self.is_new_file,
            "is_deleted_file": self.is_deleted_file,
            "is_untracked": self.is_untracked,
            "is_large_file": self.is_large_file,
            "is_truncated": self.is_truncated,
            "hunks": [h.to_dict() for h in self.hunks] if include_hunks else [],
        }
        return result


@dataclass(slots=True)
class DiffSummary:
    """当前工作区 diff 的摘要对象。"""

    is_dirty: bool = False
    stats: DiffStats = field(default_factory=DiffStats)
    files: dict[str, DiffFileEntry] = field(default_factory=dict)
    kind: str = "working_tree"

    def to_dict(self, *, include_hunks: bool = True) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "is_dirty": self.is_dirty,
            "stats": self.stats.to_dict(),
            "files": {
                k: v.to_dict(include_hunks=include_hunks)
                for k, v in self.files.items()
            },
        }


@dataclass(slots=True)
class DiffTurnSummary:
    """上一轮对话 diff 的摘要对象。"""

    turn_index: int = 0
    timestamp: str = ""
    user_prompt_preview: str = ""
    stats: DiffStats = field(default_factory=DiffStats)
    files: dict[str, DiffFileEntry] = field(default_factory=dict)
    kind: str = "conversation_turn"
    # 阶段 B1: change_set 稳定标识(惰性回填自 change_sets.json)
    change_set_id: str = ""
    request_id: str = ""
    assistant_message_id: str = ""
    user_message_id: str = ""
    status: str = "completed"

    def to_dict(self, *, include_hunks: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "turn_index": self.turn_index,
            "timestamp": self.timestamp,
            "user_prompt_preview": self.user_prompt_preview,
            "stats": self.stats.to_dict(),
            "files": {
                k: v.to_dict(include_hunks=include_hunks)
                for k, v in self.files.items()
            },
        }
        if self.change_set_id:
            result["change_set_id"] = self.change_set_id
            result["request_id"] = self.request_id
            result["assistant_message_id"] = self.assistant_message_id
            result["user_message_id"] = self.user_message_id
            result["status"] = self.status
        return result


@dataclass(slots=True)
class DiffRepoInfo:
    """Diff 状态中的仓库元信息子对象。"""

    is_git: bool = False
    repo_root: str | None = None
    branch: str | None = None
    head: str | None = None
    transient: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_git": self.is_git,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "head": self.head,
            "transient": self.transient,
        }


@dataclass(slots=True)
class ProjectGitDiffStatus:
    """Diff 状态聚合的顶层返回对象。"""

    project_id: str = ""
    session_id: str | None = None
    work_mode: str = "work"
    repo: DiffRepoInfo = field(default_factory=DiffRepoInfo)
    current: DiffSummary | None = None
    last_turn: DiffTurnSummary | None = None
    generated_at: float = 0.0

    def to_dict(self, *, include_hunks: bool = True) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "session_id": self.session_id,
            "work_mode": self.work_mode,
            "repo": self.repo.to_dict(),
            "current": self.current.to_dict(include_hunks=include_hunks) if self.current else None,
            "last_turn": self.last_turn.to_dict(include_hunks=include_hunks) if self.last_turn else None,
            "generated_at": self.generated_at,
        }


def _to_relative_path(file_path: str, repo_root: str | None) -> str:
    """将绝对路径转换为相对 ``repo_root`` 的路径;无法转换时返回原路径。"""
    if not file_path:
        return file_path
    if repo_root:
        try:
            return os.path.relpath(file_path, repo_root)
        except ValueError:
            # Windows 上跨盘符 relpath 会抛 ValueError
            return file_path
    return file_path


def _infer_file_status(entry: dict[str, Any]) -> str:
    """从 DiffService 文件条目推断 ``DiffFileEntry.status`` 字段。

    DiffService 原始返回中没有显式 status 字段,按可用信号映射:
      - ``isUntracked=True`` → ``"added"``(未跟踪文件)
      - ``isNewFile=True`` → ``"added"``(新增已跟踪文件)
      - ``isDeletedFile=True`` → ``"deleted"``
      - 其他 → ``"modified"``

    已知局限:历史 rename 需要 file_ops 额外记录 rename 语义,当前只能准确
    覆盖 added/deleted/modified。
    """
    status = entry.get("status") or entry.get("changeType")
    if isinstance(status, str) and status.strip():
        return status.strip()
    if entry.get("isUntracked") or entry.get("isNewFile"):
        return "added"
    if entry.get("isDeletedFile"):
        return "deleted"
    return "modified"


def _convert_stats(raw_stats: dict[str, Any] | None) -> DiffStats:
    """转换 camelCase stats → snake_case DiffStats。"""
    if not raw_stats or not isinstance(raw_stats, dict):
        return DiffStats()
    return DiffStats(
        files_changed=int(raw_stats.get("filesChanged", 0) or 0),
        lines_added=int(raw_stats.get("linesAdded", 0) or 0),
        lines_removed=int(raw_stats.get("linesRemoved", 0) or 0),
    )


def get_session_extra_history_roots(
    session_id: str | None,
    *,
    sessions_root: str | Path | None = None,
    agent_workspace_root: str | Path | None = None,
) -> list[str]:
    """Return team/member/worktree/sub-agent roots for file history monitoring."""
    sid = str(session_id or "").strip()
    if not sid:
        return []
    safe_sid = _safe_team_path_segment(sid)
    try:
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        # Team roots may be written by a different process; force disk read so
        # diff/restore sees the latest persisted metadata.
        metadata_kwargs: dict[str, Any] = {
            "cache_bust": True,
            "enable_writeback": False,
        }
        if sessions_root is not None:
            metadata_kwargs["sessions_root"] = sessions_root
        metadata = get_session_metadata(sid, **metadata_kwargs)
    except Exception:  # noqa: BLE001
        return []
    raw_roots = metadata.get("team_file_monitor_roots")
    roots: list[str] = []
    seen: set[str] = set()

    def add_root(raw: Any) -> None:
        if not isinstance(raw, str) or not raw.strip():
            return
        root = raw.strip()
        try:
            key = str(Path(root).expanduser().resolve())
        except Exception:
            key = root
        if key in seen:
            return
        seen.add(key)
        roots.append(root)

    raw_root_values = raw_roots if isinstance(raw_roots, list) else []
    if isinstance(raw_roots, list):
        for raw in raw_roots:
            add_root(raw)
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                raw_path = Path(raw.strip()).expanduser()
                if raw_path.name == "team-workspace":
                    add_root(str(raw_path.parent / "workspaces"))
            except Exception:  # noqa: BLE001
                continue

    team_name = str(
        metadata.get("runtime_team_name") or metadata.get("team_name") or ""
    ).strip()
    if team_name:
        spawned_member_names = _session_team_member_names(sid)
        for raw in raw_root_values:
            if not isinstance(raw, str) or team_name not in raw:
                continue
            try:
                path = Path(raw.strip()).expanduser()
                parts = path.parts
                idx = -1
                for i in range(len(parts) - 1, -1, -1):
                    if parts[i] == team_name:
                        idx = i
                        break
                if idx >= 0:
                    home = Path(*parts[: idx + 1])
                    add_root(str(home / "team-workspace"))
                    add_root(str(home / "workspaces"))
                    add_root(str(home / "sessions" / safe_sid / "worktrees"))
                    for member_name in spawned_member_names:
                        safe_member = _safe_team_path_segment(member_name)
                        add_root(str(home / "workspaces" / f"{safe_member}_workspace"))
            except Exception:  # noqa: BLE001
                continue
        try:
            from openjiuwen.agent_teams.paths import (
                independent_member_workspace,
                team_home,
                team_session_worktrees_dir,
            )

            home = team_home(team_name)
            add_root(str(home / "team-workspace"))
            add_root(str(home / "workspaces"))
            add_root(str(team_session_worktrees_dir(team_name, sid)))
            for member_name in spawned_member_names:
                safe_member = _safe_team_path_segment(member_name)
                add_root(str(home / "workspaces" / f"{safe_member}_workspace"))
                add_root(str(independent_member_workspace(member_name)))
        except Exception:  # noqa: BLE001
            pass

    _discover_sub_agent_workspaces(
        sid,
        add_root,
        agent_workspace_root=agent_workspace_root,
    )

    return roots


def _discover_sub_agent_workspaces(
    session_id: str,
    add_root: Callable[[Any], None],
    *,
    agent_workspace_root: str | Path | None = None,
) -> None:
    """Scan workspace/sub_agents for sub-agent dirs belonging to *session_id*.

    In single-agent mode the parent session has no ``team_name`` and no
    ``team_file_monitor_roots`` metadata, so the normal team-root inference
    in ``get_session_extra_history_roots`` produces nothing.  Sub-agent
    workspaces live under ``<agent_workspace>/sub_agents/<sub_session_id>/``
    with ``sub_session_id = <session_id>_sub_<type>_<suffix>``.  We enumerate
    matching directories here and add them as extra history roots so that
    ``_read_agent_history`` can pick up the sub-agent's ``.agent_history``
    entries.
    """
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    workspace_root = (
        Path(agent_workspace_root)
        if agent_workspace_root is not None
        else get_agent_workspace_dir()
    )
    sub_agents_dir = workspace_root / "sub_agents"
    if not sub_agents_dir.is_dir():
        return
    prefix = f"{session_id}_sub_"
    try:
        children = list(sub_agents_dir.iterdir())
    except OSError:
        return
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        if child.name.startswith(prefix):
            add_root(str(child))


def _convert_hunks(raw_hunks: list[dict[str, Any]] | None) -> list[DiffHunk]:
    """转换 camelCase hunk 列表 → snake_case DiffHunk 列表。"""
    if not raw_hunks or not isinstance(raw_hunks, list):
        return []
    result: list[DiffHunk] = []
    for raw in raw_hunks:
        if not isinstance(raw, dict):
            continue
        result.append(DiffHunk(
            old_start=int(raw.get("oldStart", 0) or 0),
            old_lines=int(raw.get("oldLines", 0) or 0),
            new_start=int(raw.get("newStart", 0) or 0),
            new_lines=int(raw.get("newLines", 0) or 0),
            lines=list(raw.get("lines", []) or []),
        ))
    return result


def _convert_file_entry(
    file_path: str,
    entry: dict[str, Any],
    *,
    repo_root: str | None,
    include_hunks: bool,
) -> DiffFileEntry:
    """转换单个 DiffService 文件条目 → DiffFileEntry。

    ``file_path`` 为 DiffService 返回的 key(绝对路径),转换为相对 ``repo_root``
    的路径用于 Web 展示。
    """
    rel_path = _to_relative_path(file_path, repo_root)
    return DiffFileEntry(
        file_path=rel_path,
        status=_infer_file_status(entry),
        lines_added=int(entry.get("linesAdded", 0) or 0),
        lines_removed=int(entry.get("linesRemoved", 0) or 0),
        is_binary=bool(entry.get("isBinary", False)),
        is_new_file=bool(entry.get("isNewFile", False)),
        is_deleted_file=bool(entry.get("isDeletedFile", False)),
        is_untracked=bool(entry.get("isUntracked", False)),
        is_large_file=bool(entry.get("isLargeFile", False)),
        is_truncated=bool(entry.get("isTruncated", False)),
        hunks=_convert_hunks(entry.get("hunks")) if include_hunks else [],
    )


def _convert_file_map(
    raw_files: dict[str, Any] | None,
    *,
    repo_root: str | None,
    include_files: bool,
    include_hunks: bool,
) -> dict[str, DiffFileEntry]:
    """转换 DiffService files 映射 → DiffFileEntry 映射。"""
    if not include_files or not raw_files or not isinstance(raw_files, dict):
        return {}
    result: dict[str, DiffFileEntry] = {}
    for file_path, entry in raw_files.items():
        if not isinstance(entry, dict):
            continue
        converted = _convert_file_entry(
            file_path, entry, repo_root=repo_root, include_hunks=include_hunks,
        )
        result[converted.file_path] = converted
    return result


def _convert_current_diff(
    raw_diff: dict[str, Any] | None,
    *,
    repo_root: str | None,
    include_files: bool,
    include_hunks: bool,
    repo_is_dirty: bool = False,
) -> DiffSummary:
    """转换 ``DiffService.get_git_diff()`` 返回 → DiffSummary。

    ``is_dirty`` 语义与 ``GitRepoStatus.is_dirty`` 对齐:既包含已跟踪文件的
    改动(``stats.files_changed > 0``),也包含 untracked 文件。新版
    ``DiffService.get_git_diff`` 会将 untracked 文件计入 stats；这里仍保留
    ``files`` 中 ``is_untracked=True`` 的检查，兼容旧快照或边界场景。

    ``include_files=False`` 时 ``files`` 为空,无法通过 ``has_untracked``
    检测 untracked 文件,此时使用 ``repo_is_dirty``(来自 ``GitRepoStatus.is_dirty``)
    兜底。``repo_is_dirty`` 由 ``git status --porcelain`` 直接判定,涵盖 untracked
    文件和已跟踪文件改动,是最权威的 dirty 判定来源。
    """
    if not raw_diff or not isinstance(raw_diff, dict):
        # raw_diff 为空但 repo_is_dirty=True:工作区有 untracked 但 diff 服务未返回
        # (边界场景),以 repo_is_dirty 为准
        return DiffSummary(is_dirty=repo_is_dirty, stats=DiffStats(), files={})
    stats = _convert_stats(raw_diff.get("stats"))
    files = _convert_file_map(
        raw_diff.get("files"),
        repo_root=repo_root,
        include_files=include_files,
        include_hunks=include_hunks,
    )
    # 新版 stats.files_changed 已包含 untracked；has_untracked 保留为旧快照/边界兜底。
    # include_files=False 时 files 为空,使用 repo_is_dirty 兜底:
    # summary 首次订阅正是 include_files=False,repo_is_dirty 与 git status 口径对齐。
    has_untracked = any(f.is_untracked for f in files.values()) if include_files else False
    return DiffSummary(
        is_dirty=stats.files_changed > 0 or has_untracked or repo_is_dirty,
        stats=stats,
        files=files,
    )


def _convert_turn_diff(
    turn: dict[str, Any] | None,
    *,
    repo_root: str | None,
    include_files: bool,
    include_hunks: bool,
) -> DiffTurnSummary | None:
    """转换单个 turn diff dict → DiffTurnSummary。"""
    if not turn or not isinstance(turn, dict):
        return None
    stats = _convert_stats(turn.get("stats"))
    files = _convert_file_map(
        turn.get("files"),
        repo_root=repo_root,
        include_files=include_files,
        include_hunks=include_hunks,
    )
    return DiffTurnSummary(
        turn_index=int(turn.get("turnIndex", 0) or 0),
        timestamp=str(turn.get("timestamp", "") or ""),
        user_prompt_preview=str(turn.get("userPromptPreview", "") or ""),
        stats=stats,
        files=files,
        change_set_id=str(turn.get("change_set_id", "") or ""),
        request_id=str(turn.get("request_id", "") or ""),
        assistant_message_id=str(turn.get("assistant_message_id", "") or ""),
        user_message_id=str(turn.get("user_message_id", "") or ""),
        status=str(turn.get("status", "completed") or "completed"),
    )


def _historical_repo_context(
    turn: dict[str, Any], fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回持久化的历史 Git 上下文，缺失时使用当前上下文兜底。"""
    fallback = fallback or {}
    return {
        "repo_root": turn.get("repo_root") or fallback.get("repo_root"),
        "branch": turn.get("branch") or fallback.get("branch"),
        "base_head": turn.get("base_head") or fallback.get("base_head"),
    }


def _convert_turn_summary(
    turn: dict[str, Any], *, repo_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """转换单个 turn diff dict 为摘要(不含 hunks)。

    用于 ``project.git.turn_diff_list`` 摘要接口,响应包含文件列表但不含
    hunk,用于刷新后恢复历史编辑卡片。
    """
    stats = _convert_stats(turn.get("stats"))
    historical_repo = _historical_repo_context(turn, repo_context)
    files = _convert_file_map(
        turn.get("files"),
        repo_root=historical_repo.get("repo_root"),
        include_files=True,
        include_hunks=False,
    )
    result: dict[str, Any] = {
        "kind": "conversation_turn",
        "turn_index": int(turn.get("turnIndex", 0) or 0),
        "timestamp": str(turn.get("timestamp", "") or ""),
        "user_prompt_preview": str(turn.get("userPromptPreview", "") or ""),
        "stats": stats.to_dict(),
        **historical_repo,
        "files": {
            k: v.to_dict(include_hunks=False)
            for k, v in files.items()
        },
    }
    change_set_id = turn.get("change_set_id")
    if change_set_id:
        result["change_set_id"] = str(change_set_id)
        result["request_id"] = str(turn.get("request_id", "") or "")
        result["assistant_message_id"] = str(turn.get("assistant_message_id", "") or "")
        result["user_message_id"] = str(turn.get("user_message_id", "") or "")
        result["status"] = str(turn.get("status", "completed") or "completed")
    return result


def _supports_no_git_fallback(error: Any) -> bool:
    """Return True when Jiuwen file-op history can back the diff view."""
    return str(getattr(error, "code", "") or "") in {
        "NOT_GIT_REPOSITORY",
        "GIT_NOT_FOUND",
    }


def _repo_context_from_status(project: Any, *, reject_transient: bool = False) -> dict[str, Any]:
    """读取 Git 上下文，必要时抛出结构化 Git 错误。"""
    from jiuwenswarm.server.runtime.session.project_git import (
        get_project_git_service,
    )
    git_service = get_project_git_service()
    repo_status = git_service.status(project)
    if repo_status.error is not None:
        if _supports_no_git_fallback(repo_status.error):
            project_dir = str(getattr(project, "project_dir", "") or "") or None
            return {
                "repo_root": project_dir,
                "branch": None,
                "base_head": None,
            }
        raise GitOperationError(repo_status.error)
    if reject_transient and repo_status.transient:
        project_id = str(getattr(project, "project_id", "") or "")
        project_dir = str(getattr(project, "project_dir", "") or "")
        raise GitOperationError(GitError(
            "GIT_TRANSIENT_STATE",
            "git is in transient state (merge/rebase)",
            hint="请先解决中间状态(merge/rebase/cherry-pick)后重试",
            retryable=False,
            repo={
                "project_id": project_id,
                "project_dir": project_dir,
                "repo_root": repo_status.repo_root,
                "branch": repo_status.branch,
                "head": repo_status.head,
                "transient": repo_status.transient,
            },
        ))
    return {
        "repo_root": repo_status.repo_root,
        "branch": repo_status.branch,
        "base_head": repo_status.head,
    }


def _repo_context_for_history(project: Any) -> dict[str, Any]:
    """历史轮次回放专用:读取 Git 上下文,失败时降级为 project_dir 兜底。

    与 ``_repo_context_from_status`` 的区别:
      - 不抛 ``GIT_TRANSIENT_STATE``:历史轮次回放基于 file_ops + change_set
        snapshot,不执行 git 命令,transient 状态不应阻断历史预览。
      - 不抛其他 Git 错误:timeout/command_failed 等与历史 snapshot 无关。
      - 失败时返回 ``{"repo_root": project_dir, "branch": None, "base_head": None}``,
        让历史 turn 的 ``_historical_repo_context`` 仍可用(优先级高于 fallback)。

    设计原则:历史轮次的 repo 上下文已持久化在 change_set entry,当前 git 状态
    只是 fallback。fallback 失败时用 project_dir 兜底,而非阻断整个请求。
    """
    try:
        return _repo_context_from_status(project, reject_transient=False)
    except GitOperationError:
        # transient / timeout / command_failed 等:用 project_dir 兜底
        project_dir = str(getattr(project, "project_dir", "") or "") or None
        return {
            "repo_root": project_dir,
            "branch": None,
            "base_head": None,
        }


class DiffStatusService:
    """面向 Web 的 diff 状态聚合服务(设计文档 §2.4 / §4.1.16)。

    复用现有 ``DiffService`` 能力,负责 schema 转换(camelCase → snake_case)、
    空状态与 transient 语义转换、与 ``ProjectGitService`` 的 repo 状态合并。
    不修改 ``DiffService`` 原始返回,避免破坏 TUI ``command.diff`` 等既有消费方。
    """

    @staticmethod
    def get_project_diff_status(
        *,
        project: Any,
        session_id: str | None = None,
        include_files: bool = False,
        include_hunks: bool = False,
        hunk_paths: list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> ProjectGitDiffStatus:
        """聚合当前工作区 diff 和上一轮对话 diff(设计文档 §4.1.16)。

        Args:
            project: 已校验的 Project 对象(由 handler 完成存在性/work_mode 校验)
            session_id: 会话 ID,用于查询上一轮对话 diff;为空时不返回 last_turn
            include_files: 是否返回文件列表;为 false 时 files 为 ``{}``
            include_hunks: 是否返回 hunk;为 true 时隐含 ``include_files=true``

        Returns:
            ProjectGitDiffStatus: 聚合后的 diff 状态对象,调用方调 ``to_dict()``
            作为接口 payload

        说明:
          - transient 状态下 ``current`` 为 ``None``,仍成功返回 ``repo.transient=true``
          - 无 turn diff 时 ``last_turn`` 为 ``None``
          - ``include_hunks=True`` 时隐含 ``include_files=True``
        """
        project_id = getattr(project, "project_id", "")
        project_dir = getattr(project, "project_dir", "")
        work_mode = getattr(project, "work_mode", "work") or "work"

        effective_include_files = include_files or include_hunks

        from jiuwenswarm.server.runtime.session.project_git import (
            get_project_git_service,
        )
        git_service = get_project_git_service()
        repo_status = git_service.status(project)

        no_git_fallback = bool(
            repo_status.error is not None
            and _supports_no_git_fallback(repo_status.error)
        )
        if repo_status.error is not None and not no_git_fallback:
            raise GitOperationError(repo_status.error)

        repo_root = (
            getattr(repo_status, "repo_root", None)
            or (str(project_dir) or None if no_git_fallback else None)
        )
        repo_info = DiffRepoInfo(
            is_git=bool(getattr(repo_status, "is_git", False)) and not no_git_fallback,
            repo_root=repo_root,
            branch=None if no_git_fallback else getattr(repo_status, "branch", None),
            head=None if no_git_fallback else getattr(repo_status, "head", None),
            transient=(
                False
                if no_git_fallback
                else bool(getattr(repo_status, "transient", False))
            ),
        )

        current: DiffSummary | None = None
        repo_is_git = bool(getattr(repo_status, "is_git", False))
        repo_is_transient = bool(getattr(repo_status, "transient", False))
        if repo_is_git and not repo_is_transient:
            diff_service = get_diff_service()
            try:
                raw_diff = diff_service.get_git_diff(
                    project_dir,
                    include_files=effective_include_files,
                    include_hunks=include_hunks,
                    hunk_paths=hunk_paths,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[DiffStatus] get_git_diff failed (project=%s dir=%s): %s",
                    project_id, project_dir, exc,
                )
                raise
            current = _convert_current_diff(
                raw_diff,
                repo_root=repo_root,
                include_files=effective_include_files,
                include_hunks=include_hunks,
                repo_is_dirty=bool(getattr(repo_status, "is_dirty", False)),
            )

        last_turn: DiffTurnSummary | None = None
        if session_id:
            diff_service = get_diff_service()
            extra_history_roots = get_session_extra_history_roots(session_id)
            try:
                turns = diff_service.get_turn_diff_summaries(
                    session_id,
                    project_dir,
                    repo_context={
                        "repo_root": repo_root,
                        "branch": repo_info.branch,
                        "base_head": repo_info.head,
                    },
                    extra_history_roots=extra_history_roots,
                )
            except Exception as exc:  # noqa: BLE001
                # 与 get_git_diff 保持对称:错误向上抛,让 handler 感知并触发
                # 订阅状态回滚。否则 source=last_turn 时会静默
                # 返回空数据,客户端误以为订阅成功但拿不到内容。
                logger.warning(
                    "[DiffStatus] get_turn_diff_summaries failed (session=%s): %s",
                    session_id, exc,
                )
                raise
            if turns:
                last_turn = _convert_turn_diff(
                    turns[0],
                    repo_root=repo_root,
                    include_files=effective_include_files,
                    include_hunks=include_hunks,
                )

        return ProjectGitDiffStatus(
            project_id=project_id,
            session_id=session_id,
            work_mode=work_mode,
            repo=repo_info,
            current=current,
            last_turn=last_turn,
            generated_at=time.time(),
        )

    @staticmethod
    def get_turn_diff_list(
        *,
        project: Any,
        session_id: str,
        limit: int = 50,
        cursor: int = 0,
    ) -> dict[str, Any]:
        """返回历史轮次摘要列表。"""
        project_id = getattr(project, "project_id", "")
        project_dir = getattr(project, "project_dir", "")
        # 历史轮次回放不依赖当前 git 状态:用 _repo_context_for_history 兜底,
        # 避免 transient/timeout 等错误阻断 file_ops 历史预览。
        repo_context = _repo_context_for_history(project)
        diff_service = get_diff_service()
        extra_history_roots = get_session_extra_history_roots(session_id)
        turns = diff_service.get_turn_diff_summaries(
            session_id,
            project_dir,
            repo_context=repo_context,
            extra_history_roots=extra_history_roots,
        )
        total = len(turns)
        cursor = max(0, int(cursor or 0))
        if cursor > total:
            cursor = total
        page_turns = turns[cursor:]
        if limit > 0:
            page_turns = page_turns[:limit]
        next_cursor = cursor + len(page_turns)
        summaries = [
            _convert_turn_summary(t, repo_context=repo_context)
            for t in page_turns
        ]
        return {
            "project_id": project_id,
            "session_id": session_id,
            **repo_context,
            "turns": summaries,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "has_more": next_cursor < total,
            "limit": limit,
            "total": total,
        }

    @staticmethod
    def get_turn_diff_detail(
        *,
        project: Any,
        session_id: str,
        turn_index: int | None = None,
        change_set_id: str | None = None,
        include_files: bool = True,
        include_hunks: bool = True,
    ) -> dict[str, Any] | None:
        """返回指定轮次详情，优先按 ``change_set_id`` 查询。"""
        project_id = getattr(project, "project_id", "")
        project_dir = getattr(project, "project_dir", "")
        # 历史轮次回放不依赖当前 git 状态:用 _repo_context_for_history 兜底,
        # 避免 transient/timeout 等错误阻断 file_ops 历史预览。
        repo_context = _repo_context_for_history(project)
        repo_root = repo_context.get("repo_root")

        diff_service = get_diff_service()
        extra_history_roots = get_session_extra_history_roots(session_id)
        turn = diff_service.get_turn_diff(
            session_id,
            turn_index=turn_index,
            change_set_id=change_set_id,
            project_dir=project_dir,
            repo_context=repo_context,
            extra_history_roots=extra_history_roots,
        )
        if turn is None:
            return None
        turn_summary = _convert_turn_diff(
            turn,
            repo_root=turn.get("repo_root") or repo_root,
            include_files=include_files or include_hunks,
            include_hunks=include_hunks,
        )
        result = turn_summary.to_dict(include_hunks=include_hunks)
        result["project_id"] = project_id
        result["session_id"] = session_id
        result.update(repo_context)
        result.update(_historical_repo_context(turn, repo_context))
        return result


_service_instance: DiffStatusService | None = None


def get_diff_status_service() -> DiffStatusService:
    """返回 ``DiffStatusService`` 单例。"""
    global _service_instance
    if _service_instance is None:
        _service_instance = DiffStatusService()
    return _service_instance


def reset_diff_status_service() -> None:
    """重置单例(仅供测试)。"""
    global _service_instance
    _service_instance = None


_FILES_EVENT_FIELDS: tuple[str, ...] = (
    "file_path", "status", "change_type", "lines_added", "lines_removed",
    "is_binary", "is_new_file", "is_deleted_file", "is_untracked",
    "is_large_file", "is_truncated",
)


def file_entry_to_dict_no_hunks(entry: dict[str, Any]) -> dict[str, Any]:
    """将已序列化的文件条目 dict 转换为不含 hunk 的事件格式。

    用于 ``diff_files_changed`` 事件(设计文档 §3.6):
    files 事件只需文件路径/状态/行数统计,不需要 hunk 内容。

    与 ``DiffFileEntry.to_dict(include_hunks=False)`` 输出一致,
    但接受已序列化的 dict 输入(避免反复对象重建)。
    """
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
    """批量转换文件映射:去除 hunk,过滤非 dict 条目。

    用于 ``diff_files_changed`` 事件 payload 的 files 字段构造。
    """
    if not files_dict:
        return {}
    result: dict[str, Any] = {}
    for path, entry in files_dict.items():
        if not isinstance(entry, dict):
            continue
        result[path] = file_entry_to_dict_no_hunks(entry)
    return result


# ── 事件 payload 构造 helper(供 handler 与 registry 共用,避免重复实现) ──

def extract_files_from_status(
    status_dict: dict[str, Any],
    source: str,
) -> dict[str, Any] | None:
    """从 ``ProjectGitDiffStatus.to_dict()`` 中提取指定 source 的 files 映射。

    Args:
        status_dict: 已序列化的 diff status dict
        source: ``"current"`` 或 ``"last_turn"``

    Returns:
        files 映射;对应分支不存在时返回 ``None``
    """
    if source == "current":
        current = status_dict.get("current")
        return (current or {}).get("files") if current else None
    if source == "last_turn":
        last_turn = status_dict.get("last_turn")
        return (last_turn or {}).get("files") if last_turn else None
    return None


def build_summary_entry(current: dict[str, Any] | None) -> dict[str, Any] | None:
    """构造 summary 事件/快照中的 current 条目(``files`` 固定 ``{}``)。

    summary 层只关心统计信息,文件列表由 files 层负责。

    Returns:
        构造的 summary 条目;``current`` 为空时返回 ``None``。
    """
    if not current:
        return None
    return {
        "kind": current.get("kind", "working_tree"),
        "is_dirty": current.get("is_dirty", False),
        "stats": current.get("stats", {}),
        "files": {},
    }


def build_turn_summary_entry(last_turn: dict[str, Any] | None) -> dict[str, Any] | None:
    """构造 summary 事件/快照中的 last_turn 条目(``files`` 固定 ``{}``)。

    与 ``build_summary_entry`` 对称,仅用于 last_turn 分支。
    """
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
