# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real Git work trees queried through production Ops and USER handlers."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest
from openjiuwen.core.events import Event, EventHandlerOpsBuilder, SessionContext

from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.handler_ops import session_ops
from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput
from jiuwenswarm.server.runtime.session import session_repo
from jiuwenswarm.server.runtime.session.session_repo import SessionRepoError
from .management.support import write_package


REPO_EXTENSION = """\
class Extension:
    def register(self, registrar):
        registrar.register_event_handler("ui.ready", self.handle, {})

    async def handle(self, event, ops, next_handler):
        info = await ops.session.repo()
        receipt = await next_handler(event)
        return {
            "repo": info,
            "cwd": await ops.session.cwd(),
            "root": await ops.session.root(),
            "model": await ops.session.model(),
            "package": ops.package_metadata.package_id,
            "accepted": receipt.accepted,
            "ui_available": callable(ops.ui.render),
        }


def create_extension():
    return Extension()
"""


def _git(directory: Path, *arguments: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *arguments],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=10,
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("real Git executable required")
    directory = tmp_path / "仓库 with spaces"
    directory.mkdir()
    _git(directory, "init", "-b", "feature/repo-test")
    (directory / "readme.txt").write_text("first\n", encoding="utf-8")
    _git(directory, "add", "readme.txt")
    _git(directory, "-c", "commit.gpgsign=false", "commit", "-m", "initial")
    (directory / "src").mkdir()
    return directory


def _ops(cwd: object, metadata: dict | None = None):
    return EventHandlerOpsBuilder().build(session_context=SessionContext("repo-session", cwd, metadata or {}))


def _ui_event(payload: dict | None = None) -> Event:
    return Event("ui.ready", UiActionInput("ui.ready", RenderTarget("repo-session", "chat.header"), payload or {}))


@pytest.fixture
async def repo_host(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "store")
    instance = await assembly.install_extensions()
    assert instance is not None
    package = write_package(tmp_path / "source", package_id="repo.reader", module_source=REPO_EXTENSION)
    try:
        reports = await instance.manager.install([package])
        assert reports[0].status == "SUCCESS"
        yield instance
    finally:
        await assembly.shutdown_extensions()


@pytest.mark.asyncio
async def test_user_package_reads_real_repo_and_existing_ops(repo_host, repository: Path) -> None:
    context = SessionContext("repo-session", str(repository / "src"), {"project_root": str(repository / "src")})
    before = {name: (repository / ".git" / name).read_bytes() for name in ("HEAD", "config", "index")}
    result = await repo_host.bus.trigger(_ui_event({"cwd": str(repository.parent), "repo": "forged"}), context)
    assert result == {
        "repo": {"root": str(repository.resolve()), "remote": None, "internal": False, "name": None},
        "cwd": context.cwd,
        "root": context.cwd,
        "model": None,
        "package": "repo.reader",
        "accepted": True,
        "ui_available": True,
    }
    assert {name: (repository / ".git" / name).read_bytes() for name in before} == before


@pytest.mark.asyncio
async def test_query_facts_are_fresh_and_results_are_not_shared(repository: Path) -> None:
    ops = _ops(str(repository))
    first = await session_ops.session_repo(ops)
    original_head = first["head"]
    first["repo_root"] = "user mutation"
    _git(repository, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "second")
    second = await session_ops.session_repo(ops)
    assert second["repo_root"] == str(repository.resolve())
    assert second["head"] != original_head
    assert second["head"] == _git(repository, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_branch_name_is_not_affected_by_same_named_tag(repository: Path) -> None:
    _git(repository, "tag", "feature/repo-test")
    info = await session_ops.session_repo(_ops(str(repository)))
    assert info["branch"] == "feature/repo-test"


@pytest.mark.asyncio
async def test_detached_head_does_not_invent_branch(repository: Path) -> None:
    head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "--detach", head)
    assert await session_ops.session_repo(_ops(str(repository / "src"))) == {
        "repo_root": str(repository.resolve()), "branch": None, "head": head, "detached": True,
    }


@pytest.mark.asyncio
async def test_unborn_repository_reports_branch_without_commit(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "unborn")
    assert await session_ops.session_repo(_ops(str(tmp_path))) == {
        "repo_root": str(tmp_path.resolve()), "branch": "unborn", "head": None, "detached": False,
    }


@pytest.mark.asyncio
async def test_linked_worktree_returns_its_own_root(repository: Path, tmp_path: Path) -> None:
    linked = tmp_path / "linked 工作树"
    _git(repository, "worktree", "add", "-b", "feature/linked", str(linked))
    (linked / "child").mkdir()
    assert (linked / ".git").is_file()
    assert await session_ops.session_repo(_ops(str(linked / "child"))) == {
        "repo_root": str(linked.resolve()), "branch": "feature/linked",
        "head": _git(repository, "rev-parse", "HEAD"), "detached": False,
    }


@pytest.mark.asyncio
async def test_nonrepo_is_not_initialized(tmp_path: Path) -> None:
    assert await session_ops.session_repo(_ops(str(tmp_path))) is None
    assert not (tmp_path / ".git").exists()


