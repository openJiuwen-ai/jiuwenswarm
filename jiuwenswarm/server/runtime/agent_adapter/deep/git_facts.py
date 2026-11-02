"""会话 runtime_state 用的稳定 git 事实缓存。

职责：
- 廉价读取 ``HEAD`` 文件（不 spawn git）以判断 checkout 是否漂移
- 按 ``(git_bin, project_dir)`` 缓存不变事实：is_repo / user.name /
  main_branch / head_file，带 FIFO 上限防止按 session 无限增长
- 供写 runtime_state 时复用，避免每轮重复跑六次不变的 git 子进程

会话级分支 / status / recent_commits 快照仍由门面 ``_GitSnapshot`` 维护。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

def _read_git_head(head_file: str) -> str:
    """Read the HEAD pointer directly, without spawning git.

    Changes whenever the checkout moves — switching branches rewrites the
    symbolic ref, and a detached checkout writes a different commit id — while
    staying untouched by ordinary edits to the working tree. That makes it a
    cheap way to tell "the conversation moved to a different checkout" apart
    from "the user edited a file", which need opposite treatment: the former
    must invalidate the snapshot, the latter must not.

    Args:
        head_file: Absolute path to the git directory's HEAD file.

    Returns:
        Stripped file contents, or an empty string when it cannot be read —
        in which case the caller degrades to holding the snapshot as-is.
    """
    if not head_file:
        return ""
    try:
        return Path(head_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class _StableGitFacts:
    """Git facts about a project that do not change between turns of a chat.

    Attributes:
        is_repo: Whether the directory is inside a git work tree.
        user_name: ``user.name`` from git config; empty when unset.
        main_branch: First of origin/main, origin/master, main, master that
            resolves; empty when none do.
        head_file: Absolute path to the git directory's HEAD file. Resolved via
            git so it is correct for a subdirectory, a worktree or a submodule,
            where it is nowhere near ``<project_dir>/.git/HEAD``.
    """

    is_repo: bool
    user_name: str
    main_branch: str
    head_file: str


_STABLE_GIT_FACTS: dict[str, _StableGitFacts] = {}
_STABLE_GIT_FACTS_LOCK = threading.Lock()
# Cache ceiling: ``project_dir`` is per-session (the session workspace), so the
# key space grows with sessions and an unbounded dict leaks one entry per
# session forever (issue#5240 residual hunt: +100 entries / 2min ≈ 1MB/h).
# FIFO-evict the oldest insert at the cap; a miss only costs re-running the
# six cached git subprocesses, never correctness.
_STABLE_GIT_FACTS_MAX = 1024


def _resolve_stable_git_facts(
    git_bin: str,
    project_dir: str,
    run_git: Callable[[list[str]], str],
) -> _StableGitFacts:
    """Resolve the per-project git facts once and reuse them afterwards.

    ``_write_runtime_state`` runs on every turn and used to spawn up to nine git
    subprocesses each time. Six of them answered questions whose answers cannot
    change while a conversation is in progress — whether the directory is a repo,
    who the committer is, and which of the four candidate names the main branch
    goes by — so they are resolved once per project directory. Branch, status and
    recent commits stay live, because those are exactly what changes while the
    user works.

    Args:
        git_bin: Resolved git executable, part of the cache key so a toolchain
            switch is not served a stale answer.
        project_dir: Directory the git commands run in.
        run_git: Callable running git in ``project_dir`` and returning stripped
            stdout, or an empty string on failure.

    Returns:
        The cached facts for this project directory.
    """
    cache_key = f"{git_bin}\0{project_dir}"
    with _STABLE_GIT_FACTS_LOCK:
        cached = _STABLE_GIT_FACTS.get(cache_key)
    if cached is not None:
        return cached

    is_repo = run_git(["rev-parse", "--is-inside-work-tree"]) == "true"
    user_name = ""
    main_branch = ""
    head_file = ""
    if is_repo:
        user_name = run_git(["config", "user.name"])
        for candidate in ("origin/main", "origin/master", "main", "master"):
            if run_git(["rev-parse", "--verify", "--quiet", candidate]):
                main_branch = candidate
                break
        git_dir = run_git(["rev-parse", "--absolute-git-dir"])
        if git_dir:
            head_file = str(Path(git_dir) / "HEAD")

    facts = _StableGitFacts(
        is_repo=is_repo,
        user_name=user_name,
        main_branch=main_branch,
        head_file=head_file,
    )
    with _STABLE_GIT_FACTS_LOCK:
        if len(_STABLE_GIT_FACTS) >= _STABLE_GIT_FACTS_MAX:
            _STABLE_GIT_FACTS.pop(next(iter(_STABLE_GIT_FACTS)), None)
        _STABLE_GIT_FACTS[cache_key] = facts
    return facts
