# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""cron 快照机制退役后的 gateway 侧行为（D-20261028-1105）.

chat.send 不再串行等待 cron.jobs.sync RPC；proactive.tick 的 toggle 保护
统一由服务端 controller 裁决；ack 信封补齐 session_id 以支持按 session
路由的部署形态。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("openjiuwen_protocol")

from agent_gateway.cron.controller import CronController
from agent_gateway.message_handler import message_handler as mh
from agent_gateway.message_handler.message_handler import MessageHandler


def _make_controller() -> CronController:
    store = SimpleNamespace(
        get_job=AsyncMock(return_value=None),
        delete_job=AsyncMock(return_value=True),
        update_job=AsyncMock(
            return_value=SimpleNamespace(
                id="job-1",
                mode="chat",
                project_id="default",
                user_id=None,
                enabled=True,
                to_dict=lambda: {"id": "job-1"},
            )
        ),
    )
    scheduler = SimpleNamespace(
        _lifecycle_mutation_lock=asyncio.Lock(),
        project_execution_allowed=AsyncMock(return_value=True),
        reload=AsyncMock(),
    )
    return CronController(store=store, scheduler=scheduler)


@pytest.mark.asyncio
async def test_toggle_job_rejects_proactive_tick_with_explicit_error() -> None:
    """toggle 对 proactive.tick 服务端显式拒绝（web/TUI/E2A 三入口共用）。"""
    controller = _make_controller()
    controller._store.get_job = AsyncMock(
        return_value=SimpleNamespace(
            id="proactive-1",
            mode="proactive.tick",
            project_id="default",
            user_id=None,
            enabled=False,
        )
    )

    with pytest.raises(ValueError, match="主动推荐定时任务"):
        await controller.toggle_job("proactive-1", True)

    controller._store.update_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_toggle_job_allows_normal_job() -> None:
    controller = _make_controller()
    controller._store.get_job = AsyncMock(
        return_value=SimpleNamespace(
            id="job-1",
            mode="chat",
            project_id="default",
            user_id=None,
            enabled=True,
            work_mode="code",
            targets="web",
        )
    )

    result = await controller.toggle_job("job-1", False)

    assert result == {"id": "job-1"}
    controller._store.update_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_job_rejects_enabled_change_on_proactive_tick() -> None:
    """update 显式拒绝改 enabled，错误口径与 toggle 一致。"""
    controller = _make_controller()
    controller._store.get_job = AsyncMock(
        return_value=SimpleNamespace(
            id="proactive-1",
            mode="proactive.tick",
            project_id="default",
            user_id=None,
            enabled=True,
        )
    )

    with pytest.raises(ValueError, match="不能手动启停"):
        await controller.update_job("proactive-1", {"enabled": False})

    controller._store.update_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_job_allows_unchanged_enabled_echo_on_proactive_tick() -> None:
    """全量回显（enabled 与当前一致）不阻断合法的调度编辑。"""
    controller = _make_controller()
    controller._store.get_job = AsyncMock(
        return_value=SimpleNamespace(
            id="proactive-tick-auto",
            mode="proactive.tick",
            project_id="default",
            user_id=None,
            enabled=True,
            work_mode="code",
            timezone="Asia/Shanghai",
            targets="web",
        )
    )

    job = await controller.update_job(
        "proactive-tick-auto",
        {"enabled": True, "cron_expr": "30 0 * * * * *"},
    )

    assert job == {"id": "job-1"}
    controller._store.update_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_job_rejects_proactive_tick_without_force() -> None:
    """手动删除 proactive.tick 在 controller 显式拒绝（与 toggle 守卫对称）。"""
    controller = _make_controller()
    controller._store.get_job = AsyncMock(
        return_value=SimpleNamespace(
            id="proactive-1",
            mode="proactive.tick",
            project_id="default",
            user_id=None,
            enabled=True,
        )
    )

    with pytest.raises(ValueError, match="不能删除"):
        await controller.delete_job("proactive-1")

    controller._store.delete_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_job_force_path_still_serves_auto_sync() -> None:
    """proactive_cron_sync 走 force=True + delete_sessions=False，不受守卫影响。"""
    controller = _make_controller()
    controller._store.get_job = AsyncMock(
        return_value=SimpleNamespace(
            id="proactive-tick-auto",
            mode="proactive.tick",
            project_id="default",
            user_id=None,
            enabled=True,
        )
    )

    deleted = await controller.delete_job(
        "proactive-tick-auto", force=True, delete_sessions=False
    )

    assert deleted is True
    controller._store.delete_job.assert_awaited_once_with(
        "proactive-tick-auto", force=True
    )
    controller._scheduler.reload.assert_awaited_once()