@pytest.mark.asyncio
async def test_bare_repository_has_no_working_tree(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--bare")
    assert await session_ops.session_repo(_ops(str(tmp_path))) is None


@pytest.mark.asyncio
async def test_missing_cwd_does_not_run_git_or_use_process_cwd(monkeypatch) -> None:
    def unexpected_read(cwd):
        raise AssertionError("no cwd must not probe the process directory")

    monkeypatch.setattr(session_ops, "read_session_repo", unexpected_read)
    assert await session_ops.session_repo(_ops(None)) is None


@pytest.mark.asyncio
async def test_session_is_required() -> None:
    with pytest.raises(RuntimeError, match="requires SessionContext"):
        await session_ops.session_repo(EventHandlerOpsBuilder().build())


@pytest.mark.asyncio
@pytest.mark.parametrize("value,error", [("relative", ValueError), ("", ValueError), (42, TypeError)])
async def test_invalid_bound_cwd_is_rejected(value, error) -> None:
    with pytest.raises(error):
        await session_ops.session_repo(_ops(value))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "file"])
async def test_unavailable_workspace_is_explicit(tmp_path: Path, kind: str) -> None:
    target = tmp_path / kind
    if kind == "file":
        target.write_text("file", encoding="utf-8")
    with pytest.raises(SessionRepoError) as caught:
        await session_ops.session_repo(_ops(str(target)))
    assert caught.value.code == "WORKSPACE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_git_missing_is_explicit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_repo.shutil, "which", lambda name: None)
    with pytest.raises(SessionRepoError) as caught:
        await session_ops.session_repo(_ops(str(tmp_path)))
    assert caught.value.code == "GIT_NOT_FOUND"


@pytest.mark.asyncio
async def test_timeout_is_explicit_without_raw_output(tmp_path: Path, monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("secret command", kwargs["timeout"], stderr="private output")

    monkeypatch.setattr(session_repo.subprocess, "run", timeout)
    with pytest.raises(SessionRepoError) as caught:
        await session_ops.session_repo(_ops(str(tmp_path)))
    assert caught.value.code == "GIT_QUERY_TIMEOUT"
    assert "private" not in str(caught.value) and caught.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stderr,code", [
    ("fatal: detected dubious ownership in repository secret-path", "GIT_DUBIOUS_OWNERSHIP"),
    ("fatal: private config error", "GIT_QUERY_FAILED"),
])
async def test_git_failures_are_sanitized(tmp_path: Path, monkeypatch, stderr: str, code: str) -> None:
    monkeypatch.setattr(session_repo.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 128, "", stderr))
    with pytest.raises(SessionRepoError) as caught:
        await session_ops.session_repo(_ops(str(tmp_path)))
    assert caught.value.code == code
    assert "secret-path" not in str(caught.value) and "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_git_environment_cannot_redirect_to_another_repository(repository: Path, tmp_path: Path, monkeypatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-b", "wrong")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(repository))
    info = await session_ops.session_repo(_ops(str(repository / "src")))
    assert info["repo_root"] == str(repository.resolve())
    assert info["branch"] == "feature/repo-test"


@pytest.mark.asyncio
async def test_concurrent_user_handlers_keep_their_own_worktree(repo_host, repository: Path, tmp_path: Path) -> None:
    linked = tmp_path / "second-session"
    _git(repository, "worktree", "add", "-b", "second", str(linked))
    contexts = [SessionContext("repo-session", str(path)) for path in (repository, linked)]
    results = await asyncio.gather(*(repo_host.bus.trigger(_ui_event(), context) for context in contexts))
    assert [result["repo"]["root"] for result in results] == [str(repository.resolve()), str(repository.resolve())]
    assert [result["cwd"] for result in results] == [str(repository), str(linked)]


@pytest.mark.asyncio
async def test_user_reload_and_uninstall_leave_platform_capability(repo_host, repository: Path) -> None:
    reloaded = await repo_host.manager.reload("repo.reader")
    assert reloaded.status == "SUCCESS"
    context = SessionContext("repo-session", str(repository))
    assert (await repo_host.bus.trigger(_ui_event(), context))["repo"]["root"] == str(repository.resolve())
    removed = await repo_host.manager.uninstall("repo.reader")
    assert removed.status == "SUCCESS"
    assert (await repo_host.bus.trigger(_ui_event(), context)).status == "ready"

    async def reader(event, ops, next_handler):
        return await ops.session.repo()

    repo_host.bus.register_event_handler("ui.ready", reader, {})
    assert (await repo_host.bus.trigger(_ui_event(), context))["root"] == str(repository.resolve())


@pytest.mark.asyncio
async def test_git_query_runs_off_event_loop(tmp_path: Path, monkeypatch) -> None:
    loop_thread = threading.get_ident()

    def read(cwd):
        assert threading.get_ident() != loop_thread
        return {"repo_root": cwd}

    monkeypatch.setattr(session_ops, "read_session_repo", read)
    assert await session_ops.session_repo(_ops(str(tmp_path))) == {"repo_root": str(tmp_path)}


def test_all_git_commands_share_one_deadline(tmp_path: Path, monkeypatch) -> None:
    clock = iter([10.0, 10.0, 11.0, 15.1])
    monkeypatch.setattr(session_repo.time, "monotonic", lambda: next(clock))
    timeouts = []

    def run(arguments, **kwargs):
        timeouts.append(kwargs["timeout"])
        output = "true\n" if len(timeouts) == 1 else str(tmp_path) + "\n"
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(session_repo.subprocess, "run", run)
    with pytest.raises(SessionRepoError) as caught:
        session_repo.read_session_repo(str(tmp_path))
    assert caught.value.code == "GIT_QUERY_TIMEOUT"
    assert timeouts == [5.0, 4.0]
