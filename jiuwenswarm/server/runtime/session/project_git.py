"""ProjectGitService: 项目目录的 Git 仓库探测与分支操作服务(设计文档 §3.4 / §6)。

提供 ``ensure_on_project_create`` / ``probe`` / ``status`` / ``init`` /
``switch_branch`` / ``create_branch`` 等接口。

安全边界:禁止 ``shell=True``;分支名用 ``git check-ref-format --branch`` 校验;
路径必须来自已登记 project 的 ``project_dir``;写操作默认 10 秒超时
(``GIT_COMMAND_TIMEOUT_SEC`` / ``GIT_DIFF_TIMEOUT_SEC``),超时返回
``GIT_COMMAND_TIMEOUT``。merge/rebase/cherry-pick 中间状态下 ``status``/``probe``
返回 ``transient=true``,仅写操作返回 ``GIT_TRANSIENT_STATE``。
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jiuwenswarm.common.git_safe_directory import (
    is_dubious_ownership_error,
    safe_directory_hint,
)
from jiuwenswarm.server.runtime.session.project_store import Project, save_project

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float, *, min_value: float = 0.1) -> float:
    """Read a float environment variable with a safe fallback.

    非法值回退到 default;有效但低于 ``min_value`` 的值也会被钳到下限,
    避免 0/负值导致 ``subprocess.run(timeout=0)`` 立即触发 TimeoutExpired。
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "[ProjectGit] invalid %s=%r, falling back to %.1f",
            name, raw, default,
        )
        return default
    if value < min_value:
        logger.warning(
            "[ProjectGit] %s=%.3f below min %.1f, clamped",
            name, value, min_value,
        )
        return min_value
    return value


# 写操作默认 10 秒超时,避免阻塞 watcher 主循环;可通过环境变量覆盖
GIT_COMMAND_TIMEOUT_SEC: float = _env_float("JIUWEN_GIT_COMMAND_TIMEOUT_SEC", 10.0)
GIT_DIFF_TIMEOUT_SEC: float = _env_float("JIUWEN_GIT_DIFF_TIMEOUT_SEC", 10.0)
# push 涉及网络,默认 60 秒;可通过环境变量覆盖
GIT_PUSH_TIMEOUT_SEC: float = _env_float("JIUWEN_GIT_PUSH_TIMEOUT_SEC", 60.0)

# 输出截断上限(设计文档 §3.4 GitError: stdout/stderr ≤ 4000 字符)
_GIT_OUTPUT_TRUNCATE = 4000


@dataclass(slots=True)
class GitError:
    """Git 操作失败时的结构化错误对象。"""

    code: str
    message: str
    command: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    hint: str = ""
    retryable: bool = False
    repo: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout[:_GIT_OUTPUT_TRUNCATE],
            "stderr": self.stderr[:_GIT_OUTPUT_TRUNCATE],
            "hint": self.hint,
            "retryable": self.retryable,
            "repo": self.repo,
        }


@dataclass(slots=True)
class GitRepoStatus:
    """某一时刻项目目录的 Git 仓库完整状态。"""

    is_git: bool = False
    repo_root: str | None = None
    branch: str | None = None
    head: str | None = None
    detached: bool = False
    transient: bool = False
    upstream: str | None = None
    is_dirty: bool = False
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    conflicted: int = 0
    local_branches: list[str] = field(default_factory=list)
    remote_branches: list[str] = field(default_factory=list)
    error: GitError | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_git": self.is_git,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "head": self.head,
            "detached": self.detached,
            "transient": self.transient,
            "upstream": self.upstream,
            "is_dirty": self.is_dirty,
            "staged": self.staged,
            "unstaged": self.unstaged,
            "untracked": self.untracked,
            "conflicted": self.conflicted,
            "local_branches": list(self.local_branches),
            "remote_branches": list(self.remote_branches),
            "error": self.error.to_dict() if self.error else None,
        }


@dataclass(slots=True)
class GitProbeResult:
    """``ensure_on_project_create()`` 返回值。"""

    status: str  # ready | not_git | git_missing | transient | error | disabled
    repo_root: str | None = None
    branch: str | None = None
    initialized_by_jiuwenswarm: bool = False
    error: GitError | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "initialized_by_jiuwenswarm": self.initialized_by_jiuwenswarm,
            "error": self.error.to_dict() if self.error else None,
        }


@dataclass(slots=True)
class GitOperationResult:
    """``switch_branch()`` / ``create_branch()`` / ``commit()`` / ``push()`` 等写操作的返回值。"""

    success: bool
    repo_status: GitRepoStatus
    previous_branch: str | None = None
    error: GitError | None = None
    # commit 专用:新提交的短 hash
    commit_hash: str | None = None
    # push 专用:实际推送的远程名
    pushed_remote: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "repo_status": self.repo_status.to_dict(),
            "previous_branch": self.previous_branch,
            "error": self.error.to_dict() if self.error else None,
            "commit_hash": self.commit_hash,
            "pushed_remote": self.pushed_remote,
        }


class GitOperationError(RuntimeError):
    """Git 操作失败,携带结构化 ``GitError`` 供 handler 层映射错误码。"""

    def __init__(self, git_error: GitError) -> None:
        self.git_error = git_error
        super().__init__(git_error.message)


def resolve_git_project(
    project_id: str, *, cache_bust: bool = False,
) -> tuple[Any, str | None, str | None]:
    """校验并加载可用于 Git 操作的 code 项目(共享 helper)。

    被 ``app_web_handlers.py`` 的 Git RPC handler 和 ``git_ws_handler.py`` 的
    /ws/git handler 共用,确保校验逻辑一致。

    Args:
        project_id: 项目 ID;空/默认项目/不存在/隐藏/work 模式 → 拒绝。
        cache_bust: ``False`` 用于只读操作(避免绕过缓存);``True`` 用于写操作。

    Returns:
        ``(project, error_message, error_code)``: 成功时后两项为 ``None``;
        失败时 project 为 ``None``,调用方应直接 ``send_response`` 返回错误。
    """
    from jiuwenswarm.common.work_mode import is_default_project_id
    if not project_id:
        return None, "project_id is required", "BAD_REQUEST"
    if is_default_project_id(project_id):
        # 默认项目(default / default_code)禁止 Git 操作
        return None, "git operations not available for this project", "FORBIDDEN"
    from jiuwenswarm.server.runtime.session import project_store
    proj = project_store.get_project_by_id(project_id, cache_bust=cache_bust)
    if proj is None or proj.hidden:
        return None, "project not found", "NOT_FOUND"
    if proj.work_mode != "code":
        # work 模式项目不开放 Git 接口
        return None, "git operations not available for this project", "FORBIDDEN"
    return proj, None, None


def send_git_error_response(
    channel: Any, ws: Any, req_id: str, error: Any,
) -> Any:
    """发送 Git 结构化错误响应(共享 helper)。

    ``error`` 可以是:
      - ``GitOperationError`` 异常(通过 ``.git_error`` 属性提取 ``GitError``)
      - ``GitError`` 对象直接传入(如 ``repo_status.error``)
      - 其他异常(返回 ``INTERNAL_ERROR``)

    ``GitError`` 映射为 ``payload.detail``;非 Git 异常返回 ``INTERNAL_ERROR``。

    Returns:
        coroutine: 调用方需 ``await``。
    """
    if isinstance(error, GitError):
        git_error = error
    else:
        git_error = getattr(error, "git_error", None)
    if git_error is not None:
        detail = git_error.to_dict() if hasattr(git_error, "to_dict") else dict(git_error)
        return channel.send_response(
            ws, req_id, ok=False,
            payload={"detail": detail},
            error=git_error.message,
            code=git_error.code,
        )
    logger.warning("[GitHandler] error: %s", error)
    return channel.send_response(
        ws, req_id, ok=False,
        error=f"handler error: {error}", code="INTERNAL_ERROR",
    )


def _find_git_executable() -> str | None:
    """查找 git 可执行文件,找不到返回 ``None``。"""
    import shutil

    return shutil.which("git")


