# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Verify host assembly, real extension ZIPs, and persistent store lifecycle."""

import json
import logging
from pathlib import Path
from zipfile import ZipFile

import pytest

from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.event_types.context_usage import (
    ContextUsageUpdate,
    register_context_usage_events,
)
from jiuwenswarm.extensions_v2.management import ExtensionManager
from tests.unit_tests.extensions_v2.management.support import make_bus

PACKAGE_ID = "example.store-probe"

_EXTENSION_PY = """\
import asyncio
import logging

from openjiuwen.core.events import Event, EventHandlerOps, EventNext
from openjiuwen.core.extension import ExtensionRegistrar

logger = logging.getLogger("jiuwenswarm.server.extensions.store_probe")


class StoreProbe:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def register(self, registrar: ExtensionRegistrar) -> None:
        registrar.register_event_handler("context.usage.updated", self.on_usage, {})

    async def on_usage(self, event: Event, ops: EventHandlerOps, next_handler: EventNext) -> object:
        async with self._lock:
            await self.probe(ops)
        return await next_handler(event)

    async def probe(self, ops: EventHandlerOps) -> None:
        package_id = await ops.package.name()
        previous = await ops.store.get("counter", 0)
        count = previous + 1
        saved = await ops.store.set("counter", count)
        logger.info("[store-probe] set package=%s previous=%s current=%s saved=%s", package_id, previous, count, saved)
        if not saved:
            return
        restored = await ops.store.get("counter")
        scratch_saved = await ops.store.set("scratch", {"temporary": True})
        keys = await ops.store.keys()
        deleted = await ops.store.delete("scratch")
        remaining = await ops.store.keys()
        logger.info(
            "[store-probe] get=%s keys=%s scratch_saved=%s delete=%s remaining=%s session=%s",
            restored, keys, scratch_saved, deleted, remaining,
            ops.session_context.session_id if ops.session_context else None,
        )


def create_extension() -> StoreProbe:
    return StoreProbe()
"""

_MANIFEST = """{
  "schemaVersion": 1,
  "package_type": "extension",
  "package": {
    "domain": "agentserver",
    "id": "example.store-probe",
    "version": "1.0.0"
  },
  "display": {
    "name": "Store Probe"
  },
  "extensions": [
    {
      "id": "store-probe",
      "runtime": "backend",
      "module": "extension.py"
    }
  ]
}
"""


