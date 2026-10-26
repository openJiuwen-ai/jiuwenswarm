# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.gateway.config_poll.appliers import TableApplyContext
from jiuwenswarm.gateway.config_poll.scheduler import (
    ConfigPollScheduler,
    config_poll_enabled,
    config_poll_interval_seconds,
)
from jiuwenswarm.gateway.config_poll.syncer import ConfigPollSyncer


def test_config_poll_disabled_without_agent_runtime(monkeypatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.delenv("GATEWAY_CONFIG_POLL_ENABLED", raising=False)
    assert config_poll_enabled() is False


def test_config_poll_enabled_with_agent_runtime(monkeypatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.delenv("GATEWAY_CONFIG_POLL_ENABLED", raising=False)
    with patch(
        "jiuwenswarm.gateway.config_poll.scheduler.get_config",
        return_value={"gateway": {}},
    ):
        assert config_poll_enabled() is True
        assert config_poll_interval_seconds() >= 1.0


def test_config_poll_respects_yaml_enabled_false(monkeypatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.delenv("GATEWAY_CONFIG_POLL_ENABLED", raising=False)
    with patch(
        "jiuwenswarm.gateway.config_poll.scheduler.get_config",
        return_value={"gateway": {"config_poll": {"enabled": False}}},
    ):
        assert config_poll_enabled() is False


@pytest.mark.asyncio
async def test_syncer_applies_when_row_updated_at_changes() -> None:
    ts1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    apply_mock = AsyncMock()
    syncer = ConfigPollSyncer()

    async def _rows(table: str) -> list[dict]:
        if table != "logging_config":
            return []
        ts = ts1 if "logging_config" not in syncer._last_rows else ts2
        return [{"updated_at": ts.isoformat()}]

    with patch(
        "jiuwenswarm.gateway.config_poll.syncer.list_table_records",
        side_effect=_rows,
    ), patch.dict(
        "jiuwenswarm.gateway.config_poll.syncer.TABLE_APPLIERS",
        {"logging_config": apply_mock},
        clear=False,
    ):
        await syncer.run_once()
        apply_mock.assert_awaited_once()
        assert syncer._last_rows["logging_config"] == {"default": ts1.isoformat()}

        await syncer.run_once()
        assert apply_mock.await_count == 2
        assert syncer._last_rows["logging_config"] == {"default": ts2.isoformat()}


@pytest.mark.asyncio
async def test_syncer_skips_when_snapshot_unchanged() -> None:
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    apply_mock = AsyncMock()
    syncer = ConfigPollSyncer()

    async def _rows(table: str) -> list[dict]:
        if table != "logging_config":
            return []
        return [{"updated_at": ts.isoformat()}]

    with patch(
        "jiuwenswarm.gateway.config_poll.syncer.list_table_records",
        side_effect=_rows,
    ), patch.dict(
        "jiuwenswarm.gateway.config_poll.syncer.TABLE_APPLIERS",
        {"logging_config": apply_mock},
        clear=False,
    ):
        await syncer.run_once()
        await syncer.run_once()
        apply_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_syncer_applies_when_all_rows_deleted() -> None:
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    apply_mock = AsyncMock()
    syncer = ConfigPollSyncer()

    async def _rows(table: str) -> list[dict]:
        if table != "logging_config":
            return []
        if "logging_config" not in syncer._last_rows:
            return [{"updated_at": ts.isoformat()}]
        return []

    with patch(
        "jiuwenswarm.gateway.config_poll.syncer.list_table_records",
        side_effect=_rows,
    ), patch.dict(
        "jiuwenswarm.gateway.config_poll.syncer.TABLE_APPLIERS",
        {"logging_config": apply_mock},
        clear=False,
    ):
        await syncer.run_once()
        await syncer.run_once()
        assert apply_mock.await_count == 2
        assert syncer._last_rows["logging_config"] == {}


@pytest.mark.asyncio
async def test_syncer_detects_deleted_channel_row() -> None:
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    apply_mock = AsyncMock()
    syncer = ConfigPollSyncer()

    async def _rows(table: str) -> list[dict]:
        if table != "channel_config":
            return []
        if "channel_config" not in syncer._last_rows:
            return [
                {
                    "channel_id": "c1",
                    "status": "active",
                    "updated_at": ts.isoformat(),
                }
            ]
        return [
            {
                "channel_id": "c2",
                "status": "active",
                "updated_at": ts.isoformat(),
            }
        ]

    with patch(
        "jiuwenswarm.gateway.config_poll.syncer.list_table_records",
        side_effect=_rows,
    ), patch.dict(
        "jiuwenswarm.gateway.config_poll.syncer.TABLE_APPLIERS",
        {"channel_config": apply_mock},
        clear=False,
    ):
        await syncer.run_once()
        await syncer.run_once()
        assert apply_mock.await_count == 2
        second_ctx = apply_mock.await_args_list[1].args[0]
        assert second_ctx.removed_channel_ids == frozenset({"c1"})


@pytest.mark.asyncio
async def test_syncer_keeps_snapshot_when_apply_fails() -> None:
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    apply_mock = AsyncMock(side_effect=RuntimeError("boom"))
    syncer = ConfigPollSyncer()

    async def _rows(table: str) -> list[dict]:
        if table != "logging_config":
            return []
        return [{"updated_at": ts.isoformat()}]

    with patch(
        "jiuwenswarm.gateway.config_poll.syncer.list_table_records",
        side_effect=_rows,
    ), patch.dict(
        "jiuwenswarm.gateway.config_poll.syncer.TABLE_APPLIERS",
        {"logging_config": apply_mock},
        clear=False,
    ):
        await syncer.run_once()
        assert apply_mock.await_count == 1
        assert "logging_config" not in syncer._last_rows

        apply_mock.side_effect = None
        await syncer.run_once()
        assert apply_mock.await_count == 2
        assert syncer._last_rows["logging_config"] == {"default": ts.isoformat()}


@pytest.mark.asyncio
async def test_syncer_normalizes_updated_at_for_snapshot() -> None:
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    apply_mock = AsyncMock()
    syncer = ConfigPollSyncer()
    calls = {"n": 0}

    async def _rows(table: str) -> list[dict]:
        if table != "logging_config":
            return []
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"updated_at": ts}]
        return [{"updated_at": ts.isoformat()}]

    with patch(
        "jiuwenswarm.gateway.config_poll.syncer.list_table_records",
        side_effect=_rows,
    ), patch.dict(
        "jiuwenswarm.gateway.config_poll.syncer.TABLE_APPLIERS",
        {"logging_config": apply_mock},
        clear=False,
    ):
        await syncer.run_once()
        await syncer.run_once()
        apply_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_logging_config_table_uses_passed_rows() -> None:
    from jiuwenswarm.gateway.config_poll.appliers import apply_logging_config_table

    with patch(
        "jiuwenswarm.common.utils.apply_logging_config_payload",
    ) as apply_payload:
        await apply_logging_config_table(
            TableApplyContext(
                rows=[{"level": "DEBUG", "gateway": "WARNING"}],
            ),
        )
        apply_payload.assert_called_once_with(
            {
                "level": "DEBUG",
                "gateway": "WARNING",
                "console_level": None,
                "channel": None,
                "agent_server": None,
                "full": None,
            }
        )


@pytest.mark.asyncio
async def test_scheduler_start_stop() -> None:
    scheduler = ConfigPollScheduler(enabled=True, interval_seconds=0.05)
    run_once = AsyncMock()
    scheduler._syncer.run_once = run_once  # type: ignore[method-assign]

    await scheduler.start()
    await asyncio.sleep(0.12)
    await scheduler.stop()

    assert run_once.await_count >= 1


@pytest.mark.asyncio
async def test_scheduler_disabled_does_not_start_loop() -> None:
    scheduler = ConfigPollScheduler(enabled=False, interval_seconds=0.05)
    run_once = AsyncMock()
    scheduler._syncer.run_once = run_once  # type: ignore[method-assign]

    await scheduler.start()
    await asyncio.sleep(0.08)
    await scheduler.stop()

    run_once.assert_not_awaited()