def _is_transient_state(project_dir: str) -> tuple[bool, str]:
    """检测 merge/rebase/cherry-pick 中间状态。

    Returns:
        ``(is_transient, kind)``: ``kind`` 为 "merge" / "rebase" / "cherry-pick" 等
    """
    dot_git = Path(project_dir) / ".git"
    if not dot_git.exists():
        return False, ""
    git_dir = dot_git if dot_git.is_dir() else None
    if git_dir is None:
        try:
            content = dot_git.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                git_dir = Path(project_dir) / content.split("gitdir:", 1)[1].strip()
                # 使用 absolute() 而非 resolve():不解析 symlink,避免通过 symlink
                # 访问的 worktree gitdir 与 git 内部管理的真实路径不一致,导致
                # transient 状态(merge/rebase 等)检测失败。
                git_dir = git_dir.absolute()
        except Exception:  # noqa: BLE001
            return False, ""
    if git_dir is None or not git_dir.exists():
        return False, ""
    for kind in ("merge", "rebase-merge", "rebase-apply", "cherry-pick", "revert"):
        if (git_dir / kind).exists():
            return True, kind
    return False, ""


def _run_git(
    args: list[str],
    *,
    cwd: str,
    timeout: float = GIT_COMMAND_TIMEOUT_SEC,
    stdin_input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """执行 git 命令,禁止 ``shell=True``。

    使用 ``_find_git_executable()`` 返回的完整路径调用 git,避免仅依赖
    ``PATH`` 中的 ``"git"`` 字符串。

    Args:
        stdin_input: 非空时作为命令 stdin 传入(用于 commit message 通过 ``-F -`` 注入,
            避免命令行长度限制与 shell 转义问题)。

    Raises:
        FileNotFoundError: git 可执行文件不存在
        subprocess.TimeoutExpired: 命令超时
    """
    git_exe = _find_git_executable()
    if git_exe is None:
        raise FileNotFoundError("git executable not found")
    cmd_str = "git " + " ".join(args)
    logger.debug("[ProjectGit] run: %s (cwd=%s)", cmd_str, cwd)
    return subprocess.run(
        [git_exe, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        # 显式指定 UTF-8 解码:git 输出通常为 UTF-8(含中文文件名/分支名/commit message),
        # 默认 locale.getpreferredencoding() 在 Windows 上是 cp1252/cp936,会触发
        # UnicodeDecodeError 导致整个 _git_to_repo_status 失败。errors="replace"
        # 保证极端情况下不抛解码异常(牺牲少量字符精度换可用性)。
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        check=False,  # 不抛 CalledProcessError,由调用方判断 returncode
        input=stdin_input,
    )


def _dubious_ownership_error_if_needed(
    project: Project,
    project_dir: str,
    result: subprocess.CompletedProcess[str],
) -> GitError | None:
    """Return a structured error when Git rejects repo ownership."""
    if not is_dubious_ownership_error(result):
        return None
    return _make_repo_error(
        "GIT_DUBIOUS_OWNERSHIP",
        "git repository ownership check failed",
        project,
        command="git rev-parse --show-toplevel",
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        hint=safe_directory_hint(project_dir),
        retryable=False,
    )


def _truncate(s: str) -> str:
    return (s or "")[:_GIT_OUTPUT_TRUNCATE]


def _is_branch_held_by_worktree(stderr: str) -> bool:
    """检测 git stderr 是否表示目标分支被 worktree 占用。

    stale(worktree 目录已删但 ``.git/worktrees`` 管理条目残留)与 live(团队仍在运行)
    两种情形 stderr 相同,需配合 ``git worktree prune`` + 重试来区分。
    """
    s = (stderr or "").lower()
    return "already used by worktree" in s or "already checked out" in s


def _find_worktrees_holding_branch(
    repo_root: str,
    branch: str,
) -> list[str]:
    """返回占用目标分支的 worktree 工作目录路径(不含主仓库)。

    通过 ``git worktree list --porcelain`` 解析 porcelain 输出,
    branch 字段为 ``refs/heads/<branch>`` 的 worktree 即为占用者。
    主仓库本身(worktree 列表第一项)被显式排除,避免误 detach 用户
    当前工作区。命令失败或解析异常时返回空列表,调用方回退到错误
    分类逻辑提示用户手动处理。
    """
    try:
        cp = _run_git(
            ["worktree", "list", "--porcelain"],
            cwd=repo_root,
        )
    except FileNotFoundError:
        return []
    if cp.returncode != 0:
        return []
    target_ref = f"refs/heads/{branch}"
    real_root = os.path.realpath(repo_root)
    worktrees: list[str] = []
    wt_path: str | None = None
    for line in cp.stdout.splitlines():
        if line.startswith("worktree "):
            wt_path = line[len("worktree "):].strip()
        elif line.startswith("branch ") and wt_path is not None:
            ref = line[len("branch "):].strip()
            if ref == target_ref:
                if os.path.realpath(wt_path) != real_root:
                    worktrees.append(wt_path)
            wt_path = None
    return worktrees


def _make_error(
    code: str,
    message: str,
    *,
    command: str = "",
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    hint: str = "",
    retryable: bool = False,
    project: Project | None = None,
) -> GitError:
    repo_ctx: dict[str, Any] | None = None
    if project is not None:
        repo_ctx = {
            "project_id": project.project_id,
            "repo_root": project.project_dir,
            "branch": None,
            "transient": False,
        }
    return GitError(
        code=code,
        message=message,
        command=command,
        exit_code=exit_code,
        stdout=_truncate(stdout),
        stderr=_truncate(stderr),
        hint=hint,
        retryable=retryable,
        repo=repo_ctx,
    )


def _make_repo_error(
    code: str,
    message: str,
    project: Project,
    *,
    command: str = "",
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    hint: str = "",
    retryable: bool = False,
    branch: str | None = None,
    transient: bool = False,
) -> GitError:
    """构造带完整 repo 上下文的 GitError。"""
    return GitError(
        code=code,
        message=message,
        command=command,
        exit_code=exit_code,
        stdout=_truncate(stdout),
        stderr=_truncate(stderr),
        hint=hint,
        retryable=retryable,
        repo={
            "project_id": project.project_id,
            "repo_root": project.project_dir,
            "branch": branch,
            "transient": transient,
        },
    )


def _file_not_found_error(
    project: Project,
    project_dir: str,
    *,
    branch: str | None = None,
    command: str = "",
) -> GitError:
    """区分 FileNotFoundError 来源:cwd 不存在 → PROJECT_DIR_MISSING,git 可执行文件缺失 → GIT_NOT_FOUND。

    ``subprocess.run(cwd=missing_dir)`` 也会抛 ``FileNotFoundError``,与 git 可执行文件
    缺失的异常同型。此处通过二次检查目录是否存在来消歧(TOCTOU 窗口收窄)。
    """
    if not project_dir or not Path(project_dir).exists():
        return _make_repo_error(
            "PROJECT_DIR_MISSING",
            "project directory does not exist",
            project,
            command=command,
            branch=branch,
            hint="请检查项目目录是否存在或路径是否正确",
            retryable=False,
        )
    return _make_repo_error(
        "GIT_NOT_FOUND",
        "git executable not found",
        project,
        command=command,
        branch=branch,
        hint="请安装 Git 后调用 project.git.probe 重新探测",
        retryable=True,
    )


def _git_to_repo_status(
    project: Project,
    *,
    persist: bool = False,
) -> GitRepoStatus:
    """读取项目目录的 Git 状态,返回 ``GitRepoStatus``。

    Args:
        project: 项目实体
        persist: 是否在探测后写回 ``Project.git`` 快照(含错误状态;仅 ``probe``/``init``/写操作使用)
    """

    def _err_status(err: GitError) -> GitRepoStatus:
        """构造错误状态;persist=True 时同时写回 ``Project.git`` 快照(设计文档要求 probe() 持久化错误)。"""
        status = GitRepoStatus(is_git=False, error=err)
        if persist:
            _persist_git_snapshot(project, status)
        return status

    project_dir = project.project_dir
    if not project_dir or not Path(project_dir).exists():
        return _err_status(
            _make_repo_error(
                "PROJECT_DIR_MISSING",
                "project directory does not exist",
                project,
                hint="请检查项目目录是否存在或路径是否正确",
                retryable=False,
            )
        )
    git_exe = _find_git_executable()
    if git_exe is None:
        return _err_status(
            _make_repo_error(
                "GIT_NOT_FOUND",
                "git executable not found",
                project,
                hint="请安装 Git 后调用 project.git.probe 重新探测",
                retryable=True,
            )
        )
    try:
        cp = _run_git(["rev-parse", "--show-toplevel"], cwd=project_dir)
    except FileNotFoundError:
        return _err_status(_file_not_found_error(project, project_dir, command="git rev-parse --show-toplevel"))
    except subprocess.TimeoutExpired:
        return _err_status(
            _make_repo_error(
                "GIT_COMMAND_TIMEOUT",
                "git command timed out",
                project,
                command="git rev-parse --show-toplevel",
                hint="Git 响应过慢,请稍后重试或检查仓库大小",
                retryable=True,
            )
        )
    if cp.returncode != 0:
        dubious_error = _dubious_ownership_error_if_needed(project, project_dir, cp)
        if dubious_error is not None:
            return _err_status(dubious_error)
        return _err_status(
            _make_repo_error(
                "NOT_GIT_REPOSITORY",
                "not a git repository",
                project,
                command="git rev-parse --show-toplevel",
                exit_code=cp.returncode,
                stderr=cp.stderr,
                hint="调用 project.git.init 初始化仓库",
                retryable=False,
            )
        )
    repo_root = cp.stdout.strip()
    is_transient, _transient_kind = _is_transient_state(project_dir)
    branch: str | None = None
    head: str | None = None
    detached = False
    upstream: str | None = None
    try:
        cp_b = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=project_dir)
        if cp_b.returncode == 0:
            branch = cp_b.stdout.strip()
        else:
            detached = True
            cp_d = _run_git(["rev-parse", "--short", "HEAD"], cwd=project_dir)
            if cp_d.returncode == 0:
                head = cp_d.stdout.strip()
                branch = head  # detached 时 branch 字段填 head 短哈希
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    if branch and not detached:
        try:
            cp_u = _run_git(
                ["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"],
                cwd=project_dir,
            )
            if cp_u.returncode == 0:
                upstream = cp_u.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    if not head:
        try:
            cp_h = _run_git(["rev-parse", "--short", "HEAD"], cwd=project_dir)
            if cp_h.returncode == 0:
                head = cp_h.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    staged = unstaged = untracked = conflicted = 0
    is_dirty = False
    try:
        cp_s = _run_git(
            ["status", "--porcelain", "--no-renames"],
            cwd=project_dir,
            timeout=GIT_DIFF_TIMEOUT_SEC,
        )
        if cp_s.returncode == 0:
            for line in cp_s.stdout.splitlines():
                if not line:
                    continue
                xy = line[:2]
                if xy in ("DD", "AU", "UD", "UA", "DU", "AA", "UU"):
                    conflicted += 1
                elif xy[0] == "?":
                    untracked += 1
                else:
                    if xy[0] in ("A", "M", "D", "R", "C", "T"):
                        staged += 1
                    if xy[1] in ("M", "D", "T"):
                        unstaged += 1
            is_dirty = staged > 0 or unstaged > 0 or untracked > 0 or conflicted > 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    local_branches: list[str] = []
    remote_branches: list[str] = []
    try:
        cp_lb = _run_git(
            ["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            cwd=project_dir,
        )
        if cp_lb.returncode == 0:
            local_branches = [b for b in cp_lb.stdout.splitlines() if b]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # 处理 unborn HEAD:刚 git init 的仓库 HEAD 指向 refs/heads/<branch> 但 ref 尚未创建,
    # 此时 symbolic-ref 能取到分支名但 for-each-ref 返回空,需补回未生成的分支
    if (
        branch
        and not detached
        and local_branches == []
    ):
        local_branches = [branch]
    try:
        cp_rb = _run_git(
            ["for-each-ref", "--format=%(refname:short)", "refs/remotes/"],
            cwd=project_dir,
        )
        if cp_rb.returncode == 0:
            remote_branches = [b for b in cp_rb.stdout.splitlines() if b and not b.endswith("/HEAD")]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    status = GitRepoStatus(
        is_git=True,
        repo_root=repo_root,
        branch=branch,
        head=head,
        detached=detached,
        transient=is_transient,
        upstream=upstream,
        is_dirty=is_dirty,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        conflicted=conflicted,
        local_branches=local_branches,
        remote_branches=remote_branches,
        error=None,
    )
    if persist:
        _persist_git_snapshot(project, status)
    return status


def _persist_git_snapshot(project: Project, status: GitRepoStatus) -> None:
    """将 ``GitRepoStatus`` 写回 ``Project.git`` 子对象并持久化。"""
    if status.error is not None:
        git_snapshot: dict[str, Any] = {
            "enabled": False,
            "repo_root": status.repo_root or "",
            "initialized_by_jiuwenswarm": bool(
                project.git.get("initialized_by_jiuwenswarm", False)
            ),
            "detected_at": project.git.get("detected_at") or time.time(),
            "branch": status.branch or "",
            "status": _map_status_string(status),
            "error": status.error.message,
            "error_code": status.error.code,
            "hint": status.error.hint,
            "is_dirty": status.is_dirty,
        }
    else:
        git_snapshot = {
            "enabled": True,
            "repo_root": status.repo_root or "",
            "initialized_by_jiuwenswarm": bool(
                project.git.get("initialized_by_jiuwenswarm", False)
            ),
            "detected_at": project.git.get("detected_at") or time.time(),
            "branch": status.branch or "",
            "status": "ready" if not status.transient else "transient",
            "error": "",
            "error_code": "",
            "hint": "",
            "is_dirty": status.is_dirty,
        }
    project.git = git_snapshot
    try:
        save_project(project)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ProjectGit] failed to persist git snapshot for project=%s: %s",
            project.project_id, exc,
        )


