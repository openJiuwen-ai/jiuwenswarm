from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill import skill_manager as skill_manager_module
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


@pytest.mark.parametrize("method_name", ["_git_clone", "_git_pull"])
def test_marketplace_git_operation_times_out_and_reaps_process(
    method_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the internal timeout must leave the call stuck past the deadline."""
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    clone_path = tmp_path / "clone"
    repository_path = tmp_path / "repository"
    if method_name == "_git_pull":
        repository_path.mkdir()
        (repository_path / "keep-me").write_text("existing repository", encoding="utf-8")

    async def exercise() -> tuple[str | None, list[asyncio.subprocess.Process]]:
        original_create_subprocess_exec = asyncio.create_subprocess_exec
        processes: list[asyncio.subprocess.Process] = []

        async def start_hanging_process(*args, **kwargs):
            if method_name == "_git_clone":
                process_dest = Path(args[-1])
                process_dest.mkdir()
                (process_dest / "partial").write_text("incomplete clone", encoding="utf-8")
            process = await original_create_subprocess_exec(
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
                stdout=kwargs.get("stdout"),
                stderr=kwargs.get("stderr"),
            )
            processes.append(process)
            return process

        monkeypatch.setattr(
            skill_manager_module,
            "_MARKETPLACE_GIT_TIMEOUT_SECONDS",
            0.05,
            raising=False,
        )
        monkeypatch.setattr(
            skill_manager_module.asyncio,
            "create_subprocess_exec",
            start_hanging_process,
        )

        method = getattr(manager, method_name)
        operation = (
            method("https://example.invalid/marketplace.git", clone_path)
            if method_name == "_git_clone"
            else method(repository_path)
        )
        try:
            result = await asyncio.wait_for(operation, timeout=0.5)
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
        return result, processes

    result, processes = asyncio.run(exercise())

    assert result is None
    assert processes
    assert all(process.returncode is not None for process in processes)
    if method_name == "_git_clone":
        assert not clone_path.exists()
        assert not list(tmp_path.glob(".clone.clone-*"))
    else:
        assert (repository_path / "keep-me").read_text(encoding="utf-8") == "existing repository"


def test_concurrent_marketplace_clones_do_not_delete_each_others_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    clone_path = tmp_path / "clone"

    class SuccessfulClone:
        returncode: int | None = None

        async def communicate(self):
            await asyncio.sleep(0.05)
            self.returncode = 0
            return b"", b""

    async def start_clone(*args, **_kwargs):
        process_dest = Path(args[-1])
        process_dest.mkdir()
        (process_dest / ".git").mkdir()
        return SuccessfulClone()

    async def get_commit(repo_path: Path) -> str | None:
        return "commit-123" if (repo_path / ".git").is_dir() else None

    monkeypatch.setattr(skill_manager_module.asyncio, "create_subprocess_exec", start_clone)
    monkeypatch.setattr(manager, "_git_get_commit", get_commit)

    async def exercise() -> list[str | None]:
        return await asyncio.gather(
            manager._git_clone("https://example.invalid/marketplace.git", clone_path),
            manager._git_clone("https://example.invalid/marketplace.git", clone_path),
        )

    results = asyncio.run(exercise())

    assert clone_path.is_dir()
    assert (clone_path / ".git").is_dir()
    assert not list(tmp_path.glob(".clone.clone-*"))
    assert results == ["commit-123", "commit-123"]
