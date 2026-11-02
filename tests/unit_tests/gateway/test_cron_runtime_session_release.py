# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cron permission-resume settle releases the runtime slot only when the turn ends."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


def _clear_message_handler_singleton() -> None:
    setattr(MessageHandler, "_instance", None)


@pytest.fixture(autouse=True)
def _isolate_message_handler_singleton():
    """Avoid leaking MessageHandler singleton into other gateway UTs (xdist worker)."""
    _clear_message_handler_singleton()
    yield
    _clear_message_handler_singleton()


class _ReleaseClient:
    def __init__(self) -> None:
        self.released: list[str] = []

    async def release_session(self, session_id: str) -> str:
        self.released.append(session_id)
        return "evicted"

    async def send_request(self, env: object) -> None:
        raise AssertionError(env)

    async def send_request_stream(self, env: object):
        if False:  # pragma: no cover - keeps this an async generator
            yield env


class _HoldRegistry:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.released: list[str] = []

    def release_cron_permission_hold(self, session_id: str) -> None:
        if self.fail:
            raise RuntimeError("hold store down")
        self.released.append(session_id)


def _handler(client: _ReleaseClient) -> MessageHandler:
    _clear_message_handler_singleton()
    return MessageHandler(client)


def _resume_env(*, source: str = "permission_interrupt") -> SimpleNamespace:
    return SimpleNamespace(
        request_id="web-resume-1",
        params={
            "source": source,
            "request_id": "ask-1",
            "answers": [{"selected_options": ["本次允许"]}],
        },
    )


async def _settle(
    handler: MessageHandler,
    *,
    env: SimpleNamespace | None = None,
    still_awaiting_hitl: bool = False,
    cancelled: bool = False,
    request_error: BaseException | None = None,
) -> None:
    await handler._settle_cron_permission_hold_after_interrupt_stream(
        env=env or _resume_env(),
        session_id="cron_real_1",
        still_awaiting_hitl=still_awaiting_hitl,
        cancelled=cancelled,
        request_error=request_error,
    )


@pytest.mark.asyncio
async def test_settled_resume_releases_hold_and_runtime_slot() -> None:
    client = _ReleaseClient()
    handler = _handler(client)
    registry = _HoldRegistry()
    handler.set_cron_registry(registry)

    await _settle(handler)

    assert registry.released == ["cron_real_1"]
    assert client.released == ["cron_real_1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "source"),
    [
        ({"still_awaiting_hitl": True}, "permission_interrupt"),
        ({"cancelled": True}, "permission_interrupt"),
        ({"request_error": RuntimeError("resume failed")}, "permission_interrupt"),
        ({}, "user"),
    ],
)
async def test_unsettled_or_followup_does_not_release(
    kwargs: dict,
    source: str,
) -> None:
    client = _ReleaseClient()
    handler = _handler(client)
    registry = _HoldRegistry()
    handler.set_cron_registry(registry)

    await _settle(handler, env=_resume_env(source=source), **kwargs)

    assert registry.released == []
    assert client.released == []


@pytest.mark.asyncio
async def test_followup_inflight_keeps_runtime_slot() -> None:
    client = _ReleaseClient()
    handler = _handler(client)
    registry = _HoldRegistry()
    handler.set_cron_registry(registry)

    async def _park() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_park())
    handler._stream_tasks["follow-up"] = task
    handler._stream_sessions["follow-up"] = "cron_real_1"
    try:
        await _settle(handler)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert registry.released == ["cron_real_1"]
    assert client.released == []


@pytest.mark.asyncio
async def test_hold_release_failure_keeps_runtime_slot() -> None:
    client = _ReleaseClient()
    handler = _handler(client)
    handler.set_cron_registry(_HoldRegistry(fail=True))

    await _settle(handler)

    assert client.released == []
