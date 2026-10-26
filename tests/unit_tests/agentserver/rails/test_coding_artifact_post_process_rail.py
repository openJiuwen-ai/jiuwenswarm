# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.code.rails import (
    CodingArtifactPostProcessRail,
)
from jiuwenswarm.agents.harness.code.rails.coding_artifact_post_process_rail import (
    add_officeace_coauthor_header,
)


class _FakeSession:
    def get_session_id(self) -> str:
        return "code-session"


def _tool_ctx(path: str) -> SimpleNamespace:
    return SimpleNamespace(
        session=_FakeSession(),
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="tool-call-1"),
            tool_name="write_file",
            tool_args={"path": path},
            tool_result=f"Wrote {path}",
            tool_msg=None,
        ),
    )


@pytest.mark.asyncio
async def test_coding_artifact_rail_fires_hook_once_without_task_lifecycle(
    tmp_path, monkeypatch
) -> None:
    output = tmp_path / "artifact.png"
    ctx = _tool_ctx(str(output))
    rail = CodingArtifactPostProcessRail()
    calls: list[dict] = []

    async def _record_hook(**kwargs) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.rails.coding_artifact_post_process_rail."
        "fire_artifact_hook",
        _record_hook,
    )

    await rail.before_invoke(ctx)
    await rail.before_tool_call(ctx)
    output.write_bytes(b"image")
    await rail.after_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert len(calls) == 1
    assert calls[0]["session_id"] == "code-session"
    assert calls[0]["tool_name"] == "write_file"
    assert calls[0]["artifact_paths"] == [str(output)]
    assert calls[0]["task_id"] is None
    assert not hasattr(rail, "_todo_map")
    assert not hasattr(rail, "_active_tasks")


@pytest.mark.asyncio
async def test_coding_artifact_rail_deduplicates_unchanged_file_across_invokes(
    tmp_path, monkeypatch
) -> None:
    output = tmp_path / "artifact.png"
    ctx = _tool_ctx(str(output))
    rail = CodingArtifactPostProcessRail()
    calls: list[dict] = []

    async def _record_hook(**kwargs) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.rails.coding_artifact_post_process_rail."
        "fire_artifact_hook",
        _record_hook,
    )

    await rail.before_invoke(ctx)
    await rail.before_tool_call(ctx)
    output.write_bytes(b"unchanged-image")
    await rail.after_tool_call(ctx)

    await rail.before_invoke(ctx)
    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert len(calls) == 1
    assert rail._tool_start_times == {}
    assert len(rail._hooked_artifacts) == 1


@pytest.mark.asyncio
async def test_coding_artifact_rail_ignores_todo_tools(monkeypatch) -> None:
    rail = CodingArtifactPostProcessRail()
    ctx = SimpleNamespace(
        session=_FakeSession(),
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="todo-call-1"),
            tool_name="todo_modify",
            tool_args={"id": "1", "status": "in_progress"},
            tool_result="updated",
            tool_msg=None,
        ),
    )

    async def _unexpected_hook(**_kwargs) -> bool:
        raise AssertionError("todo tools must not trigger artifact hooks")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.rails.coding_artifact_post_process_rail."
        "fire_artifact_hook",
        _unexpected_hook,
    )

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert rail._tool_start_times == {}


def test_coauthor_header_preserves_python_shebang_cookie_and_crlf(tmp_path) -> None:
    output = tmp_path / "script.py"
    output.write_bytes(
        b"#!/usr/bin/env python\r\n# -*- coding: utf-8 -*-\r\nprint('ok')\r\n"
    )

    assert add_officeace_coauthor_header(output)
    assert output.read_bytes() == (
        b"#!/usr/bin/env python\r\n# -*- coding: utf-8 -*-\r\n"
        b"# Co-authored by OfficeAce Coding Agent\r\nprint('ok')\r\n"
    )
    assert not add_officeace_coauthor_header(output)


def test_coauthor_header_uses_language_comment_and_skips_non_code(tmp_path) -> None:
    typescript = tmp_path / "app.ts"
    typescript.write_text("export const value = 1;\n", encoding="utf-8")
    image = tmp_path / "image.png"
    image.write_bytes(b"image")

    assert add_officeace_coauthor_header(typescript)
    assert typescript.read_text(encoding="utf-8").startswith(
        "// Co-authored by OfficeAce Coding Agent\n"
    )
    assert not add_officeace_coauthor_header(image)
    assert image.read_bytes() == b"image"


@pytest.mark.asyncio
async def test_coauthor_header_is_disabled_by_default(tmp_path, monkeypatch) -> None:
    output = tmp_path / "artifact.py"
    ctx = _tool_ctx(str(output))
    rail = CodingArtifactPostProcessRail()

    async def _successful_hook(**_kwargs) -> bool:
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.rails.coding_artifact_post_process_rail."
        "fire_artifact_hook",
        _successful_hook,
    )

    await rail.before_tool_call(ctx)
    output.write_text("print('ok')\n", encoding="utf-8")
    await rail.after_tool_call(ctx)

    assert output.read_text(encoding="utf-8") == "print('ok')\n"


@pytest.mark.asyncio
async def test_coauthor_header_enabled_for_detected_code_artifact(
    tmp_path, monkeypatch
) -> None:
    output = tmp_path / "artifact.py"
    ctx = _tool_ctx(str(output))
    rail = CodingArtifactPostProcessRail(coauthor_header_enabled=True)

    async def _successful_hook(**_kwargs) -> bool:
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.rails.coding_artifact_post_process_rail."
        "fire_artifact_hook",
        _successful_hook,
    )

    await rail.before_tool_call(ctx)
    output.write_text("print('ok')\n", encoding="utf-8")
    await rail.after_tool_call(ctx)

    assert output.read_text(encoding="utf-8") == (
        "# Co-authored by OfficeAce Coding Agent\nprint('ok')\n"
    )
