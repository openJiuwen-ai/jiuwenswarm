"""``skilldev.file.read`` 的路径守卫必须把读取范围锁在 skill 目录内."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.skill.skilldev.deps import SkillDevDeps
from jiuwenswarm.server.runtime.skill.skilldev.service import SkillDevService
from jiuwenswarm.server.runtime.skill.skilldev.workspace import WorkspaceProvider


def _service(base_dir) -> SkillDevService:
    deps = SkillDevDeps(
        model_name="test-model",
        model_client_config={},
        mcp_tools_factory=lambda: [],
        sysop_config=None,
        state_store=None,
        workspace_provider=WorkspaceProvider(base_dir),
    )
    return SkillDevService(deps)


async def _read(service: SkillDevService, task_id: str, path: str):
    request = AgentRequest(
        request_id="r",
        channel_id="web",
        session_id=None,
        req_method=ReqMethod.SKILLDEV_FILE_READ,
        params={"task_id": task_id, "path": path},
    )
    return [chunk async for chunk in service.handle(request)]


@pytest.mark.asyncio
async def test_file_read_rejects_sibling_skill_directory_escape(tmp_path) -> None:
    """../skill_backup 这类同级 skill* 目录不能因裸 startswith 前缀被放行。"""
    (tmp_path / "task-1" / "skill").mkdir(parents=True)
    (tmp_path / "task-1" / "skill" / "SKILL.md").write_text("ok", encoding="utf-8")
    (tmp_path / "task-1" / "skill_backup").mkdir()
    (tmp_path / "task-1" / "skill_backup" / "secret.txt").write_text(
        "secret", encoding="utf-8"
    )

    chunks = await _read(_service(tmp_path), "task-1", "../skill_backup/secret.txt")
    payload = chunks[-1].payload
    assert payload.get("event_type") == "skilldev.error"
    assert "secret" not in str(payload)


@pytest.mark.asyncio
async def test_file_read_rejects_traversal_outside_workspace(tmp_path) -> None:
    (tmp_path / "task-1" / "skill").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "leak.txt").write_text("leak", encoding="utf-8")

    chunks = await _read(
        _service(tmp_path), "task-1", f"../../elsewhere/{outside.name}/leak.txt"
    )
    assert chunks[-1].payload.get("event_type") == "skilldev.error"

    absolute = await _read(_service(tmp_path), "task-1", str(outside / "leak.txt"))
    assert absolute[-1].payload.get("event_type") == "skilldev.error"


@pytest.mark.asyncio
async def test_file_read_still_returns_files_inside_skill_dir(tmp_path) -> None:
    (tmp_path / "task-1" / "skill" / "assets").mkdir(parents=True)
    (tmp_path / "task-1" / "skill" / "SKILL.md").write_text("ok", encoding="utf-8")
    (tmp_path / "task-1" / "skill" / "assets" / "a.txt").write_text(
        "aaa", encoding="utf-8"
    )

    service = _service(tmp_path)
    root = await _read(service, "task-1", "SKILL.md")
    assert root[-1].payload.get("ok") is True
    assert root[-1].payload.get("content") == "ok"

    nested = await _read(service, "task-1", "assets/a.txt")
    assert nested[-1].payload.get("ok") is True
    assert nested[-1].payload.get("content") == "aaa"
