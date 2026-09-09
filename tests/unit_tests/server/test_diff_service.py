import json
import os
import subprocess
from pathlib import Path

import pytest

from jiuwenswarm.server.utils.diff_service import MAX_DIFF_SIZE_BYTES, DiffService


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_git_diff_from_subdir_includes_untracked_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    tracked = repo / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")

    tracked.write_text("after\n", encoding="utf-8")
    subdir = repo / "pkg"
    subdir.mkdir()
    untracked = repo / "未跟踪.txt"
    untracked.write_text("line one\nline two\n", encoding="utf-8")

    diff = DiffService().get_git_diff(str(subdir))

    assert diff is not None
    assert str(tracked) in diff["files"]
    assert str(untracked) in diff["files"]
    untracked_info = diff["files"][str(untracked)]
    assert untracked_info["status"] == "added"
    assert untracked_info["isNewFile"] is True
    assert untracked_info["isUntracked"] is True
    assert len(untracked_info["hunks"]) == 1
    assert untracked_info["hunks"][0]["lines"] == ["+line one", "+line two"]
    assert untracked_info["linesAdded"] == 2
    assert untracked_info["isTruncated"] is False
    assert diff["stats"]["filesChanged"] == 2
    assert diff["stats"]["linesAdded"] == 3
    assert diff["stats"]["linesRemoved"] == 1


