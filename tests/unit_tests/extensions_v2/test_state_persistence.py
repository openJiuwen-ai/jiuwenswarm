# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Verify host assembly injects JsonState and session state survives restart."""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.event_types.context_usage import (
    ContextUsageUpdate,
    register_context_usage_events,
)
from jiuwenswarm.extensions_v2.management import ExtensionManager
from tests.unit_tests.extensions_v2.management.support import make_bus

PACKAGE_ID = "example.state-probe"

_EXTENSION_PY = '''\
import asyncio
from openjiuwen.core.events import Event, EventHandlerOps, EventNext
from openjiuwen.core.extension import ExtensionRegistrar
from openjiuwen.core.extension.data import StateRef

COUNT = StateRef("example.state-probe", "count")


class StateProbe:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def register(self, registrar: ExtensionRegistrar) -> None:
        registrar.register_event_handler("context.usage.updated", self.on_usage, {})

    async def on_usage(self, event: Event, ops: EventHandlerOps, next_handler: EventNext) -> object:
        async with self._lock:
            snapshot = await ops.state.get(COUNT)
            value = (snapshot.value if snapshot.exists else 0) + 1
            await ops.state.set(COUNT, value, expected_version=snapshot.version)
        return await next_handler(event)


def create_extension() -> StateProbe:
    return StateProbe()
'''

_MANIFEST = """\
schemaVersion: 1
package:
  id: example.state-probe
  version: 1.0.0
  displayName: State Probe
  description: Exercise durable session state after a chat round.
extensions:
  - id: state-probe
    runtime: backend
    module: extension.py
"""


def _archive(tmp_path: Path) -> Path:
    root = tmp_path / "state_probe_src"
    root.mkdir()
    (root / "extension.py").write_text(_EXTENSION_PY, encoding="utf-8")
    (root / "manifest.yaml").write_text(_MANIFEST, encoding="utf-8")
    archive = tmp_path / "state_probe.zip"
    with ZipFile(archive, "w") as target:
        for path in root.iterdir():
            target.write(path, path.name)
    return archive


def _session_state_path(state_dir: Path, session_id: str) -> Path:
    return state_dir / PACKAGE_ID / "sessions" / f"{session_id}.json"


async def _manager(tmp_path: Path):
    bus = make_bus()
    register_context_usage_events(bus)
    manager = ExtensionManager(
        tmp_path / "installed",
        store_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )
    await manager.init(bus)
    return manager, bus


async def _trigger(bus, session_id: str) -> None:
    result = await bus.trigger(
        Event(
            "context.usage.updated",
            ContextUsageUpdate(session_id=session_id, request_id=f"req-{session_id}"),
        ),
        SessionContext(session_id),
    )
    assert result.accepted


async def test_zip_state_survives_restart_and_uninstall_deletes(tmp_path) -> None:
    manager, bus = await _manager(tmp_path)
    assert (await manager.install([_archive(tmp_path)]))[0].status == "SUCCESS"
    await _trigger(bus, "alpha")
    path = _session_state_path(tmp_path / "state", "alpha")
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["count"]["value"] == 1
    await manager.shutdown()
    assert path.exists()

    restarted, other_bus = await _manager(tmp_path)
    assert (await restarted.load_installed())[0].status == "SUCCESS"
    await _trigger(other_bus, "alpha")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["count"]["value"] == 2
    assert (await restarted.uninstall(PACKAGE_ID)).status == "SUCCESS"
    assert not (tmp_path / "state" / PACKAGE_ID).exists()
    await restarted.shutdown()


async def test_agentserver_assembly_injects_the_host_state_directory(tmp_path, monkeypatch) -> None:
    await assembly.shutdown_extensions()
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "installed")
    instance = await assembly.install_extensions()
    assert instance is not None
    try:
        states = instance.manager._states
        assert states is not None
        assert states.directory == (tmp_path / "package-state").resolve()
        assert (await instance.manager.install([_archive(tmp_path)]))[0].status == "SUCCESS"
        await _trigger(instance.bus, "assembled")
        path = _session_state_path(tmp_path / "package-state", "assembled")
        assert json.loads(path.read_text(encoding="utf-8"))["count"]["value"] == 1
        assert (await instance.manager.uninstall(PACKAGE_ID)).status == "SUCCESS"
        assert not (tmp_path / "package-state" / PACKAGE_ID).exists()
    finally:
        await assembly.shutdown_extensions()
