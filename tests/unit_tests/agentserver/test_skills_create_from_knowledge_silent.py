# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""知识转技能静默执行：把 agent 流内的错误透传到最终异常."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.skill.skill_manager import (
    ERROR_SKILL_INVALID_PACKAGE,
    SkillRpcError,
)

_MISSING_SKILL_MD = "未在输出目录找到有效 Skill（缺少 SKILL.md）"
_NO_MODEL = "还没有配置可用的模型：请在设置里配置模型，或登录获取限时免费模型"


class _FakeSkillManager:
    def __init__(self, tmp_path: Path, *, result=None, error=None):
        self.skills_dir = str(tmp_path / "skills")
        self._result = result
        self._error = error

    def list_installed_skill_dir_names(self) -> list[str]:
        return []

    def finalize_create_from_knowledge(self, output_dir, **_kwargs):
        if self._error is not None:
            raise self._error
        return self._result or {"success": True}


class _FakeAdapter:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    async def process_message_stream_impl(self, _request, _inputs):
        for event in self._events:
            yield SimpleNamespace(payload=event)


def _build_agent(tmp_path: Path, events: list[dict], manager) -> JiuWenSwarm:
    agent = object.__new__(JiuWenSwarm)
    agent._skill_manager = manager
    agent._coerce_optional_str_list = lambda value: list(value or [])
    agent._build_skills_knowledge_followup_request = lambda *a, **kw: SimpleNamespace()
    agent._adapter_mode_for_request = lambda _request: "agent"
    agent._ensure_adapter = lambda mode: _FakeAdapter(events)
    agent._cleanup_knowledge_upload_file = lambda _path: None

    async def _build_inputs(_request):
        return None, None, None

    async def _refresh():
        return None

    agent._build_inputs_async = _build_inputs
    agent._refresh_skill_rails_after_change = _refresh
    return agent


def _payload(output_dir: Path) -> dict:
    return {
        "followup_prompt": "根据文档生成技能",
        "output_dir": str(output_dir),
        "input_file": "",
    }


@pytest.mark.asyncio
async def test_silent_stream_error_is_appended_to_finalize_error(tmp_path: Path) -> None:
    """流内 chat.error 的原文应追加到 finalize 阶段错误之后."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    manager = _FakeSkillManager(
        tmp_path,
        error=SkillRpcError(ERROR_SKILL_INVALID_PACKAGE, _MISSING_SKILL_MD),
    )
    agent = _build_agent(
        tmp_path, [{"event_type": "chat.error", "error": _NO_MODEL}], manager
    )

    with pytest.raises(SkillRpcError) as exc_info:
        await agent._run_skills_create_from_knowledge_silent(
            SimpleNamespace(), _payload(output_dir)
        )

    assert exc_info.value.code == ERROR_SKILL_INVALID_PACKAGE
    assert _MISSING_SKILL_MD in exc_info.value.message
    assert _NO_MODEL in exc_info.value.message
    assert not output_dir.exists()


@pytest.mark.asyncio
async def test_silent_stream_without_error_keeps_finalize_message(tmp_path: Path) -> None:
    """流内没有 chat.error 时，最终错误信息保持原样."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    manager = _FakeSkillManager(
        tmp_path,
        error=SkillRpcError(ERROR_SKILL_INVALID_PACKAGE, _MISSING_SKILL_MD),
    )
    agent = _build_agent(
        tmp_path, [{"event_type": "chat.final", "content": "done"}], manager
    )

    with pytest.raises(SkillRpcError) as exc_info:
        await agent._run_skills_create_from_knowledge_silent(
            SimpleNamespace(), _payload(output_dir)
        )

    assert exc_info.value.message == _MISSING_SKILL_MD


@pytest.mark.asyncio
async def test_silent_stream_success_returns_result_and_cleans_output_dir(
    tmp_path: Path,
) -> None:
    """正常路径返回 finalize 结果，并清理隔离输出目录."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    expected = {"success": True, "skill": {"name": "git-commit-convention"}}
    manager = _FakeSkillManager(tmp_path, result=expected)
    agent = _build_agent(
        tmp_path, [{"event_type": "chat.final", "content": "ok"}], manager
    )

    result = await agent._run_skills_create_from_knowledge_silent(
        SimpleNamespace(), _payload(output_dir)
    )

    assert result == expected
    assert not output_dir.exists()
