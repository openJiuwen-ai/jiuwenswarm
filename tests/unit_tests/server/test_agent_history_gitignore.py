import asyncio
import subprocess
import threading
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.utils.diff_service import DiffService


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.mark.asyncio
async def test_runtime_state_git_probe_is_non_blocking_and_coalesced(monkeypatch):
    adapter = JiuWenSwarmDeepAdapter()
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def _slow_write(**_kwargs) -> None:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(adapter, "_write_runtime_state", _slow_write)
    adapter._schedule_runtime_state_write(
        mode="agent",
        language="zh",
        channel="web",
        session_id="web_runtime_state",
        project_dir=None,
    )
    await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
    first_task = adapter._runtime_state_write_task
    assert first_task is not None and not first_task.done()

    adapter._schedule_runtime_state_write(
        mode="agent",
        language="zh",
        channel="web",
        session_id="web_runtime_state",
        project_dir=None,
    )
    assert adapter._runtime_state_write_task is first_task
    assert calls == 1

    release.set()
    await asyncio.wait_for(first_task, timeout=1)


def test_ensure_project_gitignore_agent_history_updates_repo_root(tmp_path):
    repo = tmp_path / "repo"
    subdir = repo / "pkg"
    subdir.mkdir(parents=True)
    _git(repo, "init")

    JiuWenSwarmDeepAdapter._ensure_project_gitignore_agent_history(str(subdir))
    JiuWenSwarmDeepAdapter._ensure_project_gitignore_agent_history(str(subdir))

    gitignore = repo / ".gitignore"
    assert gitignore.exists()
    content = gitignore.read_text(encoding="utf-8")
    assert content.count(".agent_history/") == 1


@pytest.mark.parametrize(
    "existing_rule",
    [
        ".agent_history",
        ".agent_history/",
        ".agent_history/*",
        ".agent_history/**",
        ".agent_history/**/*",
        "**/.agent_history",
        "**/.agent_history/",
        "**/.agent_history/*",
    ],
)
def test_ensure_project_gitignore_agent_history_idempotent_for_equivalent_rules(
    tmp_path, existing_rule
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")

    gitignore = repo / ".gitignore"
    gitignore.write_text(f"build/\n{existing_rule}\n", encoding="utf-8")

    before = gitignore.read_bytes()

    JiuWenSwarmDeepAdapter._ensure_project_gitignore_agent_history(str(repo))
    JiuWenSwarmDeepAdapter._ensure_project_gitignore_agent_history(str(repo))

    after = gitignore.read_bytes()
    assert before == after, (
        f".gitignore should remain unchanged for equivalent rule {existing_rule!r}"
    )


def test_ensure_project_gitignore_agent_history_preserves_crlf(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")

    gitignore = repo / ".gitignore"
    original = b"build/\r\ndist/\r\n"
    gitignore.write_bytes(original)

    JiuWenSwarmDeepAdapter._ensure_project_gitignore_agent_history(str(repo))

    after = gitignore.read_bytes()
    # Original CRLF lines must be preserved verbatim.
    assert after.startswith(original)
    # Appended rule uses LF (consistent with the rest of the addition) and is
    # separated from existing content by exactly one blank line.
    assert after.endswith(b"# JiuwenSwarm runtime file operation logs\n.agent_history/\n")
    assert after.count(b".agent_history/") == 1


def test_git_diff_excludes_unignored_agent_history(tmp_path):
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
    history_dir = repo / ".agent_history"
    history_dir.mkdir()
    (history_dir / "file_ops_jiuwenswarm_sess.json").write_text(
        "{\n  \"file.txt\": []\n}\n",
        encoding="utf-8",
    )

    diff = DiffService().get_git_diff(str(repo))

    assert diff is not None
    assert diff["stats"] == {"filesChanged": 1, "linesAdded": 1, "linesRemoved": 1}
    assert str(history_dir / "file_ops_jiuwenswarm_sess.json") not in diff["files"]


def test_gitignore_probe_never_spawns_subprocess(tmp_path, monkeypatch):
    """create_instance 卡死事故回归守护：git 探测必须为纯文件系统实现.

    Windows 上 subprocess.run 超时 kill 后会无超时 communicate 等管道 EOF，
    管道写端一旦被其他存活进程继承便永久悬挂。因此仓库探测禁止再引入
    任何子进程调用。
    """

    def _forbidden(*args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("gitignore probe must not spawn subprocesses")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    # 非仓库目录：静默返回
    plain = tmp_path / "plain"
    plain.mkdir()
    JiuWenSwarmDeepAdapter._ensure_project_gitignore_agent_history(str(plain))
    assert not (plain / ".gitignore").exists()

    # 有 .git 目录的仓库：正常写入（不依赖 git 可执行文件）
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    JiuWenSwarmDeepAdapter._ensure_project_gitignore_agent_history(str(repo))
    content = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".agent_history/" in content


def test_find_git_worktree_root_supports_worktree_file(tmp_path):
    """worktree/submodule 的 .git 是文件不是目录，也必须识别."""
    repo = tmp_path / "worktree"
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: ../elsewhere\n", encoding="utf-8")

    assert JiuWenSwarmDeepAdapter._find_git_worktree_root(str(nested)) == str(repo)
    assert JiuWenSwarmDeepAdapter._find_git_worktree_root(str(tmp_path / "none")) is None


@pytest.mark.asyncio
async def test_gitignore_housekeeping_is_fire_and_forget(monkeypatch, tmp_path):
    """后台调度不得阻塞调用方，异常也不得外抛."""
    adapter = JiuWenSwarmDeepAdapter()
    started = threading.Event()
    release = threading.Event()

    def _slow(_project_dir) -> None:
        started.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(adapter, "_ensure_project_gitignore_agent_history", _slow)
    adapter._schedule_project_gitignore_agent_history(str(tmp_path))
    # 调度立即返回，任务在后台等待
    await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
    tasks = [t for t in adapter._housekeeping_tasks if not t.done()]
    assert len(tasks) == 1

    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)

    # 失败路径：housekeeping 抛错被吞掉，task 正常结束
    def _boom(_project_dir) -> None:
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(adapter, "_ensure_project_gitignore_agent_history", _boom)
    adapter._schedule_project_gitignore_agent_history(str(tmp_path))
    failing = [t for t in adapter._housekeeping_tasks if not t.done()]
    await asyncio.wait_for(asyncio.gather(*failing), timeout=1)
    assert all(t.exception() is None for t in failing)