def _persist_probe_result(project: Project, result: GitProbeResult) -> None:
    """将 ``GitProbeResult`` 写回 ``Project.git`` 子对象并持久化。

    用于 ``ensure_on_project_create`` 的所有探测路径(空目录 init 路径
    除外,该路径已通过 ``init() → _git_to_repo_status(persist=True)`` 持久化)。
    """
    git_snapshot: dict[str, Any] = {
        "enabled": result.status in ("ready", "transient"),
        "repo_root": result.repo_root or "",
        "initialized_by_jiuwenswarm": result.initialized_by_jiuwenswarm,
        "detected_at": project.git.get("detected_at") or time.time(),
        "branch": result.branch or "",
        "status": result.status,
        "error": result.error.message if result.error else "",
        "error_code": result.error.code if result.error else "",
        "hint": result.error.hint if result.error else "",
        # 保留 init() 已持久化的 is_dirty;非 init 路径(未探测 dirty)默认 False
        "is_dirty": project.git.get("is_dirty", False),
    }
    project.git = git_snapshot
    try:
        save_project(project)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ProjectGit] failed to persist probe result for project=%s: %s",
            project.project_id, exc,
        )


def _map_status_string(status: GitRepoStatus) -> str:
    """从 GitRepoStatus 推断 Project.git.status 字符串。"""
    if status.error is None:
        return "ready" if not status.transient else "transient"
    code = status.error.code
    if code == "NOT_GIT_REPOSITORY":
        return "not_git"
    if code == "GIT_NOT_FOUND":
        return "git_missing"
    if code == "GIT_DUBIOUS_OWNERSHIP":
        return "dubious_ownership"
    if code == "GIT_COMMAND_TIMEOUT":
        return "error"
    return "error"


