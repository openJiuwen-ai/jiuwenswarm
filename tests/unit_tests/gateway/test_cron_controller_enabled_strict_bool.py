# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""cron toggle / PATCH 的 enabled 严格布尔校验（#5725）。

``bool("false")`` 为 True：此前非布尔 enabled 被静默强转，字符串 "false"
会把禁用任务打开（语义反转）。入口层必须拒绝非布尔值且不改动任务。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.store import CronJobStore

_NON_BOOL_VALUES = ["false", "true", 0, 1, None, {"not": "boolean"}, []]


def _make_controller(tmp_path) -> tuple[CronController, CronJobStore]:
    store = CronJobStore(path=tmp_path / "cron_jobs.json")
    scheduler = SimpleNamespace(
        reload=AsyncMock(),
        project_execution_allowed=AsyncMock(return_value=True),
    )
    return CronController(store=store, scheduler=scheduler), store


async def _create_disabled_job(controller: CronController) -> str:
    job = await controller.create_job(
        {
            "name": "job",
            "cron_expr": "0 0 9 * * ? *",
            "timezone": "Asia/Shanghai",
            "description": "reminder",
            "targets": "web",
            "enabled": False,
        }
    )
    assert job["enabled"] is False
    return job["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", _NON_BOOL_VALUES)
async def test_toggle_job_rejects_non_bool_enabled(tmp_path, value):
    controller, store = _make_controller(tmp_path)
    job_id = await _create_disabled_job(controller)

    with pytest.raises(ValueError, match="enabled must be a boolean"):
        await controller.toggle_job(job_id, value)

    assert (await store.get_job(job_id)).enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("value", _NON_BOOL_VALUES)
async def test_update_job_rejects_non_bool_enabled(tmp_path, value):
    controller, store = _make_controller(tmp_path)
    job_id = await _create_disabled_job(controller)

    with pytest.raises(ValueError, match="enabled must be a boolean"):
        await controller.update_job(job_id, {"enabled": value})

    assert (await store.get_job(job_id)).enabled is False


@pytest.mark.asyncio
async def test_toggle_and_update_accept_real_booleans(tmp_path):
    controller, store = _make_controller(tmp_path)
    job_id = await _create_disabled_job(controller)

    assert (await controller.toggle_job(job_id, True))["enabled"] is True
    assert (await controller.toggle_job(job_id, False))["enabled"] is False
    assert (await controller.update_job(job_id, {"enabled": True}))["enabled"] is True
    assert (await controller.update_job(job_id, {"enabled": False}))["enabled"] is False
    assert (await store.get_job(job_id)).enabled is False


_CREATE_PARAMS = {
    "name": "job",
    "cron_expr": "0 0 9 * * ? *",
    "timezone": "Asia/Shanghai",
    "description": "reminder",
    "targets": "web",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", _NON_BOOL_VALUES)
async def test_create_job_rejects_non_bool_enabled(tmp_path, value):
    controller, store = _make_controller(tmp_path)

    with pytest.raises(ValueError, match="enabled must be a boolean"):
        await controller.create_job({**_CREATE_PARAMS, "enabled": value})

    assert await store.list_jobs() == []


@pytest.mark.asyncio
async def test_create_job_accepts_real_booleans_and_defaults_to_enabled(tmp_path):
    controller, _store = _make_controller(tmp_path)

    assert (await controller.create_job(dict(_CREATE_PARAMS)))["enabled"] is True
    assert (await controller.create_job({**_CREATE_PARAMS, "enabled": True}))["enabled"] is True
    assert (await controller.create_job({**_CREATE_PARAMS, "enabled": False}))["enabled"] is False


class _ToggleCapturingController:
    def __init__(self) -> None:
        self.toggle_calls: list[tuple[str, object]] = []

    async def get_job(self, job_id: str) -> dict:
        return {"id": job_id, "user_id": ""}

    async def toggle_job(self, job_id: str, enabled):
        self.toggle_calls.append((job_id, enabled))
        return {"id": job_id, "enabled": enabled}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", "true", 0, 1, True, False])
async def test_message_handler_toggle_passes_enabled_through(value):
    """gateway 命令通道不得预先 bool() 强转，交由 controller 严格校验。"""
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler

    controller = _ToggleCapturingController()
    handler = object.__new__(MessageHandler)
    handler.agent_client = object()
    handler._cron_controller = controller
    handler._stream_modes = {}
    handler._stream_user_ids = {}
    handler._stream_sessions = {}
    handler._stream_metadata = {}
    handler._non_stream_chat_messages = {}

    async def _publish(_message) -> None:
        return None

    handler.publish_robot_messages = _publish

    await handler._handle_cron_push_payload(
        payload={"action": "toggle", "data": {"job_id": "job-1", "enabled": value}},
        request_id="req-1",
        channel_id="web",
        session_id="sess-1",
        metadata=None,
    )

    assert len(controller.toggle_calls) == 1
    job_id, passed = controller.toggle_calls[0]
    assert job_id == "job-1"
    assert passed is value


@pytest.mark.asyncio
async def test_message_handler_toggle_string_false_does_not_enable_job(tmp_path):
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler

    controller, store = _make_controller(tmp_path)
    job_id = await _create_disabled_job(controller)
    handler = object.__new__(MessageHandler)
    handler.agent_client = object()
    handler._cron_controller = controller
    handler._stream_modes = {}
    handler._stream_user_ids = {}
    handler._stream_sessions = {}
    handler._stream_metadata = {}
    handler._non_stream_chat_messages = {}

    async def _publish(_message) -> None:
        return None

    handler.publish_robot_messages = _publish

    await handler._handle_cron_push_payload(
        payload={"action": "toggle", "data": {"job_id": job_id, "enabled": "false"}},
        request_id="req-1",
        channel_id="web",
        session_id="sess-1",
        metadata=None,
    )

    assert (await store.get_job(job_id)).enabled is False
