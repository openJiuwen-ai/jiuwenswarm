# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Verify host assembly, real extension ZIPs, and persistent store lifecycle."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.event_types.context_usage import (
    ContextUsageUpdate,
    notify_context_usage,
    register_context_usage_events,
)
from jiuwenswarm.extensions_v2.management import ExtensionManager
from tests.unit_tests.extensions_v2.management.support import make_bus

EXAMPLE = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "extensions_v2" / "examples" / "store_probe"
PACKAGE_ID = "example.store-probe"


@pytest.fixture
def store_probe_logs(caplog):
    logger = logging.getLogger("jiuwenswarm.server.extensions.store_probe")
    caplog.set_level(logging.INFO, logger=logger.name)
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def _archive(tmp_path: Path, source: Path = EXAMPLE) -> Path:
    archive = tmp_path / f"{source.name}.zip"
    with ZipFile(archive, "w") as target:
        for path in sorted(source.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                target.write(path, path.relative_to(source))
    return archive


async def _manager(tmp_path: Path):
    bus = make_bus()
    register_context_usage_events(bus)
    manager = ExtensionManager(tmp_path / "installed", store_dir=tmp_path / "data")
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
    manager = ExtensionManager(tmp_path / "installed", store_dir=tmp_path / "data")
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


@pytest.mark.parametrize("weather_first", [True, False])
async def test_usage_notifier_runs_both_packages_and_preserves_render_deduplication(
    tmp_path, monkeypatch, store_probe_logs, weather_first,
) -> None:
    await assembly.shutdown_extensions()
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "installed")
    frames = []

    async def send_push(message):
        frames.append(message["payload"])
        return True

    instance = await assembly.install_extensions(SimpleNamespace(send_push=send_push))
    assert instance is not None
    try:
        weather = _archive(tmp_path, EXAMPLE.parent / "token_weather")
        probe = _archive(tmp_path)
        for archive in ((weather, probe) if weather_first else (probe, weather)):
            report = (await instance.manager.install([archive]))[0]
            assert report.status == "SUCCESS", report
        update = ContextUsageUpdate("coexist", "r1", input_tokens=10, output_tokens=5, total_tokens=15)
        task = notify_context_usage(update)
        assert task is not None
        await task
        path = tmp_path / "package-data" / f"{PACKAGE_ID}.json"
        assert path.exists(), "The usage chain did not reach Store Probe"
        assert json.loads(path.read_text(encoding="utf-8")) == {"counter": 1}
        assert len(frames) == 2
        assert all(frame["event_type"] == "webui.render" for frame in frames)
        assert all(frame["root"]["component"] == "Box" for frame in frames)
        assert "previous=0 current=1 saved=True" in store_probe_logs.text

        task = notify_context_usage(update)
        assert task is not None
        await task
        assert json.loads(path.read_text(encoding="utf-8")) == {"counter": 2}
        assert len(frames) == 2
        assert "previous=1 current=2 saved=True" in store_probe_logs.text
    finally:
        await assembly.shutdown_extensions()