def test_git_diff_stats_include_tracked_files_beyond_detail_cap(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    for i in range(60):
        file_path = repo / f"file-{i:02d}.txt"
        file_path.write_text("before\n", encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    for i in range(60):
        file_path = repo / f"file-{i:02d}.txt"
        file_path.write_text("after\n", encoding="utf-8")

    diff = DiffService().get_git_diff(str(repo))

    assert diff is not None
    assert len(diff["files"]) == 50
    assert diff["stats"]["filesChanged"] == 60
    assert diff["stats"]["linesAdded"] == 60
    assert diff["stats"]["linesRemoved"] == 60


@pytest.mark.skipif(os.name == "nt", reason="Windows paths cannot contain tab characters")
def test_git_diff_preserves_tabs_in_file_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "core.quotepath", "false")

    tab_name = "dir\tfile.txt"
    tab_file = repo / tab_name
    tab_file.write_text("before\nbefore2\nbefore3\n", encoding="utf-8")
    _git(repo, "add", "--", tab_name)
    _git(repo, "commit", "-m", "initial")

    tab_file.write_text("after\nafter2\n", encoding="utf-8")

    diff = DiffService().get_git_diff(str(repo))

    assert diff is not None
    tab_abs = str(tab_file)
    assert tab_abs in diff["files"]
    file_info = diff["files"][tab_abs]
    assert file_info["linesAdded"] == 2
    assert file_info["linesRemoved"] == 3


def test_git_diff_staged_rename_with_modification_keeps_hunks(tmp_path):
    """staged rename + 内容修改时, hunks 应通过新路径对齐 (非空)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    old = repo / "old.txt"
    old.write_text("line1\nline2\nline3\n", encoding="utf-8")
    _git(repo, "add", "old.txt")
    _git(repo, "commit", "-m", "initial")

    _git(repo, "mv", "old.txt", "new.txt")
    new = repo / "new.txt"
    new.write_text("line1\nCHANGED\nline3\n", encoding="utf-8")
    _git(repo, "add", "new.txt")

    diff = DiffService().get_git_diff(str(repo))

    assert diff is not None
    assert str(new) in diff["files"]
    file_info = diff["files"][str(new)]
    assert file_info["linesAdded"] == 1
    assert file_info["linesRemoved"] == 1
    # rename 前 numstat key 是 "old => new", 与 hunk key "new" 不一致会导致 hunks 丢失
    assert len(file_info["hunks"]) == 1
    assert file_info["hunks"][0]["lines"]  # 非空


def test_git_diff_staged_rename_brace_form_keeps_hunks(tmp_path):
    """目录级 rename 触发 brace 简写 numstat (a/{b => c}/d.txt) 时, hunks 仍对齐."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    old = repo / "a" / "b" / "d.txt"
    old.parent.mkdir(parents=True)
    old.write_text("line1\nline2\nline3\n", encoding="utf-8")
    _git(repo, "add", "a/b/d.txt")
    _git(repo, "commit", "-m", "initial")

    # a/b/d.txt -> a/c/d.txt: 共同前缀 a/ 和后缀 /d.txt, numstat 输出 a/{b => c}/d.txt
    new = repo / "a" / "c" / "d.txt"
    new.parent.mkdir(parents=True)
    _git(repo, "mv", "a/b/d.txt", "a/c/d.txt")
    new.write_text("line1\nCHANGED\nline3\n", encoding="utf-8")
    _git(repo, "add", "a/c/d.txt")

    diff = DiffService().get_git_diff(str(repo))

    assert diff is not None
    assert str(new) in diff["files"]
    file_info = diff["files"][str(new)]
    assert file_info["linesAdded"] == 1
    assert file_info["linesRemoved"] == 1
    # brace 简写 numstat key "a/{b => c}/d.txt" 需展开为 "a/c/d.txt" 与 hunk key 对齐
    assert len(file_info["hunks"]) == 1
    assert file_info["hunks"][0]["lines"]  # 非空


def test_git_diff_summary_does_not_run_full_patch(monkeypatch):
    from jiuwenswarm.server.utils import diff_service as ds_mod

    calls: list[tuple[str, ...]] = []

    def fake_run_git_command(project_dir, args):
        calls.append(tuple(args))
        if args == ["diff", "HEAD", "--shortstat"]:
            return " 1 file changed, 2 insertions(+), 1 deletion(-)\n"
        if args == ["-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard"]:
            return ""
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(ds_mod.DiffService, "_get_git_toplevel", staticmethod(lambda p: "/repo"))
    monkeypatch.setattr(ds_mod.DiffService, "_is_in_transient_git_state", staticmethod(lambda p: False))
    monkeypatch.setattr(ds_mod.DiffService, "_run_git_command", staticmethod(fake_run_git_command))
    diff = ds_mod.DiffService().get_git_diff(
        "/repo",
        include_files=False,
        include_hunks=False,
    )

    assert diff == {
        "stats": {"filesChanged": 1, "linesAdded": 2, "linesRemoved": 1},
        "files": {},
    }
    assert ("diff", "HEAD") not in calls
    assert ("diff", "HEAD", "--numstat") not in calls


def test_git_diff_files_layer_does_not_run_full_patch(monkeypatch):
    from jiuwenswarm.server.utils import diff_service as ds_mod

    calls: list[tuple[str, ...]] = []

    def fake_run_git_command(project_dir, args):
        calls.append(tuple(args))
        if args == ["diff", "HEAD", "--shortstat"]:
            return " 1 file changed, 2 insertions(+), 1 deletion(-)\n"
        if args == ["diff", "HEAD", "--numstat"]:
            return "2\t1\ta.txt\n"
        if args == ["diff", "HEAD", "--name-status"]:
            return "M\ta.txt\n"
        if args == ["-c", "core.quotepath=false", "status", "--porcelain=v1"]:
            return " M a.txt\n"
        if args == ["-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard"]:
            return ""
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(ds_mod.DiffService, "_get_git_toplevel", staticmethod(lambda p: "/repo"))
    monkeypatch.setattr(ds_mod.DiffService, "_is_in_transient_git_state", staticmethod(lambda p: False))
    monkeypatch.setattr(ds_mod.DiffService, "_run_git_command", staticmethod(fake_run_git_command))

    diff = ds_mod.DiffService().get_git_diff(
        "/repo",
        include_files=True,
        include_hunks=False,
    )

    assert diff is not None
    assert diff["files"][str(Path("/repo") / "a.txt")]["hunks"] == []
    assert ("diff", "HEAD") not in calls


def test_git_diff_detail_layer_limits_patch_to_requested_paths(monkeypatch):
    from jiuwenswarm.server.utils import diff_service as ds_mod

    calls: list[tuple[str, ...]] = []

    def fake_run_git_command(project_dir, args):
        calls.append(tuple(args))
        if args == ["diff", "HEAD", "--shortstat"]:
            return " 2 files changed, 2 insertions(+), 2 deletions(-)\n"
        if args == ["diff", "HEAD", "--numstat"]:
            return "1\t1\ta.txt\n1\t1\tb.txt\n"
        if args == ["diff", "HEAD", "--name-status"]:
            return "M\ta.txt\nM\tb.txt\n"
        if args == ["-c", "core.quotepath=false", "status", "--porcelain=v1"]:
            return " M a.txt\n M b.txt\n"
        if args == ["--literal-pathspecs", "diff", "HEAD", "--", "a.txt"]:
            return (
                "diff --git a/a.txt b/a.txt\n"
                "--- a/a.txt\n"
                "+++ b/a.txt\n"
                "@@ -1 +1 @@\n"
                "-old\n"
                "+new\n"
            )
        if args == ["-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard"]:
            return ""
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(ds_mod.DiffService, "_get_git_toplevel", staticmethod(lambda p: "/repo"))
    monkeypatch.setattr(ds_mod.DiffService, "_is_in_transient_git_state", staticmethod(lambda p: False))
    monkeypatch.setattr(ds_mod.DiffService, "_run_git_command", staticmethod(fake_run_git_command))
    monkeypatch.setattr(
        ds_mod.DiffService,
        "_run_git_diff_limited",
        staticmethod(lambda project_dir, args: (fake_run_git_command(project_dir, args), False)),
    )

    diff = ds_mod.DiffService().get_git_diff(
        "/repo",
        include_files=True,
        include_hunks=True,
        hunk_paths=["a.txt"],
    )

    assert diff is not None
    assert diff["files"][str(Path("/repo") / "a.txt")]["hunks"]
    assert diff["files"][str(Path("/repo") / "b.txt")]["hunks"] == []
    assert ("diff", "HEAD") not in calls
    assert ("--literal-pathspecs", "diff", "HEAD", "--", "a.txt") in calls


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")


def test_git_diff_untracked_large_file_marked_large(tmp_path):
    """超过 1MB 的 untracked 文件标记 isLargeFile、不返回内容（与 tracked 大文件对齐）。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-m", "init")

    big = repo / "big.txt"
    content = "\n".join(f"line {i}: " + "x" * 250 for i in range(5000)) + "\n"
    big.write_bytes(content.encode("utf-8"))
    assert big.stat().st_size > MAX_DIFF_SIZE_BYTES

    diff = DiffService().get_git_diff(str(repo))
    assert diff is not None
    info = diff["files"][str(big)]
    assert info["isUntracked"] is True
    assert info["isLargeFile"] is True
    assert info["hunks"] == []
    assert info["isTruncated"] is False
    assert info["linesAdded"] == 5000


def test_git_diff_untracked_file_under_1mb_shows_full(tmp_path):
    """1MB 以内的 untracked 文件整文件返回，不做 400 行截断。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-m", "init")

    small = repo / "small.txt"
    content = "\n".join(f"line {i}: " + "y" * 50 for i in range(500)) + "\n"
    small.write_bytes(content.encode("utf-8"))
    assert small.stat().st_size < MAX_DIFF_SIZE_BYTES

    diff = DiffService().get_git_diff(str(repo))
    assert diff is not None
    info = diff["files"][str(small)]
    assert info["isUntracked"] is True
    assert info["isLargeFile"] is False
    assert info["isTruncated"] is False
    assert len(info["hunks"]) == 1
    assert len(info["hunks"][0]["lines"]) == 500
    assert info["linesAdded"] == 500


def test_git_diff_tracked_large_file_marked_large(tmp_path):
    """超过 1MB 的 tracked 改动标记 isLargeFile、不返回内容。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracked = repo / "tracked.txt"
    tracked.write_bytes(b"initial\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "init")

    content = "\n".join(f"line {i}: " + "x" * 250 for i in range(5000)) + "\n"
    tracked.write_bytes(content.encode("utf-8"))
    assert tracked.stat().st_size > MAX_DIFF_SIZE_BYTES

    diff = DiffService().get_git_diff(str(repo))
    assert diff is not None
    info = diff["files"][str(tracked)]
    assert info["isLargeFile"] is True
    assert info["hunks"] == []
    assert info["isTruncated"] is False


def test_git_diff_tracked_file_under_1mb_shows_full(tmp_path):
    """1MB 以内的 tracked 改动整段返回，不做 400 行截断。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracked = repo / "tracked.txt"
    tracked.write_bytes(b"initial\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "init")

    content = "\n".join(f"line {i}: " + "y" * 50 for i in range(500)) + "\n"
    tracked.write_bytes(content.encode("utf-8"))
    assert tracked.stat().st_size < MAX_DIFF_SIZE_BYTES

    diff = DiffService().get_git_diff(str(repo))
    assert diff is not None
    info = diff["files"][str(tracked)]
    assert info["isLargeFile"] is False
    assert info["isTruncated"] is False
    assert len(info["hunks"]) == 1
    added = [
        line for line in info["hunks"][0]["lines"]
        if line.startswith("+") and not line.startswith("+++")
    ]
    assert len(added) == 500


def test_git_diff_tracked_large_file_small_edit_shows_preview(tmp_path):
    """磁盘文件超过 1MB 但实际 diff 很小：按 diff 大小判定，仍返回预览。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracked = repo / "tracked.txt"
    content = "\n".join(f"line {i}: " + "x" * 250 for i in range(5000)) + "\n"
    tracked.write_bytes(content.encode("utf-8"))
    assert tracked.stat().st_size > MAX_DIFF_SIZE_BYTES
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "init")

    lines = content.splitlines()
    lines[2500] = "line 2500: " + "y" * 250
    tracked.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    diff = DiffService().get_git_diff(str(repo))
    assert diff is not None
    info = diff["files"][str(tracked)]
    assert info["isLargeFile"] is False
    assert info["isTruncated"] is False
    assert info["linesAdded"] == 1
    assert info["linesRemoved"] == 1
    assert info["hunks"]
    added = [
        line for line in info["hunks"][0]["lines"]
        if line.startswith("+") and not line.startswith("+++")
    ]
    assert added == ["+line 2500: " + "y" * 250]


@pytest.fixture
def _sessions_dir(tmp_path, monkeypatch):
    """Mock get_agent_sessions_dir 返回 tmp_path/sessions。"""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    from jiuwenswarm.server.utils import diff_service as ds_mod
    monkeypatch.setattr(ds_mod, "get_agent_sessions_dir", lambda: sessions)
    return sessions


def _write_metadata(sessions_dir: Path, session_id: str, payload: dict) -> None:
    sdir = sessions_dir / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "metadata.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def test_get_project_dir_prefers_channel_metadata_cwd(_sessions_dir):
    """channel_metadata.cwd 命中时优先返回(向后兼容,不破坏旧路径)。"""
    _write_metadata(_sessions_dir, "s1", {
        "channel_metadata": {"cwd": "/legacy/cwd"},
        "project_dir": "/top/level",
    })
    assert DiffService._get_project_dir_from_metadata("s1") == "/legacy/cwd"


def test_get_project_dir_falls_back_to_top_level_project_dir(_sessions_dir):
    """P1 修复核心:旧 cwd 字段缺失时回退顶层 project_dir。

    覆盖 Web/code 模式新会话:init_session_metadata 写顶层 project_dir,
    但 channel_metadata 不含 cwd,旧实现会漏读导致 file_ops 漏扫项目目录。
    """
    _write_metadata(_sessions_dir, "s2", {
        "project_dir": "/top/level",
        "channel_metadata": {},
    })
    assert DiffService._get_project_dir_from_metadata("s2") == "/top/level"


def test_get_project_dir_returns_none_when_all_missing(_sessions_dir):
    """所有字段缺失时返回 None(向后兼容旧行为,不抛异常)。"""
    _write_metadata(_sessions_dir, "s3", {"channel_metadata": {}})
    assert DiffService._get_project_dir_from_metadata("s3") is None
