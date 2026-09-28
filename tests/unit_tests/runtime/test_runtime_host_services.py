from __future__ import annotations

import pytest

from jiuwenswarm.runtime import host_services


@pytest.fixture(autouse=True)
def _isolate_host_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_services, "_runtime_push_handler", None)
    monkeypatch.setattr(host_services, "_runtime_push_handlers", [])


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
