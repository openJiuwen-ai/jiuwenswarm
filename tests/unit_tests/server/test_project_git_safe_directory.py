from __future__ import annotations

import subprocess

import pytest

from jiuwenswarm.server.runtime.session.project_store import Project


def _cp(args: list[str], returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_git_status_probe_returns_dubious_ownership_error(
    monkeypatch,
    tmp_path,
):
    from jiuwenswarm.server.runtime.session import project_git

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = Project(
        project_id="proj_test",
        name="test",
        project_dir=str(project_dir),
        work_mode="code",
    )
    calls: list[list[str]] = []

    def fake_run_git(args, *, cwd, timeout=project_git.GIT_COMMAND_TIMEOUT_SEC):
        calls.append(args)
        if args == ["rev-parse", "--show-toplevel"]:
            return _cp(
                ["git", *args],
                128,
                stderr=(
                    "fatal: detected dubious ownership in repository at "
                    f"'{project_dir}'"
                ),
            )
        return _cp(["git", *args], 0)

    monkeypatch.setattr(project_git, "_run_git", fake_run_git)
    monkeypatch.setattr(project_git, "_find_git_executable", lambda: "git")

    status = project_git._git_to_repo_status(project)

    assert status.error is not None
    assert status.error.code == "GIT_DUBIOUS_OWNERSHIP"
    assert status.error.stderr
    assert "git config --global --add safe.directory" in status.error.hint
    assert project_dir.resolve().as_posix() in status.error.hint
    assert calls == [["rev-parse", "--show-toplevel"]]


def test_project_create_probe_returns_dubious_ownership_error(
    monkeypatch,
    tmp_path,
):
    from jiuwenswarm.server.runtime.session import project_git

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("demo", encoding="utf-8")
    project = Project(
        project_id="proj_test",
        name="test",
        project_dir=str(project_dir),
        work_mode="code",
    )

    def fake_run_git(args, *, cwd, timeout=project_git.GIT_COMMAND_TIMEOUT_SEC):
        if args == ["rev-parse", "--show-toplevel"]:
            return _cp(
                ["git", *args],
                128,
                stderr="fatal: dubious ownership",
            )
        return _cp(["git", *args], 0)

    monkeypatch.setattr(project_git, "_run_git", fake_run_git)
    monkeypatch.setattr(project_git, "_find_git_executable", lambda: "git")

    result = project_git.ProjectGitService()._probe_on_project_create(project)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "GIT_DUBIOUS_OWNERSHIP"
    assert "git config --global --add safe.directory" in result.error.hint


def test_git_init_returns_dubious_ownership_error(
    monkeypatch,
    tmp_path,
):
    from jiuwenswarm.server.runtime.session import project_git

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = Project(
        project_id="proj_test",
        name="test",
        project_dir=str(project_dir),
        work_mode="code",
    )

    def fake_run_git(args, *, cwd, timeout=project_git.GIT_COMMAND_TIMEOUT_SEC):
        if args == ["check-ref-format", "--branch", "main"]:
            return _cp(["git", *args], 0, stdout="main\n")
        if args == ["init", "-b", "main", str(project_dir)]:
            return _cp(
                ["git", *args],
                128,
                stderr="fatal: detected dubious ownership in repository",
            )
        return _cp(["git", *args], 0)

    monkeypatch.setattr(project_git, "_run_git", fake_run_git)
    monkeypatch.setattr(project_git, "_find_git_executable", lambda: "git")

    status = project_git.ProjectGitService.init(project)

    assert status.error is not None
    assert status.error.code == "GIT_DUBIOUS_OWNERSHIP"
    assert "git config --global --add safe.directory" in status.error.hint


def test_dubious_ownership_status_maps_to_specific_snapshot_status():
    from jiuwenswarm.server.runtime.session import project_git

    status = project_git.GitRepoStatus(
        is_git=False,
        error=project_git.GitError(
            "GIT_DUBIOUS_OWNERSHIP",
            "git repository ownership check failed",
        ),
    )

    assert project_git._map_status_string(status) == "dubious_ownership"


def test_git_status_counts_type_change_as_dirty(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.session import project_git

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = Project(
        project_id="proj_test",
        name="test",
        project_dir=str(project_dir),
        work_mode="code",
    )

    def fake_run_git(args, *, cwd, timeout=project_git.GIT_COMMAND_TIMEOUT_SEC):
        if args == ["rev-parse", "--show-toplevel"]:
            return _cp(["git", *args], 0, stdout=str(project_dir))
        if args == ["symbolic-ref", "--short", "HEAD"]:
            return _cp(["git", *args], 0, stdout="main\n")
        if args == ["rev-parse", "--abbrev-ref", "main@{upstream}"]:
            return _cp(["git", *args], 1)
        if args == ["rev-parse", "--short", "HEAD"]:
            return _cp(["git", *args], 0, stdout="abc123\n")
        if args == ["status", "--porcelain", "--no-renames"]:
            return _cp(["git", *args], 0, stdout=" T link.txt\n")
        if args == ["for-each-ref", "--format=%(refname:short)", "refs/heads/"]:
            return _cp(["git", *args], 0, stdout="main\n")
        if args == ["for-each-ref", "--format=%(refname:short)", "refs/remotes/"]:
            return _cp(["git", *args], 0)
        return _cp(["git", *args], 0)

    monkeypatch.setattr(project_git, "_run_git", fake_run_git)
    monkeypatch.setattr(project_git, "_find_git_executable", lambda: "git")

    status = project_git._git_to_repo_status(project)

    assert status.is_dirty is True
    assert status.unstaged == 1


def test_create_branch_checkout_failure_returns_post_create_status(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.session import project_git

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = Project(
        project_id="proj_test",
        name="test",
        project_dir=str(project_dir),
        work_mode="code",
    )
    calls: list[list[str]] = []

    def fake_run_git(args, *, cwd, timeout=project_git.GIT_COMMAND_TIMEOUT_SEC):
        calls.append(args)
        if args == ["check-ref-format", "--branch", "feature"]:
            return _cp(["git", *args], 0, stdout="feature\n")
        if args == ["show-ref", "--verify", "refs/heads/feature"]:
            return _cp(["git", *args], 1)
        if args == ["branch", "feature"]:
            return _cp(["git", *args], 0)
        if args == ["checkout", "feature"]:
            return _cp(["git", *args], 1, stderr="checkout failed")
        return _cp(["git", *args], 0)

    pre_status = project_git.GitRepoStatus(
        is_git=True,
        repo_root=str(project_dir),
        branch="main",
        local_branches=["main"],
    )
    post_status = project_git.GitRepoStatus(
        is_git=True,
        repo_root=str(project_dir),
        branch="main",
        local_branches=["feature", "main"],
    )
    status_calls: list[bool] = []

    def fake_git_to_repo_status(project, *, persist=False):
        status_calls.append(persist)
        return pre_status if len(status_calls) == 1 else post_status

    monkeypatch.setattr(project_git, "_run_git", fake_run_git)
    monkeypatch.setattr(project_git, "_git_to_repo_status", fake_git_to_repo_status)

    result = project_git.ProjectGitService.create_branch(project, "feature")

    assert result.success is False
    assert result.error is not None
    assert result.repo_status is post_status
    assert "feature" in result.repo_status.local_branches
    assert status_calls == [False, True]
    assert ["checkout", "feature"] in calls


def test_switch_branch_refuses_live_worktree_without_detach(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.session import project_git

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = Project(
        project_id="proj_test",
        name="test",
        project_dir=str(project_dir),
        work_mode="code",
    )
    calls: list[list[str]] = []
    held_stderr = (
        "fatal: 'feature' is already used by worktree at "
        f"'{tmp_path / 'worktree'}'"
    )

    def fake_run_git(args, *, cwd, timeout=project_git.GIT_COMMAND_TIMEOUT_SEC):
        calls.append(args)
        if args == ["check-ref-format", "--branch", "feature"]:
            return _cp(["git", *args], 0, stdout="feature\n")
        if args == ["show-ref", "--verify", "refs/heads/feature"]:
            return _cp(["git", *args], 0)
        if args == ["checkout", "feature"]:
            return _cp(["git", *args], 1, stderr=held_stderr)
        if args == ["-c", "safe.directory=*", "worktree", "prune"]:
            return _cp(["git", *args], 0)
        return _cp(["git", *args], 0)

    pre_status = project_git.GitRepoStatus(
        is_git=True,
        repo_root=str(project_dir),
        branch="main",
        local_branches=["feature", "main"],
    )

    monkeypatch.setattr(project_git, "_run_git", fake_run_git)
    monkeypatch.setattr(project_git, "_git_to_repo_status", lambda project, *, persist=False: pre_status)
    monkeypatch.setattr(
        project_git,
        "_find_worktrees_holding_branch",
        lambda project_dir, branch: [str(tmp_path / "worktree")],
    )

    result = project_git.ProjectGitService.switch_branch(project, "feature")

    assert result.success is False
    assert result.error is not None
    assert "worktree" in result.error.message
    assert ["checkout", "--detach"] not in calls


def test_switch_branch_hint_includes_holding_worktree_path(monkeypatch, tmp_path):
    """Bug 修复:holding 调用结果应进入 hint,让用户看到具体占用 worktree 路径。"""
    from jiuwenswarm.server.runtime.session import project_git

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = Project(
        project_id="proj_test",
        name="test",
        project_dir=str(project_dir),
        work_mode="code",
    )
    holding_path = str(tmp_path / "team_wt")
    held_stderr = (
        f"fatal: 'feature' is already used by worktree at '{holding_path}'"
    )

    def fake_run_git(args, *, cwd, timeout=project_git.GIT_COMMAND_TIMEOUT_SEC):
        if args == ["check-ref-format", "--branch", "feature"]:
            return _cp(["git", *args], 0, stdout="feature\n")
        if args == ["show-ref", "--verify", "refs/heads/feature"]:
            return _cp(["git", *args], 0)
        if args == ["checkout", "feature"]:
            return _cp(["git", *args], 1, stderr=held_stderr)
        if args == ["-c", "safe.directory=*", "worktree", "prune"]:
            return _cp(["git", *args], 0)
        return _cp(["git", *args], 0)

    pre_status = project_git.GitRepoStatus(
        is_git=True,
        repo_root=str(project_dir),
        branch="main",
        local_branches=["feature", "main"],
    )
    monkeypatch.setattr(project_git, "_run_git", fake_run_git)
    monkeypatch.setattr(
        project_git, "_git_to_repo_status",
        lambda project, *, persist=False: pre_status,
    )
    monkeypatch.setattr(
        project_git, "_find_worktrees_holding_branch",
        lambda project_dir, branch: [holding_path],
    )

    result = project_git.ProjectGitService.switch_branch(project, "feature")

    assert result.success is False
    assert result.error is not None
    # hint 应包含具体占用 worktree 路径,而非仅泛泛提示
    assert holding_path in result.error.hint


def test_switch_branch_allows_untracked_files_when_clean_is_required(monkeypatch, tmp_path):
    """未跟踪文件不会被 checkout 改写，不能触发保护性切换拦截。"""
    from jiuwenswarm.server.runtime.session import project_git

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = Project(
        project_id="proj_test",
        name="test",
        project_dir=str(project_dir),
        work_mode="code",
    )
    calls: list[list[str]] = []

    def fake_run_git(args, *, cwd, timeout=project_git.GIT_COMMAND_TIMEOUT_SEC):
        calls.append(args)
        if args == ["check-ref-format", "--branch", "feature"]:
            return _cp(["git", *args], 0, stdout="feature\n")
        if args == ["show-ref", "--verify", "refs/heads/feature"]:
            return _cp(["git", *args], 0)
        if args == ["checkout", "feature"]:
            return _cp(["git", *args], 0)
        return _cp(["git", *args], 0)

    pre_status = project_git.GitRepoStatus(
        is_git=True,
        repo_root=str(project_dir),
        branch="main",
        is_dirty=True,
        untracked=1,
        local_branches=["feature", "main"],
    )
    post_status = project_git.GitRepoStatus(
        is_git=True,
        repo_root=str(project_dir),
        branch="feature",
        is_dirty=True,
        untracked=1,
        local_branches=["feature", "main"],
    )
    statuses = iter((pre_status, post_status))

    monkeypatch.setattr(project_git, "_run_git", fake_run_git)
    monkeypatch.setattr(
        project_git, "_git_to_repo_status", lambda project, *, persist=False: next(statuses),
    )

    result = project_git.ProjectGitService.switch_branch(
        project, "feature", require_clean=True,
    )

    assert result.success is True
    assert ["checkout", "feature"] in calls


def test_create_branch_checkout_filenotfound_returns_pre_status(monkeypatch, tmp_path):
    """Bug 修复:FileNotFoundError 路径不再二次探测(git 已消失,二次探测必再失败)。"""
    from jiuwenswarm.server.runtime.session import project_git

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = Project(
        project_id="proj_test",
        name="test",
        project_dir=str(project_dir),
        work_mode="code",
    )

    def fake_run_git(args, *, cwd, timeout=project_git.GIT_COMMAND_TIMEOUT_SEC):
        if args == ["check-ref-format", "--branch", "feature"]:
            return _cp(["git", *args], 0, stdout="feature\n")
        if args == ["show-ref", "--verify", "refs/heads/feature"]:
            return _cp(["git", *args], 1)
        if args == ["branch", "feature"]:
            return _cp(["git", *args], 0)
        if args == ["checkout", "feature"]:
            raise FileNotFoundError("git executable not found")
        return _cp(["git", *args], 0)

    pre_status = project_git.GitRepoStatus(
        is_git=True,
        repo_root=str(project_dir),
        branch="main",
        local_branches=["main"],
    )
    status_calls: list[bool] = []

    def fake_git_to_repo_status(project, *, persist=False):
        status_calls.append(persist)
        return pre_status

    monkeypatch.setattr(project_git, "_run_git", fake_run_git)
    monkeypatch.setattr(project_git, "_git_to_repo_status", fake_git_to_repo_status)

    result = project_git.ProjectGitService.create_branch(project, "feature")

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "GIT_NOT_FOUND"
    # 仅调用一次 pre_status 探测(persist=False),不再二次探测
    assert status_calls == [False]


def test_create_branch_checkout_timeout_returns_pre_status(monkeypatch, tmp_path):
    """Bug 修复:TimeoutExpired 路径不再二次探测(避免延长错误响应时间)。"""
    from jiuwenswarm.server.runtime.session import project_git

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = Project(
        project_id="proj_test",
        name="test",
        project_dir=str(project_dir),
        work_mode="code",
    )

    def fake_run_git(args, *, cwd, timeout=project_git.GIT_COMMAND_TIMEOUT_SEC):
        if args == ["check-ref-format", "--branch", "feature"]:
            return _cp(["git", *args], 0, stdout="feature\n")
        if args == ["show-ref", "--verify", "refs/heads/feature"]:
            return _cp(["git", *args], 1)
        if args == ["branch", "feature"]:
            return _cp(["git", *args], 0)
        if args == ["checkout", "feature"]:
            raise subprocess.TimeoutExpired(cmd=["git", *args], timeout=10)
        return _cp(["git", *args], 0)

    pre_status = project_git.GitRepoStatus(
        is_git=True,
        repo_root=str(project_dir),
        branch="main",
        local_branches=["main"],
    )
    status_calls: list[bool] = []

    def fake_git_to_repo_status(project, *, persist=False):
        status_calls.append(persist)
        return pre_status

    monkeypatch.setattr(project_git, "_run_git", fake_run_git)
    monkeypatch.setattr(project_git, "_git_to_repo_status", fake_git_to_repo_status)

    result = project_git.ProjectGitService.create_branch(project, "feature")

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "GIT_COMMAND_TIMEOUT"
    assert status_calls == [False]


@pytest.mark.parametrize(
    ("marker", "expected_kind"),
    [
        ("MERGE_HEAD", "merge"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
    ],
)
def test_is_transient_state_recognizes_git_markers(tmp_path, marker, expected_kind):
    from jiuwenswarm.server.runtime.session import project_git

    project_dir = tmp_path / "project"
    git_dir = project_dir / ".git"
    git_dir.mkdir(parents=True)
    marker_path = git_dir / marker
    if marker.startswith("rebase-"):
        marker_path.mkdir()
    else:
        marker_path.write_text("pending\n", encoding="utf-8")

    assert project_git._is_transient_state(str(project_dir)) == (True, expected_kind)


def test_merge_transient_state_blocks_all_guarded_writes(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.session import project_git

    project_dir = tmp_path / "project"
    git_dir = project_dir / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "MERGE_HEAD").write_text("merge-head\n", encoding="utf-8")
    project = Project(
        project_id="proj_test",
        name="test",
        project_dir=str(project_dir),
        work_mode="code",
    )
    calls: list[list[str]] = []

    def fake_run_git(args, *, cwd, timeout=project_git.GIT_COMMAND_TIMEOUT_SEC):
        calls.append(args)
        if args == ["check-ref-format", "--branch", "feature"]:
            return _cp(["git", *args], 0, stdout="feature\n")
        if args == ["rev-parse", "--show-toplevel"]:
            return _cp(["git", *args], 0, stdout=f"{project_dir}\n")
        if args == ["symbolic-ref", "--short", "HEAD"]:
            return _cp(["git", *args], 0, stdout="main\n")
        if args == ["rev-parse", "--short", "HEAD"]:
            return _cp(["git", *args], 0, stdout="abc1234\n")
        if args == ["rev-parse", "--abbrev-ref", "main@{upstream}"]:
            return _cp(["git", *args], 1)
        if args == ["status", "--porcelain", "--no-renames"]:
            return _cp(["git", *args], 0, stdout="UU conflicted.txt\n")
        if args == ["for-each-ref", "--format=%(refname:short)", "refs/heads/"]:
            return _cp(["git", *args], 0, stdout="main\nfeature\n")
        if args == ["for-each-ref", "--format=%(refname:short)", "refs/remotes/"]:
            return _cp(["git", *args], 0)
        raise AssertionError(f"write guard allowed unexpected git command: {args}")

    monkeypatch.setattr(project_git, "_find_git_executable", lambda: "git")
    monkeypatch.setattr(project_git, "_run_git", fake_run_git)

    results = [
        project_git.ProjectGitService.switch_branch(project, "feature"),
        project_git.ProjectGitService.create_branch(project, "feature"),
        project_git.ProjectGitService.commit(project, "must be blocked"),
        project_git.ProjectGitService.push(project, branch="main"),
    ]

    assert [result.error.code for result in results if result.error] == [
        "GIT_TRANSIENT_STATE",
        "GIT_TRANSIENT_STATE",
        "GIT_TRANSIENT_STATE",
        "GIT_TRANSIENT_STATE",
    ]
    assert all(result.repo_status.transient for result in results)
    assert not any(args[0] in {"checkout", "branch", "add", "commit", "push"} for args in calls)


def test_is_transient_state_uses_absolute_not_resolve(monkeypatch, tmp_path):
    """Bug 修复:``_is_transient_state`` 用 absolute() 不解析 symlink。

    场景:``.git`` 文件指向通过 symlink 访问的 worktree gitdir。
    ``resolve()`` 会解析 symlink 得到真实路径,可能与 git 内部管理的路径不一致,
    导致后续 ``merge``/``rebase`` 目录检测失败。``absolute()`` 保留 symlink。
    """
    from jiuwenswarm.server.runtime.session import project_git

    # 真实 gitdir 在别处,merge 目录在真实路径下
    real_git_dir = tmp_path / "real_git_dir"
    real_git_dir.mkdir()
    (real_git_dir / "MERGE_HEAD").write_text("merge-head\n", encoding="utf-8")

    # 通过 symlink 访问真实 gitdir(模拟 worktree 通过 symlink 挂载)
    symlink_git_dir = tmp_path / "symlink_git_dir"
    try:
        symlink_git_dir.symlink_to(real_git_dir)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    git_file = project_dir / ".git"
    git_file.write_text(f"gitdir: {symlink_git_dir}\n", encoding="utf-8")

    is_transient, kind = project_git._is_transient_state(str(project_dir))

    assert is_transient is True
    assert kind == "merge"
