# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fixtures shared by Extensions V2 package-management tests."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from zipfile import ZipFile

from openjiuwen.core.events import Event, ExtensionEventBus


EMPTY_EXTENSION = """\
class Extension:
    def register(self, registrar):
        pass


def create_extension():
    return Extension()
"""


METADATA_EXTENSION = """\
class Extension:
    def register(self, registrar):
        registrar.register_event_handler("test.event", self.handle, {})

    async def handle(self, event, ops, next_handler):
        metadata = ops.package_metadata
        return (metadata.package_id, metadata.package_version, str(metadata.root))


def create_extension():
    return Extension()
"""


PATTERN_EXTENSION = """\
class Extension:
    def register(self, registrar):
        registrar.register_event_handler("test.event", self.handle, {"target": "a"})

    async def handle(self, event, ops, next_handler):
        return ("user", event.event_input)


def create_extension():
    return Extension()
"""


BLOCKING_EXTENSION = """\
import asyncio


class Extension:
    def register(self, registrar):
        registrar.register_event_handler("test.event", self.handle, {})

    async def handle(self, event, ops, next_handler):
        await asyncio.sleep(60)
        return "done"


def create_extension():
    return Extension()
"""


CORE_LEVEL_EXTENSION = """\
class Extension:
    def register(self, registrar):
        registrar.register_event_handler("test.event", self.handle, {}, "core")

    async def handle(self, event, ops, next_handler):
        return "user"


def create_extension():
    return Extension()
"""


TWO_HANDLERS_EXTENSION = """\
class Extension:
    def register(self, registrar):
        registrar.register_event_handler("test.event", self.handle, {})
        registrar.register_event_handler("missing.event", self.handle, {})

    async def handle(self, event, ops, next_handler):
        return "user"


def create_extension():
    return Extension()
"""


def write_package(
    root: Path,
    package_id: str = "test.pkg",
    version: str = "1.0.0",
    *,
    module_source: str | None = None,
    requires: Sequence[tuple[str, str]] = (),
    enable: bool | None = None,
    module: str = "extension.py",
    write_module: bool = True,
) -> Path:
    """Write a package directory with a manifest and optional entry module."""

    package = root / f"{package_id}-{version}"
    package.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "package_type": "extension",
        "schemaVersion": 1,
        "package": {
            "id": package_id,
            "domain": "agentserver",
            "version": version,
        },
        "display": {"name": package_id},
        "extensions": [
            {
                "id": "main",
                "runtime": "backend",
                "module": module,
            }
        ],
    }
    if requires:
        manifest["requires"] = {
            "packages": [
                {"id": required_id, "version": specifier}
                for required_id, specifier in requires
            ]
        }
    if enable is not None:
        manifest["enable"] = enable
    (package / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if write_module:
        entry = package / module
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(module_source or EMPTY_EXTENSION, encoding="utf-8")
    return package


def archive_package(root: Path, *, suffix: str = ".zip") -> Path:
    """Create a flat ZIP source accepted by the external management API."""
    archive = root.parent / f"{root.name}{suffix}"
    with ZipFile(archive, "w") as package_zip:
        for file in root.rglob("*"):
            if file.is_file():
                package_zip.write(file, file.relative_to(root))
    return archive


def make_bus() -> ExtensionEventBus:
    """Create a bus with one matchable test event."""

    bus = ExtensionEventBus()

    async def core(event: Event, ops: object, next_handler: object) -> tuple[str, object]:
        del ops, next_handler
        return ("core", event.event_input)

    bus.register_event_type("test.event", _matches, core)
    return bus


async def freeze_bus(bus: ExtensionEventBus) -> None:
    """Run the host-required first ``ops.build`` trigger."""

    await bus.trigger(Event("ops.build", None))


def _matches(event_input: object, match_pattern: Mapping[str, object]) -> bool:
    if not match_pattern:
        return True
    if not isinstance(event_input, Mapping):
        return False
    for path, expected in match_pattern.items():
        current: object = event_input
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return False
            current = current[part]
        if current != expected:
            return False
    return True
