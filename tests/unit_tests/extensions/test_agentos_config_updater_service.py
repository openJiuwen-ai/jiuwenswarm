# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for AgentOS config updater service lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.extensions.agentos.config_updater.service import (
    ApplyResult,
    ConfigUpdaterService,
    build_refresh_handler,
)


class WithOverrides:
    def __init__(self) -> None:
        self.applied: list[dict] = []

    def apply_remote_overrides(self, overrides: dict) -> None:
        self.applied.append(overrides)


class WithoutOverrides:
    pass


class FakeClient:
    """Stands in for ConfigUpdaterClient; records lifecycle calls."""

    def __init__(self) -> None:
        self.watch_called = asyncio.Event()
        self.closed = False
        self.cancelled = False

    async def watch_loop(self, on_event) -> None:
        self.watch_called.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self) -> None:
        self.closed = True


def _service(*, endpoints: list[str], client: FakeClient | None = None,
              refresh_handler=None):
    return ConfigUpdaterService(
        etcd_endpoints=endpoints,
        config={"sandbox": {"type": "yuanrong"}},
        refresh_handler=refresh_handler,
        client=client,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# build_refresh_handler
# --------------------------------------------------------------------------

def test_build_refresh_handler_noop_without_method():
    handler = build_refresh_handler(WithoutOverrides())
    # Must be callable and harmless, not None.
    assert handler({"gateway": {}}) is None


def test_build_refresh_handler_forwards_merged_document():
    client = WithOverrides()
    handler = build_refresh_handler(client)

    merged = {"gateway": {"agentos": {"sandbox_idle_timeout_seconds": 120}}}
    handler(merged)

    assert client.applied == [merged]


# --------------------------------------------------------------------------
# enabled
# --------------------------------------------------------------------------

def test_disabled_without_endpoints():
    assert _service(endpoints=[]).enabled is False


def test_disabled_with_blank_endpoints():
    assert _service(endpoints=["", "   "]).enabled is False


def test_enabled_with_endpoints():
    assert _service(endpoints=["http://etcd.test:2379"]).enabled is True


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

async def test_start_is_noop_without_endpoints():
    service = _service(endpoints=[])
    await service.start()
    await service.stop()  # safe


async def test_start_creates_watcher_and_stop_cancels_it():
    fake = FakeClient()
    service = _service(endpoints=["http://etcd.test:2379"], client=fake)

    await service.start()
    await asyncio.wait_for(fake.watch_called.wait(), timeout=2)

    await service.stop()

    assert fake.cancelled is True
    assert fake.closed is True


async def test_start_is_idempotent():
    fake = FakeClient()
    service = _service(endpoints=["http://etcd.test:2379"], client=fake)

    await service.start()
    await asyncio.wait_for(fake.watch_called.wait(), timeout=2)
    await service.start()  # second call must not spawn a second task

    await service.stop()


async def test_stop_without_start_is_safe():
    service = _service(endpoints=["http://etcd.test:2379"])
    await service.stop()


async def test_stop_closes_client_even_on_cancel():
    fake = FakeClient()
    service = _service(endpoints=["http://etcd.test:2379"], client=fake)

    await service.start()
    await asyncio.wait_for(fake.watch_called.wait(), timeout=2)
    await service.stop()

    assert fake.closed is True


async def test_refresh_handler_is_wired_into_applier():
    """The injected handler must be the one the applier calls."""
    fake = FakeClient()
    seen: list[dict] = []

    async def refresh(merged: dict) -> None:
        seen.append(merged)

    service = _service(
        endpoints=["http://etcd.test:2379"],
        client=fake,
        refresh_handler=refresh,
    )
    await service.start()
    await asyncio.wait_for(fake.watch_called.wait(), timeout=2)
    await service.stop()
    # Wiring is exercised end-to-end in test_applier; here we only assert the
    # service accepted and stored it without error.
    assert seen == []


async def test_watch_event_refreshes_full_merged_snapshot():
    fake = FakeClient()
    seen: list[dict] = []

    async def refresh(merged: dict) -> None:
        seen.append(merged)

    service = ConfigUpdaterService(
        etcd_endpoints=["http://etcd.test:2379"],
        config={
            "gateway": {"cron": {"store_backend": "etcd"}},
            "sandbox": {"type": "yuanrong", "cpu": 1000},
        },
        refresh_handler=refresh,
        client=fake,  # type: ignore[arg-type]
    )

    async def watch_loop(on_event):
        fetched = type(
            "Fetched",
            (),
            {
                "section": {
                    "gateway": {
                        "agentos": {"sandbox_idle_timeout_seconds": 120}
                    },
                    "sandbox": {"cpu": 2000, "memory": 4096},
                },
                "mod_revision": 5,
            },
        )()
        await on_event(fetched)
        fake.watch_called.set()
        await asyncio.sleep(3600)

    fake.watch_loop = watch_loop  # type: ignore[method-assign]
    await service.start()
    await asyncio.wait_for(fake.watch_called.wait(), timeout=2)

    assert seen == [
        {
            "gateway": {
                "cron": {"store_backend": "etcd"},
                "agentos": {"sandbox_idle_timeout_seconds": 120},
            },
            "sandbox": {"type": "yuanrong", "cpu": 2000, "memory": 4096},
        }
    ]
    await service.stop()


async def test_retryable_apply_retries_without_restarting_watch(monkeypatch):
    fake = FakeClient()
    service = _service(
        endpoints=["http://etcd.test:2379"],
        client=fake,
    )
    attempts = 0

    class FakeApplier:
        async def apply(self, fetched):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return ApplyResult(
                    skipped_reason="update-failed",
                    errors=["temporary read failure"],
                    retryable=True,
                )
            return ApplyResult(applied=True)

    async def watch_loop(on_event):
        await on_event(type("Fetched", (), {"mod_revision": 5})())
        fake.watch_called.set()
        await asyncio.sleep(3600)

    fake.watch_loop = watch_loop  # type: ignore[method-assign]
    monkeypatch.setattr(
        "jiuwenswarm.extensions.agentos.config_updater.service._APPLY_RETRY_INITIAL_DELAY",
        0.01,
    )
    task = asyncio.create_task(service._watch(fake, FakeApplier()))  # type: ignore[arg-type]
    await asyncio.wait_for(fake.watch_called.wait(), timeout=2)

    assert attempts == 2
    assert fake.closed is False

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.closed is True
