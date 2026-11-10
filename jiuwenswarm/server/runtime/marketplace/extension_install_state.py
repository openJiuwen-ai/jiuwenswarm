# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent provenance for installed system plugin packages."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExtensionInstallRecord:
    package_id: str
    version: str
    origin: str
    asset_id: str = ""
    install_path: str = ""
    active: bool = True
    enabled: bool = True
    installed_at: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "ExtensionInstallRecord | None":
        package_id = value.get("package_id")
        version = value.get("version")
        origin = value.get("origin")
        if not isinstance(package_id, str) or not package_id.strip():
            return None
        if not isinstance(version, str) or not version.strip():
            return None
        if origin not in {"hub", "user", "preset"}:
            origin = "user"
        asset_id = value.get("asset_id")
        install_path = value.get("install_path")
        installed_at = value.get("installed_at")
        active = value.get("active")
        enabled = value.get("enabled")
        return cls(
            package_id=package_id.strip(),
            version=version.strip(),
            origin=origin,
            asset_id=asset_id.strip() if isinstance(asset_id, str) else "",
            install_path=install_path if isinstance(install_path, str) else "",
            active=active if isinstance(active, bool) else True,
            enabled=enabled if isinstance(enabled, bool) else True,
            installed_at=installed_at if isinstance(installed_at, str) else "",
        )


class ExtensionInstallState:
    """A small atomic JSON store keyed by ``package_id@version``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def for_install_dir(cls, install_dir: Path) -> "ExtensionInstallState":
        return cls(Path(install_dir).expanduser().resolve().parent / "extension" / "system_plugin_install_state.json")

    def _read(self) -> dict[str, ExtensionInstallRecord]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        packages = payload.get("packages") if isinstance(payload, dict) else None
        if not isinstance(packages, list):
            return {}
        result: dict[str, ExtensionInstallRecord] = {}
        for value in packages:
            if not isinstance(value, dict):
                continue
            record = ExtensionInstallRecord.from_dict(value)
            if record is not None:
                result[_key(record.package_id, record.version)] = record
        return result

    def _write(self, records: dict[str, ExtensionInstallRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                json.dump(
                    {"packages": [asdict(records[key]) for key in sorted(records)]},
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.write("\n")
            temp_path.replace(self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def get(self, package_id: str, version: str = "") -> ExtensionInstallRecord | None:
        records = [
            record
            for record in self._read().values()
            if record.package_id == package_id and (not version or record.version == version)
        ]
        if not records:
            return None
        return max(records, key=_record_preference)

    def by_asset(self, asset_id: str) -> ExtensionInstallRecord | None:
        if not asset_id:
            return None
        records = [record for record in self._read().values() if record.asset_id == asset_id]
        return max(records, key=_record_preference) if records else None

    def upsert(self, record: ExtensionInstallRecord) -> None:
        records = self._read()
        key = _key(record.package_id, record.version)
        previous = records.get(key)
        records[key] = ExtensionInstallRecord(
            package_id=record.package_id,
            version=record.version,
            origin=record.origin,
            asset_id=record.asset_id,
            install_path=record.install_path,
            active=record.active,
            enabled=record.enabled,
            installed_at=record.installed_at or _timestamp(),
        ) if previous is None else _updated(previous, record)
        self._write(records)

    def activate(self, record: ExtensionInstallRecord) -> None:
        """Record one version as the loaded copy and retire the package's other versions."""
        records = self._read()
        for key, existing in list(records.items()):
            if existing.package_id != record.package_id or existing.version == record.version:
                continue
            records[key] = ExtensionInstallRecord(
                package_id=existing.package_id,
                version=existing.version,
                origin=existing.origin,
                asset_id=existing.asset_id,
                install_path=existing.install_path,
                active=False,
                enabled=False,
                installed_at=existing.installed_at,
            )
        key = _key(record.package_id, record.version)
        previous = records.get(key)
        records[key] = ExtensionInstallRecord(
            package_id=record.package_id,
            version=record.version,
            origin=record.origin,
            asset_id=record.asset_id,
            install_path=record.install_path,
            active=True,
            enabled=True,
            installed_at=record.installed_at or (previous.installed_at if previous else "") or _timestamp(),
        )
        self._write(records)

    def mark(self, package_id: str, *, version: str = "", active: bool | None = None, enabled: bool | None = None) -> None:
        records = self._read()
        changed = False
        for key, record in list(records.items()):
            if record.package_id != package_id or (version and record.version != version):
                continue
            records[key] = ExtensionInstallRecord(
                package_id=record.package_id,
                version=record.version,
                origin=record.origin,
                asset_id=record.asset_id,
                install_path=record.install_path,
                active=record.active if active is None else active,
                enabled=record.enabled if enabled is None else enabled,
                installed_at=record.installed_at,
            )
            changed = True
        if changed:
            self._write(records)

    def remove(self, package_id: str, version: str = "") -> None:
        records = self._read()
        kept = {
            key: record
            for key, record in records.items()
            if record.package_id != package_id or (version and record.version != version)
        }
        if len(kept) != len(records):
            self._write(kept)


def _key(package_id: str, version: str) -> str:
    return f"{package_id}@{version}"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_preference(record: ExtensionInstallRecord) -> tuple[bool, bool, str]:
    """Prefer the loaded copy, then the enabled copy, then the newest record."""

    return record.active, record.enabled, record.installed_at


def _updated(previous: ExtensionInstallRecord, record: ExtensionInstallRecord) -> ExtensionInstallRecord:
    return ExtensionInstallRecord(
        package_id=record.package_id,
        version=record.version,
        origin=record.origin or previous.origin,
        asset_id=record.asset_id or previous.asset_id,
        install_path=record.install_path or previous.install_path,
        active=record.active,
        enabled=record.enabled,
        installed_at=record.installed_at or previous.installed_at or _timestamp(),
    )
