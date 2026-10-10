# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""skills.rebuild 同步静默 Agent follow-up 契约测试."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module


@pytest.mark.asyncio
async def test_skills_rebuild_awaits_silent_impl_and_returns_success_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RPC 同步 await process_message_stream_impl，对外仅 {success:true}."""

    impl_calls: list[dict[str, Any]] = []

    class FakeAdapter:
        async def process_message_stream_impl(self, request, inputs):
            impl_calls.append(
                {
                    "session_id": request.session_id,
                    "query": (request.params or {}).get("query"),
                    "log_as_user": (request.params or {}).get("log_as_user"),
                    "metadata": dict(request.metadata or {}),
                    "inputs_query": inputs.get("query"),
                }
            )
            if False:  # pragma: no cover - keep async generator shape
                yield None

    followup_payload = {
        "success": True,
        "result_type": "followup",
        "action": "run_rebuild_followup",
        "followup_prompt": "Please rebuild local-doc silently",
        "skill_name": "local-doc",
        "rebuild_target": {
            "skill_dir": "",
            "content_root": "",
            "swap_workspace": False,
            "is_default": True,
        },
    }

    swarm = interface_module.JiuWenSwarm()
    swarm._skill_manager = MagicMock()
    swarm._skill_manager.handle_skills_rebuild = AsyncMock(return_value=followup_payload)
    swarm._refresh_skill_rails_after_change = AsyncMock()

    monkeypatch.setattr(swarm, "_ensure_adapter", lambda **_kwargs: FakeAdapter())
    monkeypatch.setattr(
        swarm,
        "_build_inputs",
        lambda request: ({"query": request.params.get("query")}, "disabled", ""),
    )

    # 确认不再走外层 process_message_stream / create_task
    outer_called = {"n": 0}

    async def _unexpected_outer(*_a, **_k):
        outer_called["n"] += 1
        if False:  # pragma: no cover
            yield None

    monkeypatch.setattr(swarm, "process_message_stream", _unexpected_outer)

    request = AgentRequest(
        request_id="req-rebuild-silent",
        channel_id="web",
        session_id="user-session-1",
        req_method=ReqMethod.SKILLS_REBUILD,
        params={"name": "local-doc", "version": None, "mode": "agent"},
        is_stream=False,
        metadata={"channel": "web"},
    )

    response = await swarm._handle_skills_request(request)
    assert response is not None
    assert response.ok is True
    assert response.payload == {"success": True}
    assert outer_called["n"] == 0
    assert len(impl_calls) == 1
    call = impl_calls[0]
    assert call["session_id"] == "skills-rebuild-req-rebuild-silent"
    assert ":" not in call["session_id"]
    assert call["query"] == "Please rebuild local-doc silently"
    assert call["log_as_user"] is False
    assert call["metadata"].get("skills_rebuild_silent") is True
    assert call["inputs_query"] == "Please rebuild local-doc silently"
    swarm._refresh_skill_rails_after_change.assert_awaited()


@pytest.mark.asyncio
async def test_skills_rebuild_agent_failure_returns_business_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BoomAdapter:
        async def process_message_stream_impl(self, *_a, **_k):
            raise RuntimeError("agent boom")
            if False:  # pragma: no cover
                yield None

    swarm = interface_module.JiuWenSwarm()
    swarm._skill_manager = MagicMock()
    swarm._skill_manager.handle_skills_rebuild = AsyncMock(
        return_value={
            "success": True,
            "result_type": "followup",
            "followup_prompt": "rebuild me",
            "skill_name": "x",
        }
    )
    monkeypatch.setattr(swarm, "_ensure_adapter", lambda **_kwargs: BoomAdapter())
    monkeypatch.setattr(
        swarm,
        "_build_inputs",
        lambda request: ({"query": "rebuild me"}, "disabled", ""),
    )
    monkeypatch.setattr(swarm, "_refresh_skill_rails_after_change", AsyncMock())

    request = AgentRequest(
        request_id="req-rebuild-fail",
        channel_id="web",
        session_id="user-session-1",
        req_method=ReqMethod.SKILLS_REBUILD,
        params={"name": "x", "version": None},
        is_stream=False,
    )
    response = await swarm._handle_skills_request(request)
    assert response is not None
    assert response.ok is False
    assert response.payload.get("code") == "SKILL_REBUILD_FAILED"
    assert "agent boom" in str(response.payload.get("message") or "")


@pytest.mark.asyncio
async def test_skills_rebuild_empty_followup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm = interface_module.JiuWenSwarm()
    swarm._skill_manager = MagicMock()
    swarm._skill_manager.handle_skills_rebuild = AsyncMock(
        return_value={
            "success": True,
            "result_type": "followup",
            "followup_prompt": "   ",
            "skill_name": "x",
        }
    )
    monkeypatch.setattr(swarm, "_ensure_adapter", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(swarm, "_refresh_skill_rails_after_change", AsyncMock())

    request = AgentRequest(
        request_id="req-rebuild-empty",
        channel_id="web",
        session_id="user-session-1",
        req_method=ReqMethod.SKILLS_REBUILD,
        params={"name": "x", "version": None},
        is_stream=False,
    )
    response = await swarm._handle_skills_request(request)
    assert response is not None
    assert response.ok is False
    assert response.payload.get("code") == "SKILL_REBUILD_FAILED"


@pytest.mark.asyncio
async def test_send_file_noop_when_skills_rebuild_silent(tmp_path) -> None:
    from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu

    file_path = tmp_path / "SKILL.md"
    file_path.write_text("body", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id="r1",
        session_id="skills-rebuild-req",
        channel_id="web",
        metadata={"skills_rebuild_silent": True},
    )
    result = await toolkit.send_file(str(file_path))
    assert "静默模式禁止" in result
