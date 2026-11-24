"""Instance records stay bounded, atomic and free of finished recovery tasks."""

import asyncio
from datetime import datetime, timedelta

from jiuwenswarm.extensions.app_market.app_market import storage as storage_module
from jiuwenswarm.extensions.app_market.app_market.market_service import MarketService
from jiuwenswarm.extensions.app_market.app_market.models import AppInstance
from jiuwenswarm.extensions.app_market.app_market.storage import (
    MAX_STOPPED_INSTANCES_PER_APP,
    MarketStorage,
)
from jiuwenswarm.extensions.app_market.manager import AppMarketManager


def _created(day: int) -> str:
    return (datetime(2026, 11, 1) + timedelta(days=day)).isoformat()


def _stopped(instance_id: str, *, app_id: str = "app", day: int = 0) -> AppInstance:
    return AppInstance(
        instance_id=instance_id,
        app_id=app_id,
        state="stopped",
        created_at=_created(day),
    )


def test_instance_record_replacement_is_atomic(tmp_path, monkeypatch):
    storage = MarketStorage(str(tmp_path))
    instance = _stopped("i1")
    storage.save_instance(instance)
    record = tmp_path / "instances" / "i1.json"
    original = record.read_bytes()

    def fail_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(storage_module.os, "replace", fail_replace)
    instance.state = "running"
    storage.save_instance(instance)

    assert record.read_bytes() == original
    assert not list((tmp_path / "instances").glob("*.tmp"))


def test_prune_keeps_newest_stopped_records_and_active_instances(tmp_path):
    storage = MarketStorage(str(tmp_path))
    total = MAX_STOPPED_INSTANCES_PER_APP + 3
    for index in range(total):
        storage.save_instance(_stopped(f"old{index}", day=index))
    storage.save_instance(
        AppInstance(instance_id="live", app_id="app", state="running", created_at=_created(total))
    )
    storage.save_instance(_stopped("other-app", app_id="other"))

    storage.prune_instances("app")

    records = {item.instance_id: item for item in storage.list_instances("app")}
    stopped = {instance_id for instance_id, item in records.items() if item.state == "stopped"}
    assert len(stopped) == MAX_STOPPED_INSTANCES_PER_APP
    assert stopped == {
        f"old{index}"
        for index in range(total - MAX_STOPPED_INSTANCES_PER_APP, total)
    }
    assert records["live"].state == "running"
    assert storage.load_instance("other-app") is not None


def test_stop_app_prunes_old_records(tmp_path):
    async def scenario():
        service = MarketService(str(tmp_path))
        total = MAX_STOPPED_INSTANCES_PER_APP + 3
        for index in range(total):
            service._storage.save_instance(_stopped(f"old{index}", day=index))
        current = AppInstance(
            instance_id="current",
            app_id="app",
            state="running",
            created_at=_created(total),
        )
        service._storage.save_instance(current)

        assert await service.stop_app("current")

        records = service._storage.list_instances("app")
        stopped = [item for item in records if item.state == "stopped"]
        assert len(stopped) == MAX_STOPPED_INSTANCES_PER_APP
        assert any(item.instance_id == "current" for item in stopped)
        assert not any(item.instance_id == "old0" for item in records)

    asyncio.run(scenario())


def test_failed_start_prunes_old_records_and_keeps_detail(tmp_path):
    async def scenario():
        service = MarketService(str(tmp_path))
        total = MAX_STOPPED_INSTANCES_PER_APP + 3
        for index in range(total):
            service._storage.save_instance(_stopped(f"old{index}", day=index))
        failed = AppInstance(
            instance_id="failed",
            app_id="app",
            state="starting",
            created_at=_created(total),
        )
        service._storage.save_instance(failed)

        await service._finish_failed_start(failed, None, "启动超时")

        records = service._storage.list_instances("app")
        stopped = [item for item in records if item.state == "stopped"]
        assert len(stopped) == MAX_STOPPED_INSTANCES_PER_APP
        kept = service._storage.load_instance("failed")
        assert kept is not None
        assert kept.detail == "启动超时"

    asyncio.run(scenario())


def test_market_service_prunes_legacy_records_on_start(tmp_path):
    storage = MarketStorage(str(tmp_path))
    for index in range(MAX_STOPPED_INSTANCES_PER_APP + 4):
        storage.save_instance(_stopped(f"old{index}", day=index))
    storage.save_instance(
        AppInstance(instance_id="live", app_id="app", state="running", created_at=_created(99))
    )

    MarketService(str(tmp_path))

    records = {item.instance_id: item for item in storage.list_instances("app")}
    assert records["live"].state == "running"
    assert len([item for item in records.values() if item.state == "stopped"]) == (
        MAX_STOPPED_INSTANCES_PER_APP
    )


def test_finished_recovery_task_is_dropped(tmp_path):
    async def scenario():
        manager = AppMarketManager(str(tmp_path))
        manager._service._storage.save_instance(
            AppInstance(
                instance_id="dead",
                app_id="missing-app",
                port=18090,
                pid=2_000_000_000,
                state="running",
                status_token="token",
                created_at=_created(0),
            )
        )

        listed = manager.list_instances()["instances"]
        assert listed[0]["state"] == "stopping"
        assert "dead" in manager._recovery_tasks

        await asyncio.gather(*manager._recovery_tasks.values())
        await asyncio.sleep(0)

        assert "dead" not in manager._recovery_tasks
        await manager.shutdown()

    asyncio.run(scenario())
