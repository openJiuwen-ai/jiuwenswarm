from __future__ import annotations

import pytest

from jiuwenswarm.runtime import host_services


@pytest.fixture(autouse=True)
def _isolate_host_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_services, "_runtime_push_handler", None)
    monkeypatch.setattr(host_services, "_runtime_wake_handler", None)
    monkeypatch.setattr(host_services, "_runtime_xiaoyi_channel_provider", None)
    monkeypatch.setattr(host_services, "_runtime_push_handlers", [])
    monkeypatch.setattr(host_services, "_runtime_wake_handlers", [])
    monkeypatch.setattr(host_services, "_runtime_xiaoyi_channel_providers", [])


@pytest.mark.asyncio
async def test_push_owner_out_of_order_restore_does_not_revive_stale_owner() -> None:
    calls: list[str] = []

    async def first(_message: dict) -> None:
        calls.append("first")

    async def second(_message: dict) -> None:
        calls.append("second")

    first_previous = host_services.install_runtime_push_handler(first)
    second_previous = host_services.install_runtime_push_handler(second)

    host_services.restore_runtime_push_handler(first, first_previous)
    assert await host_services.send_runtime_push({"value": 1}) is True
    assert calls == ["second"]

    host_services.restore_runtime_push_handler(second, second_previous)
    assert await host_services.send_runtime_push({"value": 2}) is False
    assert calls == ["second"]


@pytest.mark.asyncio
async def test_wake_owner_out_of_order_restore_does_not_revive_stale_owner() -> None:
    calls: list[str] = []

    async def first(_message: object) -> None:
        calls.append("first")

    async def second(_message: object) -> None:
        calls.append("second")

    first_previous = host_services.install_runtime_wake_handler(first)
    second_previous = host_services.install_runtime_wake_handler(second)

    host_services.restore_runtime_wake_handler(first, first_previous)
    assert await host_services.send_runtime_wake(object()) is True
    assert calls == ["second"]

    host_services.restore_runtime_wake_handler(second, second_previous)
    assert await host_services.send_runtime_wake(object()) is False
    assert calls == ["second"]


def test_xiaoyi_owner_out_of_order_restore_does_not_revive_stale_owner() -> None:
    first_channel = object()
    second_channel = object()

    def first(_channel_id: str) -> object:
        return first_channel

    def second(_channel_id: str) -> object:
        return second_channel

    first_previous = host_services.install_runtime_xiaoyi_channel_provider(first)
    second_previous = host_services.install_runtime_xiaoyi_channel_provider(second)

    host_services.restore_runtime_xiaoyi_channel_provider(first, first_previous)
    assert host_services.get_runtime_xiaoyi_channel() is second_channel

    host_services.restore_runtime_xiaoyi_channel_provider(second, second_previous)
    assert host_services.get_runtime_xiaoyi_channel() is None


@pytest.mark.asyncio
async def test_runtime_host_push_transport_has_explicit_unavailable_contract() -> None:
    transport = host_services.RuntimeHostPushTransport()

    with pytest.raises(RuntimeError, match="without a resident host"):
        await transport.send_push({"value": 1})

    captured: list[dict] = []

    async def capture(message: dict) -> None:
        captured.append(message)

    previous = host_services.install_runtime_push_handler(capture)
    try:
        await transport.send_push({"value": 2})
    finally:
        host_services.restore_runtime_push_handler(capture, previous)

    assert captured == [{"value": 2}]


@pytest.mark.asyncio
async def test_runtime_push_propagates_explicit_delivery_failure() -> None:
    async def reject(_message: dict) -> bool:
        return False

    previous = host_services.install_runtime_push_handler(reject)
    try:
        assert await host_services.send_runtime_push({"value": 1}) is False
        with pytest.raises(RuntimeError, match="without a resident host"):
            await host_services.RuntimeHostPushTransport().send_push({"value": 2})
    finally:
        host_services.restore_runtime_push_handler(reject, previous)
