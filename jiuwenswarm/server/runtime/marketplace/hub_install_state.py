# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent provenance for marketplace packages prepared from Hub."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from jiuwenswarm.server.runtime.marketplace.hub_asset_port import HubAssetKind
from jiuwenswarm.server.runtime.marketplace.hub_asset_type_adapter import (
    HubAssetTypeConflictError,
    resolve_hub_asset_kind,
)


@dataclass(frozen=True, slots=True)
class HubInstallRecord:
    asset_id: str
    kind: HubAssetKind
    version: str
    checksum_sha256: str
    installed_at: str
    package_id: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "HubInstallRecord | None":
        raw_kind = value.get("kind")
        if raw_kind not in {"agent_template", "plugin", "mcp"}:
            try:
                raw_kind = resolve_hub_asset_kind(
                    str(value.get("plugin_type") or "")
                )
            except HubAssetTypeConflictError:
                return None
        fields = {
            key: value.get(key)
            for key in ("asset_id", "version", "checksum_sha256", "installed_at")
        }
        if raw_kind is None or not all(
            isinstance(item, str) for item in fields.values()
        ):
            return None
        if not fields["asset_id"] or not fields["version"]:
            return None
        package_id = value.get("package_id")
        if not isinstance(package_id, str) or not package_id.strip():
            package_id = fields["asset_id"]
        return cls(kind=raw_kind, package_id=package_id.strip(), **fields)


class HubInstallStateStore:
    def __init__(self, kind_root: Path) -> None:
        self.path = kind_root / "hub_state.json"

    def _read(self) -> dict[str, HubInstallRecord]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        records = payload.get("packages") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return {}
        result: dict[str, HubInstallRecord] = {}
        for value in records:
            if not isinstance(value, dict):
                continue
            record = HubInstallRecord.from_dict(value)
            if record is not None:
                result[record.asset_id] = record
        return result

    def _write(self, records: dict[str, HubInstallRecord]) -> None:
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

    def get(self, asset_id: str) -> HubInstallRecord | None:
        return self._read().get(str(asset_id or "").strip())

    def all(self) -> dict[str, HubInstallRecord]:
        return self._read()

    def get_by_package_id(self, package_id: str) -> HubInstallRecord | None:
        wanted = str(package_id or "").strip()
        if not wanted:
            return None
        return next(
            (record for record in self._read().values() if record.package_id == wanted),
            None,
        )

    def upsert(self, record: HubInstallRecord) -> None:
        records = self._read()
        records[record.asset_id] = record
        self._write(records)

    def remove(self, asset_id: str) -> None:
        records = self._read()
        if records.pop(str(asset_id or "").strip(), None) is not None:
            self._write(records)
