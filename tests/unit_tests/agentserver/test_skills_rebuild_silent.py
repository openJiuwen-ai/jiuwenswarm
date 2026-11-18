# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""skills.rebuild 后台静默 Agent follow-up 契约测试."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module


@pytest.mark.asyncio
async def test_skills_rebuild_returns_pending_and_finishes_in_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RPC 立刻返回 pending。静默 Agent 在后台跑完后，任务才变成 done."""

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
    assert response.payload["success"] is True
    assert response.payload["pending"] is True
    rebuild_id = response.payload["rebuild_id"]
    assert rebuild_id
    assert outer_called["n"] == 0
    assert impl_calls == []

    from jiuwenswarm.server.runtime.skill.skill_manager import (
        get_rebuild_job,
        wait_rebuild_task,
    )

    await wait_rebuild_task(rebuild_id)
    assert len(impl_calls) == 1
    call = impl_calls[0]
    assert call["session_id"] == "skills-rebuild-req-rebuild-silent"
    assert ":" not in call["session_id"]
    assert call["query"] == "Please rebuild local-doc silently"
    assert call["log_as_user"] is False
    assert call["metadata"].get("skills_rebuild_silent") is True
    assert call["inputs_query"] == "Please rebuild local-doc silently"
    swarm._refresh_skill_rails_after_change.assert_awaited()
    assert get_rebuild_job(rebuild_id)["status"] == "done"


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
    assert response.ok is True
    assert response.payload.get("pending") is True
    rebuild_id = response.payload["rebuild_id"]

    from jiuwenswarm.server.runtime.skill.skill_manager import (
        get_rebuild_job,
        wait_rebuild_task,
    )

    await wait_rebuild_task(rebuild_id)
    job = get_rebuild_job(rebuild_id)
    assert job["status"] == "failed"
    assert job["code"] == "SKILL_REBUILD_FAILED"
    assert "agent boom" in job["detail"]


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
    assert response.ok is True
    assert response.payload.get("pending") is True
    rebuild_id = response.payload["rebuild_id"]

    from jiuwenswarm.server.runtime.skill.skill_manager import (
        get_rebuild_job,
        wait_rebuild_task,
    )

    await wait_rebuild_task(rebuild_id)
    job = get_rebuild_job(rebuild_id)
    assert job["status"] == "failed"
    assert job["code"] == "SKILL_REBUILD_FAILED"
    assert "follow-up prompt" in job["detail"]


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


def test_live_rebuild_is_reused_and_a_dead_one_asks_for_retry(tmp_path) -> None:
    from jiuwenswarm.server.runtime.skill.skill_manager import (
        note_rebuild_live,
        read_rebuild_job,
        take_active_rebuild,
        write_rebuild_job,
    )

    write_rebuild_job(
        tmp_path,
        {
            "rebuild_id": "job-live",
            "status": "running",
            "name": "local-doc",
            "code": "",
            "detail": "",
        },
    )
    note_rebuild_live("job-live", True)
    try:
        active = take_active_rebuild(tmp_path, "local-doc")
        assert active is not None
        assert active["pending"] is True
        assert active["rebuild_id"] == "job-live"
    finally:
        note_rebuild_live("job-live", False)

    assert take_active_rebuild(tmp_path, "local-doc") is None
    job = read_rebuild_job(tmp_path)
    assert job is not None
    assert job["status"] == "failed"
    assert "重试" in job["detail"]


@pytest.mark.asyncio
async def test_rebuild_status_marks_a_file_failed_when_the_task_is_gone(tmp_path) -> None:
    from jiuwenswarm.server.runtime.skill.skill_manager import (
        SkillManager,
        read_rebuild_job,
        write_rebuild_job,
    )
    import jiuwenswarm.server.runtime.skill.skill_manager as skills

    write_rebuild_job(
        tmp_path,
        {
            "rebuild_id": "job-stale",
            "status": "running",
            "name": "local-doc",
            "code": "",
            "detail": "",
        },
    )
    skills._REBUILD_JOBS_BY_ID.pop("job-stale", None)
    skills._LIVE_REBUILD_IDS.discard("job-stale")

    manager = SkillManager.__new__(SkillManager)
    manager._resolve_local_skill_dir = lambda _name: tmp_path  # type: ignore[method-assign]
    result = await SkillManager.handle_skills_rebuild_status(
        manager,
        {"name": "local-doc", "rebuild_id": "job-stale"},
    )
    assert result["success"] is False
    assert result["status"] == "failed"
    assert "重试" in result["detail"]
    stored = read_rebuild_job(tmp_path)
    assert stored is not None
    assert stored["status"] == "failed"


def test_fresh_heartbeat_is_not_marked_failed_on_another_process(tmp_path) -> None:
    """别的 pod 没有本进程任务，但心跳还新鲜时不能把 running 改成失败。"""
    from datetime import datetime, timezone

    from jiuwenswarm.server.runtime.skill.skill_manager import (
        SkillManager,
        read_rebuild_job,
        rebuild_heartbeat_now,
        take_active_rebuild,
        write_rebuild_job,
    )
    import jiuwenswarm.server.runtime.skill.skill_manager as skills

    write_rebuild_job(
        tmp_path,
        {
            "rebuild_id": "job-remote",
            "status": "running",
            "name": "local-doc",
            "code": "",
            "detail": "",
            "heartbeat_at": rebuild_heartbeat_now(),
        },
    )
    skills._REBUILD_JOBS_BY_ID.pop("job-remote", None)
    skills._LIVE_REBUILD_IDS.discard("job-remote")

    active = take_active_rebuild(tmp_path, "local-doc")
    assert active is not None
    assert active["rebuild_id"] == "job-remote"
    assert active["pending"] is True

    manager = SkillManager.__new__(SkillManager)
    manager._resolve_local_skill_dir = lambda _name: tmp_path  # type: ignore[method-assign]
    result = asyncio.run(
        SkillManager.handle_skills_rebuild_status(
            manager,
            {"name": "local-doc", "rebuild_id": "job-remote"},
        )
    )
    assert result["success"] is True
    assert result["status"] == "running"
    stored = read_rebuild_job(tmp_path)
    assert stored is not None
    assert stored["status"] == "running"
    assert datetime.now(timezone.utc).isoformat() >= str(stored["heartbeat_at"])


def test_expired_heartbeat_marks_rebuild_failed(tmp_path) -> None:
    from datetime import datetime, timedelta, timezone

    from jiuwenswarm.server.runtime.skill.skill_manager import (
        SkillManager,
        read_rebuild_job,
        write_rebuild_job,
    )
    import jiuwenswarm.server.runtime.skill.skill_manager as skills

    expired = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    write_rebuild_job(
        tmp_path,
        {
            "rebuild_id": "job-expired",
            "status": "running",
            "name": "local-doc",
            "code": "",
            "detail": "",
            "heartbeat_at": expired,
        },
    )
    skills._REBUILD_JOBS_BY_ID.pop("job-expired", None)
    skills._LIVE_REBUILD_IDS.discard("job-expired")

    manager = SkillManager.__new__(SkillManager)
    manager._resolve_local_skill_dir = lambda _name: tmp_path  # type: ignore[method-assign]
    result = asyncio.run(
        SkillManager.handle_skills_rebuild_status(
            manager,
            {"name": "local-doc", "rebuild_id": "job-expired"},
        )
    )
    assert result["success"] is False
    assert result["status"] == "failed"
    assert "重试" in result["detail"]
    stored = read_rebuild_job(tmp_path)
    assert stored is not None
    assert stored["status"] == "failed"


@pytest.mark.asyncio
async def test_rebuild_file_is_done_only_after_the_silent_agent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.skill.skill_manager import (
        read_rebuild_job,
        wait_rebuild_task,
    )

    seen: dict[str, str] = {}

    class FakeAdapter:
        async def process_message_stream_impl(self, request, inputs):
            job = read_rebuild_job(tmp_path)
            assert job is not None
            seen["during"] = str(job["status"])
            if False:  # pragma: no cover
                yield None

    followup_payload = {
        "success": True,
        "result_type": "followup",
        "followup_prompt": "rewrite the skill",
        "skill_name": "local-doc",
        "rebuild_target": {
            "skill_dir": str(tmp_path),
            "content_root": "",
            "swap_workspace": False,
            "is_default": False,
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
    request = AgentRequest(
        request_id="req-rebuild-file",
        channel_id="web",
        session_id="user-session-1",
        req_method=ReqMethod.SKILLS_REBUILD,
        params={"name": "local-doc", "version": None},
        is_stream=False,
    )
    response = await swarm._handle_skills_request(request)
    assert response is not None and response.payload["pending"] is True
    await wait_rebuild_task(response.payload["rebuild_id"])
    assert seen["during"] == "running"
    finished = read_rebuild_job(tmp_path)
    assert finished is not None
    assert finished["status"] == "done"
