# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.memory_catalog import (
    MemoryCatalogError,
    MemoryScopeInput,
    ResolvedMemoryScope,
    ensure_trusted_project,
    list_memory_sources,
)
from jiuwenswarm.runtime.session_provisioner import SessionDescriptor


def _started_runtime() -> AgentRuntime:
    runtime = AgentRuntime(initializer=AsyncMock())
    runtime._started = True
    return runtime


@pytest.mark.asyncio
async def test_memory_status_does_not_start_manager_index_or_watcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _started_runtime()
    forbidden = AsyncMock(side_effect=AssertionError("must not start manager"))
    monkeypatch.setattr(runtime.agent_manager, "get_agent", forbidden)

    result = await runtime.get_memory_status(
        MemoryScopeInput(
            channel_id="process_cli",
            project_dir=str(tmp_path),
            trusted_dirs=(str(tmp_path),),
        )
    )

    assert result.index_inspected is False
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_binding_overrides_caller_scope_and_hides_other_channel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bound = tmp_path / "bound"
    supplied = tmp_path / "supplied"
    bound.mkdir()
    supplied.mkdir()
    runtime = _started_runtime()
    descriptor = SessionDescriptor(
        session_id="owned",
        channel_id="process_cli",
        mode="team.code.normal",
        work_mode="code",
        project_dir=str(bound),
    )
    monkeypatch.setattr(
        runtime,
        "describe_session",
        AsyncMock(return_value=descriptor),
    )

    result = await runtime.get_memory_locations(
        MemoryScopeInput(
            channel_id="process_cli",
            session_id="owned",
            mode="agent.work.normal",
            project_dir=str(supplied),
        )
    )
    assert Path(result.project_dir) == bound.resolve()

    monkeypatch.setattr(
        runtime,
        "describe_session",
        AsyncMock(
            return_value=SessionDescriptor(
                session_id="foreign",
                channel_id="web",
                mode="agent.code.normal",
                work_mode="code",
                project_dir=str(bound),
            )
        ),
    )
    with pytest.raises(MemoryCatalogError) as caught:
        await runtime.get_memory_locations(
            MemoryScopeInput(
                channel_id="process_cli",
                session_id="foreign",
                project_dir=str(supplied),
                trusted_dirs=(str(tmp_path),),
            )
        )
    assert caught.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_sessionless_project_requires_real_trusted_root(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted"
    project = trusted / "project"
    collision = tmp_path / "trusted-other"
    project.mkdir(parents=True)
    collision.mkdir()
    runtime = _started_runtime()

    result = await runtime.get_memory_locations(
        MemoryScopeInput(
            channel_id="process_cli",
            project_dir=str(project),
            trusted_dirs=(str(tmp_path / "stale-root"), str(trusted)),
        )
    )
    assert Path(result.project_dir) == project.resolve()

    with pytest.raises(MemoryCatalogError) as caught:
        await runtime.get_memory_locations(
            MemoryScopeInput(
                channel_id="process_cli",
                project_dir=str(collision),
                trusted_dirs=(str(trusted),),
            )
        )
    assert caught.value.code == "FORBIDDEN"


def test_directory_link_cannot_escape_trusted_root(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    outside = tmp_path / "outside"
    link = trusted / "linked-project"
    trusted.mkdir()
    outside.mkdir()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")

    with pytest.raises(MemoryCatalogError) as caught:
        ensure_trusted_project(link.resolve(strict=True), (str(trusted),))
    assert caught.value.code == "FORBIDDEN"


def _patch_memory_roots(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace: Path,
    user_home: Path,
) -> None:
    from jiuwenswarm.common import coding_memory_paths, utils

    monkeypatch.setattr(utils, "get_agent_workspace_dir", lambda: workspace)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    original = coding_memory_paths.resolve_project_coding_memory_dir
    monkeypatch.setattr(
        coding_memory_paths,
        "resolve_project_coding_memory_dir",
        lambda *, agent_workspace_dir, project_dir: original(
            agent_workspace_dir=workspace,
            project_dir=project_dir,
        ),
    )


def test_catalog_lists_only_known_markdown_and_excludes_file_link_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    user_home = tmp_path / "home"
    outside = tmp_path / "outside-secret.md"
    (project / ".jiuwen" / "rules").mkdir(parents=True)
    workspace.mkdir()
    user_home.mkdir()
    secret = "sk-memory-catalog-must-not-leak"
    (project / "JIUWENSWARM.md").write_text(secret, encoding="utf-8")
    (project / "notes.md").write_text("not a memory source", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    link = project / ".jiuwen" / "rules" / "escape.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pass
    _patch_memory_roots(
        monkeypatch,
        workspace=workspace,
        user_home=user_home,
    )

    def reject_content_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Memory catalog must not read file content")

    monkeypatch.setattr(Path, "open", reject_content_read)

    result = list_memory_sources(
        ResolvedMemoryScope(
            channel_id="process_cli",
            session_id="",
            mode="agent.code.normal",
            project_dir=project.resolve(),
        )
    )
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)
    names = {Path(item.path).name for item in result.files}

    assert names == {"JIUWENSWARM.md"}
    assert "notes.md" not in serialized
    assert "outside-secret.md" not in serialized
    assert secret not in serialized
    assert "api_key" not in serialized


def test_catalog_excludes_file_link_that_resolves_outside_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    rules = project / ".jiuwen" / "rules"
    workspace = tmp_path / "workspace"
    user_home = tmp_path / "home"
    outside = tmp_path / "outside.md"
    rules.mkdir(parents=True)
    workspace.mkdir()
    user_home.mkdir()
    outside.write_text("outside", encoding="utf-8")
    link = rules / "escape.md"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file links unavailable: {exc}")
    _patch_memory_roots(
        monkeypatch,
        workspace=workspace,
        user_home=user_home,
    )

    result = list_memory_sources(
        ResolvedMemoryScope(
            channel_id="process_cli",
            session_id="",
            mode="agent.code.normal",
            project_dir=project.resolve(),
        )
    )

    assert result.files == ()


def test_same_named_projects_have_distinct_coding_memory_locations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "one" / "project"
    second = tmp_path / "two" / "project"
    workspace = tmp_path / "workspace"
    user_home = tmp_path / "home"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    workspace.mkdir()
    user_home.mkdir()
    _patch_memory_roots(
        monkeypatch,
        workspace=workspace,
        user_home=user_home,
    )
    from jiuwenswarm.runtime.memory_catalog import get_memory_locations

    first_result = get_memory_locations(
        ResolvedMemoryScope("process_cli", "", "agent.code.normal", first)
    )
    second_result = get_memory_locations(
        ResolvedMemoryScope("process_cli", "", "agent.code.normal", second)
    )

    assert first_result.coding_memory_dir != second_result.coding_memory_dir
    assert os.path.basename(first_result.coding_memory_dir).startswith("project-")
    assert os.path.basename(second_result.coding_memory_dir).startswith("project-")