def _bare_handler() -> MessageHandler:
    handler = object.__new__(MessageHandler)
    handler.agent_client = SimpleNamespace()
    handler._stream_metadata = {}
    handler._stream_modes = {}
    handler._non_stream_chat_messages = {}
    return handler


def _capture_ack(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    async def fake_send(client, env, *, label: str = ""):
        captured["env"] = env
        captured["label"] = label
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(mh, "send_agent_request_with_timeout", fake_send)
    return captured


@pytest.mark.asyncio
async def test_cron_command_ack_envelope_carries_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_ack(monkeypatch)
    handler = _bare_handler()

    await handler._push_cron_command_ack(
        command_id="cmd-1",
        data={"deleted": True},
        user_id="alice",
        session_id="sess-1",
    )

    env = captured["env"]
    assert env.session_id == "sess-1"
    assert env.user_id == "alice"
    assert env.method == "cron.command.ack"


@pytest.mark.asyncio
async def test_cron_run_now_ack_envelope_carries_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_ack(monkeypatch)
    handler = _bare_handler()

    await handler._push_cron_run_now_ack(
        request_id="req-1",
        job_id="job-1",
        run_id="run-1",
        user_id="alice",
        session_id="sess-1",
    )

    env = captured["env"]
    assert env.session_id == "sess-1"
    assert env.user_id == "alice"
    assert env.method == "cron.run_now.ack"


@pytest.mark.asyncio
async def test_cron_push_payload_forwards_session_id_to_command_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """钉住 _handle_cron_push_payload → command ack 的 session_id 透传。"""
    captured = _capture_ack(monkeypatch)
    handler = _bare_handler()
    handler._cron_controller = SimpleNamespace(
        get_job=AsyncMock(return_value={"id": "job-1", "user_id": "alice"}),
        delete_job=AsyncMock(return_value=True),
    )

    await handler._handle_cron_push_payload(
        payload={
            "action": "delete",
            "command_id": "cmd-1",
            "data": {"job_id": "job-1"},
        },
        request_id="req-1",
        channel_id="",
        session_id="sess-1",
        metadata=None,
        user_id="alice",
    )

    env = captured["env"]
    assert env.session_id == "sess-1"
    assert env.user_id == "alice"
    assert env.method == "cron.command.ack"


@pytest.mark.asyncio
async def test_cron_push_payload_forwards_session_id_to_run_now_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """钉住 _handle_cron_push_payload → run_now ack 的 session_id 透传。"""
    captured = _capture_ack(monkeypatch)
    handler = _bare_handler()
    handler._cron_controller = SimpleNamespace(
        get_job=AsyncMock(return_value={"id": "job-1", "user_id": "alice"}),
        run_now=AsyncMock(return_value="run-1"),
    )

    await handler._handle_cron_push_payload(
        payload={"action": "run_now", "data": {"job_id": "job-1"}},
        request_id="req-1",
        channel_id="",
        session_id="sess-1",
        metadata=None,
        user_id="alice",
    )

    env = captured["env"]
    assert env.session_id == "sess-1"
    assert env.user_id == "alice"
    assert env.method == "cron.run_now.ack"


def test_cron_snapshot_sync_fully_retired() -> None:
    """per-turn 快照推送机制整体下线：方法与门控常量都不再存在。"""
    assert not hasattr(MessageHandler, "_sync_agentos_cron_jobs")
    assert not hasattr(mh, "_CRON_SNAPSHOT_STREAM_METHODS")