@pytest.fixture
def store_probe_logs(caplog):
    logger = logging.getLogger("jiuwenswarm.server.extensions.store_probe")
    caplog.set_level(logging.INFO, logger=logger.name)
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def _archive(tmp_path: Path, source: Path | None = None) -> Path:
    if source is None:
        source = tmp_path / "store_probe_src"
        source.mkdir(exist_ok=True)
        (source / "extension.py").write_text(_EXTENSION_PY, encoding="utf-8")
        (source / "manifest.json").write_text(_MANIFEST, encoding="utf-8")
    archive = tmp_path / f"{source.name}.zip"
    with ZipFile(archive, "w") as target:
        for path in sorted(source.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                target.write(path, path.relative_to(source))
    return archive


async def _manager(tmp_path: Path):
    bus = make_bus()
    register_context_usage_events(bus)
    manager = ExtensionManager(
        tmp_path / "installed",
        store_dir=tmp_path / "data",
        report_path=tmp_path / "report.yaml",
    )
    await manager.init(bus)
    return manager, bus


async def _trigger(bus, session_id: str) -> None:
    result = await bus.trigger(
        Event("context.usage.updated", ContextUsageUpdate(session_id=session_id, request_id=f"req-{session_id}")),
        SessionContext(session_id),
    )
    assert result.accepted


async def test_zip_crud_logging_reload_cross_session_restart_and_uninstall(tmp_path, store_probe_logs) -> None:
    manager, bus = await _manager(tmp_path)
    reports = await manager.install([_archive(tmp_path)])
    assert reports[0].status == "SUCCESS"
    path = tmp_path / "data" / f"{PACKAGE_ID}.json"
    assert not path.exists()
    await _trigger(bus, "first")
    assert json.loads(path.read_text(encoding="utf-8")) == {"counter": 1}
    await _trigger(bus, "second")
    assert json.loads(path.read_text(encoding="utf-8")) == {"counter": 2}
    assert (await manager.reload(PACKAGE_ID)).status == "SUCCESS"
    await _trigger(bus, "third")
    assert json.loads(path.read_text(encoding="utf-8")) == {"counter": 3}
    await manager.shutdown()
    assert path.exists()
    restarted, other_bus = await _manager(tmp_path)
    assert (await restarted.load_installed())[0].status == "SUCCESS"
    await _trigger(other_bus, "fourth")
    assert json.loads(path.read_text(encoding="utf-8")) == {"counter": 4}
    log_output = store_probe_logs.text
    assert "previous=3 current=4 saved=True" in log_output
    assert "get=4 keys=['counter', 'scratch'] scratch_saved=True delete=True remaining=['counter']" in log_output
    assert (await restarted.uninstall(PACKAGE_ID)).status == "SUCCESS"
    assert not path.exists()
    await restarted.shutdown()


async def test_inactive_uninstall_delegates_to_core_and_deletes_data(tmp_path) -> None:
    manager, bus = await _manager(tmp_path)
    assert (await manager.install([_archive(tmp_path)]))[0].status == "SUCCESS"
    await _trigger(bus, "first")
    await manager.shutdown()
    restarted, _ = await _manager(tmp_path)
    assert restarted.active_packages() == ()
    assert (await restarted.uninstall(PACKAGE_ID)).status == "SUCCESS"
    assert not (tmp_path / "data" / f"{PACKAGE_ID}.json").exists()
    assert not (tmp_path / "installed" / PACKAGE_ID).exists()
    await restarted.shutdown()


async def test_cleanup_failure_retains_store_and_can_retry(tmp_path) -> None:
    attempts = []

    async def cleanup(package_id: str, generation: int, reason: str) -> None:
        attempts.append(reason)
        assert manager.registry.runtime.get_active(package_id) is not None
        assert (tmp_path / "data" / f"{package_id}.json").exists()
        if len(attempts) == 1:
            raise RuntimeError("host resource cleanup failed")

    bus = make_bus()
    register_context_usage_events(bus)
    manager = ExtensionManager(
        tmp_path / "installed",
        store_dir=tmp_path / "data",
        report_path=tmp_path / "report.yaml",
    )
    await manager.init(bus, cleanup_callbacks=(cleanup,))
    assert (await manager.install([_archive(tmp_path)]))[0].status == "SUCCESS"
    await _trigger(bus, "first")
    failed = await manager.uninstall(PACKAGE_ID)
    assert failed.status == "FAILED"
    assert (tmp_path / "data" / f"{PACKAGE_ID}.json").exists()
    assert manager.registry.runtime.get_active(PACKAGE_ID) is not None
    assert (await manager.uninstall(PACKAGE_ID)).status == "SUCCESS"
    assert attempts == ["uninstall", "uninstall"]
    await manager.shutdown()


async def test_agentserver_assembly_injects_the_host_store_directory(tmp_path, monkeypatch) -> None:
    await assembly.shutdown_extensions()
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "installed")
    instance = await assembly.install_extensions()
    assert instance is not None
    try:
        assert (await instance.manager.install([_archive(tmp_path)]))[0].status == "SUCCESS"
        await _trigger(instance.bus, "assembled")
        path = tmp_path / "package-data" / f"{PACKAGE_ID}.json"
        assert json.loads(path.read_text(encoding="utf-8")) == {"counter": 1}
        assert (await instance.manager.uninstall(PACKAGE_ID)).status == "SUCCESS"
        assert not path.exists()
    finally:
        await assembly.shutdown_extensions()