def _validate_branch_name(branch: str, project: Project) -> str:
    """分支名校验,非法时抛 ``GitOperationError(BRANCH_INVALID)``。

    Returns:
        规范化后的分支名(``git check-ref-format --branch`` 的 stdout 输出,
        会去除 ``refs/heads/`` 前缀)。stdout 为空时回退到原始输入。
    """
    if not branch or not branch.strip():
        raise GitOperationError(
            _make_repo_error(
                "BRANCH_INVALID",
                "invalid branch name",
                project,
                hint="分支名不能为空",
                retryable=False,
            )
        )
    # 先检查目录是否存在,避免 subprocess.run(cwd=不存在) 的 FileNotFoundError
    # 被误判为 GIT_NOT_FOUND(实际应返回 PROJECT_DIR_MISSING)
    project_dir = project.project_dir
    if not project_dir or not Path(project_dir).exists():
        raise GitOperationError(
            _make_repo_error(
                "PROJECT_DIR_MISSING",
                "project directory does not exist",
                project,
                hint="请检查项目目录是否存在或路径是否正确",
                retryable=False,
            )
        )
    try:
        cp = _run_git(["check-ref-format", "--branch", branch], cwd=project_dir)
    except FileNotFoundError as exc:
        raise GitOperationError(
            _file_not_found_error(project, project_dir, command="git check-ref-format --branch")
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitOperationError(
            _make_repo_error(
                "GIT_COMMAND_TIMEOUT",
                "git command timed out",
                project,
                command="git check-ref-format --branch",
                retryable=True,
            )
        ) from exc
    if cp.returncode != 0:
        raise GitOperationError(
            _make_repo_error(
                "BRANCH_INVALID",
                "invalid branch name",
                project,
                command="git check-ref-format --branch",
                exit_code=cp.returncode,
                stderr=cp.stderr,
                hint="请使用合法的 Git 分支名",
                retryable=False,
            )
        )
    # ``git check-ref-format --branch`` 的 stdout 是规范化后的分支名(去除 refs/heads/ 前缀),
    # 调用方应使用此值以保证与 git 内部解析一致
    normalized = cp.stdout.strip()
    return normalized or branch


class ProjectGitService:
    """项目 Git 服务(设计文档 §3.4 / §6)。

    所有方法均同步执行(无 await),可被 async handler 直接调用。
    写操作超时由 ``GIT_COMMAND_TIMEOUT_SEC`` / ``GIT_DIFF_TIMEOUT_SEC`` 控制,
    超时返回 ``GIT_COMMAND_TIMEOUT`` 不阻塞主循环。
    """

    def ensure_on_project_create(self, project: Project) -> GitProbeResult:
        """新建项目时探测/初始化 Git(设计文档 §6)。

        所有探测结果(除 ``disabled`` 外)均通过 ``_persist_probe_result``
        写回 ``Project.git`` 快照并持久化。调用方可直接重新读取 project
        获取最新 git 字段,无需额外转换。

        规则:
          - work_mode="work": 不执行 Git 探测,返回 ``status="disabled"``
          - work_mode="code":
            - git 可执行文件缺失 → ``status="git_missing"``
            - 目录不存在 → ``status="error"``(error.code=PROJECT_DIR_MISSING)
            - 已是 Git 仓库 → ``status="ready"``,不执行 ``git init``
            - 目录非空但不是 Git 仓库 → 不自动 init,返回 ``status="not_git"``
            - 目录为空(或仅含 .git) → 主动 ``git init``,返回 ``initialized_by_jiuwenswarm=True``
            - 中间状态 → ``status="transient"``

        Returns:
            GitProbeResult: 探测结果,供调用方决定 ``Project.git`` 初始值
        """
        result = self._probe_on_project_create(project)
        if result.status != "disabled":
            _persist_probe_result(project, result)
        return result

    def _probe_on_project_create(self, project: Project) -> GitProbeResult:
        """``ensure_on_project_create`` 的纯探测逻辑,不持久化。"""
        if project.work_mode != "code":
            return GitProbeResult(status="disabled")
        project_dir = project.project_dir
        if not project_dir or not Path(project_dir).exists():
            err = _make_repo_error(
                "PROJECT_DIR_MISSING",
                "project directory does not exist",
                project,
                hint="请检查项目目录",
                retryable=False,
            )
            return GitProbeResult(status="error", error=err)
        if _find_git_executable() is None:
            err = _make_repo_error(
                "GIT_NOT_FOUND",
                "git executable not found",
                project,
                hint="请安装 Git",
                retryable=True,
            )
            return GitProbeResult(status="git_missing", error=err)
        try:
            cp = _run_git(["rev-parse", "--show-toplevel"], cwd=project_dir)
        except FileNotFoundError:
            err = _file_not_found_error(project, project_dir, command="git rev-parse --show-toplevel")
            if err.code == "GIT_NOT_FOUND":
                return GitProbeResult(status="git_missing", error=err)
            return GitProbeResult(status="error", error=err)
        except subprocess.TimeoutExpired:
            err = _make_repo_error(
                "GIT_COMMAND_TIMEOUT",
                "git command timed out",
                project,
                command="git rev-parse --show-toplevel",
                retryable=True,
            )
            return GitProbeResult(status="error", error=err)
        if cp.returncode == 0:
            is_transient, _ = _is_transient_state(project_dir)
            if is_transient:
                return GitProbeResult(
                    status="transient",
                    repo_root=cp.stdout.strip(),
                )
            branch = self._read_branch(project_dir)
            return GitProbeResult(
                status="ready",
                repo_root=cp.stdout.strip(),
                branch=branch,
            )
        dubious_error = _dubious_ownership_error_if_needed(project, project_dir, cp)
        if dubious_error is not None:
            return GitProbeResult(status="error", error=dubious_error)
        try:
            entries = list(Path(project_dir).iterdir())
        except OSError:
            entries = []
        non_git_entries = [e for e in entries if e.name != ".git"]
        if not non_git_entries:
            init_result = self.init(project, initial_branch="main")
            if init_result.error is None:
                return GitProbeResult(
                    status="ready",
                    repo_root=init_result.repo_root,
                    branch=init_result.branch,
                    initialized_by_jiuwenswarm=True,
                )
            return GitProbeResult(
                status="error",
                repo_root=init_result.repo_root,
                error=init_result.error,
            )
        return GitProbeResult(status="not_git")

    @staticmethod
    def probe(project: Project) -> GitRepoStatus:
        """重新探测项目 Git 状态并刷新 ``Project.git`` 快照,不执行 ``git init``。"""
        status = _git_to_repo_status(project, persist=True)
        return status

    @staticmethod
    def status(project: Project) -> GitRepoStatus:
        """查询项目 Git 状态(不持久化)。"""
        return _git_to_repo_status(project, persist=False)

    @staticmethod
    def init(
        project: Project,
        initial_branch: str = "main",
    ) -> GitRepoStatus:
        """初始化 Git 仓库,写回 ``Project.git`` 快照。"""
        project_dir = project.project_dir
        if not project_dir or not Path(project_dir).exists():
            err = _make_repo_error(
                "PROJECT_DIR_MISSING",
                "project directory does not exist",
                project,
                retryable=False,
            )
            return GitRepoStatus(is_git=False, error=err)
        if _find_git_executable() is None:
            err = _make_repo_error(
                "GIT_NOT_FOUND",
                "git executable not found",
                project,
                hint="请安装 Git 后重试",
                retryable=True,
            )
            return GitRepoStatus(is_git=False, error=err)
        try:
            initial_branch = _validate_branch_name(initial_branch, project)
        except GitOperationError as exc:
            return GitRepoStatus(is_git=False, error=exc.git_error)
        try:
            cp = _run_git(
                ["init", "-b", initial_branch, project_dir],
                cwd=project_dir,
            )
        except FileNotFoundError:
            err = _file_not_found_error(project, project_dir, command="git init -b")
            return GitRepoStatus(is_git=False, error=err)
        except subprocess.TimeoutExpired:
            err = _make_repo_error(
                "GIT_COMMAND_TIMEOUT",
                "git command timed out",
                project,
                command="git init",
                retryable=True,
            )
            return GitRepoStatus(is_git=False, error=err)
        if cp.returncode != 0:
            dubious_error = _dubious_ownership_error_if_needed(project, project_dir, cp)
            if dubious_error is not None:
                return GitRepoStatus(is_git=False, error=dubious_error)
            if "unknown switch" in cp.stderr or "invalid option" in cp.stderr:
                # git < 2.28 不支持 ``init -b``,回退到 ``init`` + ``symbolic-ref``。
                # 用 ``symbolic-ref HEAD refs/heads/<branch>`` 替代 ``checkout -b``:
                # 后者在无 commit 的全新仓库上会因找不到 HEAD 指向的 commit 而失败。
                try:
                    cp2 = _run_git(["init"], cwd=project_dir)
                    cp = cp2
                    if cp2.returncode == 0 and initial_branch:
                        cp_sr = _run_git(
                            ["symbolic-ref", "HEAD", f"refs/heads/{initial_branch}"],
                            cwd=project_dir,
                        )
                        if cp_sr.returncode != 0:
                            err = _make_repo_error(
                                "GIT_COMMAND_FAILED",
                                "git command failed",
                                project,
                                command=f"git symbolic-ref HEAD refs/heads/{initial_branch}",
                                exit_code=cp_sr.returncode,
                                stdout=cp_sr.stdout,
                                stderr=cp_sr.stderr,
                                hint="Git 初始化成功但设置初始分支失败",
                                retryable=True,
                            )
                            return GitRepoStatus(is_git=False, error=err)
                except FileNotFoundError:
                    err = _file_not_found_error(project, project_dir, command="git init")
                    return GitRepoStatus(is_git=False, error=err)
                except subprocess.TimeoutExpired:
                    err = _make_repo_error(
                        "GIT_COMMAND_TIMEOUT",
                        "git command timed out",
                        project,
                        command="git init",
                        retryable=True,
                    )
                    return GitRepoStatus(is_git=False, error=err)
        if cp.returncode != 0:
            dubious_error = _dubious_ownership_error_if_needed(project, project_dir, cp)
            if dubious_error is not None:
                return GitRepoStatus(is_git=False, error=dubious_error)
            err = _make_repo_error(
                "GIT_COMMAND_FAILED",
                "git command failed",
                project,
                command="git init",
                exit_code=cp.returncode,
                stderr=cp.stderr,
                hint="请检查目录权限或 Git 版本",
                retryable=True,
            )
            return GitRepoStatus(is_git=False, error=err)
        project.git["initialized_by_jiuwenswarm"] = True
        return _git_to_repo_status(project, persist=True)

    @staticmethod
    def switch_branch(
        project: Project,
        branch: str,
        *,
        require_clean: bool = False,
    ) -> GitOperationResult:
        """切换分支。

        Args:
            project: 项目实体
            branch: 目标分支名
            require_clean: 默认为 False。为 True 时要求已跟踪文件没有未提交
                修改;未跟踪文件不阻止切换,否则返回 ``WORKTREE_DIRTY``
        """
        try:
            branch = _validate_branch_name(branch, project)
        except GitOperationError as exc:
            return GitOperationResult(
                success=False,
                repo_status=GitRepoStatus(error=exc.git_error),
                error=exc.git_error,
            )
        pre_status = _git_to_repo_status(project, persist=False)
        if pre_status.error is not None:
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=pre_status.error,
            )
        if pre_status.transient:
            err = _make_repo_error(
                "GIT_TRANSIENT_STATE",
                "git is in transient state (merge/rebase)",
                project,
                branch=pre_status.branch,
                transient=True,
                hint="请先解决中间状态(merge/rebase/cherry-pick)后重试",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        # ``is_dirty`` 也包含 untracked 文件，但未跟踪文件本身不会被 Git
        # checkout/switch 改写。分支选择器的“保护性切换”只应阻止已跟踪
        # 文件的暂存、未暂存或冲突修改，保持与 Git 的实际行为一致。
        has_tracked_changes = bool(
            pre_status.staged or pre_status.unstaged or pre_status.conflicted
        )
        if require_clean and has_tracked_changes:
            err = _make_repo_error(
                "WORKTREE_DIRTY",
                "tracked files have uncommitted changes",
                project,
                branch=pre_status.branch,
                hint="请先提交或 stash 改动",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        try:
            cp_show = _run_git(
                ["show-ref", "--verify", f"refs/heads/{branch}"],
                cwd=project.project_dir,
            )
        except FileNotFoundError:
            err = _file_not_found_error(
                project, project.project_dir,
                branch=pre_status.branch,
                command=f"git show-ref --verify refs/heads/{branch}",
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        except subprocess.TimeoutExpired:
            err = _make_repo_error(
                "GIT_COMMAND_TIMEOUT",
                "git command timed out",
                project,
                command=f"git show-ref --verify refs/heads/{branch}",
                branch=pre_status.branch,
                retryable=True,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        if cp_show.returncode != 0:
            err = _make_repo_error(
                "BRANCH_NOT_FOUND",
                "branch not found",
                project,
                command=f"git show-ref --verify refs/heads/{branch}",
                exit_code=cp_show.returncode,
                stderr=cp_show.stderr,
                branch=pre_status.branch,
                hint=f"分支 {branch} 不存在,请先创建",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        previous_branch = pre_status.branch
        try:
            cp_co = _run_git(
                ["checkout", branch],
                cwd=project.project_dir,
            )
        except FileNotFoundError:
            err = _file_not_found_error(
                project, project.project_dir,
                branch=previous_branch,
                command=f"git checkout {branch}",
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        except subprocess.TimeoutExpired:
            err = _make_repo_error(
                "GIT_COMMAND_TIMEOUT",
                "git command timed out",
                project,
                command=f"git checkout {branch}",
                branch=previous_branch,
                retryable=True,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        if cp_co.returncode != 0:
            # Stale worktree admin entries (e.g. a dissolved team whose
            # worktree dirs were rmtree'd without `git worktree remove`)
            # make `git checkout <branch>` fail with "already used by
            # worktree" even though the worktree no longer exists. Prune
            # stale admin and retry once; live worktrees remain user-managed.
            prune_co = None
            if _is_branch_held_by_worktree(cp_co.stderr):
                logger.info(
                    "[ProjectGit] checkout %s blocked by worktree; "
                    "pruning stale admin and retrying (cwd=%s)",
                    branch, project.project_dir,
                )
                try:
                    prune_co = _run_git(
                        ["-c", "safe.directory=*", "worktree", "prune"],
                        cwd=project.project_dir,
                    )
                    cp_co = _run_git(["checkout", branch], cwd=project.project_dir)
                except FileNotFoundError:
                    err = _file_not_found_error(
                        project, project.project_dir,
                        branch=previous_branch,
                        command=f"git checkout {branch}",
                    )
                    return GitOperationResult(
                        success=False,
                        repo_status=pre_status,
                        previous_branch=previous_branch,
                        error=err,
                    )
                except subprocess.TimeoutExpired:
                    err = _make_repo_error(
                        "GIT_COMMAND_TIMEOUT",
                        "git command timed out",
                        project,
                        command=f"git checkout {branch}",
                        branch=previous_branch,
                        retryable=True,
                    )
                    return GitOperationResult(
                        success=False,
                        repo_status=pre_status,
                        previous_branch=previous_branch,
                        error=err,
                    )
                if cp_co.returncode == 0:
                    post_status = _git_to_repo_status(project, persist=True)
                    return GitOperationResult(
                        success=True,
                        repo_status=post_status,
                        previous_branch=previous_branch,
                    )
            # LIVE worktree(团队未解散)占用目标分支时不再自动 detach 占用者。
            # 自动修改其他 worktree 的 HEAD 会破坏正在运行的团队/agent 上下文;
            # 保留 stale worktree prune,但 live 占用交由用户显式解散或手动处理。
            holding: list[str] = []
            if _is_branch_held_by_worktree(cp_co.stderr):
                holding = _find_worktrees_holding_branch(
                    project.project_dir, branch,
                )
                if holding:
                    logger.info(
                        "[ProjectGit] checkout %s blocked by live worktree(s) "
                        "%s; refusing automatic detach (cwd=%s)",
                        branch, holding, project.project_dir,
                    )
            held = _is_branch_held_by_worktree(cp_co.stderr)
            if held and prune_co is not None and prune_co.returncode != 0:
                msg = "清理 stale worktree 失败,无法切换分支"
                hint = ("git worktree prune 执行失败(rc={rc}): {stderr}。"
                        "请手动在仓库目录执行: git -c safe.directory=* worktree prune").format(
                    rc=prune_co.returncode, stderr=(prune_co.stderr or "")[:300])
            elif held and holding:
                msg = "分支被其他 worktree 占用"
                hint = ("占用 {branch} 的 worktree: {paths}。"
                        "请先解散对应团队,或手动在占用 worktree 执行 "
                        "git checkout --detach 后重试").format(
                    branch=branch,
                    paths=", ".join(holding[:3]),
                )
            elif held:
                msg = "分支被其他 worktree 占用"
                hint = "请先解散占用该分支的团队,或手动处理对应 worktree 后重试"
            elif "would be overwritten" in cp_co.stderr:
                msg = "切换分支失败:本地改动阻止切换"
                hint = "请先提交或 stash 改动后重试"
            else:
                msg = "git command failed"
                hint = "请先提交或 stash 改动后重试"
            err = _make_repo_error(
                "GIT_COMMAND_FAILED", msg, project,
                command=f"git checkout {branch}",
                exit_code=cp_co.returncode,
                stdout=cp_co.stdout,
                stderr=cp_co.stderr,
                branch=previous_branch,
                hint=hint,
                retryable=True,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                previous_branch=previous_branch,
                error=err,
            )
        post_status = _git_to_repo_status(project, persist=True)
        return GitOperationResult(
            success=True,
            repo_status=post_status,
            previous_branch=previous_branch,
        )

    @staticmethod
    def create_branch(
        project: Project,
        branch: str,
        *,
        checkout: bool = True,
        start_point: str | None = None,
    ) -> GitOperationResult:
        """新建分支,可选同时切换。

        Args:
            project: 项目实体
            branch: 新分支名
            checkout: True 时创建后切换到新分支
            start_point: 起始点(commit/branch),None 时从当前 HEAD
        """
        try:
            branch = _validate_branch_name(branch, project)
        except GitOperationError as exc:
            return GitOperationResult(
                success=False,
                repo_status=GitRepoStatus(error=exc.git_error),
                error=exc.git_error,
            )
        pre_status = _git_to_repo_status(project, persist=False)
        if pre_status.error is not None:
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=pre_status.error,
            )
        if pre_status.transient:
            err = _make_repo_error(
                "GIT_TRANSIENT_STATE",
                "git is in transient state (merge/rebase)",
                project,
                branch=pre_status.branch,
                transient=True,
                hint="请先解决中间状态后重试",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        try:
            cp_show = _run_git(
                ["show-ref", "--verify", f"refs/heads/{branch}"],
                cwd=project.project_dir,
            )
        except FileNotFoundError:
            err = _file_not_found_error(
                project, project.project_dir,
                branch=pre_status.branch,
                command=f"git show-ref --verify refs/heads/{branch}",
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        except subprocess.TimeoutExpired:
            err = _make_repo_error(
                "GIT_COMMAND_TIMEOUT",
                "git command timed out",
                project,
                command=f"git show-ref --verify refs/heads/{branch}",
                branch=pre_status.branch,
                retryable=True,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        if cp_show.returncode == 0:
            err = _make_repo_error(
                "BRANCH_ALREADY_EXISTS",
                "branch already exists",
                project,
                command=f"git show-ref --verify refs/heads/{branch}",
                branch=pre_status.branch,
                hint=f"分支 {branch} 已存在",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        previous_branch = pre_status.branch
        # start_point 以 "-" 开头会被 git 解析为选项(选项注入),显式拒绝
        if start_point and start_point.startswith("-"):
            err = _make_repo_error(
                "BRANCH_INVALID",
                f"invalid start_point: {start_point}",
                project,
                branch=previous_branch,
                hint="start_point 不能以 '-' 开头",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        branch_args = ["branch", branch]
        if start_point:
            branch_args.append(start_point)
        try:
            cp_b = _run_git(branch_args, cwd=project.project_dir)
        except FileNotFoundError:
            err = _file_not_found_error(
                project, project.project_dir,
                branch=previous_branch,
                command="git branch",
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        except subprocess.TimeoutExpired:
            err = _make_repo_error(
                "GIT_COMMAND_TIMEOUT",
                "git command timed out",
                project,
                command="git branch",
                branch=previous_branch,
                retryable=True,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        if cp_b.returncode != 0:
            err = _make_repo_error(
                "GIT_COMMAND_FAILED",
                "git command failed",
                project,
                command=" ".join(["git", *branch_args]),
                exit_code=cp_b.returncode,
                stdout=cp_b.stdout,
                stderr=cp_b.stderr,
                branch=previous_branch,
                hint="请检查 start_point 是否存在",
                retryable=True,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        if checkout:
            try:
                cp_co = _run_git(
                    ["checkout", branch],
                    cwd=project.project_dir,
                )
            except FileNotFoundError:
                # git 可执行文件消失:无法重新探测,且 ``_git_to_repo_status`` 内部
                # 会再次抛 ``FileNotFoundError`` 被 ``_file_not_found_error`` 捕获,
                # 多一次失败探测无意义。返回 pre_status,但分支已在仓库中创建,
                # 下次 status/probe 会反映新分支。
                err = _file_not_found_error(
                    project, project.project_dir,
                    branch=previous_branch,
                    command=f"git checkout {branch}",
                )
                return GitOperationResult(
                    success=False,
                    repo_status=pre_status,
                    previous_branch=previous_branch,
                    error=err,
                )
            except subprocess.TimeoutExpired:
                # git 卡住:重新探测可能再次 timeout,延长错误响应时间。
                # 返回 pre_status 避免延长等待;分支已创建,下次 probe 会反映。
                err = _make_repo_error(
                    "GIT_COMMAND_TIMEOUT",
                    "git command timed out",
                    project,
                    command=f"git checkout {branch}",
                    branch=previous_branch,
                    retryable=True,
                )
                return GitOperationResult(
                    success=False,
                    repo_status=pre_status,
                    previous_branch=previous_branch,
                    error=err,
                )
            if cp_co.returncode != 0:
                # git 仍可用但 checkout 失败(如本地改动阻止切换):重新探测以让
                # ``local_branches`` 包含新创建的分支,前端列表即时更新。
                post_status = _git_to_repo_status(project, persist=True)
                err = _make_repo_error(
                    "GIT_COMMAND_FAILED",
                    "git command failed",
                    project,
                    command=f"git checkout {branch}",
                    exit_code=cp_co.returncode,
                    stdout=cp_co.stdout,
                    stderr=cp_co.stderr,
                    branch=previous_branch,
                    hint="分支已创建但切换失败,请手动切换",
                    retryable=True,
                )
                return GitOperationResult(
                    success=False,
                    repo_status=post_status,
                    previous_branch=previous_branch,
                    error=err,
                )
        post_status = _git_to_repo_status(project, persist=True)
        return GitOperationResult(
            success=True,
            repo_status=post_status,
            previous_branch=previous_branch,
        )

    @staticmethod
    def _read_branch(project_dir: str) -> str | None:
        """读取当前分支名(辅助 ensure_on_project_create)。"""
        try:
            cp = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=project_dir)
            if cp.returncode == 0:
                return cp.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    @staticmethod
    def commit(
        project: Project,
        message: str,
        *,
        stage_all: bool = False,
        paths: list[str] | None = None,
        amend: bool = False,
        no_verify: bool = False,
    ) -> GitOperationResult:
        """提交当前工作区改动到当前分支(设计文档 §4.9 ``project.git.commit``)。

        Args:
            project: 项目实体
            message: commit message,非空;支持多行(用 ``\\n`` 分隔)
            stage_all: True 时先 ``git add -A`` 暂存全部改动(tracked+untracked);
                与 ``paths`` 互斥。默认值的自动推导(未传 paths 时默认 True,
                传 paths 时默认 False)由 handler 层 ``_strict_bool_param`` 的
                ``default=(paths_param is None)`` 完成,service 层只接收明确的 bool。
            paths: 显式指定暂存路径;与 ``stage_all`` 互斥
            amend: True 时 ``git commit --amend``,覆盖最近一次提交
            no_verify: True 时追加 ``--no-verify`` 跳过 hooks

        Returns:
            GitOperationResult: ``commit_hash`` 字段为新提交的短 hash;
            ``previous_branch`` 未使用(保留为 None)。

        错误码:
          - ``GIT_TRANSIENT_STATE``: merge/rebase 等中间态
          - ``NOTHING_TO_COMMIT``: 没有可提交的改动(amend=false 时)
          - ``GIT_COMMAND_FAILED``: hooks 拒绝、签名失败等
          - ``GIT_COMMAND_TIMEOUT``: 命令超时
          - 共享前置: ``PROJECT_DIR_MISSING`` / ``GIT_NOT_FOUND`` / ``NOT_GIT_REPOSITORY``
        """
        # message 校验:空串/纯空白拒绝
        if not message or not message.strip():
            err = _make_repo_error(
                "BAD_REQUEST",
                "commit message is required",
                project,
                hint="请提供非空的 commit message",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=GitRepoStatus(error=err),
                error=err,
            )
        # 互斥校验:stage_all 与 paths 不能同时传
        if stage_all and paths:
            err = _make_repo_error(
                "BAD_REQUEST",
                "stage_all and paths are mutually exclusive",
                project,
                hint="请只传 stage_all 或 paths 之一",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=GitRepoStatus(error=err),
                error=err,
            )

        pre_status = _git_to_repo_status(project, persist=False)
        if pre_status.error is not None:
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=pre_status.error,
            )
        if pre_status.transient:
            err = _make_repo_error(
                "GIT_TRANSIENT_STATE",
                "git is in transient state (merge/rebase)",
                project,
                branch=pre_status.branch,
                transient=True,
                hint="请先解决中间状态(merge/rebase/cherry-pick)后重试",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )

        project_dir = project.project_dir

        # 防御性校验:paths 显式传了空 list 时拒绝。
        # None 表示"不指定路径"(跳过 add);空 list 表示"指定了零个路径"(无效)。
        # 区分两者是为了避免调用方传 paths=[] 后静默跳过 add,误提交预先暂存的其他改动。
        if paths is not None and len(paths) == 0:
            err = _make_repo_error(
                "BAD_REQUEST",
                "paths is empty; pass None to skip staging, or non-empty list",
                project,
                branch=pre_status.branch,
                hint="请传非空 paths,或不传 paths 以使用已暂存内容",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )

        # staging 之后的失败路径应返回暂存后的状态(而非 pre_status),
        # 否则前端 UI 的 staged/dirty 状态会失真。未 staging 时保持 pre_status。
        status_after_stage = pre_status

        # 暂存阶段(可选)
        if stage_all or paths:
            if stage_all:
                add_args = ["add", "-A"]
            else:
                # paths 元素以 "-" 开头会被 git 解析为选项(选项注入),显式拒绝
                # 使用 "--" 隔离,确保后续参数被视为路径而非选项
                safe_paths: list[str] = []
                for p in paths or []:
                    if not isinstance(p, str) or not p.strip():
                        continue
                    if p.startswith("-"):
                        err = _make_repo_error(
                            "BAD_REQUEST",
                            f"invalid path: {p}",
                            project,
                            branch=pre_status.branch,
                            hint="path 不能以 '-' 开头",
                            retryable=False,
                        )
                        return GitOperationResult(
                            success=False,
                            repo_status=pre_status,
                            error=err,
                        )
                    safe_paths.append(p.strip())
                if not safe_paths:
                    err = _make_repo_error(
                        "BAD_REQUEST",
                        "paths is empty after filtering",
                        project,
                        branch=pre_status.branch,
                        hint="请提供至少一个有效路径",
                        retryable=False,
                    )
                    return GitOperationResult(
                        success=False,
                        repo_status=pre_status,
                        error=err,
                    )
                add_args = ["add", "--", *safe_paths]
            try:
                cp_add = _run_git(add_args, cwd=project_dir)
            except FileNotFoundError:
                err = _file_not_found_error(
                    project, project_dir,
                    branch=pre_status.branch,
                    command="git " + " ".join(add_args),
                )
                return GitOperationResult(
                    success=False,
                    repo_status=pre_status,
                    error=err,
                )
            except subprocess.TimeoutExpired:
                err = _make_repo_error(
                    "GIT_COMMAND_TIMEOUT",
                    "git add timed out",
                    project,
                    command="git " + " ".join(add_args),
                    branch=pre_status.branch,
                    retryable=True,
                )
                return GitOperationResult(
                    success=False,
                    repo_status=pre_status,
                    error=err,
                )
            if cp_add.returncode != 0:
                err = _make_repo_error(
                    "GIT_COMMAND_FAILED",
                    "git add failed",
                    project,
                    command="git " + " ".join(add_args),
                    exit_code=cp_add.returncode,
                    stdout=cp_add.stdout,
                    stderr=cp_add.stderr,
                    branch=pre_status.branch,
                    hint="请检查路径是否存在或权限是否正确",
                    retryable=False,
                )
                return GitOperationResult(
                    success=False,
                    repo_status=pre_status,
                    error=err,
                )
            # add 成功:index 已变,读一次最新状态供后续失败路径返回,
            # 避免 commit 失败时前端 UI 的 staged/dirty 状态失真。
            status_after_stage = _git_to_repo_status(project, persist=False)

        # 空提交预判(amend=false 时):
        # 用 ``git diff --cached --quiet`` 检查 staged 区是否为空,比匹配 commit
        # 输出文案更可靠——git 的 "nothing to commit" / "no changes added to commit"
        # / "nothing added to commit but untracked files present" 等文案因场景与
        # 版本而异,容易漏判。``--quiet`` 退出码 0 表示无 staged 改动,非 0 表示有。
        # amend=true 时跳过:amend 即使 staged 为空也能用新 message 覆盖上次提交。
        if not amend:
            try:
                cp_staged = _run_git(
                    ["diff", "--cached", "--quiet"], cwd=project_dir,
                )
            except FileNotFoundError:
                err = _file_not_found_error(
                    project, project_dir,
                    branch=pre_status.branch,
                    command="git diff --cached --quiet",
                )
                return GitOperationResult(
                    success=False,
                    repo_status=status_after_stage,
                    error=err,
                )
            except subprocess.TimeoutExpired:
                err = _make_repo_error(
                    "GIT_COMMAND_TIMEOUT",
                    "git diff --cached timed out",
                    project,
                    command="git diff --cached --quiet",
                    branch=pre_status.branch,
                    retryable=True,
                )
                return GitOperationResult(
                    success=False,
                    repo_status=status_after_stage,
                    error=err,
                )
            # 退出码 0 = staged 区为空 → 无可提交内容
            if cp_staged.returncode == 0:
                err = _make_repo_error(
                    "NOTHING_TO_COMMIT",
                    "nothing to commit, staged area is empty",
                    project,
                    command="git diff --cached --quiet",
                    exit_code=0,
                    branch=pre_status.branch,
                    hint=(
                        "请先暂存改动(stage_all=true 或指定 paths);"
                        "若改动仅未 track,需 git add 后再提交"
                    ),
                    retryable=False,
                )
                return GitOperationResult(
                    success=False,
                    repo_status=status_after_stage,
                    error=err,
                )

        # 提交阶段:message 通过 stdin 传入(``-F -``),避免命令行长度限制与 shell 转义问题
        commit_args = ["commit", "-F", "-"]
        if amend:
            commit_args.append("--amend")
        if no_verify:
            commit_args.append("--no-verify")
        try:
            cp_commit = _run_git(
                commit_args, cwd=project_dir, stdin_input=message,
            )
        except FileNotFoundError:
            err = _file_not_found_error(
                project, project_dir,
                branch=pre_status.branch,
                command="git " + " ".join(commit_args),
            )
            return GitOperationResult(
                success=False,
                repo_status=status_after_stage,
                error=err,
            )
        except subprocess.TimeoutExpired:
            err = _make_repo_error(
                "GIT_COMMAND_TIMEOUT",
                "git commit timed out",
                project,
                command="git " + " ".join(commit_args),
                branch=pre_status.branch,
                hint="hooks 可能耗时过长,可尝试 no_verify=true 或检查仓库大小",
                retryable=True,
            )
            return GitOperationResult(
                success=False,
                repo_status=status_after_stage,
                error=err,
            )
        if cp_commit.returncode != 0:
            # 空提交兜底检测:
            # 非 amend 时,上方 ``git diff --cached --quiet`` 已预判并提前返回
            # NOTHING_TO_COMMIT,正常不会走到这里。此处作为 defense-in-depth 兜底,
            # 覆盖 amend=true 无新内容的场景,以及预判因竞态(暂存与提交间文件被
            # 还原)漏掉的极端情况。匹配多种 git 文案变体,避免漏判。
            combined_output = (
                (cp_commit.stdout or "") + "\n" + (cp_commit.stderr or "")
            ).lower()
            nothing_markers = (
                "nothing to commit",
                "no changes added to commit",
                "nothing added to commit",
            )
            if any(m in combined_output for m in nothing_markers):
                err = _make_repo_error(
                    "NOTHING_TO_COMMIT",
                    "nothing to commit, working tree clean",
                    project,
                    command="git " + " ".join(commit_args),
                    exit_code=cp_commit.returncode,
                    stdout=cp_commit.stdout,
                    stderr=cp_commit.stderr,
                    branch=pre_status.branch,
                    hint="请先暂存改动(stage_all=true 或指定 paths)",
                    retryable=False,
                )
                return GitOperationResult(
                    success=False,
                    repo_status=status_after_stage,
                    error=err,
                )
            err = _make_repo_error(
                "GIT_COMMAND_FAILED",
                "git commit failed",
                project,
                command="git " + " ".join(commit_args),
                exit_code=cp_commit.returncode,
                stdout=cp_commit.stdout,
                stderr=cp_commit.stderr,
                branch=pre_status.branch,
                hint="请检查 hooks 输出或 commit message 是否合法",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=status_after_stage,
                error=err,
            )

        # 读取新提交的短 hash
        commit_hash: str | None = None
        try:
            cp_h = _run_git(["rev-parse", "--short", "HEAD"], cwd=project_dir)
            if cp_h.returncode == 0:
                commit_hash = cp_h.stdout.strip() or None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        post_status = _git_to_repo_status(project, persist=True)
        return GitOperationResult(
            success=True,
            repo_status=post_status,
            commit_hash=commit_hash,
        )

    @staticmethod
    def push(
        project: Project,
        *,
        remote: str = "origin",
        branch: str | None = None,
        set_upstream: bool = False,
        force: bool = False,
        delete: bool = False,
    ) -> GitOperationResult:
        """推送本地分支到远程(设计文档 §4.10 ``project.git.push``)。

        Args:
            project: 项目实体
            remote: 远程名,默认 ``"origin"``;以 ``-`` 开头拒绝
            branch: 本地分支;不传时用当前分支
            set_upstream: True 时追加 ``-u``,建立远程 tracking 关系
            force: True 时使用 ``--force-with-lease``(更安全的强推,非 ``--force``)
            delete: True 时删除远程分支(``git push <remote> --delete <branch>``);
                与 ``set_upstream`` / ``force`` 互斥

        Returns:
            GitOperationResult: ``pushed_remote`` 字段为实际推送的远程名。

        错误码:
          - ``BAD_REQUEST``: 参数互斥冲突
          - ``GIT_TRANSIENT_STATE``: 中间态
          - ``DETACHED_HEAD``: detached HEAD 且未传 ``branch``
          - ``BRANCH_INVALID``: ``branch`` 非法
          - ``REMOTE_NOT_FOUND``: ``remote`` 不存在
          - ``PUSH_REJECTED``: 远程拒绝(non-fast-forward / protected / 权限)
          - ``PUSH_NO_UPSTREAM``: 未设 upstream 且未传 ``branch``
          - ``GIT_COMMAND_TIMEOUT``: push 超时(网络慢)
          - ``GIT_COMMAND_FAILED``: 其他 push 失败
        """
        # 参数互斥校验
        if delete and (set_upstream or force):
            err = _make_repo_error(
                "BAD_REQUEST",
                "delete is mutually exclusive with set_upstream and force",
                project,
                hint="删除远程分支时不能同时 set_upstream 或 force",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=GitRepoStatus(error=err),
                error=err,
            )
        # remote 校验:以 "-" 开头会被 git 解析为选项(选项注入)
        if not remote or remote.startswith("-"):
            err = _make_repo_error(
                "BAD_REQUEST",
                f"invalid remote: {remote}",
                project,
                hint="remote 不能为空且不能以 '-' 开头",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=GitRepoStatus(error=err),
                error=err,
            )

        pre_status = _git_to_repo_status(project, persist=False)
        if pre_status.error is not None:
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=pre_status.error,
            )
        if pre_status.transient:
            err = _make_repo_error(
                "GIT_TRANSIENT_STATE",
                "git is in transient state (merge/rebase)",
                project,
                branch=pre_status.branch,
                transient=True,
                hint="请先解决中间状态(merge/rebase/cherry-pick)后重试",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )

        project_dir = project.project_dir

        # 确定要推送的分支
        effective_branch = branch
        if effective_branch:
            # 显式传了 branch,走 _validate_branch_name 校验
            try:
                effective_branch = _validate_branch_name(effective_branch, project)
            except GitOperationError as exc:
                return GitOperationResult(
                    success=False,
                    repo_status=pre_status,
                    error=exc.git_error,
                )
        else:
            # 未传 branch:用当前分支
            if pre_status.detached:
                err = _make_repo_error(
                    "DETACHED_HEAD",
                    "detached HEAD, cannot push without explicit branch",
                    project,
                    branch=pre_status.branch,
                    hint="请先切换到分支或显式传 branch 参数",
                    retryable=False,
                )
                return GitOperationResult(
                    success=False,
                    repo_status=pre_status,
                    error=err,
                )
            effective_branch = pre_status.branch
            if not effective_branch:
                err = _make_repo_error(
                    "PUSH_NO_UPSTREAM",
                    "no current branch and no branch specified",
                    project,
                    hint="请显式传 branch 参数",
                    retryable=False,
                )
                return GitOperationResult(
                    success=False,
                    repo_status=pre_status,
                    error=err,
                )

        # 远程存在性校验:git remote get-url <remote>
        try:
            cp_remote = _run_git(
                ["remote", "get-url", remote], cwd=project_dir,
            )
        except FileNotFoundError:
            err = _file_not_found_error(
                project, project_dir,
                branch=effective_branch,
                command=f"git remote get-url {remote}",
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        except subprocess.TimeoutExpired:
            err = _make_repo_error(
                "GIT_COMMAND_TIMEOUT",
                "git remote get-url timed out",
                project,
                command=f"git remote get-url {remote}",
                branch=effective_branch,
                retryable=True,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        if cp_remote.returncode != 0:
            err = _make_repo_error(
                "REMOTE_NOT_FOUND",
                f"remote not found: {remote}",
                project,
                command=f"git remote get-url {remote}",
                exit_code=cp_remote.returncode,
                stderr=cp_remote.stderr,
                branch=effective_branch,
                hint=f"请先添加远程: git remote add {remote} <url>",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )

        # 构造 push 命令
        if delete:
            push_args = ["push", remote, "--delete", effective_branch]
        else:
            push_args = ["push"]
            if set_upstream:
                push_args.append("-u")
            if force:
                # --force-with-lease 比 --force 更安全:若远程有他人新提交会拒绝,
                # 避免误覆盖;仍需前端二次确认
                push_args.append("--force-with-lease")
            push_args.extend([remote, effective_branch])

        try:
            cp_push = _run_git(
                push_args,
                cwd=project_dir,
                timeout=GIT_PUSH_TIMEOUT_SEC,
            )
        except FileNotFoundError:
            err = _file_not_found_error(
                project, project_dir,
                branch=effective_branch,
                command="git " + " ".join(push_args),
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        except subprocess.TimeoutExpired:
            err = _make_repo_error(
                "GIT_COMMAND_TIMEOUT",
                "git push timed out",
                project,
                command="git " + " ".join(push_args),
                branch=effective_branch,
                hint="网络可能较慢,请检查远程仓库连通性或增大 JIUWEN_GIT_PUSH_TIMEOUT_SEC",
                retryable=True,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )
        if cp_push.returncode != 0:
            stderr_lower = (cp_push.stderr or "").lower()
            # 远程拒绝:non-fast-forward / protected branch / 权限不足
            if any(
                token in stderr_lower
                for token in ("rejected", "denied", "non-fast-forward", "failed to push")
            ):
                err = _make_repo_error(
                    "PUSH_REJECTED",
                    "git push rejected by remote",
                    project,
                    command="git " + " ".join(push_args),
                    exit_code=cp_push.returncode,
                    stdout=cp_push.stdout,
                    stderr=cp_push.stderr,
                    branch=effective_branch,
                    hint="远程拒绝推送:可能是 non-fast-forward(需先 pull)或分支受保护或权限不足",
                    retryable=False,
                )
                return GitOperationResult(
                    success=False,
                    repo_status=pre_status,
                    error=err,
                )
            err = _make_repo_error(
                "GIT_COMMAND_FAILED",
                "git push failed",
                project,
                command="git " + " ".join(push_args),
                exit_code=cp_push.returncode,
                stdout=cp_push.stdout,
                stderr=cp_push.stderr,
                branch=effective_branch,
                hint="请检查远程仓库配置与网络连通性",
                retryable=False,
            )
            return GitOperationResult(
                success=False,
                repo_status=pre_status,
                error=err,
            )

        post_status = _git_to_repo_status(project, persist=True)
        return GitOperationResult(
            success=True,
            repo_status=post_status,
            pushed_remote=remote,
        )


_service_instance: ProjectGitService | None = None


def get_project_git_service() -> ProjectGitService:
    """返回 ``ProjectGitService`` 单例。"""
    global _service_instance
    if _service_instance is None:
        _service_instance = ProjectGitService()
    return _service_instance


def reset_project_git_service() -> None:
    """重置单例(仅供测试)。"""
    global _service_instance
    _service_instance = None
